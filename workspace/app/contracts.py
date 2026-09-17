"""契约的校验、规范化与基础工具。

契约结构（版本不可变内容）::

    {
      "fields": [{"name": "order_id", "in": "response"|"request"|"both",
                  "type": "string", "required": true}, ...],
      "enums":  [{"name": "OrderStatus", "values": ["PENDING", ...],
                  "closed": true}, ...],
      "errors": [{"code": "NOT_FOUND", "semantics": "订单不存在"}, ...]
    }

所有名称/编码在各自集合内必须唯一。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

FIELD_LOCATIONS = {"request", "response", "both"}


class ContractError(ValueError):
    """契约或请求体不合法（映射为 HTTP 400）。"""


def _require_object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{label} 必须是对象")
    return value


def canonical_hash(payload: dict) -> str:
    """对规范化后的 JSON 计算 SHA-256，用于去重与谱系完整性。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_contract(payload: Any) -> dict:
    """校验并规范化契约，返回可直接比较/hash 的稳定结构。"""
    data = _require_object(payload, "contract")

    fields_raw = data.get("fields", [])
    enums_raw = data.get("enums", [])
    errors_raw = data.get("errors", [])
    if not isinstance(fields_raw, list) or not isinstance(enums_raw, list) \
            or not isinstance(errors_raw, list):
        raise ContractError("fields/enums/errors 必须是数组")

    fields: list[dict] = []
    seen_fields: set[str] = set()
    for item in fields_raw:
        item = _require_object(item, "field")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ContractError("field.name 必须是非空字符串")
        location = item.get("in", "both")
        if location not in FIELD_LOCATIONS:
            raise ContractError(f"field {name} 的 in 必须是 {FIELD_LOCATIONS}")
        ftype = item.get("type")
        if not isinstance(ftype, str) or not ftype:
            raise ContractError(f"field {name} 的 type 必须是非空字符串")
        required = bool(item.get("required", False))
        key = f"{location}:{name}"
        if key in seen_fields:
            raise ContractError(f"字段重复: {key}")
        seen_fields.add(key)
        fields.append({"name": name, "in": location,
                       "type": ftype, "required": required})
    fields.sort(key=lambda f: (f["in"], f["name"]))

    enums: list[dict] = []
    seen_enums: set[str] = set()
    for item in enums_raw:
        item = _require_object(item, "enum")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ContractError("enum.name 必须是非空字符串")
        if name in seen_enums:
            raise ContractError(f"枚举重复: {name}")
        values = item.get("values", [])
        if not isinstance(values, list) or not all(
                isinstance(v, str) and v for v in values):
            raise ContractError(f"enum {name} 的 values 必须是非空字符串数组")
        if len(set(values)) != len(values):
            raise ContractError(f"enum {name} 的 values 存在重复")
        seen_enums.add(name)
        enums.append({"name": name, "values": sorted(values),
                      "closed": bool(item.get("closed", False))})
    enums.sort(key=lambda e: e["name"])

    errors: list[dict] = []
    seen_errors: set[str] = set()
    for item in errors_raw:
        item = _require_object(item, "error")
        code = item.get("code")
        if not isinstance(code, str) or not code:
            raise ContractError("error.code 必须是非空字符串")
        semantics = item.get("semantics", "")
        if not isinstance(semantics, str):
            raise ContractError(f"error {code} 的 semantics 必须是字符串")
        if code in seen_errors:
            raise ContractError(f"错误码重复: {code}")
        seen_errors.add(code)
        errors.append({"code": code, "semantics": semantics})
    errors.sort(key=lambda e: e["code"])

    return {"fields": fields, "enums": enums, "errors": errors}


def normalize_scope(payload: Any) -> dict:
    """校验消费者声明的承诺使用范围。"""
    data = _require_object(payload, "scope")

    def str_list(value: Any, label: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(
                isinstance(v, str) and v for v in value):
            raise ContractError(f"{label} 必须是非空字符串数组")
        return sorted(set(value))

    used_fields = str_list(data.get("used_fields"), "used_fields")

    enum_uses_raw = data.get("enum_uses", {})
    if enum_uses_raw is None:
        enum_uses_raw = {}
    if not isinstance(enum_uses_raw, dict):
        raise ContractError("enum_uses 必须是对象")
    enum_uses: dict[str, dict] = {}
    for enum_name, use in enum_uses_raw.items():
        use = _require_object(use, f"enum_uses.{enum_name}")
        used_values = str_list(use.get("used_values"),
                               f"enum_uses.{enum_name}.used_values")
        enum_uses[enum_name] = {"used_values": used_values,
                                "closed_assumed": bool(
                                    use.get("closed_assumed", False))}

    handled_errors = str_list(data.get("handled_errors"), "handled_errors")

    return {"used_fields": used_fields, "enum_uses": enum_uses,
            "handled_errors": handled_errors}


def fields_by_key(contract: dict) -> dict[str, dict]:
    return {f"{f['in']}:{f['name']}": f for f in contract["fields"]}


def enums_by_name(contract: dict) -> dict[str, dict]:
    return {e["name"]: e for e in contract["enums"]}


def errors_by_code(contract: dict) -> dict[str, dict]:
    return {e["code"]: e for e in contract["errors"]}
