"""端到端 API 测试（标准库 unittest + urllib，无需任何第三方依赖）。

时间通过 X-Now 头注入（进程以 ALLOW_TIME_OVERRIDE=1 启动），
从而确定性地验证“迁移期限到期”“豁免自动到期”等时间语义。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 必须在 import app.web 之前设置：启用 X-Now 时间注入。
os.environ["ALLOW_TIME_OVERRIDE"] = "1"

from app.web import build_server  # noqa: E402

T0 = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)


def T_PLUS(days: float = 0, hours: float = 0) -> str:
    return (T0 + timedelta(days=days, hours=hours)).isoformat()


CONTRACT_V1 = {
    "fields": [
        {"name": "order_id", "in": "response", "type": "string",
         "required": True},
        {"name": "remark", "in": "response", "type": "string",
         "required": False},
    ],
    "enums": [
        {"name": "OrderStatus", "values": ["PENDING", "PAID"],
         "closed": True},
    ],
    "errors": [
        {"code": "NOT_FOUND", "semantics": "订单不存在"},
    ],
}


class ApiClient:
    def __init__(self, server, base_url: str):
        self.server = server
        self.base_url = base_url

    def request(self, method: str, path: str, body=None, now: str | None = None,
                expect_status: int | None = None):
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if now is not None:
            req.add_header("X-Now", now)
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = json.loads(exc.read().decode())
        if expect_status is not None:
            assert status == expect_status, \
                f"{method} {path} -> {status}: {payload}"
        return status, payload


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self.tmpdir.name) / "test.db")
        self.server = build_server("127.0.0.1", 0, db_path=db_path)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(self.server, f"http://127.0.0.1:{self.port}")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tmpdir.cleanup()

    # ---- 便捷构造函数 -----------------------------------------------------

    def create_service(self, name="checkout"):
        _, body = self.api.request("POST", "/services", {"name": name},
                                   now=T_PLUS(), expect_status=201)
        return body["id"]

    def submit(self, sid, contract, parent_id=None, submitter="team-a",
               status=201):
        return self.api.request(
            "POST", f"/services/{sid}/versions",
            {"contract": contract, "parent_id": parent_id,
             "submitter": submitter}, now=T_PLUS(),
            expect_status=status)

    def declare(self, sid, consumer, scope, deadline=None):
        return self.api.request(
            "POST", f"/services/{sid}/declarations",
            {"consumer": consumer, "scope": scope, "deadline": deadline},
            now=T_PLUS(), expect_status=201)

    def first_version(self, sid, contract=CONTRACT_V1):
        _, body = self.submit(sid, contract, parent_id=None)
        vid = body["version"]["id"]
        self.api.request("POST", f"/versions/{vid}/admit", {},
                         now=T_PLUS(), expect_status=200)
        self.api.request("POST", f"/versions/{vid}/publish",
                         {"publisher": "release-bot"}, now=T_PLUS(),
                         expect_status=200)
        return vid

    def billing_scope(self, **overrides):
        scope = {
            "used_fields": ["response:order_id", "response:remark"],
            "enum_uses": {"OrderStatus": {"used_values": ["PENDING", "PAID"],
                                          "closed_assumed": True}},
            "handled_errors": ["NOT_FOUND"],
        }
        scope.update(overrides)
        return scope

    def findings_by_change(self, review):
        return {f["change_id"]: f
                for f in review["evidence"]["result"]["findings"]} \
            if "evidence" in review else \
            {f["change_id"]: f for f in review["result"]["findings"]}


class TestHappyPath(ApiTestCase):
    def test_compatible_change_flows_to_publish(self):
        sid = self.create_service()
        vid = self.first_version(sid)

        # 纯增量（可选字段 + 开放枚举新增成员 + 新错误码）应兼容。
        v2 = {
            "fields": CONTRACT_V1["fields"] + [
                {"name": "coupon", "in": "response", "type": "string",
                 "required": False}],
            "enums": [
                {"name": "OrderStatus", "values": ["PENDING", "PAID"],
                 "closed": False}],
            "errors": CONTRACT_V1["errors"] + [
                {"code": "RATE_LIMITED", "semantics": "限流"}],
        }
        _, body = self.submit(sid, v2, parent_id=vid, submitter="team-b")
        v2id = body["version"]["id"]
        self.assertEqual(body["initial_review"]["result"]["decision"],
                         "CANDIDATE")
        s, body = self.api.request("POST", f"/versions/{v2id}/admit", {},
                                   now=T_PLUS(), expect_status=200)
        self.assertEqual(body["version"]["status"], "CANDIDATE")
        s, body = self.api.request("POST", f"/versions/{v2id}/publish",
                                   {"publisher": "release-bot"}, now=T_PLUS(),
                                   expect_status=200)
        self.assertEqual(body["version"]["status"], "PUBLISHED")
        # 旧版本被标记为 SUPERSEDED 但保留。
        versions = self.api.request("GET", f"/services/{sid}/versions",
                                    now=T_PLUS())[1]["versions"]
        statuses = {v["id"]: v["status"] for v in versions}
        self.assertEqual(statuses[vid], "SUPERSEDED")
        self.assertEqual(statuses[v2id], "PUBLISHED")

    def test_health_and_unknown_service(self):
        s, body = self.api.request("GET", "/health", now=T_PLUS())
        self.assertEqual(s, 200)
        self.assertEqual(body["status"], "ok")
        self.api.request("GET", "/services/nope/versions", now=T_PLUS(),
                         expect_status=404)


class TestBlockingBreakingChanges(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.create_service()
        self.v1 = self.first_version(self.sid)
        self.declare(self.sid, "billing", self.billing_scope())

    def _admit_expect_reject(self, contract, change_ids):
        _, body = self.submit(self.sid, contract, parent_id=self.v1)
        vid = body["version"]["id"]
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(), expect_status=200)
        self.assertEqual(body["version"]["status"], "REJECTED")
        findings = self.findings_by_change(body["review"])
        for cid in change_ids:
            self.assertIn(cid, findings)
            self.assertEqual(findings[cid]["uncovered"], ["billing"], cid)
        return vid, findings

    def test_remove_field_blocked(self):
        contract = {"fields": [CONTRACT_V1["fields"][0]],
                    "enums": CONTRACT_V1["enums"],
                    "errors": CONTRACT_V1["errors"]}
        self._admit_expect_reject(contract, ["field.removed#response:remark"])

    def test_type_change_and_required_addition_blocked(self):
        fields = [
            {"name": "order_id", "in": "response", "type": "integer",
             "required": True},
            {"name": "remark", "in": "response", "type": "string",
             "required": False},
            {"name": "trace_id", "in": "request", "type": "string",
             "required": True},
        ]
        self._admit_expect_reject(
            {"fields": fields, "enums": CONTRACT_V1["enums"],
             "errors": CONTRACT_V1["errors"]},
            ["field.type_changed#response:order_id",
             "field.required_added#request:trace_id"])

    def test_field_became_required_blocked(self):
        fields = [
            {"name": "order_id", "in": "response", "type": "string",
             "required": True},
            {"name": "remark", "in": "response", "type": "string",
             "required": True},
        ]
        self._admit_expect_reject(
            {"fields": fields, "enums": CONTRACT_V1["enums"],
             "errors": CONTRACT_V1["errors"]},
            ["field.became_required#response:remark"])

    def test_enum_value_removed_blocked(self):
        contract = {"fields": CONTRACT_V1["fields"],
                    "enums": [{"name": "OrderStatus",
                               "values": ["PENDING"], "closed": True}],
                    "errors": CONTRACT_V1["errors"]}
        self._admit_expect_reject(
            contract, ["enum.value_removed#enum:OrderStatus"])

    def test_closed_enum_value_added_blocks_closed_assumer(self):
        contract = {"fields": CONTRACT_V1["fields"],
                    "enums": [{"name": "OrderStatus",
                               "values": ["PENDING", "PAID", "REFUNDED"],
                               "closed": True}],
                    "errors": CONTRACT_V1["errors"]}
        self._admit_expect_reject(
            contract, ["enum.value_added_closed#enum:OrderStatus"])

    def test_open_enum_addition_is_safe(self):
        # billing 不假定封闭时，开放枚举加成员是安全的。
        self.declare(
            self.sid, "billing",
            self.billing_scope(enum_uses={"OrderStatus": {
                "used_values": ["PENDING", "PAID"],
                "closed_assumed": False}}), )
        # 上面的声明会 supersed 旧的，但 setUp 已建旧的；直接登记新声明覆盖。
        contract = {"fields": CONTRACT_V1["fields"],
                    "enums": [{"name": "OrderStatus",
                               "values": ["PENDING", "PAID", "REFUNDED"],
                               "closed": False}],
                    "errors": CONTRACT_V1["errors"]}
        _, body = self.submit(self.sid, contract, parent_id=self.v1)
        vid = body["version"]["id"]
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(), expect_status=200)
        self.assertEqual(body["version"]["status"], "CANDIDATE")

    def test_error_semantics_change_blocked(self):
        contract = {"fields": CONTRACT_V1["fields"],
                    "enums": CONTRACT_V1["enums"],
                    "errors": [{"code": "NOT_FOUND",
                                "semantics": "订单或购物车不存在"}]}
        self._admit_expect_reject(
            contract, ["error.semantics_changed#error:NOT_FOUND"])

    def test_error_removed_blocked(self):
        contract = {"fields": CONTRACT_V1["fields"],
                    "enums": CONTRACT_V1["enums"], "errors": []}
        self._admit_expect_reject(
            contract, ["error.removed#error:NOT_FOUND"])


class TestMigrationDeadline(ApiTestCase):
    def test_commitment_window_admits_then_expires_and_blocks(self):
        sid = self.create_service()
        v1 = self.first_version(sid)
        deadline = (T0 + timedelta(days=30)).isoformat()
        self.declare(sid, "billing", self.billing_scope(), deadline=deadline)

        v2 = {"fields": [CONTRACT_V1["fields"][0]],
              "enums": CONTRACT_V1["enums"], "errors": CONTRACT_V1["errors"]}

        # 期限内：仅允许满足承诺范围的版本进入候选（该变化命中其使用范围）。
        _, body = self.submit(sid, v2, parent_id=v1)
        vid = body["version"]["id"]
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(10), expect_status=200)
        self.assertEqual(body["version"]["status"], "CANDIDATE")
        findings = self.findings_by_change(body["review"])
        self.assertEqual(
            findings["field.removed#response:remark"]["committed"],
            ["billing"])

        # 期限过后再次评审：承诺失效，版本被拒绝。
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(31), expect_status=200)
        self.assertEqual(body["version"]["status"], "REJECTED")
        findings = self.findings_by_change(body["review"])
        self.assertEqual(
            findings["field.removed#response:remark"]["uncovered"],
            ["billing"])

    def test_candidate_publish_recheck_after_deadline_expiry(self):
        sid = self.create_service()
        v1 = self.first_version(sid)
        self.declare(sid, "billing", self.billing_scope(),
                     deadline=T_PLUS(20))
        v2 = {"fields": [CONTRACT_V1["fields"][0]],
              "enums": CONTRACT_V1["enums"], "errors": CONTRACT_V1["errors"]}
        _, body = self.submit(sid, v2, parent_id=v1)
        vid = body["version"]["id"]
        self.api.request("POST", f"/versions/{vid}/admit", {},
                         now=T_PLUS(10), expect_status=200)
        # 拖到期限之后才发布：即时复核必须失败，候选被降级。
        s, body = self.api.request("POST", f"/versions/{vid}/publish",
                                   {"publisher": "release-bot"},
                                   now=T_PLUS(21), expect_status=409)
        self.assertIn("复核", body["error"]["message"])

    def test_past_deadline_rejected_on_registration(self):
        sid = self.create_service()
        s, body = self.api.request(
            "POST", f"/services/{sid}/declarations",
            {"consumer": "billing", "scope": self.billing_scope(),
             "deadline": T_PLUS(-1)}, now=T_PLUS(), expect_status=400)
        self.assertEqual(body["error"]["code"], "deadline_in_past")


class TestExemptions(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.sid = self.create_service()
        self.v1 = self.first_version(self.sid)
        # 两个消费者：billing 与 search
        self.declare(self.sid, "billing", self.billing_scope())
        search_scope = {
            "used_fields": ["response:order_id"],
            "enum_uses": {}, "handled_errors": [],
        }
        self.declare(self.sid, "search", search_scope)

    def test_exemption_scoped_to_caller(self):
        v2 = {"fields": [CONTRACT_V1["fields"][0]],
              "enums": CONTRACT_V1["enums"], "errors": CONTRACT_V1["errors"]}
        _, body = self.submit(self.sid, v2, parent_id=self.v1)
        vid = body["version"]["id"]
        # 无豁免：两个调用方都未覆盖。
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(), expect_status=200)
        findings = self.findings_by_change(body["review"])
        self.assertEqual(
            findings["field.removed#response:remark"]["uncovered"],
            ["billing"])

        # 只给 billing 发豁免：search 不用 remark，本就不受影响 → 通过。
        s, ex = self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0001",
             "affected_consumers": ["billing"],
             "reason": "P1 故障修复，billing 已确认忽略 remark",
             "created_by": "oncall", "expires_at": T_PLUS(2)},
            now=T_PLUS(), expect_status=201)
        self.assertTrue(ex["active"])

        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(1), expect_status=200)
        self.assertEqual(body["version"]["status"], "CANDIDATE")
        findings = self.findings_by_change(body["review"])
        self.assertEqual(
            findings["field.removed#response:remark"]["exempted"], ["billing"])

    def test_exemption_does_not_cover_other_callers(self):
        # order_id 类型改变同时影响 billing 与 search，只豁免 billing。
        fields = [{"name": "order_id", "in": "response", "type": "integer",
                   "required": True}, CONTRACT_V1["fields"][1]]
        v2 = {"fields": fields, "enums": CONTRACT_V1["enums"],
              "errors": CONTRACT_V1["errors"]}
        _, body = self.submit(self.sid, v2, parent_id=self.v1)
        vid = body["version"]["id"]
        self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0002", "affected_consumers": ["billing"],
             "reason": "紧急", "created_by": "oncall",
             "expires_at": T_PLUS(2)}, now=T_PLUS(), expect_status=201)
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(1), expect_status=200)
        self.assertEqual(body["version"]["status"], "REJECTED")
        findings = self.findings_by_change(body["review"])
        f = findings["field.type_changed#response:order_id"]
        self.assertEqual(f["exempted"], ["billing"])
        self.assertEqual(f["uncovered"], ["search"])

    def test_restricted_changes_must_match(self):
        # 豁免只限定枚举新增，字段删除不应被覆盖。
        fields = [CONTRACT_V1["fields"][0]]
        enums = [{"name": "OrderStatus",
                  "values": ["PENDING", "PAID", "REFUNDED"], "closed": True}]
        v2 = {"fields": fields, "enums": enums,
              "errors": CONTRACT_V1["errors"]}
        _, body = self.submit(self.sid, v2, parent_id=self.v1)
        vid = body["version"]["id"]
        self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0003", "affected_consumers": ["billing"],
             "restricted_changes": [
                 "enum.value_added_closed#enum:OrderStatus"],
             "reason": "仅枚举紧急扩展", "created_by": "oncall",
             "expires_at": T_PLUS(2)}, now=T_PLUS(), expect_status=201)
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(1), expect_status=200)
        self.assertEqual(body["version"]["status"], "REJECTED")
        findings = self.findings_by_change(body["review"])
        self.assertEqual(
            findings["field.removed#response:remark"]["uncovered"],
            ["billing"])
        self.assertEqual(
            findings["enum.value_added_closed#enum:OrderStatus"]["exempted"],
            ["billing"])

    def test_exemption_requires_known_consumers(self):
        s, body = self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0004", "affected_consumers": ["ghost"],
             "reason": "x", "expires_at": T_PLUS(1)},
            now=T_PLUS(), expect_status=400)
        self.assertEqual(body["error"]["code"], "unknown_consumers")

    def test_exemption_requires_expiry(self):
        s, body = self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0005", "affected_consumers": ["billing"],
             "reason": "x"}, now=T_PLUS(), expect_status=400)
        self.assertEqual(body["error"]["code"], "expiry_required")

    def test_exemption_max_duration_enforced(self):
        s, body = self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0006", "affected_consumers": ["billing"],
             "reason": "x", "expires_at": T_PLUS(30)},
            now=T_PLUS(), expect_status=400)
        self.assertEqual(body["error"]["code"], "expiry_too_long")

    def test_exemption_expires_automatically(self):
        v2 = {"fields": [CONTRACT_V1["fields"][0]],
              "enums": CONTRACT_V1["enums"], "errors": CONTRACT_V1["errors"]}
        _, body = self.submit(self.sid, v2, parent_id=self.v1)
        vid = body["version"]["id"]
        self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0007", "affected_consumers": ["billing"],
             "reason": "紧急", "expires_at": T_PLUS(2)},
            now=T_PLUS(), expect_status=201)
        # 到期后重新评审：豁免不再生效。
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(3), expect_status=200)
        self.assertEqual(body["version"]["status"], "REJECTED")

        # 豁免自动到期后尝试发布候选也应被 409 拦截。
        self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0008", "affected_consumers": ["billing"],
             "reason": "再紧急一次", "expires_at": T_PLUS(5)},
            now=T_PLUS(3), expect_status=201)
        self.api.request("POST", f"/versions/{vid}/admit", {},
                         now=T_PLUS(4), expect_status=200)
        self.api.request("POST", f"/versions/{vid}/publish",
                         {"publisher": "bot"}, now=T_PLUS(6),
                         expect_status=409)

        s, body = self.api.request(
            "GET", f"/services/{self.sid}/exemptions?include_expired=1",
            now=T_PLUS(7))
        self.assertEqual(len(body["exemptions"]), 2)
        self.assertFalse(body["exemptions"][0]["active"])

    def test_exemption_revocation(self):
        s, ex = self.api.request(
            "POST", f"/services/{self.sid}/exemptions",
            {"code": "EXM-0009", "affected_consumers": ["billing"],
             "reason": "紧急", "expires_at": T_PLUS(2)},
            now=T_PLUS(), expect_status=201)
        s, body = self.api.request(
            "POST", f"/exemptions/{ex['id']}/revoke", {}, now=T_PLUS(1),
            expect_status=200)
        self.assertFalse(body["active"])
        self.assertIsNotNone(body["revoked_at"])


class TestLineageAndWithdraw(ApiTestCase):
    def test_concurrent_forks_lineage_and_safe_withdrawal(self):
        sid = self.create_service()
        v1 = self.first_version(sid)

        # 两个团队基于同一版本并发提交，形成分叉。
        va_contract = {
            "fields": CONTRACT_V1["fields"] + [
                {"name": "a_field", "in": "response", "type": "string",
                 "required": False}],
            "enums": CONTRACT_V1["enums"], "errors": CONTRACT_V1["errors"]}
        vb_contract = {
            "fields": CONTRACT_V1["fields"] + [
                {"name": "b_field", "in": "response", "type": "string",
                 "required": False}],
            "enums": CONTRACT_V1["enums"], "errors": CONTRACT_V1["errors"]}
        _, va = self.submit(sid, va_contract, parent_id=v1,
                            submitter="team-a")
        _, vb = self.submit(sid, vb_contract, parent_id=v1,
                            submitter="team-b")
        va_id, vb_id = va["version"]["id"], vb["version"]["id"]
        self.api.request("POST", f"/versions/{va_id}/admit", {},
                         now=T_PLUS(), expect_status=200)

        # 再有团队基于候选 va 继续提交后继版本（此时 va 还未撤回）。
        vc_contract = {
            "fields": va_contract["fields"] + [
                {"name": "c_field", "in": "response", "type": "string",
                 "required": False}],
            "enums": CONTRACT_V1["enums"], "errors": CONTRACT_V1["errors"]}
        _, vc = self.submit(sid, vc_contract, parent_id=va_id,
                            submitter="team-c")
        vc_id = vc["version"]["id"]

        # 撤回候选 va。
        s, body = self.api.request(
            "POST", f"/versions/{va_id}/withdraw",
            {"reason": "方案调整，放弃 a_field"}, now=T_PLUS(1),
            expect_status=200)
        self.assertEqual(body["version"]["status"], "WITHDRAWN")
        self.assertEqual(body["child_version_ids"], [vc_id])

        # 已产生的评审仍然保留；后继版本 vc 不受影响，可独立评审、发布。
        s, reviews = self.api.request("GET", f"/versions/{va_id}/reviews",
                                      now=T_PLUS(1))
        self.assertGreaterEqual(len(reviews["reviews"]), 1)
        s, body = self.api.request("POST", f"/versions/{vc_id}/admit", {},
                                   now=T_PLUS(1), expect_status=200)
        self.assertEqual(body["version"]["status"], "CANDIDATE")
        s, body = self.api.request("POST", f"/versions/{vc_id}/publish",
                                   {"publisher": "bot"}, now=T_PLUS(1),
                                   expect_status=200)
        self.assertEqual(body["version"]["status"], "PUBLISHED")

        # 谱系完整呈现根、分叉、撤回节点。
        lineage = self.api.request("GET", f"/services/{sid}/lineage",
                                   now=T_PLUS(1))[1]
        self.assertEqual(lineage["published_version_id"], vc_id)
        statuses = {n["id"]: n["status"] for n in lineage["nodes"]}
        self.assertEqual(statuses[va_id], "WITHDRAWN")
        self.assertEqual(statuses[vb_id], "SUBMITTED")
        edges = {(e["from"], e["to"]) for e in lineage["edges"]}
        self.assertIn((v1, va_id), edges)
        self.assertIn((v1, vb_id), edges)
        self.assertIn((va_id, vc_id), edges)

    def test_withdraw_guard(self):
        sid = self.create_service()
        v1 = self.first_version(sid)
        # 已发布版本不可撤回。
        self.api.request("POST", f"/versions/{v1}/withdraw", {},
                         now=T_PLUS(), expect_status=409)

    def test_duplicate_contract_rejected(self):
        sid = self.create_service()
        self.first_version(sid)
        s, body = self.submit(sid, CONTRACT_V1, parent_id=None, status=409)
        self.assertEqual(body["error"]["code"], "conflict")


class TestPublishProvenance(ApiTestCase):
    def test_publish_record_explains_declarations_and_exemptions(self):
        sid = self.create_service()
        v1 = self.first_version(sid)
        deadline = T_PLUS(15)
        # billing 的承诺范围不包含错误处理；错误语义变化影响的是 risk 团队。
        scope = dict(self.billing_scope())
        scope["handled_errors"] = []
        self.declare(sid, "billing", scope, deadline=deadline)
        self.declare(sid, "risk",
                     {"used_fields": [], "enum_uses": {},
                      "handled_errors": ["NOT_FOUND"]})

        v2 = {
            "fields": [
                {"name": "order_id", "in": "response", "type": "string",
                 "required": True},
                {"name": "remark", "in": "response", "type": "string",
                 "required": False},
            ],
            "enums": [{"name": "OrderStatus",
                       "values": ["PENDING", "PAID", "REFUNDED"],
                       "closed": True}],
            "errors": [{"code": "NOT_FOUND",
                        "semantics": "订单或购物车不存在"}],
        }
        _, body = self.submit(sid, v2, parent_id=v1)
        vid = body["version"]["id"]
        # 枚举新增：由 billing 的迁移承诺放行；
        # 错误语义变化不在 billing 承诺范围内，必须凭限定变更的豁免放行 risk。
        self.api.request(
            "POST", f"/services/{sid}/exemptions",
            {"code": "EXM-PROV-1", "affected_consumers": ["risk"],
             "restricted_changes": [
                 "error.semantics_changed#error:NOT_FOUND"],
             "reason": "错误语义扩展经 risk 确认",
             "expires_at": T_PLUS(5)}, now=T_PLUS(), expect_status=201)
        s, body = self.api.request("POST", f"/versions/{vid}/admit", {},
                                   now=T_PLUS(1), expect_status=200)
        self.assertEqual(body["version"]["status"], "CANDIDATE")

        s, body = self.api.request("POST", f"/versions/{vid}/publish",
                                   {"publisher": "release-bot",
                                    "note": "2.1 发布"},
                                   now=T_PLUS(2), expect_status=200)
        ev = body["evidence"]
        kinds = {c["change_id"]: c for c in ev["contributions"]}
        self.assertIn("error.semantics_changed#error:NOT_FOUND", kinds)
        sem = kinds["error.semantics_changed#error:NOT_FOUND"]
        self.assertEqual(
            sem["allowed_by_exemption"][0]["exemption"], "EXM-PROV-1")
        self.assertEqual(sem["allowed_by_exemption"][0]["consumer"], "risk")
        self.assertEqual(sem["allowed_by_migration_commitment"], [])
        enum_c = kinds["enum.value_added_closed#enum:OrderStatus"]
        # 枚举变化由迁移承诺促成，与错误语义变化的豁免来源在溯源中清晰区分。
        self.assertEqual(
            [d["consumer"] for d in enum_c["allowed_by_migration_commitment"]],
            ["billing"])
        self.assertEqual(enum_c["allowed_by_exemption"], [])
        supporting_codes = [e["code"] for e in ev["supporting_exemptions"]]
        self.assertEqual(supporting_codes, ["EXM-PROV-1"])
        supporting_consumers = {d["consumer"]
                                for d in ev["supporting_declarations"]}
        self.assertEqual(supporting_consumers, {"billing"})
        self.assertIn("EXM-PROV-1", ev["explanation"])
        self.assertIn("risk", ev["explanation"])
        self.assertIn("billing", ev["explanation"])

        # 可通过独立接口取回发布溯源。
        s, rec = self.api.request("GET", f"/services/{sid}/publish",
                                  now=T_PLUS(2))
        self.assertEqual(rec["version_id"], vid)
        self.assertEqual(rec["evidence"]["publish_record_id"]
                         if "publish_record_id" in rec["evidence"] else
                         body["publish_record_id"],
                         body["publish_record_id"])


class TestConcurrentSubmissions(ApiTestCase):
    def test_parallel_submissions_get_distinct_seq_and_lineage(self):
        sid = self.create_service()
        v1 = self.first_version(sid)

        results = []
        errors = []

        def submit_one(idx):
            try:
                contract = {
                    "fields": CONTRACT_V1["fields"] + [
                        {"name": f"team_{idx}_field", "in": "response",
                         "type": "string", "required": False}],
                    "enums": CONTRACT_V1["enums"],
                    "errors": CONTRACT_V1["errors"]}
                s, body = self.api.request(
                    "POST", f"/services/{sid}/versions",
                    {"contract": contract, "parent_id": v1,
                     "submitter": f"team-{idx}"}, now=T_PLUS())
                results.append((s, body))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=submit_one, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(s == 201 for s, _ in results))
        seqs = sorted(b["version"]["seq"] for _, b in results)
        self.assertEqual(seqs, list(range(2, 10)))  # seq 唯一且连续
        hashes = [b["version"]["contract_hash"] for _, b in results]
        self.assertEqual(len(set(hashes)), 8)

        lineage = self.api.request("GET", f"/services/{sid}/lineage",
                                   now=T_PLUS())[1]
        self.assertEqual(len(lineage["edges"]), 8)
        self.assertTrue(all(e["from"] == v1 for e in lineage["edges"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
