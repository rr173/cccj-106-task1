"""兼容性引擎纯函数单元测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app import compatibility as compat
from app.contracts import normalize_contract

NOW = datetime(2026, 9, 17, tzinfo=timezone.utc)


def decl(consumer, deadline=None, **scope):
    full = {"used_fields": [], "enum_uses": {}, "handled_errors": []}
    full.update(scope)
    return {"consumer": consumer, "scope": full,
            "deadline": deadline.isoformat() if deadline else None}


def exemption(code, consumers, expires, restricted=None, active=True):
    return {"code": code, "active": active,
            "affected_consumers": consumers,
            "restricted_changes": restricted,
            "expires_at": expires.isoformat() if expires else None,
            "reason": "test"}


class CompatibilityEngineTest(unittest.TestCase):
    def test_field_type_change_affects_only_user(self):
        base = normalize_contract({"fields": [
            {"name": "f", "in": "response", "type": "string",
             "required": True}]})
        new = normalize_contract({"fields": [
            {"name": "f", "in": "response", "type": "integer",
             "required": True}]})
        r = compat.evaluate(base, new, [decl("a", used_fields=["response:f"]),
                                        decl("b")], [], NOW)
        findings = {f["change_id"]: f for f in r["findings"]}
        f = findings["field.type_changed#response:f"]
        self.assertEqual(f["affected"], ["a"])
        self.assertEqual(f["uncovered"], ["a"])
        self.assertEqual(r["decision"], "REJECTED")

    def test_optional_field_addition_is_safe(self):
        base = normalize_contract({})
        new = normalize_contract({"fields": [
            {"name": "f", "in": "response", "type": "string",
             "required": False}]})
        r = compat.evaluate(base, new, [decl("a")], [], NOW)
        self.assertEqual(r["decision"], "CANDIDATE")
        self.assertFalse(r["findings"][0]["breaking"])

    def test_required_field_addition_affects_all_consumers(self):
        base = normalize_contract({})
        new = normalize_contract({"fields": [
            {"name": "f", "in": "request", "type": "string",
             "required": True}]})
        r = compat.evaluate(base, new,
                            [decl("a", used_fields=[]), decl("b")], [], NOW)
        self.assertEqual(r["findings"][0]["affected"], ["a", "b"])
        self.assertEqual(r["decision"], "REJECTED")

    def test_enum_removal_matches_used_values(self):
        base = normalize_contract({"enums": [
            {"name": "E", "values": ["X", "Y"], "closed": True}]})
        new = normalize_contract({"enums": [
            {"name": "E", "values": ["X"], "closed": True}]})
        r = compat.evaluate(base, new, [
            decl("uses-y", enum_uses={"E": {"used_values": ["Y"],
                                            "closed_assumed": False}}),
            decl("uses-x", enum_uses={"E": {"used_values": ["X"],
                                            "closed_assumed": False}}),
        ], [], NOW)
        f = r["findings"][0]
        self.assertEqual(f["affected"], ["uses-y"])

    def test_enum_added_open_safe_when_not_assumed_closed(self):
        base = normalize_contract({"enums": [
            {"name": "E", "values": ["X"], "closed": False}]})
        new = normalize_contract({"enums": [
            {"name": "E", "values": ["X", "Z"], "closed": False}]})
        r = compat.evaluate(base, new, [
            decl("a", enum_uses={"E": {"used_values": ["X"],
                                       "closed_assumed": False}})], [], NOW)
        self.assertEqual(r["decision"], "CANDIDATE")
        self.assertEqual(r["findings"][0]["kind"],
                         compat.ENUM_VALUE_ADDED_OPEN)

    def test_enum_added_closed_affects_closed_assumer(self):
        base = normalize_contract({"enums": [
            {"name": "E", "values": ["X"], "closed": True}]})
        new = normalize_contract({"enums": [
            {"name": "E", "values": ["X", "Z"], "closed": True}]})
        r = compat.evaluate(base, new, [
            decl("a", enum_uses={"E": {"used_values": ["X"],
                                       "closed_assumed": True}}),
            decl("b", enum_uses={"E": {"used_values": ["X"],
                                       "closed_assumed": False}})],
            [], NOW)
        self.assertEqual(r["findings"][0]["affected"], ["a"])

    def test_error_removal_affects_handler(self):
        base = normalize_contract({"errors": [{"code": "E1",
                                               "semantics": "x"}]})
        new = normalize_contract({"errors": []})
        r = compat.evaluate(base, new, [decl("a", handled_errors=["E1"]),
                                        decl("b")], [], NOW)
        self.assertEqual(r["findings"][0]["affected"], ["a"])

    def test_error_semantics_change_affects_handler(self):
        base = normalize_contract({"errors": [{"code": "E1",
                                               "semantics": "old"}]})
        new = normalize_contract({"errors": [{"code": "E1",
                                              "semantics": "new"}]})
        r = compat.evaluate(base, new, [decl("a", handled_errors=["E1"])],
                            [], NOW)
        self.assertEqual(r["findings"][0]["kind"],
                         compat.ERROR_SEMANTICS_CHANGED)

    def test_deadline_commitment_window(self):
        from datetime import timedelta
        base = normalize_contract({"fields": [
            {"name": "f", "in": "response", "type": "string",
             "required": True}]})
        new = normalize_contract({})
        declarations = [decl("a", NOW + timedelta(days=2),
                             used_fields=["response:f"])]
        inside = compat.evaluate(base, new, declarations, [], NOW)
        self.assertEqual(inside["decision"], "CANDIDATE")
        self.assertEqual(inside["findings"][0]["committed"], ["a"])
        after = compat.evaluate(base, new, declarations, [],
                                NOW + timedelta(days=3))
        self.assertEqual(after["decision"], "REJECTED")
        self.assertEqual(after["findings"][0]["uncovered"], ["a"])

    def test_exemption_caller_scope_and_restricted_changes(self):
        from datetime import timedelta
        base = normalize_contract({"fields": [
            {"name": "f", "in": "response", "type": "string",
             "required": True}]})
        new = normalize_contract({})
        declarations = [decl("a", used_fields=["response:f"]),
                        decl("b", used_fields=["response:f"])]
        cid = "field.removed#response:f"

        # 只覆盖 a：b 仍未覆盖。
        ex = exemption("EXM-1", ["a"], NOW + timedelta(days=1))
        r = compat.evaluate(base, new, declarations, [ex], NOW)
        f = r["findings"][0]
        self.assertEqual(f["exempted"], ["a"])
        self.assertEqual(f["uncovered"], ["b"])

        # restricted_changes 不匹配时不覆盖。
        ex2 = exemption("EXM-2", ["a", "b"], NOW + timedelta(days=1),
                        restricted=["other.change#x"])
        r = compat.evaluate(base, new, declarations, [ex2], NOW)
        self.assertEqual(r["findings"][0]["uncovered"], ["a", "b"])

        # 过期豁免不覆盖。
        ex3 = exemption("EXM-3", ["a", "b"], NOW - timedelta(seconds=1))
        r = compat.evaluate(base, new, declarations, [ex3], NOW)
        self.assertEqual(r["findings"][0]["uncovered"], ["a", "b"])

        # 已撤销豁免不覆盖。
        ex4 = exemption("EXM-4", ["a", "b"], NOW + timedelta(days=1),
                        active=False)
        r = compat.evaluate(base, new, declarations, [ex4], NOW)
        self.assertEqual(r["findings"][0]["uncovered"], ["a", "b"])

    def test_breaking_change_without_consumers_is_allowed(self):
        base = normalize_contract({"fields": [
            {"name": "f", "in": "response", "type": "string",
             "required": True}]})
        new = normalize_contract({})
        r = compat.evaluate(base, new, [], [], NOW)
        self.assertEqual(r["decision"], "CANDIDATE")
        self.assertTrue(r["findings"][0]["breaking"])
        self.assertEqual(r["findings"][0]["uncovered"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
