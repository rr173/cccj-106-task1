"""核心领域服务：版本谱系、兼容评审、声明、豁免与发布溯源。

所有写操作在进程内串行；每次评审都对“当前仍生效”的声明与豁免
重新计算，并把完整快照写入 reviews，因此：
- 撤回候选只改变该候选自身状态，基于它产生的评审与其后继版本不受影响；
- 发布时再次（用当前时间）复核，过期豁免 / 已到期承诺无法蒙混过关；
- 发布记录中保存促成发布的全部声明、豁免与逐项结论，可逐条解释。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import compatibility as compat
from .contracts import (canonical_hash, normalize_contract,
                        normalize_scope)
from .db import (declaration_to_dict, exemption_to_dict, version_to_dict,
                 write_lock)
from .errors import ApiError, conflict, not_found

VERSION_STATUSES = ("SUBMITTED", "CANDIDATE", "REJECTED", "WITHDRAWN",
                    "PUBLISHED", "SUPERSEDED")
MAX_EXEMPTION_DAYS = int(os.environ.get("MAX_EXEMPTION_DAYS", "14"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str, field: str = "时间字段") -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ApiError(400, "invalid_datetime", f"{field} 必须是 ISO 8601 时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ===========================================================================
# 服务
# ===========================================================================

def get_or_create_service(conn: sqlite3.Connection, name: str,
                          now: datetime) -> dict:
    row = conn.execute("SELECT * FROM services WHERE name=?", (name,)).fetchone()
    if row:
        return {"id": row["id"], "name": row["name"],
                "created_at": row["created_at"]}
    sid = _new_id("svc")
    with write_lock():
        conn.execute(
            "INSERT INTO services(id, name, created_at) VALUES(?,?,?)",
            (sid, name, now.isoformat()))
        conn.commit()
    return {"id": sid, "name": name, "created_at": now.isoformat()}


def get_service(conn: sqlite3.Connection, service_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM services WHERE id=?",
                       (service_id,)).fetchone()
    if not row:
        raise not_found(f"服务 {service_id} 不存在")
    return row


def list_services(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM services ORDER BY created_at").fetchall()
    return [{"id": r["id"], "name": r["name"],
             "created_at": r["created_at"]} for r in rows]


# ===========================================================================
# 活跃声明 / 豁免的读取（所有时间判断都以 now 为准，豁免自动到期）
# ===========================================================================

def active_declarations(conn: sqlite3.Connection, service_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM declarations WHERE service_id=? AND active=1",
        (service_id,)).fetchall()
    return [declaration_to_dict(r) for r in rows]


def active_exemptions(conn: sqlite3.Connection, service_id: str,
                      now: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM exemptions WHERE service_id=? AND active=1",
        (service_id,)).fetchall()
    result = []
    for r in rows:
        ex = exemption_to_dict(r)
        # 到期即视为无效（行保留作审计，不再标记 active=0 也可，
        # 因为任何判定都必须经过 now > expires_at 这道门）。
        ex["active"] = ex["active"] and now <= parse_dt(ex["expires_at"])
        result.append(ex)
    return result


# ===========================================================================
# 版本提交与谱系
# ===========================================================================

def submit_version(conn: sqlite3.Connection, service_id: str, body: dict,
                   now: datetime) -> dict:
    get_service(conn, service_id)
    contract = normalize_contract(body.get("contract"))
    parent_id = body.get("parent_id", "__missing__")
    if parent_id == "__missing__":
        raise ApiError(400, "parent_required",
                       "必须提供 parent_id（首个版本传 null）")
    parent: sqlite3.Row | None = None
    if parent_id is not None:
        parent = conn.execute("SELECT * FROM versions WHERE id=? AND service_id=?",
                              (parent_id, service_id)).fetchone()
        if not parent:
            raise ApiError(400, "bad_parent",
                           f"父版本 {parent_id} 不存在或不属于该服务")

    contract_hash = canonical_hash(contract)
    # 同一服务内内容完全相同的版本直接拒绝，避免无意义分叉。
    dup = conn.execute(
        "SELECT id FROM versions WHERE service_id=? AND contract_hash=?",
        (service_id, contract_hash)).fetchone()
    if dup:
        raise conflict("相同契约内容已存在，无需重复提交",
                       {"existing_version_id": dup["id"]})

    submitter = str(body.get("submitter") or "unknown")
    note = str(body.get("note") or "")

    with write_lock():
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM versions "
            "WHERE service_id=?", (service_id,)).fetchone()
        seq = seq_row["next"]
        vid = _new_id("ver")
        conn.execute(
            """INSERT INTO versions(id, service_id, seq, parent_id, status,
                                    contract_json, contract_hash, submitter,
                                    created_at)
               VALUES(?,?,?,?, 'SUBMITTED', ?,?,?,?)""",
            (vid, service_id, seq, parent_id,
             json.dumps(contract, ensure_ascii=False), contract_hash,
             submitter, now.isoformat()))
        conn.commit()

    # 提交即自动做一次入场评审（结论不直接改状态，由 admission 决定）。
    review = _run_review(conn, vid, reason_stage="admission", now=now)
    return {"version": get_version(conn, vid), "initial_review": review}


def get_version(conn: sqlite3.Connection, version_id: str) -> dict:
    row = conn.execute("SELECT * FROM versions WHERE id=?",
                       (version_id,)).fetchone()
    if not row:
        raise not_found(f"版本 {version_id} 不存在")
    return version_to_dict(row)


def list_versions(conn: sqlite3.Connection, service_id: str,
                  status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM versions WHERE service_id=?"
    params: list = [service_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY seq"
    rows = conn.execute(sql, params).fetchall()
    return [version_to_dict(r) for r in rows]


def get_lineage(conn: sqlite3.Connection, service_id: str) -> dict:
    """返回完整版本谱系（含撤回版本）与边，供排查并发分叉。"""
    versions = list_versions(conn, service_id)
    nodes = [{"id": v["id"], "seq": v["seq"], "parent_id": v["parent_id"],
              "status": v["status"], "contract_hash": v["contract_hash"],
              "submitter": v["submitter"], "created_at": v["created_at"]}
             for v in versions]
    edges = [{"from": v["parent_id"], "to": v["id"]}
             for v in versions if v["parent_id"]]
    published = next((v["id"] for v in reversed(versions)
                      if v["status"] == "PUBLISHED"), None)
    roots = [v["id"] for v in versions if not v["parent_id"]]
    # 按根统计分支，直观呈现并发提交形成的分叉。
    root_of: dict[str, str] = {}
    by_id = {v["id"]: v for v in versions}
    for v in versions:
        cur = v
        while cur["parent_id"] and cur["parent_id"] in by_id:
            cur = by_id[cur["parent_id"]]
        root_of[v["id"]] = cur["id"]
    branches: dict[str, list[str]] = {}
    for vid, root in root_of.items():
        branches.setdefault(root, []).append(vid)
    return {"service_id": service_id, "roots": roots,
            "published_version_id": published, "nodes": nodes, "edges": edges,
            "branches": branches}


# ===========================================================================
# 评审 / 候选
# ===========================================================================

def _baseline(conn: sqlite3.Connection, service_id: str,
              parent_row: sqlite3.Row | None) -> sqlite3.Row | None:
    """基线选择：当前已发布版本；尚无发布时退到父版本；再退为空契约。"""
    row = conn.execute(
        "SELECT * FROM versions WHERE service_id=? AND status='PUBLISHED' "
        "ORDER BY published_at DESC LIMIT 1", (service_id,)).fetchone()
    if row:
        return row
    return parent_row


def _run_review(conn: sqlite3.Connection, version_id: str,
                reason_stage: str, now: datetime) -> dict:
    vrow = conn.execute("SELECT * FROM versions WHERE id=?",
                        (version_id,)).fetchone()
    if not vrow:
        raise not_found(f"版本 {version_id} 不存在")
    service_id = vrow["service_id"]
    parent_row = None
    if vrow["parent_id"]:
        parent_row = conn.execute("SELECT * FROM versions WHERE id=?",
                                  (vrow["parent_id"],)).fetchone()
    baseline_row = _baseline(conn, service_id, parent_row)

    candidate = json.loads(vrow["contract_json"])
    baseline = (json.loads(baseline_row["contract_json"])
                if baseline_row else {"fields": [], "enums": [], "errors": []})
    declarations = active_declarations(conn, service_id)
    exemptions = active_exemptions(conn, service_id, now)

    result = compat.evaluate(
        baseline, candidate, declarations, exemptions, now,
        baseline_version=baseline_row["id"] if baseline_row else None)

    evidence = {
        "stage": reason_stage,
        "evaluated_at": now.isoformat(),
        "version_id": version_id,
        "baseline": (None if not baseline_row else {
            "version_id": baseline_row["id"],
            "seq": baseline_row["seq"],
            "status": baseline_row["status"],
            "contract_hash": baseline_row["contract_hash"],
        }),
        "parent_version_id": vrow["parent_id"],
        "declarations_snapshot": declarations,
        "exemptions_snapshot": exemptions,
        "result": result,
    }
    rid = _new_id("rev")
    with write_lock():
        conn.execute(
            """INSERT INTO reviews(id, version_id, service_id, baseline_id,
                                   decision, reason_stage, evidence_json,
                                   created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (rid, version_id, service_id,
             baseline_row["id"] if baseline_row else None,
             result["decision"], reason_stage,
             json.dumps(evidence, ensure_ascii=False), now.isoformat()))
        conn.commit()
    evidence["id"] = rid
    return evidence


def admit_version(conn: sqlite3.Connection, version_id: str,
                  now: datetime) -> dict:
    """用当前声明/豁免重新评估并尝试进入候选。"""
    vrow = conn.execute("SELECT * FROM versions WHERE id=?",
                        (version_id,)).fetchone()
    if not vrow:
        raise not_found(f"版本 {version_id} 不存在")
    if vrow["status"] not in ("SUBMITTED", "REJECTED", "CANDIDATE"):
        raise conflict(f"状态为 {vrow['status']} 的版本不能进入候选，"
                       "仅 SUBMITTED/REJECTED/CANDIDATE 可重新评审",
                       {"status": vrow["status"]})

    # 允许对已是候选的版本重新评估：声明期限到期 / 豁免过期后，
    # 重新评审会把它降级为 REJECTED，避免候选状态随时间“变陈旧”。
    review = _run_review(conn, version_id, reason_stage="admission", now=now)
    with write_lock():
        if review["result"]["decision"] == "CANDIDATE":
            conn.execute("UPDATE versions SET status='CANDIDATE' WHERE id=?",
                         (version_id,))
        else:
            conn.execute("UPDATE versions SET status='REJECTED' WHERE id=?",
                         (version_id,))
        conn.commit()
    return {"version": get_version(conn, version_id), "review": review}


def list_reviews(conn: sqlite3.Connection, version_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM reviews WHERE version_id=? ORDER BY created_at, id",
        (version_id,)).fetchall()
    return [{"id": r["id"], "version_id": r["version_id"],
             "baseline_id": r["baseline_id"], "decision": r["decision"],
             "stage": r["reason_stage"], "created_at": r["created_at"],
             "evidence": json.loads(r["evidence_json"])} for r in rows]


def withdraw_version(conn: sqlite3.Connection, version_id: str,
                     reason: str | None, now: datetime) -> dict:
    """撤回候选。已发布版本不可撤回；其它版本（含后继评审）保持原样。"""
    vrow = conn.execute("SELECT * FROM versions WHERE id=?",
                        (version_id,)).fetchone()
    if not vrow:
        raise not_found(f"版本 {version_id} 不存在")
    if vrow["status"] == "PUBLISHED":
        raise conflict("已发布版本不可撤回", {"status": vrow["status"]})
    if vrow["status"] != "CANDIDATE":
        raise conflict("仅 CANDIDATE 状态的版本可撤回",
                       {"status": vrow["status"]})
    with write_lock():
        conn.execute(
            "UPDATE versions SET status='WITHDRAWN', withdraw_reason=? "
            "WHERE id=?", (reason or "", version_id))
        conn.commit()
    # 不级联、不删除任何 reviews / 子版本：谱系与历史完整保留。
    children = conn.execute(
        "SELECT id FROM versions WHERE parent_id=?",
        (version_id,)).fetchall()
    return {"version": get_version(conn, version_id),
            "reviews_preserved": len(list_reviews(conn, version_id)),
            "child_version_ids": [c["id"] for c in children]}


# ===========================================================================
# 发布（含即时复核与溯源）
# ===========================================================================

def publish_version(conn: sqlite3.Connection, version_id: str,
                    body: dict, now: datetime) -> dict:
    vrow = conn.execute("SELECT * FROM versions WHERE id=?",
                        (version_id,)).fetchone()
    if not vrow:
        raise not_found(f"版本 {version_id} 不存在")
    if vrow["status"] != "CANDIDATE":
        raise conflict("仅 CANDIDATE 状态的版本可发布",
                       {"status": vrow["status"]})

    # 发布前用“当前时间”重新复核：承诺到期、豁免过期都会在此暴露。
    recheck = _run_review(conn, version_id,
                          reason_stage="publish_recheck", now=now)
    if recheck["result"]["decision"] != "CANDIDATE":
        with write_lock():
            conn.execute("UPDATE versions SET status='REJECTED' WHERE id=?",
                         (version_id,))
            conn.commit()
        raise conflict("发布复核未通过：存在未被有效承诺/豁免覆盖的破坏性变更",
                       {"review_id": recheck["id"],
                        "summary": recheck["result"]["summary"]})

    declarations = active_declarations(conn, vrow["service_id"])
    exemptions = active_exemptions(conn, vrow["service_id"], now)
    inputs = compat.evaluate_summary_inputs(recheck["result"])

    # 逐项可解释证据：哪些破坏性变更是被谁的承诺、哪个豁免放行的。
    contributions: list[dict] = []
    for f in recheck["result"]["findings"]:
        if not f["breaking"]:
            continue
        contributions.append({
            "change_id": f["change_id"], "kind_cn": f["kind_cn"],
            "display": f["display"], "detail": f["detail"],
            "allowed_by_migration_commitment": [
                _decl_ref(declarations, c) for c in f["committed"]],
            "allowed_by_exemption": f["exemptions"],
            "affected_consumers": f["affected"],
        })

    used_ex_rows = [e for e in exemptions
                    if e["code"] in inputs["used_exemptions"]]
    used_decl_rows = [d for d in declarations
                      if d["consumer"] in inputs["committed_consumers"]]
    evidence = {
        "published_at": now.isoformat(),
        "version_id": version_id,
        "service_id": vrow["service_id"],
        "review_id": recheck["id"],
        "baseline": recheck["baseline"],
        "contributions": contributions,
        "supporting_declarations": used_decl_rows,
        "supporting_exemptions": used_ex_rows,
        "published_by": str((body or {}).get("publisher") or "unknown"),
        "note": str((body or {}).get("note") or ""),
        "explanation": _build_explanation(contributions, used_decl_rows,
                                          used_ex_rows),
    }

    pid = _new_id("pub")
    with write_lock():
        # 同服务旧发布版本转为 SUPERSEDED（内容与评审仍保留）。
        conn.execute(
            "UPDATE versions SET status='SUPERSEDED' "
            "WHERE service_id=? AND status='PUBLISHED'",
            (vrow["service_id"],))
        conn.execute(
            """UPDATE versions SET status='PUBLISHED', published_at=?,
                                   publish_record_id=? WHERE id=?""",
            (now.isoformat(), pid, version_id))
        conn.execute(
            """INSERT INTO publish_records(id, service_id, version_id,
                                           evidence_json, created_at)
               VALUES(?,?,?,?,?)""",
            (pid, vrow["service_id"], version_id,
             json.dumps(evidence, ensure_ascii=False), now.isoformat()))
        conn.commit()
    return {"version": get_version(conn, version_id),
            "publish_record_id": pid, "evidence": evidence}


def _decl_ref(declarations: list[dict], consumer: str) -> dict:
    for d in declarations:
        if d["consumer"] == consumer:
            return {"declaration_id": d["id"], "consumer": consumer,
                    "deadline": d["deadline"]}
    return {"consumer": consumer}


def _build_explanation(contributions: list[dict], declarations: list[dict],
                       exemptions: list[dict]) -> str:
    lines = ["本次发布由以下承诺与豁免促成："]
    if not any(c.get("allowed_by_migration_commitment") or
               c.get("allowed_by_exemption") for c in contributions):
        return "本次发布不含破坏性变更，无需承诺或豁免。"
    for c in contributions:
        parts = [f"- {c['kind_cn']} {c['display']}"]
        if c["allowed_by_migration_commitment"]:
            parts.append("迁移承诺：" + "、".join(
                f"{x['consumer']}(截止 {x.get('deadline')})"
                for x in c["allowed_by_migration_commitment"]))
        if c["allowed_by_exemption"]:
            parts.append("紧急豁免：" + "、".join(
                f"{x['exemption']} 覆盖 {x['consumer']}"
                for x in c["allowed_by_exemption"]))
        lines.append("；".join(parts))
    lines.append(f"共引用有效声明 {len(declarations)} 份、"
                 f"未到期豁免 {len(exemptions)} 个。")
    return "\n".join(lines)


def get_publish_record(conn: sqlite3.Connection, service_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM publish_records WHERE service_id=? "
        "ORDER BY created_at DESC LIMIT 1", (service_id,)).fetchone()
    if not row:
        raise not_found("该服务尚无发布记录")
    return {"id": row["id"], "service_id": row["service_id"],
            "version_id": row["version_id"], "created_at": row["created_at"],
            "evidence": json.loads(row["evidence_json"])}


# ===========================================================================
# 消费者声明（迁移期限）
# ===========================================================================

def register_declaration(conn: sqlite3.Connection, service_id: str,
                         body: dict, now: datetime) -> dict:
    get_service(conn, service_id)
    consumer = body.get("consumer")
    if not isinstance(consumer, str) or not consumer:
        raise ApiError(400, "invalid_consumer",
                       "consumer 必须是非空字符串")
    scope = normalize_scope(body.get("scope"))

    deadline = None
    if body.get("deadline") is not None:
        deadline = parse_dt(body["deadline"], "deadline")
        if deadline <= now:
            raise ApiError(400, "deadline_in_past",
                           "迁移期限必须晚于当前时间；过期后请重新登记")

    did = _new_id("dec")
    with write_lock():
        # 同一 (服务, 消费者) 只保留一份活跃声明，旧版本置为 superseded，
        # 历史评审中引用的快照不受影响。
        conn.execute(
            "UPDATE declarations SET active=0, superseded_by=? "
            "WHERE service_id=? AND consumer=? AND active=1",
            (did, service_id, consumer))
        conn.execute(
            """INSERT INTO declarations(id, service_id, consumer, scope_json,
                                        deadline, active, created_at)
               VALUES(?,?,?,?,?,1,?)""",
            (did, service_id, consumer,
             json.dumps(scope, ensure_ascii=False),
             deadline.isoformat() if deadline else None, now.isoformat()))
        conn.commit()
    return declaration_to_dict(
        conn.execute("SELECT * FROM declarations WHERE id=?",
                     (did,)).fetchone())


def list_declarations(conn: sqlite3.Connection, service_id: str,
                      include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM declarations WHERE service_id=?"
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY created_at"
    return [declaration_to_dict(r) for r in
            conn.execute(sql, (service_id,)).fetchall()]


# ===========================================================================
# 紧急豁免（限定调用方 + 强制到期）
# ===========================================================================

def create_exemption(conn: sqlite3.Connection, service_id: str,
                     body: dict, now: datetime) -> dict:
    get_service(conn, service_id)
    code = body.get("code")
    if not isinstance(code, str) or not code:
        raise ApiError(400, "invalid_code", "code 必须是非空字符串")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError(400, "invalid_reason", "紧急豁免必须填写 reason")
    created_by = str(body.get("created_by") or "unknown")

    consumers = body.get("affected_consumers")
    if not isinstance(consumers, list) or not consumers or not all(
            isinstance(c, str) and c for c in consumers):
        raise ApiError(400, "invalid_consumers",
                       "affected_consumers 必须是非空字符串数组；"
                       "豁免必须限定受影响调用方")
    if len(set(consumers)) != len(consumers):
        raise ApiError(400, "invalid_consumers", "affected_consumers 有重复")

    known = {d["consumer"] for d in active_declarations(conn, service_id)}
    unknown = sorted(set(consumers) - known)
    if unknown:
        raise ApiError(400, "unknown_consumers",
                       f"以下调用方没有有效声明，不能对其发放豁免: {unknown}",
                       {"unknown": unknown})

    restricted = body.get("restricted_changes")
    if restricted is not None:
        if not isinstance(restricted, list) or not all(
                isinstance(c, str) and c for c in restricted):
            raise ApiError(400, "invalid_restricted_changes",
                           "restricted_changes 必须是 change_id 字符串数组")
        restricted = sorted(set(restricted))

    expires_at = body.get("expires_at")
    if not expires_at:
        raise ApiError(400, "expiry_required",
                       "紧急豁免必须提供 expires_at，不允许永久绕过检查")
    expires_dt = parse_dt(expires_at, "expires_at")
    if expires_dt <= now:
        raise ApiError(400, "expiry_in_past", "expires_at 必须晚于当前时间")
    max_dt = now + timedelta(days=MAX_EXEMPTION_DAYS)
    if expires_dt > max_dt:
        raise ApiError(400, "expiry_too_long",
                       f"豁免有效期最长 {MAX_EXEMPTION_DAYS} 天",
                       {"max_expires_at": max_dt.isoformat()})

    if conn.execute("SELECT 1 FROM exemptions WHERE code=?",
                    (code,)).fetchone():
        raise conflict(f"豁免编号 {code} 已存在")

    eid = _new_id("exm")
    with write_lock():
        conn.execute(
            """INSERT INTO exemptions(id, code, service_id,
                                      affected_consumers, restricted_changes,
                                      reason, created_by, created_at,
                                      expires_at, active)
               VALUES(?,?,?,?,?,?,?,?,?,1)""",
            (eid, code, service_id,
             json.dumps(sorted(set(consumers))),
             json.dumps(restricted) if restricted is not None else None,
             reason.strip(), created_by, now.isoformat(),
             expires_dt.isoformat()))
        conn.commit()
    return exemption_to_dict(
        conn.execute("SELECT * FROM exemptions WHERE id=?",
                     (eid,)).fetchone())


def list_exemptions(conn: sqlite3.Connection, service_id: str,
                    now: datetime, include_expired: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM exemptions WHERE service_id=? ORDER BY created_at",
        (service_id,)).fetchall()
    result = []
    for r in rows:
        ex = exemption_to_dict(r)
        ex["active"] = ex["active"] and now <= parse_dt(ex["expires_at"])
        if include_expired or ex["active"]:
            result.append(ex)
    return result


def revoke_exemption(conn: sqlite3.Connection, exemption_id: str,
                     now: datetime) -> dict:
    row = conn.execute("SELECT * FROM exemptions WHERE id=?",
                       (exemption_id,)).fetchone()
    if not row:
        raise not_found(f"豁免 {exemption_id} 不存在")
    with write_lock():
        conn.execute(
            "UPDATE exemptions SET active=0, revoked_at=? WHERE id=?",
            (now.isoformat(), exemption_id))
        conn.commit()
    return exemption_to_dict(
        conn.execute("SELECT * FROM exemptions WHERE id=?",
                     (exemption_id,)).fetchone())
