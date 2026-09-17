#!/usr/bin/env python3
"""端到端演示脚本：走完「提交 -> 阻塞 -> 迁移承诺/豁免 -> 发布溯源」全链路。

用法（需先用 ALLOW_TIME_OVERRIDE=1 启动服务以便演示时间推进）::

    ALLOW_TIME_OVERRIDE=1 python3 -m app            # 终端 A
    python3 examples/demo_walkthrough.py            # 终端 B
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "http://127.0.0.1:8080"
T0 = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)


def call(method: str, path: str, body=None, now: datetime | None = None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if now is not None:
        req.add_header("X-Now", now.isoformat())
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title, status, body):
    print(f"\n===== {title} [{status}] =====")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:2600])


def main() -> None:
    # 1) 注册服务与首个已发布版本（服务已存在则复用，脚本可重复演示）
    s, svc = call("POST", "/services", {"name": "checkout"}, T0)
    if s == 409:
        _, services = call("GET", "/services")
        svc = next(x for x in services["services"] if x["name"] == "checkout")
    sid = svc["id"]
    existing = call("GET", f"/services/{sid}/versions")[1].get("versions", [])
    if existing:
        raise SystemExit(
            f"服务 checkout 已存在 {len(existing)} 个版本；"
            "如需重新演示请清空数据库（删除 CONTRACT_REGISTRY_DB 文件）。")
    v1_contract = {
        "fields": [
            {"name": "order_id", "in": "response", "type": "string",
             "required": True},
            {"name": "remark", "in": "response", "type": "string",
             "required": False}],
        "enums": [{"name": "OrderStatus", "values": ["PENDING", "PAID"],
                   "closed": True}],
        "errors": [{"code": "NOT_FOUND", "semantics": "订单不存在"}],
    }
    _, submitted = call("POST", f"/services/{sid}/versions",
                        {"contract": v1_contract, "parent_id": None,
                         "submitter": "platform"}, T0)
    v1 = submitted["version"]["id"]
    call("POST", f"/versions/{v1}/admit", {}, T0)
    call("POST", f"/versions/{v1}/publish", {"publisher": "release-bot"}, T0)

    # 2) billing 团队登记使用声明（带迁移期限）
    call("POST", f"/services/{sid}/declarations", {
        "consumer": "billing",
        "scope": {
            "used_fields": ["response:order_id", "response:remark"],
            "enum_uses": {"OrderStatus": {"used_values": ["PENDING", "PAID"],
                                          "closed_assumed": True}},
            "handled_errors": ["NOT_FOUND"]},
        "deadline": (T0 + timedelta(days=30)).isoformat(),
    }, T0)

    # 3) 提供方提交破坏性新版本：删除 remark、给封闭枚举加成员
    v2_contract = {
        "fields": [v1_contract["fields"][0]],
        "enums": [{"name": "OrderStatus",
                   "values": ["PENDING", "PAID", "REFUNDED"], "closed": True}],
        "errors": v1_contract["errors"],
    }
    _, submitted2 = call("POST", f"/services/{sid}/versions",
                         {"contract": v2_contract, "parent_id": v1,
                          "submitter": "checkout-team"}, T0)
    v2 = submitted2["version"]["id"]
    s, admitted = call("POST", f"/versions/{v2}/admit", {}, T0)
    show("迁移期限内：破坏性变化由 billing 的承诺覆盖，进入候选", s,
         {"decision": admitted["review"]["result"]["decision"],
          "findings": admitted["review"]["result"]["findings"]})

    # 4) 期限过后重新评审 -> 被拒绝；发放短期紧急豁免 -> 再次进入候选
    T_LATE = T0 + timedelta(days=31)
    s, late = call("POST", f"/versions/{v2}/admit", {}, T_LATE)
    show("迁移期限已过：承诺失效，候选被降级为 REJECTED",
         s, {"status": late["version"]["status"],
             "summary": late["review"]["result"]["summary"]})

    s, exm = call("POST", f"/services/{sid}/exemptions", {
        "code": "EXM-INC-20260917",
        "affected_consumers": ["billing"],
        "restricted_changes": [
            "field.removed#response:remark",
            "enum.value_added_closed#enum:OrderStatus"],
        "reason": "P1 故障修复需要立即清理 remark；billing 值班已口头确认",
        "created_by": "oncall-alice",
        "expires_at": (T_LATE + timedelta(days=2)).isoformat(),
    }, T_LATE)
    show("创建限定调用方、限定变更、48 小时到期的紧急豁免", s, exm)

    s, readmitted = call("POST", f"/versions/{v2}/admit", {}, T_LATE)
    show("凭豁免重新进入候选", s,
         {"status": readmitted["version"]["status"],
          "summary": readmitted["review"]["result"]["summary"]})

    # 5) 发布：发布前即时复核，记录中逐项解释承诺/豁免来源
    s, published = call("POST", f"/versions/{v2}/publish",
                        {"publisher": "release-bot", "note": "checkout 2.0"},
                        T_LATE + timedelta(hours=1))
    show("发布成功：证据解释了是哪些声明和豁免促成的", s,
         {"publish_record_id": published["publish_record_id"],
          "explanation": published["evidence"]["explanation"],
          "contributions": published["evidence"]["contributions"]})

    # 6) 豁免到期后无法再用于发布（新候选复核演示）
    T_EXPIRED = T_LATE + timedelta(days=3)
    s, exemptions = call("GET",
                         f"/services/{sid}/exemptions?include_expired=1",
                         now=T_EXPIRED)
    show("豁免自动到期（active=false），不能永久绕过检查", 200, exemptions)


if __name__ == "__main__":
    main()
