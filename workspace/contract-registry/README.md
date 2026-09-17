# Contract Registry — 接口契约登记与演进系统

面向服务团队的接口契约注册中心。提供方提交新版本时，系统结合**仍在使用的消费者声明**自动判断字段、枚举与错误语义变化是否兼容；消费者可登记**迁移期限**约束候选准入；**紧急豁免**限定调用方并自动到期；并发提交形成清晰**版本谱系**；撤回不破坏既有评审；发布结果完整**可解释**。

## 核心概念

| 概念 | 说明 |
| --- | --- |
| **Service** | 一个被治理的接口服务（如 `payments`）。 |
| **ContractVersion** | 一次版本提交，携带结构化契约 schema，通过 `parentId` 构成谱系 DAG。状态机：`SUBMITTED → CANDIDATE / BLOCKED → PUBLISHED / WITHDRAWN`。 |
| **ConsumerDeclaration** | 消费者声明自己依赖的字段、枚举值、错误码，以及承诺版本范围与迁移期限。`RETIRED` 后不再参与判断。 |
| **Exemption** | 紧急豁免：**必须指定受影响调用方**（consumer）、**必须有到期时间**且不超过 `MAX_EXEMPTION_DAYS`（默认 30 天）。到期/撤销后自动失效，无法永久绕过检查。 |
| **Review** | 每次评估生成一条**不可变**评审记录（追加式历史），撤回版本不影响已有评审。 |
| **PublishRecord** | 发布时生成的解释记录：哪些声明被满足、哪些豁免实际促成了发布。 |

## 兼容性规则

以父版本契约为基线做结构化 diff，仅以下**破坏性变化**会触发消费者声明检查（新增一律兼容）：

| 变化 | 豁免 key | 触发条件 |
| --- | --- | --- |
| 字段删除 | `field:<name>` | 某活跃声明的 `usedFields` 含该字段 |
| 字段类型变更 | `field:<name>` | 同上 |
| 字段变为必填 | `field:<name>` | 同上 |
| 枚举值删除 | `enum:<enum>=<value>` | 声明的 `usedEnumValues` 含该枚举值 |
| 错误码删除 | `error:<code>` | 声明的 `usedErrors` 含该错误码 |
| 错误语义变更（severity/meaning） | `error:<code>` | 同上 |
| 迁移窗口内超出承诺范围 | `migration:<consumer>` | `migrationDeadline` 未过且版本不满足 `committedRange` |

判定逻辑：

- 变化影响到的每个消费者，若无匹配的有效豁免 → **BLOCKING**，版本被阻止进入候选；
- 全部被豁免 → **WAIVED**，可进入候选，发布解释中会点名这些豁免；
- 无任何声明覆盖 → **UNCLAIMED**，不阻塞但留痕；
- 发布（publish）时会**重新评估一次**：声明变更、豁免过期都会在发布时点重新生效。

## 快速开始

### 本地运行

```bash
npm install
npm test          # 24 个端到端测试
npm start         # 监听 :8000，数据写入 ./data/registry.json
```

### 容器化部署

```bash
docker compose up --build
# 或
docker build -t contract-registry .
docker run -p 8000:8000 -v registry-data:/data contract-registry
```

配置项（环境变量）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 监听端口 |
| `DATA_FILE` | `./data/registry.json`（容器内 `/data/registry.json`） | JSON 持久化文件，原子写 |
| `MAX_EXEMPTION_DAYS` | `30` | 豁免最长有效期（天） |

## API 一览（前缀 `/api`）

### 服务与版本

```bash
POST   /services                                      # 注册服务 {name, ownerTeam}
GET    /services                                      # 服务列表
POST   /services/:name/versions                       # 提交版本 {version, schema, parentVersion?, submittedBy?}
GET    /services/:name/versions                       # 版本列表
GET    /services/:name/versions/:version              # 版本详情（含全部评审历史）
POST   /services/:name/versions/:version/reevaluate   # 重新评估（豁免/声明变化后）
POST   /services/:name/versions/:version/withdraw     # 撤回候选 {reason}
POST   /services/:name/versions/:version/publish      # 发布 {publishedBy} → 返回解释
GET    /services/:name/versions/:version/publish      # 查询发布解释
GET    /services/:name/lineage                        # 版本谱系（节点 + 父子边）
```

### 声明与豁免

```bash
POST   /services/:name/declarations                   # 登记声明
GET    /services/:name/declarations?status=ACTIVE     # 声明列表
POST   /services/:name/declarations/:id/retire        # 退休声明
POST   /services/:name/exemptions                     # 创建紧急豁免
GET    /services/:name/exemptions?active=true         # 豁免列表（active 实时计算）
POST   /services/:name/exemptions/:id/revoke          # 撤销豁免
```

## 端到端示例

```bash
# 1. 注册服务并提交首个版本
curl -X POST localhost:8000/api/services -H 'content-type: application/json' \
  -d '{"name":"payments","ownerTeam":"pay-team"}'

curl -X POST localhost:8000/api/services/payments/versions -H 'content-type: application/json' -d '{
  "version": "1.0.0",
  "schema": {
    "fields": {"tx.id": {"type": "string", "required": true}, "tx.legacy_ref": {"type": "string"}},
    "enums":  {"state": ["PENDING", "SETTLED"]},
    "errors": {"TX_NOT_FOUND": {"severity": "client", "meaning": "unknown transaction"}}
  }
}'

# 2. 消费者登记声明（依赖 tx.legacy_ref，并承诺迁移期内只接受 1.x）
curl -X POST localhost:8000/api/services/payments/declarations -H 'content-type: application/json' -d '{
  "consumer": "billing",
  "usedFields": ["tx.legacy_ref"],
  "committedRange": ">=1.0.0,<2.0.0",
  "migrationDeadline": "2026-10-01T00:00:00Z"
}'

# 3. 提供方提交删除该字段的新版本 → BLOCKED，评审报告指明阻塞的消费者
curl -X POST localhost:8000/api/services/payments/versions -H 'content-type: application/json' -d '{
  "version": "1.1.0",
  "schema": {
    "fields": {"tx.id": {"type": "string", "required": true}},
    "enums":  {"state": ["PENDING", "SETTLED"]},
    "errors": {"TX_NOT_FOUND": {"severity": "client", "meaning": "unknown transaction"}}
  }
}'

# 4. 紧急豁免：限定 billing、5 天后自动到期
curl -X POST localhost:8000/api/services/payments/exemptions -H 'content-type: application/json' -d '{
  "consumer": "billing",
  "violationKeys": ["field:tx.legacy_ref"],
  "reason": "billing migrates next sprint",
  "expiresAt": "2026-09-22T00:00:00Z"
}'

# 5. 重新评估 → CANDIDATE；发布 → 返回解释（billing 由豁免 #1 放行）
curl -X POST localhost:8000/api/services/payments/versions/1.1.0/reevaluate
curl -X POST localhost:8000/api/services/payments/versions/1.1.0/publish \
  -H 'content-type: application/json' -d '{"publishedBy":"release-bot"}'
```

发布解释示例（节选）：

```json
{
  "summary": "published payments@1.1.0",
  "declarations": [
    {"consumer": "billing", "outcome": "WAIVED_BY_EXEMPTION", "exemptionsUsed": [1]}
  ],
  "exemptionsApplied": [
    {"id": 1, "consumer": "billing", "violationKeys": ["field:tx.legacy_ref"], "expiresAt": "2026-09-22T00:00:00.000Z"}
  ]
}
```

## 设计要点

- **谱系与并发**：每个版本记录 `parentId`。并发提交同一父版本自然形成兄弟分支；未显式指定父版本时默认挂在最新已发布版本之下。`GET /lineage` 返回全部节点（含已撤回）与父子边。
- **撤回非破坏**：撤回仅写入状态标记与时间戳。后续版本持有自己的 schema 快照与不可变评审记录，父版本撤回不影响其存在与有效性；已发布版本不可撤回。
- **豁免安全**：创建时强制 `consumer`（限定调用方）、强制 `expiresAt` 且封顶 `MAX_EXEMPTION_DAYS`；评估时只采纳"未撤销且未过期"的豁免，过期自动失效；发布时点的复核确保过期豁免无法搭便车。
- **可解释性**：评审报告与发布解释持久化每条声明的判定（`SATISFIED` / `WAIVED_BY_EXEMPTION` / `BLOCKING`）、实际生效的豁免、迁移窗口检查与全部违规项，可随时回放查询。

## 项目结构

```
src/
  server.js    进程入口
  app.js       Express 应用与全部路由
  evaluate.js  兼容性评估引擎（声明 × 破坏性变化 × 豁免 × 迁移期限）
  diff.js      契约 schema 结构化 diff
  semver.js    语义化版本与范围匹配
  store.js     JSON 文件持久化（原子写；内存模式用于测试）
  config.js    环境变量配置
test/
  api.test.js  24 个端到端测试（node:test）
```
