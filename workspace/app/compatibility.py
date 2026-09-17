"""兼容性评估引擎（纯函数，无 IO，便于单测）。

输入两个规范化契约 + 某时刻仍生效的消费者声明 / 紧急豁免，
输出每个变更项、受影响调用方，以及声明承诺或豁免如何消解影响。

兼容判定原则：
- 字段：删除字段、字段类型改变、字段由可选变必填（请求侧）为破坏性；
  新增必填字段影响所有声明仍在使用的调用方，新增可选字段安全。
- 枚举：封闭枚举新增成员 / 删除成员影响把该成员列入使用范围，
  或假定集合封闭（不接受未知值）的调用方；开放枚举新增成员安全。
- 错误：删除错误码影响声明会处理该码的调用方；语义改变影响
  所有“感知”该错误码的调用方（处理它，或在枚举式范围里提到它）。
- 消费者可在声明中承诺在迁移期限前完成改造：期限未到时，
  其承诺范围内的破坏性变化被记为「已承诺、可放行」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import enums_by_name, errors_by_code, fields_by_key

# ---- 变更类型常量 ---------------------------------------------------------

# 字段
FIELD_REMOVED = "field.removed"
FIELD_TYPE_CHANGED = "field.type_changed"
FIELD_BECAME_REQUIRED = "field.became_required"
FIELD_REQUIRED_ADDED = "field.required_added"
FIELD_OPTIONAL_ADDED = "field.optional_added"
FIELD_BECAME_OPTIONAL = "field.became_optional"
# 枚举
ENUM_REMOVED = "enum.removed"
ENUM_VALUE_REMOVED = "enum.value_removed"
ENUM_VALUE_ADDED_CLOSED = "enum.value_added_closed"
ENUM_VALUE_ADDED_OPEN = "enum.value_added_open"
# 错误
ERROR_REMOVED = "error.removed"
ERROR_SEMANTICS_CHANGED = "error.semantics_changed"
ERROR_ADDED = "error.added"

BREAKING_KINDS = {
    FIELD_REMOVED, FIELD_TYPE_CHANGED, FIELD_BECAME_REQUIRED,
    FIELD_REQUIRED_ADDED,
    ENUM_REMOVED, ENUM_VALUE_REMOVED, ENUM_VALUE_ADDED_CLOSED,
    ERROR_REMOVED, ERROR_SEMANTICS_CHANGED,
}

CHANGE_KIND_CN = {
    FIELD_REMOVED: "删除字段",
    FIELD_TYPE_CHANGED: "字段类型改变",
    FIELD_BECAME_REQUIRED: "字段由可选变为必填",
    FIELD_REQUIRED_ADDED: "新增必填字段",
    FIELD_OPTIONAL_ADDED: "新增可选字段",
    FIELD_BECAME_OPTIONAL: "字段由必填变为可选",
    ENUM_REMOVED: "删除枚举",
    ENUM_VALUE_REMOVED: "删除枚举成员",
    ENUM_VALUE_ADDED_CLOSED: "封闭枚举新增成员",
    ENUM_VALUE_ADDED_OPEN: "开放枚举新增成员",
    ERROR_REMOVED: "删除错误码",
    ERROR_SEMANTICS_CHANGED: "错误语义改变",
    ERROR_ADDED: "新增错误码",
}


def change_id(kind: str, subject: str) -> str:
    """生成稳定的变更标识，豁免的 restricted_changes 按此匹配。"""
    return f"{kind}#{subject}"


# ---- 变更集计算 -----------------------------------------------------------

def diff_contracts(baseline: dict, candidate: dict) -> list[dict]:
    """返回两个契约之间按稳定顺序排列的变更列表。"""
    changes: list[dict] = []

    base_fields = fields_by_key(baseline)
    new_fields = fields_by_key(candidate)
    for key in sorted(set(base_fields) | set(new_fields)):
        old = base_fields.get(key)
        new = new_fields.get(key)
        _, name = key.split(":", 1)
        if old is None:
            kind = FIELD_REQUIRED_ADDED if new["required"] else FIELD_OPTIONAL_ADDED
            changes.append(_change(kind, key, name,
                                   detail={"location": new["in"],
                                           "type": new["type"],
                                           "required": new["required"]}))
        elif new is None:
            changes.append(_change(FIELD_REMOVED, key, name,
                                   detail={"location": old["in"],
                                           "type": old["type"]}))
        else:
            if old["type"] != new["type"]:
                changes.append(_change(FIELD_TYPE_CHANGED, key, name,
                                       detail={"from": old["type"],
                                               "to": new["type"]}))
            if old["required"] != new["required"]:
                kind = FIELD_BECAME_REQUIRED if new["required"] \
                    else FIELD_BECAME_OPTIONAL
                changes.append(_change(kind, key, name,
                                       detail={"from": old["required"],
                                               "to": new["required"]}))

    base_enums = enums_by_name(baseline)
    new_enums = enums_by_name(candidate)
    for name in sorted(set(base_enums) | set(new_enums)):
        old = base_enums.get(name)
        new = new_enums.get(name)
        if old is None:
            # 新增枚举整体：其中封闭枚举等价于“新增成员”，
            # 影响声明提到它或假定封闭的调用方。
            if new["values"] and new["closed"]:
                changes.append(_change(
                    ENUM_VALUE_ADDED_CLOSED, f"enum:{name}", name,
                    detail={"added": new["values"], "closed": True}))
            elif new["values"]:
                changes.append(_change(
                    ENUM_VALUE_ADDED_OPEN, f"enum:{name}", name,
                    detail={"added": new["values"], "closed": False}))
            continue
        if new is None:
            changes.append(_change(ENUM_REMOVED, f"enum:{name}", name,
                                   detail={"values": old["values"]}))
            continue
        removed = sorted(set(old["values"]) - set(new["values"]))
        added = sorted(set(new["values"]) - set(old["values"]))
        if removed:
            changes.append(_change(ENUM_VALUE_REMOVED,
                                   f"enum:{name}", name,
                                   detail={"removed": removed}))
        if added:
            kind = ENUM_VALUE_ADDED_CLOSED if new["closed"] \
                else ENUM_VALUE_ADDED_OPEN
            changes.append(_change(kind, f"enum:{name}", name,
                                   detail={"added": added,
                                           "closed": new["closed"]}))

    base_errors = errors_by_code(baseline)
    new_errors = errors_by_code(candidate)
    for code in sorted(set(base_errors) | set(new_errors)):
        old = base_errors.get(code)
        new = new_errors.get(code)
        if old is None:
            changes.append(_change(ERROR_ADDED, f"error:{code}", code,
                                   detail={"semantics": new["semantics"]}))
        elif new is None:
            changes.append(_change(ERROR_REMOVED, f"error:{code}", code,
                                   detail={"semantics": old["semantics"]}))
        elif old["semantics"] != new["semantics"]:
            changes.append(_change(ERROR_SEMANTICS_CHANGED,
                                   f"error:{code}", code,
                                   detail={"from": old["semantics"],
                                           "to": new["semantics"]}))

    return changes


def _change(kind: str, subject: str, display: str, detail: dict) -> dict:
    return {"id": change_id(kind, subject), "kind": kind,
            "subject": subject, "display": display,
            "breaking": kind in BREAKING_KINDS, "detail": detail}


# ---- 影响面计算 -----------------------------------------------------------

def _affected_consumers(change: dict, declarations: list[dict]) -> list[str]:
    """返回受该破坏性变更影响、仍在使用的消费者名称。"""
    kind = change["kind"]
    affected: set[str] = set()

    if kind in (FIELD_REMOVED, FIELD_TYPE_CHANGED, FIELD_BECAME_REQUIRED):
        subject = change["subject"]  # location:name
        for d in declarations:
            if subject in d["scope"]["used_fields"]:
                affected.add(d["consumer"])
        return sorted(affected)

    if kind == FIELD_REQUIRED_ADDED:
        # 新增必填字段：所有仍在使用该服务的消费者都要改造。
        return sorted(d["consumer"] for d in declarations)

    enum_name = change["display"]
    if kind in (ENUM_REMOVED, ENUM_VALUE_REMOVED,
                ENUM_VALUE_ADDED_CLOSED):
        removed = set(change["detail"].get("removed", []))
        added = set(change["detail"].get("added", []))
        for d in declarations:
            use = d["scope"]["enum_uses"].get(enum_name)
            if use is None:
                continue
            if kind == ENUM_VALUE_REMOVED:
                if removed & set(use["used_values"]):
                    affected.add(d["consumer"])
            elif kind == ENUM_REMOVED:
                if use["used_values"] or use["closed_assumed"]:
                    affected.add(d["consumer"])
            else:  # 封闭枚举新增成员
                if use["closed_assumed"] or (added & set(use["used_values"])):
                    # added 与 used_values 通常不相交；closed_assumed 是主因。
                    affected.add(d["consumer"])
        return sorted(affected)

    code = change["display"]
    if kind == ERROR_REMOVED:
        for d in declarations:
            if code in d["scope"]["handled_errors"]:
                affected.add(d["consumer"])
        return sorted(affected)

    if kind == ERROR_SEMANTICS_CHANGED:
        for d in declarations:
            if code in d["scope"]["handled_errors"]:
                affected.add(d["consumer"])
        return sorted(affected)

    return sorted(affected)


# ---- 豁免 -----------------------------------------------------------------

def exemption_covers(exemption: dict, change_id_value: str,
                     consumer: str, now: datetime) -> bool:
    """判断豁免是否覆盖某 (变更, 调用方)：必须有效、限定调用方且未到期。"""
    if not exemption["active"]:
        return False
    expires_at = exemption["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if now > expires_at:
        return False  # 自动到期，绝不永久绕过
    if consumer not in exemption["affected_consumers"]:
        return False
    restricted = exemption.get("restricted_changes")
    if restricted:  # 空列表/None 表示覆盖该调用方的全部破坏性变更
        if change_id_value not in restricted:
            return False
    return True


# ---- 主入口 ---------------------------------------------------------------

def evaluate(baseline: dict, candidate: dict, declarations: list[dict],
             exemptions: list[dict], now: datetime,
             baseline_version: str | None = None) -> dict:
    """评估候选契约相对基线的兼容性。

    declarations/exemptions 为数据库行的 JSON 友好形式，至少包含：
      declaration: consumer, scope, deadline
      exemption:     code, active, affected_consumers,
                     restricted_changes, expires_at(ISO), reason
    """
    changes = diff_contracts(baseline, candidate)
    findings: list[dict] = []
    breaking_count = 0

    for change in changes:
        if not change["breaking"]:
            findings.append({
                "change_id": change["id"], "kind": change["kind"],
                "kind_cn": CHANGE_KIND_CN[change["kind"]],
                "subject": change["subject"], "display": change["display"],
                "detail": change["detail"], "breaking": False,
                "affected": [], "committed": [], "permitted": [],
                "exempted": [], "uncovered": [], "exemptions": [],
                "summary": f"{CHANGE_KIND_CN[change['kind']]}："
                           f"{change['display']}（向后兼容）",
            })
            continue

        breaking_count += 1
        affected = _affected_consumers(change, declarations)
        committed: list[str] = []
        permitted: list[str] = []
        exempted_map: dict[str, str] = {}

        for d in declarations:
            consumer = d["consumer"]
            if consumer not in affected:
                continue
            # 1) 迁移承诺：期限未到即视为该调用方已承诺覆盖此变更
            #    （承诺范围在登记声明时确定，此处的影响面本身就是范围）。
            deadline = d.get("deadline")
            if deadline is not None:
                deadline_dt = (datetime.fromisoformat(deadline)
                               if isinstance(deadline, str) else deadline)
                if now <= deadline_dt:
                    committed.append(consumer)
                    permitted.append(consumer)
                    continue
            # 2) 有效豁免（受影响调用方白名单 + 到期时间双重约束）。
            for ex in exemptions:
                if exemption_covers(ex, change["id"], consumer, now):
                    exempted_map[consumer] = ex["code"]
                    permitted.append(consumer)
                    break

        exempted = sorted(exempted_map)
        uncovered = sorted(set(affected) - set(permitted))
        findings.append({
            "change_id": change["id"], "kind": change["kind"],
            "kind_cn": CHANGE_KIND_CN[change["kind"]],
            "subject": change["subject"], "display": change["display"],
            "detail": change["detail"], "breaking": True,
            "affected": affected, "committed": sorted(committed),
            "permitted": sorted(permitted),
            "exempted": exempted, "uncovered": uncovered,
            "exemptions": [{"consumer": c, "exemption": exempted_map[c]}
                           for c in exempted],
            "summary": _finding_summary(change, affected, committed,
                                        exempted, uncovered),
        })

    decision = "CANDIDATE" if all(
        not f["breaking"] or not f["uncovered"] for f in findings
    ) else "REJECTED"

    return {
        "baseline_version": baseline_version,
        "evaluated_at": now.isoformat(),
        "breaking_change_count": breaking_count,
        "decision": decision,
        "findings": findings,
        "summary": _overall_summary(decision, findings),
    }


def _finding_summary(change: dict, affected: list[str], committed: list[str],
                     exempted: list[str], uncovered: list[str]) -> str:
    label = CHANGE_KIND_CN[change["kind"]]
    if not affected:
        return f"破坏性变更 {label}：{change['display']}，但无仍在使用的消费者受影响"
    parts = [f"破坏性变更 {label}：{change['display']}，受影响调用方 {affected}"]
    if committed:
        parts.append(f"迁移承诺覆盖 {committed}")
    if exempted:
        parts.append(f"紧急豁免覆盖 {exempted}")
    if uncovered:
        parts.append(f"未覆盖、阻塞发布 {uncovered}")
    return "；".join(parts)


def _overall_summary(decision: str, findings: list[dict]) -> str:
    blocking = [f for f in findings if f["breaking"] and f["uncovered"]]
    if not blocking:
        return "所有破坏性变更均被消费者迁移承诺或有效紧急豁免覆盖，可进入候选"
    return "存在未被承诺/豁免覆盖的破坏性变更：" + "；".join(
        f"{f['kind_cn']} {f['display']} -> {f['uncovered']}" for f in blocking
    )


def evaluate_summary_inputs(result: dict[str, Any]) -> dict:
    """从评估结果中提取促成结论的声明/豁免，供发布溯源使用。"""
    declarations_seen: set[str] = set()
    exemptions_seen: set[str] = set()
    for f in result["findings"]:
        if not f["breaking"]:
            continue
        for consumer in f["committed"]:
            declarations_seen.add(consumer)
        for item in f["exemptions"]:
            exemptions_seen.add(item["exemption"])
    return {"committed_consumers": sorted(declarations_seen),
            "used_exemptions": sorted(exemptions_seen)}
