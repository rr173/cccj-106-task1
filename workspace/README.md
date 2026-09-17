# 接口契约登记与演进系统（Contract Registry）

面向服务团队的**接口契约登记、兼容性评审与版本演进**服务。提供方提交新版本时，
系统结合**仍在使用的消费者声明**逐项判断字段、枚举与错误语义变化是否兼容；
消费者用**迁移期限**承诺改造窗口；紧急情况下可发放**限定调用方、自动到期**的豁免；
并发提交形成清晰谱系；撤回候选不破坏已产生的评审；每次发布都保留可逐条解释的溯源证据。

- **零第三方依赖**：仅使用 Python 3.11 标准库（`http.server` + `sqlite3`），
  无需 pip install，镜像极小。
- 容器化：提供 `Dockerfile` 与 `docker-compose.yml`（含数据卷、健康检查、非 root 运行）。

---

## 一、快速开始

### 容器方式

```bash
docker compose up --build -d
curl http://localhost:8080/health
# {"status": "ok", "time": "..."}
```

数据持久化在命名卷 `registry-data`（容器内 `/data/registry.db`）。

### 本地方式

```bash
python3 -m app                      # 默认 0.0.0.0:8080，数据文件 data/registry.db
PORT=8090 CONTRACT_REGISTRY_DB=/tmp/r.db python3 -m app
```

### 跑测试与演示

```bash
python3 -m unittest discover -s tests -v       # 37 个用例（引擎单测 + HTTP 端到端）

# 演示完整业务链路（需要用可注入时间的模式启动服务）
ALLOW_TIME_OVERRIDE=1 python3 -m app           # 终端 A
python3 examples/demo_walkthrough.py           # 终端 B
```

> `X-Now` 请求头可注入“当前时间”，仅在 `ALLOW_TIME_OVERRIDE=1` 时生效，
> 供测试确定性地验证“期限到期、豁免过期”；生产配置默认关闭。

---

## 二、核心模型

| 模型 | 说明 |
| --- | --- |
| **Service** | 被治理的服务，唯一名称。 |
| **Version** | 契约版本，内容不可变；字段 `status` 为 `SUBMITTED / CANDIDATE / REJECTED / WITHDRAWN / PUBLISHED / SUPERSEDED`。每个版本有 `parent_id` 与服务内单调 `seq`，构成版本谱系，支持并发分叉。契约内容经规范化后计算 SHA-256，重复内容拒绝重复登记。 |
| **Review** | 一次评审结论（`admission` 入场评审 / `publish_recheck` 发布前即时复核）。证据中保存当时的基线、**全部活跃声明与豁免快照**、逐项影响分析。评审只追加、永不修改删除。 |
| **Declaration** | 消费者对服务的使用声明：承诺使用范围（字段 / 枚举成员 / 处理的错误码）+ 迁移期限 `deadline`。同一消费者重新登记会让旧声明失效（`superseded`），历史评审引用的快照不受影响。 |
| **Exemption** | 紧急豁免：必须指定受影响调用方白名单、可限定到具体变更、**必须有到期时间**（默认最长 14 天，`MAX_EXEMPTION_DAYS` 可调），可提前撤销。到期后自动失效。 |
| **PublishRecord** | 发布结果与溯源：发布前复核结论、逐项列出每个破坏性变更是被**谁的迁移承诺**或**哪个豁免**放行的，并给出人类可读的解释文本。 |

### 状态机

```
SUBMITTED ──admit(通过)──▶ CANDIDATE ──publish──▶ PUBLISHED ──新版本发布──▶ SUPERSEDED
    │                         │
    │ admit(不通过)           ├──withdraw──▶ WITHDRAWN（不级联，评审/子版本保留）
    ▼                         │
 REJECTED ◀──admit(重评)──────┘   （承诺到期/豁免过期后重评，候选会降级）
```

发布前会用**当前时间再复核一次**：即使候选早先通过，只要迁移期限已过、
豁免已到期或声明被缩窄，发布就会被拒绝（409）并把候选降级为 REJECTED。

---

## 三、兼容性规则

评估基线默认为**当前已发布版本**（无发布时退到父版本，再退为空契约）。
每个变更都会定位受影响的“仍在使用”的消费者，并判断是否被承诺或豁免覆盖。

### 字段（fields）

| 变更 | 判定 | 影响对象 |
| --- | --- | --- |
| 删除字段 | 破坏性 | 声明使用该字段的消费者 |
| 字段类型改变 | 破坏性 | 声明使用该字段的消费者 |
| 可选 → 必填（已存在字段） | 破坏性 | 声明使用该字段的消费者 |
| **新增必填字段** | 破坏性 | 所有仍在使用该服务的消费者 |
| 新增可选字段、必填 → 可选 | 兼容 | — |

### 枚举（enums）

| 变更 | 判定 | 影响对象 |
| --- | --- | --- |
| 删除枚举成员 | 破坏性 | 使用范围中包含该成员的消费者 |
| 删除整个枚举 | 破坏性 | 使用过该枚举成员或假定其封闭的消费者 |
| **封闭枚举新增成员** | 破坏性 | 假定枚举封闭（不接受未知值）的消费者 |
| 开放枚举新增成员 | 兼容 | — |

### 错误（errors）

| 变更 | 判定 | 影响对象 |
| --- | --- | --- |
| 删除错误码 | 破坏性 | 声明会处理该错误码的消费者 |
| **错误语义（semantics）改变** | 破坏性 | 声明会处理该错误码的消费者 |
| 新增错误码 | 兼容 | — |

### 放行路径（破坏性变更如何进入候选）

对每个受影响调用方依次判断：

1. **迁移承诺**：消费者声明了使用范围且 `deadline` 尚未到期 →
   记为「已承诺，期限内允许」；期限一过自动失效。
2. **紧急豁免**：存在同时满足以下全部条件的豁免 → 记为「豁免放行」：
   - `affected_consumers` 明确包含该调用方（不能全局绕过）；
   - 豁免未撤销且当前时间未超过 `expires_at`（自动到期）；
   - 若填写了 `restricted_changes`，变更 ID 必须在白名单内。
3. 两者都不满足 → `uncovered`，该版本被拒绝（REJECTED），不能进入候选、更不能发布。

没有受影响消费者的破坏性变更不会阻塞（例如在无人使用时删除字段）。

---

## 四、谱系、撤回与发布溯源

- **并发提交**：多个团队可基于同一父版本并发提交，系统分配服务内唯一连续 `seq`，
  `GET /services/{id}/lineage` 返回节点、边、根与按根聚合的分支，分叉一目了然。
- **撤回安全**：仅 CANDIDATE 可撤回（已发布不可撤回）。撤回只改候选自身状态，
  **不删除评审、不级联子版本**；已经基于它产生的评审和后继版本继续有效，
  后继版本可独立通过评审并发布。
- **发布溯源**：`GET /services/{id}/publish` 返回最近一次发布记录，
  `evidence.contributions` 逐项说明破坏性变更的放行来源（声明 ID + 期限 / 豁免编号 + 调用方），
  `evidence.explanation` 给出可直接贴进变更公告的中文说明。

---

## 五、API 速览

所有请求/响应均为 JSON。时间字段为 ISO 8601（UTC）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/services` | `{name}` 创建服务（已存在返回 409） |
| GET | `/services` | 列出服务 |
| POST | `/services/{sid}/versions` | 提交版本：`{contract, parent_id, submitter, note}`，首版 `parent_id=null`；提交即自动做一次入场评审 |
| GET | `/services/{sid}/versions?status=CANDIDATE` | 版本列表（可按状态过滤） |
| GET | `/versions/{vid}` | 版本详情（含规范化后的不可变契约与 hash） |
| POST | `/versions/{vid}/admit` | 用**当前**声明/豁免重新评审并进入候选（或降级 REJECTED） |
| POST | `/versions/{vid}/withdraw` | `{reason}` 撤回候选 |
| POST | `/versions/{vid}/publish` | `{publisher, note}` 发布（内部先做即时复核） |
| GET | `/versions/{vid}/reviews` | 该版本的全部评审与证据快照 |
| GET | `/services/{sid}/lineage` | 完整谱系（节点/边/根/分支/当前发布） |
| POST | `/services/{sid}/declarations` | 登记/更新消费者声明 |
| GET | `/services/{sid}/declarations?include_inactive=1` | 声明列表 |
| POST | `/services/{sid}/exemptions` | 创建紧急豁免 |
| GET | `/services/{sid}/exemptions?include_expired=1` | 豁免列表（自动标注到期失效） |
| POST | `/exemptions/{eid}/revoke` | 提前撤销豁免 |
| GET | `/services/{sid}/publish` | 最近一次发布记录与溯源证据 |

### 契约结构

```json
{
  "fields": [
    {"name": "order_id", "in": "response", "type": "string", "required": true}
  ],
  "enums": [
    {"name": "OrderStatus", "values": ["PENDING", "PAID"], "closed": true}
  ],
  "errors": [
    {"code": "NOT_FOUND", "semantics": "订单不存在"}
  ]
}
```

`fields[].in` 取值 `request` / `response` / `both`（默认 `both`）。

### 消费者声明

```json
{
  "consumer": "billing",
  "deadline": "2026-10-17T00:00:00Z",
  "scope": {
    "used_fields": ["response:order_id", "response:remark"],
    "enum_uses": {
      "OrderStatus": {"used_values": ["PENDING", "PAID"], "closed_assumed": true}
    },
    "handled_errors": ["NOT_FOUND"]
  }
}
```

### 紧急豁免

```json
{
  "code": "EXM-INC-20260917",
  "affected_consumers": ["billing"],
  "restricted_changes": [
    "field.removed#response:remark",
    "enum.value_added_closed#enum:OrderStatus"
  ],
  "reason": "P1 故障修复，billing 值班已确认",
  "created_by": "oncall-alice",
  "expires_at": "2026-09-19T00:00:00Z"
}
```

变更 ID 形如 `field.removed#response:remark`、`field.type_changed#response:order_id`、
`field.required_added#request:trace_id`、`enum.value_removed#enum:OrderStatus`、
`enum.value_added_closed#enum:OrderStatus`、`error.removed#error:NOT_FOUND`、
`error.semantics_changed#error:NOT_FOUND`，可从评审结果的 `findings[].change_id` 取得。
`restricted_changes` 省略或为 null 表示覆盖该调用方的全部破坏性变更。

### 错误响应

```json
{"error": {"code": "conflict", "message": "发布复核未通过：……", "details": {}}}
```

常见 `code`：`invalid_contract`(400)、`deadline_in_past`(400)、
`expiry_required`(400)、`expiry_too_long`(400)、`unknown_consumers`(400)、
`not_found`(404)、`conflict`(409)。

---

## 六、端到端示例

```bash
SID=$(curl -s localhost:8080/services -d '{"name":"checkout"}' | jq -r .id)

# 1) 发布 v1
VID=$(curl -s localhost:8080/services/$SID/versions -d '{
  "contract": {"fields":[{"name":"order_id","in":"response","type":"string","required":true}],
               "enums":[],"errors":[]},
  "parent_id": null, "submitter": "platform"}' | jq -r .version.id)
curl -s -XPOST localhost:8080/versions/$VID/admit > /dev/null
curl -s -XPOST localhost:8080/versions/$VID/publish -d '{"publisher":"bot"}' > /dev/null

# 2) billing 登记声明与迁移期限
curl -s -XPOST localhost:8080/services/$SID/declarations -d '{
  "consumer":"billing",
  "deadline":"2026-10-17T00:00:00Z",
  "scope":{"used_fields":["response:order_id"],"enum_uses":{},"handled_errors":[]}}'

# 3) 提交破坏性版本并评审（期限内 -> CANDIDATE）
# 4) 发布并取回溯源
curl -s localhost:8080/services/$SID/publish | jq .evidence.explanation
```

更多场景（阻塞、豁免限定、自动到期、并发分叉、撤回不破坏后继评审）见
`tests/test_api.py` 与 `examples/demo_walkthrough.py`。

---

## 七、配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8080` | 监听端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `CONTRACT_REGISTRY_DB` | `data/registry.db` | SQLite 路径（容器内为 `/data/registry.db`） |
| `MAX_EXEMPTION_DAYS` | `14` | 紧急豁免有效期上限（天） |
| `ALLOW_TIME_OVERRIDE` | `0` | 是否允许 `X-Now` 头注入时间（仅测试用） |
| `QUIET` | `0` | 置 `1` 关闭访问日志 |

## 八、设计说明与边界

- 存储为单文件 SQLite（WAL 模式），进程内写锁串行化写入；适合团队级治理规模。
  如需水平扩展，把 `app/db.py` 的连接层替换为 PostgreSQL 即可，SQL 均为通用语法。
- 时间一律 UTC 比较；“到期”在每次评审/发布时即时判定，不依赖后台任务，
  因此停机后重启不会让过期豁免复活。
- 评审与发布证据是**只追加的快照**，声明更新、豁免撤销/到期都不会改写历史，
  保证「最终发布结果能解释是哪些声明和豁免促成的」。
