'use strict';

/**
 * 契约 schema 结构化 diff：只产出"破坏性变化"。
 * 新增字段 / 新增枚举值 / 新增错误码视为兼容，不产生变化项。
 *
 * 每个变化项带稳定 key，供豁免精确引用：
 *   field:<name>            字段删除 / 类型变更 / 变为必填
 *   enum:<enumName>=<value> 枚举值删除
 *   error:<code>            错误码删除 / 语义变更
 */
function diffContracts(base = {}, candidate = {}) {
  const changes = [];

  const baseFields = base.fields || {};
  const candFields = candidate.fields || {};
  for (const [name, spec] of Object.entries(baseFields)) {
    if (!(name in candFields)) {
      changes.push({
        key: `field:${name}`,
        kind: 'FIELD_REMOVED',
        subject: name,
        detail: `field '${name}' was removed`,
      });
      continue;
    }
    const candSpec = candFields[name] || {};
    if (spec.type !== candSpec.type) {
      changes.push({
        key: `field:${name}`,
        kind: 'FIELD_TYPE_CHANGED',
        subject: name,
        detail: `field '${name}' type changed from '${spec.type}' to '${candSpec.type}'`,
      });
    } else if (!spec.required && candSpec.required) {
      changes.push({
        key: `field:${name}`,
        kind: 'FIELD_REQUIRED_TIGHTENED',
        subject: name,
        detail: `field '${name}' became required`,
      });
    }
  }

  const baseEnums = base.enums || {};
  const candEnums = candidate.enums || {};
  for (const [enumName, values] of Object.entries(baseEnums)) {
    const candValues = candEnums[enumName] || [];
    for (const value of values) {
      if (!candValues.includes(value)) {
        changes.push({
          key: `enum:${enumName}=${value}`,
          kind: 'ENUM_VALUE_REMOVED',
          subject: `${enumName}=${value}`,
          detail: `enum value '${value}' was removed from '${enumName}'`,
        });
      }
    }
  }

  const baseErrors = base.errors || {};
  const candErrors = candidate.errors || {};
  for (const [code, spec] of Object.entries(baseErrors)) {
    if (!(code in candErrors)) {
      changes.push({
        key: `error:${code}`,
        kind: 'ERROR_REMOVED',
        subject: code,
        detail: `error '${code}' was removed`,
      });
      continue;
    }
    const candSpec = candErrors[code] || {};
    if (spec.severity !== candSpec.severity || spec.meaning !== candSpec.meaning) {
      changes.push({
        key: `error:${code}`,
        kind: 'ERROR_SEMANTIC_CHANGED',
        subject: code,
        detail: `error '${code}' semantics changed (severity/meaning)`,
      });
    }
  }

  return changes;
}

module.exports = { diffContracts };
