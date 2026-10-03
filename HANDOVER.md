# 交接说明（shopee-ledger）

写给接手的人（或智能体）。**先读这一份，再看代码。**
最后更新：2026-10-03。

## 这是什么

Shopee 无货源跨境起步的本机执行系统。核心不是"记流水账"，而是三件事：

1. **参数有证据等级**（A/B/C/D/E）。A 级必须有可打开的 URL + 快照；E 级只能放
   `unverified_claim`，永远不能参与硬判定。
2. **不确定就报不确定**。缺数据返回 `INCOMPLETE` 并说清缺哪个字段，绝不把缺失当 0、
   绝不把"还没算出来"说成"结论已成立"。
3. **能被复核**。每条结论都要能从仓库里的文件重新推出来。

`spec/` 是制度性配置（真值），`spec/verified.json` 是随 git 走的核实成果，
`data/*.sqlite` 是派生物（已 gitignore）。**快照不是派生物**——它是原始证据，
所以 `data/snapshots/` 已入库。

## 怎么跑

```powershell
chcp 65001 > $null
cd D:\shopee-ledger
$env:PYTHONIOENCODING='utf-8'

python -m unittest discover -s tests -t .     # 557 个测试，约 45 秒
python spec\validate.py                        # 分层/引用/不变量校验
python -m shopee_ledger web --port 8765        # 网页 http://127.0.0.1:8765
powershell -File tools\daily-check.ps1         # 每日 9 步监测
```

零新依赖（只用标准库）。Python 3.12.10 在 `D:\python\python312\python.exe`。

## 当前状态（2026-10-03）

| 项 | 值 |
|---|---|
| 参数 | 58 个：**A=28 B=9 D=12 E=9** |
| 核实任务 | 完成 15 / 48 |
| 文档语料 | 212 篇（115 篇有正文），都在 `data/snapshots/` 且已入库 |
| 测试 | 557 全绿 |
| 每日任务 | 9 步全绿 |

## 数据来源质量（**说实话**）

按可信度从高到低：

### 可信

- **官方公开接口**（不需要登录）——原始返回都在 `spec/reference/` 且入库：
  - 定价模拟器：`/api/sls/site-config?date=`、`/api/site`（成功码 `200000`）
  - 多履约渠道利润计算器：`/api/site_and_channel`、`/api/fbs/get_config`
  - 发货时间计算器：`/api/get-exempt-days`、`/api/get-result`
  - 运费表 315 档、9 站费率、豁免日期表 40 条都在仓库里，可复算
- **shopee.cn/edu 文章**：227 个正文快照已入库，A 级参数可追溯到具体文章号
- **delivery 规则**：从接口样本归纳后做了 **630/630 样本外验证**，并用
  `delivery --verify` 固化成可复跑的回验（105 条官方样本）

### 需要留意的

- **D 级 12 个是经验值**，从未校准过。其中 `P-TW-MARGIN-TH`（≥15% 可做 / ≥10% 观察）
  **直接驱动"砍掉"判定**——阈值定错了整个选品方向就是错的。`calibrate` 命令现在
  报"数据不足 0/5 样本"，等真实订单才能校准。
- **B 级 9 个**：来源是官方，但官方自己说口径依品类/卖家类型而变
  （如佣金率）。属于"官方测算口径"，不是"你的店铺一定这样"。
- **一处未解决的对账差异**：官方算例（#25770 附录2，马来西亚）与本引擎
  逐项吻合，**只差平台基础设施费 0.54**。原因两种可能，我拿不出证据判定：
  要么官方算例没列这一项，要么官方把基础设施费显示在"服务费"里
  （`P-INFRA-FEE` 的 `display_note` 就是这么说的）。引擎现在会打 ⚠ 提示。
  **这是最该先查清的一处**，见 `spec/reference/order-settlement-structure.json`。
- **可达性记录不完整**：4 个参数（P-TAX-CN / P-TW-DELIVERY-DAYS / P-TW-DTS /
  P-TW-PAYOUT-CYCLE）的 URL 在 `spec/evidence/reachability.json` 里查不到，
  因为记录时用的 URL 形式不同。快照在，但报告会显示"无记录"。**这是工具的缺口**。
- **证书问题会被误读**：`chinatax.gov.cn` 依赖 urllib 读不到（证书链），但浏览器
  读得到。`check-sources` 已区分 cert_issue 与 unreachable，别当它不可达。

### 已知缺口（真拿不到）

- **4 个台湾站参数**（P-TW-EZWAY / FREQ-IMPORT / PRESALE-TAX-COLLECT /
  TAX-THRESHOLD）：来源 `help.shopee.tw` 实测打不开（两类错误都出过），
  **没有快照**。已按 INV-003 **从 A 降为 B**——不影响门禁行为（B 同样 hard_eligible），
  只是不再高估。缺口由核实任务 VT-014 等跟踪。
- **6 个登录门禁参数**：`P-ONB-ENTRY` / `P-ONB-FIRST-SITE` / `P-ONB-FLOW-REQ` /
  `P-TW-WH-ADDR-FMT` / `P-TW-HOME-DELIV-W` / `P-TW-DELIVERY-DAYS` 中尚未取到的部分。
  取证闭环已经做好（见下），只差人登录点一下。
- **2 个待实测**：`P-TW-AD-CPC`、`P-TW-CTR-BASELINE` 需要真实投放数据。
- **1 个未发生**：`P-MY-MARKET-BUDGET-DATE`（马来西亚 2027 预算案）。

## 登录门禁的取证闭环（已经能用，只差人点一下）

浏览器油猴脚本（v0.5.0，`tools/shopee-capture.user.js`，Tampermonkey 会自动更新）：

1. 登录 Shopee，打开目标页面
2. 面板下拉选参数（**58 个参数全在列表里**，没配方的排在前面并标「待人工读数」）
3. 点「抓这一页」

正文会留档并进入「待读数」队列（网页 `/checklist` 有区块，命令 `manual` 可列）。
**读数不需要用户在浏览器前**——正文已经在盘上，接着由系统读。

## 架构地图

| 文件 | 职责 |
|---|---|
| `shopee_ledger/cost_engine.py` | **唯一的利润算式**（`profit.py` 的旧引擎已删，有测试守着不许再写第二套） |
| `shopee_ledger/gates.py` | G1–G4 门禁，规则读自 `spec/rules/` |
| `shopee_ledger/expr.py` | 三值逻辑表达式引擎（缺字段 → UNKNOWN，不是 False 也不是异常） |
| `shopee_ledger/statemachine.py` | 履约状态机（状态/转移读自 `spec/modes/`） |
| `shopee_ledger/delivery.py` | 发货时效（DTS / 到仓扫描截止），规则经 630/630 验证 |
| `shopee_ledger/freight.py` | 官方运费表：按重量取档、变化检测 |
| `shopee_ledger/landed.py` | 多站点落地成本对比（解析解 `售价 = F/(1−k−R)`） |
| `shopee_ledger/store.py` | `Ledger`：候选品/订单/账单/审计的统一入口 |
| `shopee_ledger/snapshots.py` | 快照体检：文件在不在、**进没进版本库** |
| `shopee_ledger/unbound.py` | 静态检查：函数里有没有"赋值前读取" |
| `shopee_ledger/coverage.py` | 极简行覆盖率（`sys.monitoring`），工具用，不进测试套件 |

## 这个项目反复踩过的坑（建议保留这些防线）

1. **两层真值**：`spec/` 文件 + `spec/verified.json` 覆盖层。只用
   `Spec.load()` 会漏掉覆盖层里的东西（踩过很多次，包括测试自己）。
   要用 `Ledger().spec`。
2. **不要猜外部形状**：页面措辞、接口路径、日期格式、返回结构——一律先取真实样本。
   泰国税务那篇因为"我看的措辞"和"接口返回"不一致，错了三轮才收敛。
3. **关键词锚点在页面出现多次时会匹配到错的那处**（P-TH-VAT 差点被写成 17%）。
4. **函数内 import 会把该名字变成全程局部变量**（第 11 轮 `freight --check` 崩）。
5. **赋值藏在分支里、读取在分支之前**（第 12 轮 `overlap_note`）。
   第 4、5 条现在都有静态检查守着，见 `unbound.py` 与 `tests/test_cli.py`。
6. **"引用字符串在" ≠ "证据在"**：`snapshot_ref` 指向的文件可能没进 git。
7. **误报比漏报更糟**：我写 unbound 检查器第一版报了 50 条全误报（推导式），
   那种东西会让人直接把检查关掉。

## 建议的下一步（按价值排序）

1. **查清平台基础设施费那 0.54 的重叠**（见上）。这是唯一一处"我们的数字和官方
   对不上"，而且它牵动所有站点的费用合计。
2. **走一次登录门禁取证**（6 个参数，用户只需登录后点一下）。
3. **补可达性记录的 URL 归一化**（4 个参数显示"无记录"是工具问题，不是数据问题）。
4. **做行覆盖率体检**：`coverage.py` 已经能跑（在 557 个测试下约几分钟，覆盖率
   约 59%）。`api.py` / `browser.py` / `__main__.py` 是大片没被执行的地方，
   里面大概率还有类似第 11 轮那种"从没跑过所以没暴露"的问题。
5. **等真实数据**：`calibrate` 现在报 0/5 样本。有了订单，12 个 D 级经验值才能校准——
   在那之前，它们只是猜测。
