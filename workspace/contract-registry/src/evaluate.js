'use strict';

const { diffContracts } = require('./diff');
const { satisfies } = require('./semver');

/**
 * 迁移期限约束使用的豁免 key 前缀。
 * 消费者处于迁移窗口内时，候选版本必须落在其 committedRange 内；
 * 紧急情况下可用 key "migration:<consumer>" 的豁免临时放行。
 */
const MIGRATION_KEY_PREFIX = 'migration:';

/** 声明是否覆盖了某个破坏性变化。 */
function declarationCovers(decl, change) {
  switch (change.kind) {
    case 'FIELD_REMOVED':
    case 'FIELD_TYPE_CHANGED':
    case 'FIELD_REQUIRED_TIGHTENED':
      return (decl.usedFields || []).includes(change.subject);
    case 'ENUM_VALUE_REMOVED': {
      const [enumName, value] = change.subject.split('=');
      return ((decl.usedEnumValues || {})[enumName] || []).includes(value);
    }
    case 'ERROR_REMOVED':
    case 'ERROR_SEMANTIC_CHANGED':
      return (decl.usedErrors || []).includes(change.subject);
    default:
      return false;
  }
}

/** 在活跃豁免中查找匹配 consumer + key 的一条。 */
function findExemption(exemptions, consumer, key) {
  return exemptions.find((e) => e.consumer === consumer && (e.violationKeys || []).includes(key)) || null;
}

/**
 * 评估一个候选版本。
 *
 * @param {object}   ctx.declarations 该服务仍处于 ACTIVE 状态的消费者声明
 * @param {object[]} ctx.exemptions  该服务当前活跃（未撤销且未过期）的豁免
 * @param {object}   ctx.parent      父版本（谱系中的基线），根版本为 null
 * @param {object}   ctx.version     候选版本（含 version 与 schema）
 * @param {Date}     ctx.now         评估时刻
 * @returns 评估报告；decision 为 CANDIDATE 或 BLOCKED。报告整体入库，供事后审计与发布解释。
 */
function evaluateVersion({ declarations, exemptions, parent, version, now }) {
  const base = parent ? parent.schema : {};
  const changes = diffContracts(base, version.schema);

  // 1) 破坏性变化 × 活跃声明：逐消费者判定 阻塞 / 豁免
  const violations = changes.map((change) => {
    const affected = declarations.filter((d) => declarationCovers(d, change));
    const waived = [];
    const blocking = [];
    for (const d of affected) {
      const ex = findExemption(exemptions, d.consumer, change.key);
      if (ex) {
        waived.push({
          consumer: d.consumer,
          declarationId: d.id,
          exemptionId: ex.id,
          exemptionExpiresAt: ex.expiresAt,
        });
      } else {
        blocking.push({ consumer: d.consumer, declarationId: d.id });
      }
    }
    const status = affected.length === 0 ? 'UNCLAIMED' : blocking.length > 0 ? 'BLOCKING' : 'WAIVED';
    return {
      ...change,
      affectedConsumers: affected.map((d) => d.consumer),
      waived,
      blocking,
      status,
    };
  });

  // 2) 迁移期限：窗口内候选版本必须落在消费者承诺范围内
  const deadlineChecks = [];
  for (const d of declarations) {
    if (!d.migrationDeadline || !d.committedRange) continue;
    const inWindow = new Date(d.migrationDeadline) > now;
    const within = satisfies(version.version, d.committedRange);
    const entry = {
      key: MIGRATION_KEY_PREFIX + d.consumer,
      consumer: d.consumer,
      declarationId: d.id,
      committedRange: d.committedRange,
      migrationDeadline: d.migrationDeadline,
      inWindow,
      versionWithinRange: within,
      status: 'OK',
    };
    if (inWindow && !within) {
      const ex = findExemption(exemptions, d.consumer, entry.key);
      if (ex) {
        entry.status = 'WAIVED';
        entry.exemptionId = ex.id;
        entry.exemptionExpiresAt = ex.expiresAt;
      } else {
        entry.status = 'BLOCKING';
      }
    }
    deadlineChecks.push(entry);
  }

  // 3) 按声明汇总结果，供发布解释（"哪些声明与豁免促成了结果"）
  const declarationOutcomes = declarations.map((d) => {
    const related = violations.filter((v) => v.affectedConsumers.includes(d.consumer));
    const dc = deadlineChecks.find((c) => c.declarationId === d.id);
    const exemptionsUsed = new Set();
    for (const v of related) {
      for (const w of v.waived) if (w.consumer === d.consumer) exemptionsUsed.add(w.exemptionId);
    }
    if (dc && dc.exemptionId) exemptionsUsed.add(dc.exemptionId);
    const isBlocking =
      related.some((v) => v.blocking.some((b) => b.consumer === d.consumer)) ||
      (dc && dc.status === 'BLOCKING');
    const outcome = isBlocking ? 'BLOCKING' : exemptionsUsed.size > 0 ? 'WAIVED_BY_EXEMPTION' : 'SATISFIED';
    return {
      declarationId: d.id,
      consumer: d.consumer,
      claims: {
        usedFields: d.usedFields || [],
        usedEnumValues: d.usedEnumValues || {},
        usedErrors: d.usedErrors || [],
      },
      committedRange: d.committedRange || null,
      migrationDeadline: d.migrationDeadline || null,
      outcome,
      exemptionsUsed: [...exemptionsUsed],
    };
  });

  const blocked =
    violations.some((v) => v.status === 'BLOCKING') || deadlineChecks.some((c) => c.status === 'BLOCKING');

  return {
    evaluatedAt: now.toISOString(),
    baseVersion: parent ? parent.version : null,
    candidateVersion: version.version,
    changes,
    violations,
    deadlineChecks,
    declarations: declarationOutcomes,
    exemptionsConsidered: exemptions.map((e) => ({
      id: e.id,
      consumer: e.consumer,
      violationKeys: e.violationKeys,
      reason: e.reason,
      expiresAt: e.expiresAt,
    })),
    decision: blocked ? 'BLOCKED' : 'CANDIDATE',
  };
}

module.exports = { evaluateVersion, declarationCovers, findExemption, MIGRATION_KEY_PREFIX };
