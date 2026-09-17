'use strict';

/**
 * 语义化版本解析与范围匹配。
 * 支持 "1.2.3"（可带 v 前缀与 -pre/+build 后缀）以及由 >= <= > < == != = 组成、
 * 用逗号或空格分隔的范围表达式，如 ">=1.2.0,<2.0.0"。
 */

const VERSION_RE = /^\s*v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?\s*$/;
const CONSTRAINT_RE = /^(>=|<=|==|!=|>|<|=)?(.+)$/;

function parseVersion(v) {
  const m = VERSION_RE.exec(String(v));
  if (!m) throw new Error(`invalid version: ${v}`);
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function cmp(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return 0;
}

const OPS = {
  '>=': (a, b) => cmp(a, b) >= 0,
  '<=': (a, b) => cmp(a, b) <= 0,
  '>': (a, b) => cmp(a, b) > 0,
  '<': (a, b) => cmp(a, b) < 0,
  '==': (a, b) => cmp(a, b) === 0,
  '=': (a, b) => cmp(a, b) === 0,
  '!=': (a, b) => cmp(a, b) !== 0,
};

function satisfies(version, rangeExpr) {
  if (!rangeExpr || !String(rangeExpr).trim()) return true;
  const v = parseVersion(version);
  const parts = String(rangeExpr).split(/[,\s]+/).filter(Boolean);
  for (const part of parts) {
    const m = CONSTRAINT_RE.exec(part);
    if (!m) throw new Error(`invalid range constraint: ${part}`);
    const op = m[1] || '==';
    const target = parseVersion(m[2]);
    if (!OPS[op](v, target)) return false;
  }
  return true;
}

/** 校验范围表达式合法，非法时抛错。 */
function validateRange(rangeExpr) {
  if (!rangeExpr || !String(rangeExpr).trim()) throw new Error('range expression is empty');
  const parts = String(rangeExpr).split(/[,\s]+/).filter(Boolean);
  for (const part of parts) {
    const m = CONSTRAINT_RE.exec(part);
    if (!m) throw new Error(`invalid range constraint: ${part}`);
    parseVersion(m[2]);
  }
}

module.exports = { parseVersion, satisfies, validateRange };
