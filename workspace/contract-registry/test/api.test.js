'use strict';

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');

const { createApp } = require('../src/app');
const { Store } = require('../src/store');

// ---------------------------------------------------------------------------
// 测试基建：每个用例一个全新内存 store 的应用实例
// ---------------------------------------------------------------------------

let server;
let base;
let store;

async function request(method, path, body) {
  const res = await fetch(base + path, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let json = null;
  try {
    json = await res.json();
  } catch {
    /* no body */
  }
  return { status: res.status, body: json };
}

const get = (p) => request('GET', p);
const post = (p, b) => request('POST', p, b);

before(async () => {
  store = new Store(null); // 内存模式
  const app = createApp({ store, maxExemptionDays: 30 });
  server = await new Promise((resolve) => {
    const s = app.listen(0, '127.0.0.1', () => resolve(s));
  });
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => server.close());

/** 每个用例使用独立服务名，互不影响。 */
let seq = 0;
async function freshService() {
  seq += 1;
  const name = `svc-${seq}`;
  const r = await post('/api/services', { name, ownerTeam: 'team-x' });
  assert.equal(r.status, 201);
  return name;
}

const SCHEMA_V1 = {
  fields: {
    'user.id': { type: 'string', required: true },
    'user.legacy_id': { type: 'string' },
    'user.email': { type: 'string' },
  },
  enums: { status: ['ACTIVE', 'SUSPENDED'] },
  errors: { USER_NOT_FOUND: { severity: 'client', meaning: 'unknown user id' } },
};

function schemaWithoutField(field) {
  const s = JSON.parse(JSON.stringify(SCHEMA_V1));
  delete s.fields[field];
  return s;
}

function inDays(days) {
  return new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString();
}

// ---------------------------------------------------------------------------
// 版本提交与兼容性评估
// ---------------------------------------------------------------------------

test('root version with no declarations becomes CANDIDATE', async () => {
  const svc = await freshService();
  const r = await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  assert.equal(r.status, 201);
  assert.equal(r.body.status, 'CANDIDATE');
  assert.equal(r.body.parentVersion, null);
  assert.equal(r.body.reviews.length, 1);
  assert.equal(r.body.reviews[0].decision, 'CANDIDATE');
});

test('duplicate version is rejected', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  const r = await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  assert.equal(r.status, 409);
});

test('invalid version string and invalid schema are rejected', async () => {
  const svc = await freshService();
  let r = await post(`/api/services/${svc}/versions`, { version: '1.0', schema: SCHEMA_V1 });
  assert.equal(r.status, 400);
  r = await post(`/api/services/${svc}/versions`, {
    version: '1.0.0',
    schema: { fields: { f: { type: 42 } } },
  });
  assert.equal(r.status, 400);
});

test('field removal used by an active declaration is BLOCKED with violation detail', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, {
    consumer: 'billing',
    usedFields: ['user.legacy_id'],
  });

  const r = await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  assert.equal(r.status, 201);
  assert.equal(r.body.status, 'BLOCKED');
  const report = r.body.reviews.at(-1).report;
  const v = report.violations.find((x) => x.key === 'field:user.legacy_id');
  assert.equal(v.kind, 'FIELD_REMOVED');
  assert.equal(v.status, 'BLOCKING');
  assert.deepEqual(v.affectedConsumers, ['billing']);
  assert.equal(report.decision, 'BLOCKED');
});

test('breaking change not claimed by any declaration does not block', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  const r = await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  assert.equal(r.body.status, 'CANDIDATE');
  const v = r.body.reviews.at(-1).report.violations.find((x) => x.key === 'field:user.legacy_id');
  assert.equal(v.status, 'UNCLAIMED');
});

test('enum value removal and error semantic change are detected', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, {
    consumer: 'mobile-app',
    usedEnumValues: { status: ['SUSPENDED'] },
    usedErrors: ['USER_NOT_FOUND'],
  });

  const next = JSON.parse(JSON.stringify(SCHEMA_V1));
  next.enums.status = ['ACTIVE']; // 删除 SUSPENDED
  next.errors.USER_NOT_FOUND = { severity: 'client', meaning: 'account closed' }; // 语义变更

  const r = await post(`/api/services/${svc}/versions`, { version: '1.1.0', schema: next });
  assert.equal(r.body.status, 'BLOCKED');
  const violations = r.body.reviews.at(-1).report.violations;
  const enumV = violations.find((x) => x.key === 'enum:status=SUSPENDED');
  const errV = violations.find((x) => x.key === 'error:USER_NOT_FOUND');
  assert.equal(enumV.kind, 'ENUM_VALUE_REMOVED');
  assert.equal(enumV.status, 'BLOCKING');
  assert.equal(errV.kind, 'ERROR_SEMANTIC_CHANGED');
  assert.equal(errV.status, 'BLOCKING');
});

test('field type change and required tightening are breaking', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, {
    consumer: 'crm',
    usedFields: ['user.legacy_id', 'user.email'],
  });
  const next = JSON.parse(JSON.stringify(SCHEMA_V1));
  next.fields['user.legacy_id'] = { type: 'integer' };
  next.fields['user.email'] = { type: 'string', required: true };
  const r = await post(`/api/services/${svc}/versions`, { version: '1.1.0', schema: next });
  assert.equal(r.body.status, 'BLOCKED');
  const kinds = r.body.reviews.at(-1).report.violations.map((v) => v.kind).sort();
  assert.deepEqual(kinds, ['FIELD_REQUIRED_TIGHTENED', 'FIELD_TYPE_CHANGED']);
});

test('retired declarations no longer participate in evaluation', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  const d = await post(`/api/services/${svc}/declarations`, {
    consumer: 'old-consumer',
    usedFields: ['user.legacy_id'],
  });
  await post(`/api/services/${svc}/declarations/${d.body.id}/retire`);
  const r = await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  assert.equal(r.body.status, 'CANDIDATE');
});

// ---------------------------------------------------------------------------
// 紧急豁免：限定调用方、自动到期、不允许永久
// ---------------------------------------------------------------------------

test('exemption is scoped to the affected caller only', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, { consumer: 'billing', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/declarations`, { consumer: 'crm', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });

  // 只豁免 billing：crm 仍然阻塞
  await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:user.legacy_id'],
    reason: 'billing migration window',
    expiresAt: inDays(3),
  });
  let r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.body.decision, 'BLOCKED');
  const v1 = r.body.violations.find((x) => x.key === 'field:user.legacy_id');
  assert.deepEqual(v1.waived.map((w) => w.consumer), ['billing']);
  assert.deepEqual(v1.blocking.map((b) => b.consumer), ['crm']);

  // 再豁免 crm：全部放行
  await post(`/api/services/${svc}/exemptions`, {
    consumer: 'crm',
    violationKeys: ['field:user.legacy_id'],
    reason: 'crm hotfix window',
    expiresAt: inDays(3),
  });
  r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.body.decision, 'CANDIDATE');
});

test('expired exemption no longer applies (auto-expiry)', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, { consumer: 'billing', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  const ex = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:user.legacy_id'],
    reason: 'temporary',
    expiresAt: inDays(1),
  });
  assert.equal(ex.status, 201);

  let r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.body.decision, 'CANDIDATE');

  // 直接将该豁免的到期时间改到过去，模拟时间流逝
  const row = store.data.exemptions.find((e) => e.id === ex.body.id);
  row.expiresAt = new Date(Date.now() - 1000).toISOString();

  r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.body.decision, 'BLOCKED');

  // 列表接口同样反映其不再活跃
  const list = await get(`/api/services/${svc}/exemptions?active=true`);
  assert.equal(list.body.exemptions.length, 0);
});

test('exemption validation: caller scoping, expiry required, max duration', async () => {
  const svc = await freshService();
  // 缺少 consumer
  let r = await post(`/api/services/${svc}/exemptions`, {
    violationKeys: ['field:x'],
    reason: 'r',
    expiresAt: inDays(1),
  });
  assert.equal(r.status, 400);
  // 缺少 expiresAt（永久豁免不允许）
  r = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:x'],
    reason: 'r',
  });
  assert.equal(r.status, 400);
  // 到期时间在过去
  r = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:x'],
    reason: 'r',
    expiresAt: new Date(Date.now() - 1000).toISOString(),
  });
  assert.equal(r.status, 400);
  // 超过 30 天上限
  r = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:x'],
    reason: 'r',
    expiresAt: inDays(31),
  });
  assert.equal(r.status, 400);
  // 空的 violationKeys
  r = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: [],
    reason: 'r',
    expiresAt: inDays(1),
  });
  assert.equal(r.status, 400);
});

test('revoked exemption no longer applies', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, { consumer: 'billing', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  const ex = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:user.legacy_id'],
    reason: 'temporary',
    expiresAt: inDays(2),
  });
  let r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.body.decision, 'CANDIDATE');
  await post(`/api/services/${svc}/exemptions/${ex.body.id}/revoke`);
  r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.body.decision, 'BLOCKED');
});

// ---------------------------------------------------------------------------
// 迁移期限
// ---------------------------------------------------------------------------

test('migration deadline blocks out-of-range versions while in window', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, {
    consumer: 'mobile-app',
    committedRange: '>=1.0.0,<2.0.0',
    migrationDeadline: inDays(14),
  });

  // 2.0.0 超出承诺范围，且处于迁移窗口内 → 阻止进入候选
  const additive = JSON.parse(JSON.stringify(SCHEMA_V1));
  additive.fields['user.nickname'] = { type: 'string' };
  let r = await post(`/api/services/${svc}/versions`, { version: '2.0.0', schema: additive });
  assert.equal(r.body.status, 'BLOCKED');
  const dc = r.body.reviews.at(-1).report.deadlineChecks.find((c) => c.consumer === 'mobile-app');
  assert.equal(dc.status, 'BLOCKING');
  assert.equal(dc.inWindow, true);
  assert.equal(dc.versionWithinRange, false);

  // 1.5.0 在承诺范围内 → 允许
  r = await post(`/api/services/${svc}/versions`, { version: '1.5.0', schema: additive });
  assert.equal(r.body.status, 'CANDIDATE');
});

test('migration deadline can be waived by a caller-scoped exemption', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, {
    consumer: 'mobile-app',
    committedRange: '<2.0.0',
    migrationDeadline: inDays(14),
  });
  const additive = JSON.parse(JSON.stringify(SCHEMA_V1));
  additive.fields['user.nickname'] = { type: 'string' };
  await post(`/api/services/${svc}/versions`, { version: '2.0.0', schema: additive });

  await post(`/api/services/${svc}/exemptions`, {
    consumer: 'mobile-app',
    violationKeys: ['migration:mobile-app'],
    reason: 'coordinated early rollout',
    expiresAt: inDays(2),
  });
  const r = await post(`/api/services/${svc}/versions/2.0.0/reevaluate`);
  assert.equal(r.body.decision, 'CANDIDATE');
  const dc = r.body.deadlineChecks.find((c) => c.consumer === 'mobile-app');
  assert.equal(dc.status, 'WAIVED');
});

test('expired migration deadline lifts the range constraint', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  const d = await post(`/api/services/${svc}/declarations`, {
    consumer: 'mobile-app',
    committedRange: '<2.0.0',
    migrationDeadline: inDays(1),
  });
  // 模拟期限已过
  const row = store.data.declarations.find((x) => x.id === d.body.id);
  row.migrationDeadline = new Date(Date.now() - 1000).toISOString();

  const additive = JSON.parse(JSON.stringify(SCHEMA_V1));
  additive.fields['user.nickname'] = { type: 'string' };
  const r = await post(`/api/services/${svc}/versions`, { version: '2.0.0', schema: additive });
  assert.equal(r.body.status, 'CANDIDATE');
});

test('migration deadline requires committedRange and a future date', async () => {
  const svc = await freshService();
  let r = await post(`/api/services/${svc}/declarations`, {
    consumer: 'c1',
    migrationDeadline: inDays(1),
  });
  assert.equal(r.status, 400);
  r = await post(`/api/services/${svc}/declarations`, {
    consumer: 'c1',
    committedRange: '<2.0.0',
    migrationDeadline: new Date(Date.now() - 1000).toISOString(),
  });
  assert.equal(r.status, 400);
});

// ---------------------------------------------------------------------------
// 谱系与撤回
// ---------------------------------------------------------------------------

test('concurrent submissions form sibling branches in the lineage', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  const a = JSON.parse(JSON.stringify(SCHEMA_V1));
  a.fields['user.a'] = { type: 'string' };
  const b = JSON.parse(JSON.stringify(SCHEMA_V1));
  b.fields['user.b'] = { type: 'string' };
  // 两个提交都显式基于 1.0.0（并发场景）
  await post(`/api/services/${svc}/versions`, { version: '1.1.0', schema: a, parentVersion: '1.0.0' });
  await post(`/api/services/${svc}/versions`, { version: '1.2.0', schema: b, parentVersion: '1.0.0' });

  const lin = await get(`/api/services/${svc}/lineage`);
  assert.equal(lin.body.nodes.length, 3);
  const edges = lin.body.edges.map((e) => `${e.from}->${e.to}`).sort();
  assert.deepEqual(edges, ['1.0.0->1.1.0', '1.0.0->1.2.0']);
});

test('withdrawing a candidate preserves descendant reviews and lineage', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  const a = JSON.parse(JSON.stringify(SCHEMA_V1));
  a.fields['user.a'] = { type: 'string' };
  await post(`/api/services/${svc}/versions`, { version: '1.1.0', schema: a, parentVersion: '1.0.0' });
  const b = JSON.parse(JSON.stringify(a));
  b.fields['user.b'] = { type: 'string' };
  await post(`/api/services/${svc}/versions`, { version: '1.1.1', schema: b, parentVersion: '1.1.0' });

  // 撤回中间候选 1.1.0
  const w = await post(`/api/services/${svc}/versions/1.1.0/withdraw`, { reason: 'superseded' });
  assert.equal(w.body.status, 'WITHDRAWN');

  // 后续版本及其评审完好
  const child = await get(`/api/services/${svc}/versions/1.1.1`);
  assert.equal(child.body.status, 'CANDIDATE');
  assert.equal(child.body.parentVersion, '1.1.0');
  assert.equal(child.body.reviews.length, 1);
  assert.equal(child.body.reviews[0].report.baseVersion, '1.1.0');

  // 谱系完整保留被撤回节点与边
  const lin = await get(`/api/services/${svc}/lineage`);
  const node110 = lin.body.nodes.find((n) => n.version === '1.1.0');
  assert.equal(node110.status, 'WITHDRAWN');
  const edges = lin.body.edges.map((e) => `${e.from}->${e.to}`).sort();
  assert.deepEqual(edges, ['1.0.0->1.1.0', '1.1.0->1.1.1']);

  // 已撤回版本不能再评估，但历史评审仍可查
  const r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.status, 409);
  const detail = await get(`/api/services/${svc}/versions/1.1.0`);
  assert.equal(detail.body.reviews.length, 1);
});

test('published versions cannot be withdrawn', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/versions/1.0.0/publish`, { publishedBy: 'release-bot' });
  const r = await post(`/api/services/${svc}/versions/1.0.0/withdraw`, { reason: 'oops' });
  assert.equal(r.status, 409);
});

// ---------------------------------------------------------------------------
// 发布与可解释性
// ---------------------------------------------------------------------------

test('publish explanation names the declarations and exemptions that enabled it', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, { consumer: 'billing', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/declarations`, { consumer: 'crm', usedFields: ['user.id'] });

  // 删除 billing 依赖的字段 → 阻塞；为 billing 开豁免 → 进入候选
  await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  const ex = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:user.legacy_id'],
    reason: 'billing migrates next sprint',
    expiresAt: inDays(5),
  });
  const re = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(re.body.decision, 'CANDIDATE');

  const pub = await post(`/api/services/${svc}/versions/1.1.0/publish`, { publishedBy: 'release-bot' });
  assert.equal(pub.status, 201);

  // 声明维度：billing 由豁免放行，crm 自然满足
  const billing = pub.body.declarations.find((d) => d.consumer === 'billing');
  const crm = pub.body.declarations.find((d) => d.consumer === 'crm');
  assert.equal(billing.outcome, 'WAIVED_BY_EXEMPTION');
  assert.deepEqual(billing.exemptionsUsed, [ex.body.id]);
  assert.equal(crm.outcome, 'SATISFIED');

  // 豁免维度：实际促成发布的豁免被点名
  assert.equal(pub.body.exemptionsApplied.length, 1);
  assert.equal(pub.body.exemptionsApplied[0].id, ex.body.id);
  assert.equal(pub.body.exemptionsApplied[0].consumer, 'billing');

  // 发布记录可回放查询
  const again = await get(`/api/services/${svc}/versions/1.1.0/publish`);
  assert.equal(again.status, 200);
  assert.equal(again.body.summary, `published ${svc}@1.1.0`);

  // 版本状态与重复发布
  const detail = await get(`/api/services/${svc}/versions/1.1.0`);
  assert.equal(detail.body.status, 'PUBLISHED');
  const dup = await post(`/api/services/${svc}/versions/1.1.0/publish`, {});
  assert.equal(dup.status, 409);
});

test('blocked versions cannot be published', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, { consumer: 'billing', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  const r = await post(`/api/services/${svc}/versions/1.1.0/publish`, {});
  assert.equal(r.status, 409);
  assert.equal(r.body.details.decision, 'BLOCKED');
});

test('publish re-checks at publish time: an expired exemption re-blocks', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, { consumer: 'billing', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  const ex = await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:user.legacy_id'],
    reason: 'temporary',
    expiresAt: inDays(1),
  });
  let r = await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  assert.equal(r.body.decision, 'CANDIDATE');

  // 豁免到期后再发布 → 拒绝
  const row = store.data.exemptions.find((e) => e.id === ex.body.id);
  row.expiresAt = new Date(Date.now() - 1000).toISOString();
  const pub = await post(`/api/services/${svc}/versions/1.1.0/publish`, {});
  assert.equal(pub.status, 409);
});

// ---------------------------------------------------------------------------
// 其他行为
// ---------------------------------------------------------------------------

test('unknown parent version is rejected; default parent is latest published', async () => {
  const svc = await freshService();
  let r = await post(`/api/services/${svc}/versions`, {
    version: '1.0.0',
    schema: SCHEMA_V1,
    parentVersion: '9.9.9',
  });
  assert.equal(r.status, 404);

  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/versions/1.0.0/publish`, {});
  const a = JSON.parse(JSON.stringify(SCHEMA_V1));
  a.fields['user.a'] = { type: 'string' };
  r = await post(`/api/services/${svc}/versions`, { version: '1.1.0', schema: a });
  assert.equal(r.body.parentVersion, '1.0.0');
});

test('review history is append-only across re-evaluations', async () => {
  const svc = await freshService();
  await post(`/api/services/${svc}/versions`, { version: '1.0.0', schema: SCHEMA_V1 });
  await post(`/api/services/${svc}/declarations`, { consumer: 'billing', usedFields: ['user.legacy_id'] });
  await post(`/api/services/${svc}/versions`, {
    version: '1.1.0',
    schema: schemaWithoutField('user.legacy_id'),
  });
  await post(`/api/services/${svc}/exemptions`, {
    consumer: 'billing',
    violationKeys: ['field:user.legacy_id'],
    reason: 'window',
    expiresAt: inDays(1),
  });
  await post(`/api/services/${svc}/versions/1.1.0/reevaluate`);
  const detail = await get(`/api/services/${svc}/versions/1.1.0`);
  assert.equal(detail.body.reviews.length, 2);
  assert.equal(detail.body.reviews[0].decision, 'BLOCKED');
  assert.equal(detail.body.reviews[1].decision, 'CANDIDATE');
});
