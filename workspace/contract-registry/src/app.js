'use strict';

const express = require('express');
const { evaluateVersion } = require('./evaluate');
const { parseVersion, validateRange } = require('./semver');

// ---------------------------------------------------------------------------
// 错误与校验辅助
// ---------------------------------------------------------------------------

function badRequest(res, message, details) {
  return res.status(400).json({ error: 'BAD_REQUEST', message, ...(details ? { details } : {}) });
}
function notFound(res, message) {
  return res.status(404).json({ error: 'NOT_FOUND', message });
}
function conflict(res, message, details) {
  return res.status(409).json({ error: 'CONFLICT', message, ...(details ? { details } : {}) });
}

const isNonEmptyString = (s) => typeof s === 'string' && s.trim().length > 0;
const isStringArray = (a) => Array.isArray(a) && a.every((x) => typeof x === 'string');

function parseIsoDate(value) {
  if (typeof value !== 'string') return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 校验契约 schema 结构，返回错误消息或 null。 */
function validateContractSchema(schema) {
  if (typeof schema !== 'object' || schema === null || Array.isArray(schema)) {
    return 'schema must be an object';
  }
  const fields = schema.fields || {};
  const enums = schema.enums || {};
  const errors = schema.errors || {};
  if (typeof fields !== 'object' || fields === null || Array.isArray(fields)) return 'schema.fields must be an object';
  for (const [name, spec] of Object.entries(fields)) {
    if (typeof spec !== 'object' || spec === null || typeof spec.type !== 'string') {
      return `schema.fields['${name}'].type must be a string`;
    }
    if ('required' in spec && typeof spec.required !== 'boolean') {
      return `schema.fields['${name}'].required must be a boolean`;
    }
  }
  if (typeof enums !== 'object' || enums === null || Array.isArray(enums)) return 'schema.enums must be an object';
  for (const [name, values] of Object.entries(enums)) {
    if (!isStringArray(values)) return `schema.enums['${name}'] must be an array of strings`;
  }
  if (typeof errors !== 'object' || errors === null || Array.isArray(errors)) return 'schema.errors must be an object';
  for (const [code, spec] of Object.entries(errors)) {
    if (typeof spec !== 'object' || spec === null) return `schema.errors['${code}'] must be an object`;
    if ('severity' in spec && typeof spec.severity !== 'string') return `schema.errors['${code}'].severity must be a string`;
    if ('meaning' in spec && typeof spec.meaning !== 'string') return `schema.errors['${code}'].meaning must be a string`;
  }
  return null;
}

function normalizeContractSchema(schema) {
  return {
    fields: schema.fields || {},
    enums: schema.enums || {},
    errors: schema.errors || {},
  };
}

// ---------------------------------------------------------------------------
// 序列化视图
// ---------------------------------------------------------------------------

function serviceView(s) {
  return { id: s.id, name: s.name, ownerTeam: s.ownerTeam, createdAt: s.createdAt };
}

function versionSummary(v, versionsById) {
  const parent = v.parentId != null ? versionsById.get(v.parentId) : null;
  return {
    id: v.id,
    version: v.version,
    status: v.status,
    parentVersion: parent ? parent.version : null,
    submittedBy: v.submittedBy,
    createdAt: v.createdAt,
    withdrawnAt: v.withdrawnAt || null,
  };
}

function versionDetail(v, store) {
  const versionsById = new Map(store.data.versions.map((x) => [x.id, x]));
  const reviews = store.data.reviews
    .filter((r) => r.versionId === v.id)
    .sort((a, b) => a.id - b.id)
    .map((r) => ({ id: r.id, createdAt: r.createdAt, decision: r.decision, report: r.report }));
  const publish = store.data.publishes.find((p) => p.versionId === v.id) || null;
  return {
    ...versionSummary(v, versionsById),
    withdrawReason: v.withdrawReason || null,
    schema: v.schema,
    reviews,
    published: Boolean(publish),
  };
}

function declarationView(d) {
  return {
    id: d.id,
    consumer: d.consumer,
    usedFields: d.usedFields,
    usedEnumValues: d.usedEnumValues,
    usedErrors: d.usedErrors,
    committedRange: d.committedRange,
    migrationDeadline: d.migrationDeadline,
    status: d.status,
    createdAt: d.createdAt,
  };
}

function exemptionView(e, now) {
  const active = !e.revokedAt && new Date(e.expiresAt) > now;
  return {
    id: e.id,
    consumer: e.consumer,
    violationKeys: e.violationKeys,
    reason: e.reason,
    createdBy: e.createdBy,
    createdAt: e.createdAt,
    expiresAt: e.expiresAt,
    revokedAt: e.revokedAt || null,
    active,
  };
}

// ---------------------------------------------------------------------------
// 应用工厂
// ---------------------------------------------------------------------------

function createApp({ store, maxExemptionDays = 30 }) {
  const app = express();
  app.use(express.json({ limit: '2mb' }));

  const now = () => new Date();

  const findService = (name) => store.data.services.find((s) => s.name === name) || null;
  const findVersion = (serviceId, version) =>
    store.data.versions.find((v) => v.serviceId === serviceId && v.version === version) || null;

  const activeDeclarations = (serviceId) =>
    store.data.declarations.filter((d) => d.serviceId === serviceId && d.status === 'ACTIVE');

  const activeExemptions = (serviceId, at) =>
    store.data.exemptions.filter(
      (e) => e.serviceId === serviceId && !e.revokedAt && new Date(e.expiresAt) > at
    );

  /** 对某个版本执行评估并落一条不可变评审记录，同步版本状态。 */
  function runEvaluation(service, version) {
    const versionsById = new Map(store.data.versions.map((x) => [x.id, x]));
    const parent = version.parentId != null ? versionsById.get(version.parentId) : null;
    const at = now();
    const report = evaluateVersion({
      declarations: activeDeclarations(service.id),
      exemptions: activeExemptions(service.id, at),
      parent,
      version,
      now: at,
    });
    const review = {
      id: store.nextId('reviews'),
      versionId: version.id,
      createdAt: at.toISOString(),
      decision: report.decision,
      report,
    };
    store.data.reviews.push(review);
    version.status = report.decision;
    return review;
  }

  app.get('/health', (req, res) => res.json({ status: 'ok' }));

  // ------------------------------------------------------------------ services

  app.post('/api/services', (req, res) => {
    const { name, ownerTeam = '' } = req.body || {};
    if (!isNonEmptyString(name)) return badRequest(res, 'name is required');
    if (findService(name)) return conflict(res, `service '${name}' already exists`);
    const service = { id: store.nextId('services'), name, ownerTeam, createdAt: now().toISOString() };
    store.data.services.push(service);
    store.save();
    return res.status(201).json(serviceView(service));
  });

  app.get('/api/services', (req, res) => {
    return res.json({ services: store.data.services.map(serviceView) });
  });

  // ------------------------------------------------------------------ versions

  // 提交新版本：解析父版本形成谱系，随后立即评估，决定能否进入候选。
  app.post('/api/services/:name/versions', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);

    const { version, schema, parentVersion = null, submittedBy = 'anonymous' } = req.body || {};
    try {
      parseVersion(version);
    } catch (e) {
      return badRequest(res, e.message);
    }
    const schemaErr = validateContractSchema(schema);
    if (schemaErr) return badRequest(res, schemaErr);
    if (findVersion(service.id, version)) {
      return conflict(res, `version '${version}' already exists for service '${service.name}'`);
    }

    // 谱系解析：显式指定父版本，否则默认挂在最新已发布版本（无则最新未撤回版本）之下。
    const siblings = store.data.versions.filter((v) => v.serviceId === service.id);
    let parent = null;
    if (parentVersion != null) {
      parent = siblings.find((v) => v.version === parentVersion) || null;
      if (!parent) return notFound(res, `parent version '${parentVersion}' not found`);
    } else {
      const byIdDesc = (a, b) => b.id - a.id;
      parent =
        siblings.filter((v) => v.status === 'PUBLISHED').sort(byIdDesc)[0] ||
        siblings.filter((v) => v.status !== 'WITHDRAWN').sort(byIdDesc)[0] ||
        null;
    }

    const record = {
      id: store.nextId('versions'),
      serviceId: service.id,
      version,
      parentId: parent ? parent.id : null,
      schema: normalizeContractSchema(schema),
      status: 'SUBMITTED',
      submittedBy,
      createdAt: now().toISOString(),
      withdrawnAt: null,
      withdrawReason: null,
    };
    store.data.versions.push(record);
    runEvaluation(service, record);
    store.save();
    return res.status(201).json(versionDetail(record, store));
  });

  app.get('/api/services/:name/versions', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const versionsById = new Map(store.data.versions.map((x) => [x.id, x]));
    const versions = store.data.versions
      .filter((v) => v.serviceId === service.id)
      .sort((a, b) => a.id - b.id)
      .map((v) => versionSummary(v, versionsById));
    return res.json({ service: service.name, versions });
  });

  app.get('/api/services/:name/versions/:version', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const version = findVersion(service.id, req.params.version);
    if (!version) return notFound(res, `version '${req.params.version}' not found`);
    return res.json(versionDetail(version, store));
  });

  // 重新评估：豁免创建/到期、声明退休后可触发。评审记录追加，历史不可变。
  app.post('/api/services/:name/versions/:version/reevaluate', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const version = findVersion(service.id, req.params.version);
    if (!version) return notFound(res, `version '${req.params.version}' not found`);
    if (version.status === 'PUBLISHED' || version.status === 'WITHDRAWN') {
      return conflict(res, `cannot re-evaluate a ${version.status.toLowerCase()} version`);
    }
    const review = runEvaluation(service, version);
    store.save();
    return res.json(review.report);
  });

  // 撤回：仅打标记。评审历史与基于该版本的后续版本、后续评审全部保留。
  app.post('/api/services/:name/versions/:version/withdraw', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const version = findVersion(service.id, req.params.version);
    if (!version) return notFound(res, `version '${req.params.version}' not found`);
    if (version.status === 'PUBLISHED') return conflict(res, 'published versions cannot be withdrawn');
    if (version.status === 'WITHDRAWN') return conflict(res, 'version is already withdrawn');
    version.status = 'WITHDRAWN';
    version.withdrawnAt = now().toISOString();
    version.withdrawReason = (req.body && req.body.reason) || null;
    store.save();
    return res.json(versionDetail(version, store));
  });

  // 发布：发布前做一次全新评估（声明/豁免可能已变化），通过则生成可解释的发布记录。
  app.post('/api/services/:name/versions/:version/publish', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const version = findVersion(service.id, req.params.version);
    if (!version) return notFound(res, `version '${req.params.version}' not found`);
    if (version.status === 'WITHDRAWN') return conflict(res, 'withdrawn versions cannot be published');
    if (store.data.publishes.some((p) => p.versionId === version.id)) {
      return conflict(res, `version '${version.version}' is already published`);
    }

    const review = runEvaluation(service, version);
    if (review.decision !== 'CANDIDATE') {
      store.save();
      return conflict(res, 'version is blocked and cannot be published', review.report);
    }

    const publishedBy = (req.body && req.body.publishedBy) || 'anonymous';
    const usedExemptionIds = new Set(review.report.declarations.flatMap((d) => d.exemptionsUsed));
    const explanation = {
      summary: `published ${service.name}@${version.version}`,
      publishedBy,
      publishedAt: now().toISOString(),
      reviewId: review.id,
      baseVersion: review.report.baseVersion,
      // 每条活跃声明的判定结果（满足 / 豁免放行），以及实际促成发布的豁免
      declarations: review.report.declarations,
      exemptionsApplied: review.report.exemptionsConsidered.filter((e) => usedExemptionIds.has(e.id)),
      migrationWindows: review.report.deadlineChecks,
      violations: review.report.violations,
    };
    const record = {
      id: store.nextId('publishes'),
      versionId: version.id,
      publishedBy,
      publishedAt: explanation.publishedAt,
      explanation,
    };
    store.data.publishes.push(record);
    version.status = 'PUBLISHED';
    store.save();
    return res.status(201).json(explanation);
  });

  app.get('/api/services/:name/versions/:version/publish', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const version = findVersion(service.id, req.params.version);
    if (!version) return notFound(res, `version '${req.params.version}' not found`);
    const record = store.data.publishes.find((p) => p.versionId === version.id);
    if (!record) return notFound(res, `version '${version.version}' has not been published`);
    return res.json(record.explanation);
  });

  // 谱系：全部版本节点（含已撤回）+ 父子边，并发提交呈现为同父兄弟分支。
  app.get('/api/services/:name/lineage', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const versions = store.data.versions
      .filter((v) => v.serviceId === service.id)
      .sort((a, b) => a.id - b.id);
    const byId = new Map(versions.map((v) => [v.id, v]));
    const nodes = versions.map((v) => {
      const parent = v.parentId != null ? byId.get(v.parentId) : null;
      return {
        id: v.id,
        version: v.version,
        status: v.status,
        parentVersion: parent ? parent.version : null,
        submittedBy: v.submittedBy,
        createdAt: v.createdAt,
        withdrawnAt: v.withdrawnAt || null,
      };
    });
    const edges = nodes
      .filter((n) => n.parentVersion != null)
      .map((n) => ({ from: n.parentVersion, to: n.version }));
    return res.json({ service: service.name, nodes, edges });
  });

  // ------------------------------------------------------------------ declarations

  app.post('/api/services/:name/declarations', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);

    const {
      consumer,
      usedFields = [],
      usedEnumValues = {},
      usedErrors = [],
      committedRange = null,
      migrationDeadline = null,
    } = req.body || {};

    if (!isNonEmptyString(consumer)) return badRequest(res, 'consumer is required');
    if (!isStringArray(usedFields)) return badRequest(res, 'usedFields must be an array of strings');
    if (!isStringArray(usedErrors)) return badRequest(res, 'usedErrors must be an array of strings');
    if (typeof usedEnumValues !== 'object' || usedEnumValues === null || Array.isArray(usedEnumValues)) {
      return badRequest(res, 'usedEnumValues must be an object of enum name to string array');
    }
    for (const [k, v] of Object.entries(usedEnumValues)) {
      if (!isStringArray(v)) return badRequest(res, `usedEnumValues['${k}'] must be an array of strings`);
    }
    if (committedRange != null) {
      try {
        validateRange(committedRange);
      } catch (e) {
        return badRequest(res, `invalid committedRange: ${e.message}`);
      }
    }
    let deadlineIso = null;
    if (migrationDeadline != null) {
      const d = parseIsoDate(migrationDeadline);
      if (!d) return badRequest(res, 'migrationDeadline must be an ISO-8601 datetime');
      if (d <= now()) return badRequest(res, 'migrationDeadline must be in the future');
      if (committedRange == null) {
        return badRequest(res, 'committedRange is required when migrationDeadline is set');
      }
      deadlineIso = d.toISOString();
    }

    const decl = {
      id: store.nextId('declarations'),
      serviceId: service.id,
      consumer,
      usedFields,
      usedEnumValues,
      usedErrors,
      committedRange,
      migrationDeadline: deadlineIso,
      status: 'ACTIVE',
      createdAt: now().toISOString(),
    };
    store.data.declarations.push(decl);
    store.save();
    return res.status(201).json(declarationView(decl));
  });

  app.get('/api/services/:name/declarations', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    let decls = store.data.declarations.filter((d) => d.serviceId === service.id);
    if (req.query.status) decls = decls.filter((d) => d.status === req.query.status);
    return res.json({ declarations: decls.map(declarationView) });
  });

  // 退休声明：消费者下线或不再使用该接口后，其声明不再参与兼容性判断。
  app.post('/api/services/:name/declarations/:id/retire', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const decl = store.data.declarations.find(
      (d) => d.serviceId === service.id && d.id === Number(req.params.id)
    );
    if (!decl) return notFound(res, `declaration ${req.params.id} not found`);
    if (decl.status === 'RETIRED') return conflict(res, 'declaration is already retired');
    decl.status = 'RETIRED';
    store.save();
    return res.json(declarationView(decl));
  });

  // ------------------------------------------------------------------ exemptions

  // 创建紧急豁免：必须限定受影响调用方（consumer），必须到期，且不允许超过上限天数。
  app.post('/api/services/:name/exemptions', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);

    const { consumer, violationKeys, reason, expiresAt, createdBy = 'anonymous' } = req.body || {};
    if (!isNonEmptyString(consumer)) return badRequest(res, 'consumer is required: exemptions must be scoped to affected callers');
    if (!isStringArray(violationKeys) || violationKeys.length === 0) {
      return badRequest(res, 'violationKeys must be a non-empty array of strings');
    }
    if (!isNonEmptyString(reason)) return badRequest(res, 'reason is required');
    const expiry = parseIsoDate(expiresAt);
    if (!expiry) return badRequest(res, 'expiresAt is required and must be an ISO-8601 datetime: permanent exemptions are not allowed');
    if (expiry <= now()) return badRequest(res, 'expiresAt must be in the future');
    const maxExpiry = new Date(now().getTime() + maxExemptionDays * 24 * 60 * 60 * 1000);
    if (expiry > maxExpiry) {
      return badRequest(res, `exemptions may not exceed ${maxExemptionDays} days`);
    }

    const exemption = {
      id: store.nextId('exemptions'),
      serviceId: service.id,
      consumer,
      violationKeys,
      reason,
      createdBy,
      createdAt: now().toISOString(),
      expiresAt: expiry.toISOString(),
      revokedAt: null,
    };
    store.data.exemptions.push(exemption);
    store.save();
    return res.status(201).json(exemptionView(exemption, now()));
  });

  app.get('/api/services/:name/exemptions', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const at = now();
    let list = store.data.exemptions.filter((e) => e.serviceId === service.id);
    if (req.query.active === 'true') {
      list = list.filter((e) => !e.revokedAt && new Date(e.expiresAt) > at);
    } else if (req.query.active === 'false') {
      list = list.filter((e) => e.revokedAt || new Date(e.expiresAt) <= at);
    }
    return res.json({ exemptions: list.map((e) => exemptionView(e, at)) });
  });

  app.post('/api/services/:name/exemptions/:id/revoke', (req, res) => {
    const service = findService(req.params.name);
    if (!service) return notFound(res, `service '${req.params.name}' not found`);
    const exemption = store.data.exemptions.find(
      (e) => e.serviceId === service.id && e.id === Number(req.params.id)
    );
    if (!exemption) return notFound(res, `exemption ${req.params.id} not found`);
    if (exemption.revokedAt) return conflict(res, 'exemption is already revoked');
    exemption.revokedAt = now().toISOString();
    store.save();
    return res.json(exemptionView(exemption, now()));
  });

  // 404 & 错误处理
  app.use((req, res) => notFound(res, 'route not found'));
  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, next) => {
    if (err && err.type === 'entity.parse.failed') return badRequest(res, 'request body must be valid JSON');
    return res.status(500).json({ error: 'INTERNAL', message: err.message });
  });

  return app;
}

module.exports = { createApp };
