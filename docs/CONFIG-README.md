# 智能卖家后台 · 架构、模块与配置

本目录是「设计文档 + 模块手册 + 机器可读配置 + 校验器 + 行动清单」五件套。

> **无货源不是后台，只是履约模式之一。市场是会变的维度，不是写死的架构。**

| 文件 | 给谁 | 用途 |
|---|---|---|
| [智能卖家后台_总体架构_v1.0.html](智能卖家后台_总体架构_v1.0.html) | 人 | **平台级设计**：三层结构、正交维度、九个模块、模式边界、迁移映射 |
| [Shopee无货源跨境起步手册_v3.0.html](Shopee无货源跨境起步手册_v3.0.html) | 人 | **模块手册**：履约 · 无货源（DROPSHIP）的实现说明 |
| [下一步行动清单.md](下一步行动清单.md) | 人 | **你要动手的事**：6 件阻塞第一单的任务、取证步骤、回填模板 |
| [资料更新_2026-10-03.md](资料更新_2026-10-03.md) | 人 | 本轮检索结果、证据等级、对配置的影响 |
| `spec/*` | 系统 | 分层声明式配置 |
| `spec/validate.py` | 工具 | 跨文件分层校验（引用完整性 + 14 条不变量） |

---

## 1. 一条原则

**不变的能力上提为平台级，会变的模式下沉为插件。**

```
横向能力（平台级，任何模式复用）
  ParamRegistry  EvidenceCenter  GateService  CostEngine  AuditLog  KpiCalibrator
        ▲
正交维度（可组合，不是模块）
  平台 Shopee|Lazada|TikTok|Temu  ×  市场 TW|MY|TH|PH|VN|BR|MX
  ×  履约模式 DROPSHIP|STOCK|PLATFORM_MANAGED  ×  店铺
        ▲
业务模块（九个）
  M1 主体与店铺   M2 选品与商品   M3 供应链与采购   M4 履约   M5 定价与利润
  M6 流量与增长   M7 资金与结算   M8 风控与合规     M9 复盘与决策
```

---

## 2. 文件结构

```
spec/
├── registry.json                 项目定位、平台/市场/模式/模块注册表、枚举
├── entities.json                 24 个实体与字段字典（layer 标注归属；4 张只增不改）
├── governance.json               14 条不变量、告警、事件、KPI、校准、复核日历
├── params/
│   ├── platform.shopee.json      平台级：费率、免佣、入驻、平台规则 + M6 流量（16）
│   ├── market.tw.json            台湾市场：关税、EZ WAY、物流、时效、结算（16）
│   ├── market.th.json            泰国市场：低值进口税 5 项（5，政府来源）
│   ├── market.my.json            马来西亚：LVG 10% 销售税（3）
│   └── assumptions.json          平台无关假设与治理项（11）
├── rules/
│   ├── platform.json             横向规则：治理/选品/供应商/取证/成本/上架/风控/资金/复盘/流量（29）
│   └── modes/dropship.json       无货源专属规则（14）
├── modes/
│   ├── dropship.json             模式定义 + 专属参数 + 订单状态机（已实现）
│   ├── stock.json                骨架（待实现）
│   └── platform_managed.json     骨架（待实现）
├── evidence/
│   ├── verification-tasks.json   48 条证据采集任务（6 条阻塞第一单）
│   └── snapshots/                来源快照（SNAP-<市场/平台>-<序号>-<主题>）
├── validate.py                   分层校验器
└── manual-core.json              ⚠ 已废弃路标，请删除
```

合计：**52 参数 / 45 规则 / 48 任务 / 24 实体 / 14 不变量 / 9 模块 / 3 模式 / 5 市场**。

---

## 3. 后台怎么接

1. **按 `registry.project.load_order` 顺序加载**，每层用自己的 `layer` 字段识别。
2. **参数按四维 scope 寻址**：`{platform, market, mode}`，`"*"` 表示通用。
3. **门禁走统一出口**：`GateService.check()` 返回
   `{gate_id, rule_id, result, evidence_level, message, next_action, param_ids[]}`。
   `result ∈ {PASS, INCOMPLETE, WARN, REJECT}`，**禁止只返回布尔值**。
4. **模式只实现 `owns` 里的能力**（`INV-013` / 硬门禁 `R-GOV-004`）。
5. **市场可行性由参数驱动**，是 advisory，不得自动停售（`R-MKT-001`）。
6. **三张表只增不改**：`CostSnapshot`、`SupplierQuote`、`AuditLog`。

---

## 4. 证据等级 = 门禁资格

| 等级 | 含义 | 能否硬拦 | 系统的正确行为 |
|---|---|---|---|
| A | 官方硬规则 | 可以（未过期） | 硬拦；必须有来源 + 查询日期 + **快照** |
| B | 卖家后台实测 | 可以 | 硬拦，绑定平台/市场 |
| C | 自己实测 | 可以 | 单条记录级硬拦 |
| D | 经验启发 | **不可以** | 只能 WARN / AUTO_FILL |
| E | 待验证 | **不可以** | 只能生成任务或 advisory；未核实数值放 `unverified_claim` |

**已知缺口**：A–E 没有"权威二手"这一档。泰国参数目前是政府转述（`source_tier: government_secondary`），只能靠字段与备注表达。建议下一轮正式引入 `source_tier` 维度。

---

## 5. 市场维度：无货源不是只限台湾

| 市场 | 低值免税 | 无货源可行性 | 依据 |
|---|---|---|---|
| **TW** | 门槛仍在（2000 TWD） | `viable` | SLS 时效 4-8 天、距离最近。**2026 全站大免运（店取 199 / 宅配 490）是最大变量** |
| **TH** | 已取消（1 铢起征） | `degraded` | 关税+增值税合计不低于 17%；但**税由平台下单时代收**，冲击在买家端价格而非卖家毛利 |
| **MY** | LVG 10% 且强化执行 | `degraded` | 50 令吉商品被征 5 令吉，叠加佣金物流后利润可能归零；2026-10-09 预算案是关键节点 |
| **BR / MX** | 20% 关税体系 / 收紧 | `not_viable` | 长时效 + 高税负 |

---

## 6. 验收不变量（写进 CI）

`governance.json#invariants` 共 14 条，最关键的七条：

- `INV-001` 硬门禁依赖的参数必须是 A/B/C。
- `INV-002` E 级参数 `value` 必须为 `null`。
- `INV-003` A 级参数必须有来源 + 查询日期 + 快照（**无快照视为无证据**）。
- `INV-006` 数值字段必须区分 `null` 与 `0`。
- `INV-011` 读取 `unverified_claim` 的规则必须 `advisory_only`、非硬。
- `INV-012` / `INV-013` 参数 scope 必须与文件层级一致；履约模式不得越权。
- `INV-014` 参数的 `task_ref` 与任务的 `target_param_id` / `also_targets` 必须双向一致。

---

## 7. 运行校验器

**PowerShell 里带引号的 exe 路径必须以 `&` 开头**，否则报「意外的标记 -c」。

```
& "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" "C:\Users\Administrator\Documents\deepseek-harness\default-workspace\spec\validate.py"
```

工程在 `D:\shopee-ledger` 的话，把整个 `spec/` 复制过去再跑。预期：

```
counts : params=52 rules=45 tasks=48 entities=24 invariants=14 modules=9 modes=3 markets=5
cross-verified (5): P-TW-COMMISSION, P-TW-FREE-ORDERS, P-TW-FREESHIP-FEE, P-TW-PRESALE-FEE, P-TW-TXN-FEE
```

会有若干 WARN（已知待办，不是错误）：9 条 A 级缺快照、若干硬规则已声明降级、`manual-core.json` 建议删除。出现 **ERROR** 请把整段贴回。

---

## 8. 已知限制

1. **本会话无法执行命令**：`pwsh` 每次启动都以 `0xC0000142` 退出。`spec/` 下的 JSON 经**人工核对 + 一次独立校验**（语法、ID 唯一性、引用完整性、分层一致性全部通过），但**未经解析器验证**——请先跑第 7 节。
2. **A 级依据仍无官方快照**：`shopee.cn/edu` 为 JS 空壳，`help.shopee.tw` 不可达。费率与免佣规则已用独立媒体交叉验证（逐字一致），但按 `INV-003`，**交叉验证不能替代快照**。
3. **泰国/马来西亚结论是 E 级**：泰国为政府二手来源，马来西亚为行业媒体；官方站点在本环境不可达，需你本地取证。
4. **Shopee 官方广告/搜索营销 PDF 抓不到**（内容类型不支持）：这是 M6 的一手材料，需你下载。
5. **M6 已有骨架但未完成**：6 个参数 + 4 条规则，仍缺广告与关键词的完整模型。
6. **`stock` / `platform_managed` 只有骨架**。

---

## 9. 下一步

1. 跑校验器，把 `spec/` 全部跑成 `PASS`。
2. 按 [下一步行动清单.md](下一步行动清单.md) 走：先补 9 个 A 级快照，再推 6 件阻塞第一单的事。
3. 把 Shopee 官方广告 PDF、泰国/马来官方页面下载后发我，我升级参数等级。
4. 决定 M6 的数据模型边界（曝光/点击/加购/转化/ACoS），它决定这个后台是「防亏工具」还是「赚钱工具」。

---

*架构 v1.0 · 2026-10-03 · 取代 spec/manual-core.json (v3.0)*
