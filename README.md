# 产季互助预付与实际损失份额结清

柑橘合作社产季互助池的办理服务：成员交材料/补正/查缺件，互助社审批应急预付、
按资金回执结清，出资代表核对共同额度的占用与释放，并完成采购补偿拆分、
终局损失结清、应付/待退/已退与池余额管理。

纯 Python 3.11 标准库实现（资料读取、联调、测试、HTTP 服务均无第三方依赖）。

```bash
# 原有：检查合成资料关联关系
python3 -m unittest discover -s tests -v

# 本服务自带的规则测试（47 个用例：1 个原有资料检查 + 46 个服务用例）
python3 -m unittest discover -s tests

# 种子口径自检 + 完整业务演示
python3 app.py check
python3 app.py demo
python3 app.py demo-paid

# 启动 JSON API + 网页控制台（默认 http://127.0.0.1:8000）
python3 app.py serve --port 8000
# 或 python3 -m mutual_aid serve --persist
```

`data/source_records.json` 是可供本地使用的合成记录，无真实机构数据和凭据。
用 `python3 -m json.tool data/source_records.json` 查阅。

---

## 业务口径（与《互助约定与往来凭证》一致）

- 金额一律为整数**分**；比例用基点（`emergency_bp=7000` 即 70%），比例额向下取整。
- 园地经营边界为 **`[from, to)`**：`to` 当日由后任经营（O3 的 2026-11-26 归 P03）。
- 业务份额键 **`(shipment, leg, party_ref)`** 指向实际承担费用的成员代码；
  果园代码不代替经营主体，花名可能对应多个经营者，歧义时必须用成员代码。
- 一个份额的暂定损失 = Σ`cost_cents` − Σ`resale_cents`（补送/处置收入对冲）。
- 应急预付请求 = min(暂定损失 × 70%, 每户季上限剩余)。
- 池不足且同批有多户合规请求时**比例分摊、地板取整**，余下每一分按
  “最早 `verified` 时间，相同则成员代码”的顺序补 1。种子 B4 正好得到
  **E1/P01=85928、E2/P02=64072**，合计 150000，余 1 分给 09:00 核实的 E1。
- 已核准待资金方回执的承诺继续占额；只有**明确撤销**或 **`returned` 回执**
  释放额度。`paid` 回执是现金出账，不改变“现金−待付承诺”口径下的可用额。
- 下一批不得挤占上一批未完承诺：种子载入后
  现金 64072、待付承诺 64072、**下一批真实可用 = 0**。
- 采购方事先承诺不算兑现；只有 `state=paid` 且可核对外部 ref/paid_on 的凭证
  才冲减损失。一证多项时，有 `part_cents` 且份额可核对的先入账；
  `part_cents` 缺失、份额无法核对或凭证总额超出明细分摊的部分进 **suspense
  待核对**，事后可明确分配，**绝不任选一户扣回**。Y501 入账 P01 100000、
  P02 82000，另 **17000 分待核对**（含 P03 无金额项）。
- 经营人变更只影响新支出：前任的待退始终挂在前任成员代码上，不会变成后任欠款。
- 别名、更正、同 `(成员, shipment, leg)` 或同幂等键的重复提交都更新同一项损失，
  不产生新损失。

### 两种“多余款”的不同后续处置

| 情形 | 状态 | 可执行处置 | 对额度/余额 |
| --- | --- | --- | --- |
| 已承诺、**尚未实际拨付**的多余部分 | commitment `pending` | `release_commitment_excess` / 结清自动调减 / `cancel_commitment` | 立即释放 reserved，可用额增加 |
| **已到账**的多付款 | 形成 `refund` 应收（待退） | `record_recovery` 登记成员还款（recovery 凭据） | 现金入池后可用额增加，状态转“已退” |

### 结清公式（`mutual_aid/core.py: settlement_plan`）

```
最终损失 E = max(0, cost − resale − 已确认采购补偿)
预付已付 A_paid，待付承诺 A_pending
待付承诺保留到 min(A_pending, max(0, E − A_paid))，其余作为未拨付多余释放
已付多余 = max(0, A_paid − E)                 -> 成员待退
预付不足 = max(0, E − A_paid − A_pending)     -> 池应付
若补偿款进了池(H)：先与待退对冲保留，剩余 H−对冲额 作为补偿转付应付
```

种子的典型结果（补偿默认采购方直接给成员，故不动池现金，直到成员还款）：

- **E1/P01**：173000 − 100000 = 最终损失 73000；已付 85928 → **待退 12928**，
  还款后池现金/可用各 +12928。
- **E2/P02（P814 仍 pending）**：129000 − 82000 = 47000；承诺 64072 →
  **调减/释放 17072**，只保留 47000 待拨付。
- **E2/P02（P814 已 paid）**：已到账多付 17072 → **待退 → recovery 已退**。
- **E3/P03**：补偿份额金额缺失不能认，最终损失仍为 104000；
  待 17000 suspense 明确分到 P03 后才变为 87000。

---

## Python API

```python
from mutual_aid import MutualAidService

svc = MutualAidService.from_data_file("data/source_records.json")

svc.available_balance()                 # 真实可用余额
svc.next_batch_view("B5")                # 上一批未完承诺 + 下一批可用
svc.preview_batch("B5")                  # 试算比例分摊（不写状态）
svc.approve_batch("B5")                  # 核准预付，生成 pending 承诺
svc.record_receipt("P814", "returned")   # 资金回执：paid / returned
svc.top_up_batch_shortfall("B1")         # 钱到位后按原批公平顺序补足

svc.submit_claim(member="P02", shipment="S1", leg=1, round="B6")
svc.submit_material(claim_id, "cost_voucher", ref="INV-1", status="accepted")
svc.deficiencies(claim_id)               # 这次临时支出还缺什么
svc.member_deficiencies("P02")
svc.verify_claim(claim_id)

svc.register_compensation(voucher_dict, proceeds_to="member" | "pool")
svc.attribute_suspense("S1", [{"shipment": "...", "leg": 2,
                               "party_ref": "P03", "part_cents": 17000}])
svc.settle_claim("E1")                   # 结清：调减承诺 + 应付/待退
svc.record_recovery("D1", cents=12928, ref="R9001")
svc.pay_receivable("D2", ref="PAY1")

svc.quota_audit("P810")                  # 出资代表：占用/释放依据与流水
svc.member_view("P01")                   # 成员本人权益 + 已脱敏调整依据
svc.as_member("P01").view()              # 门户对象，强制本人范围
svc.as_funder().quota_audit("P814")

svc.save(".runtime/state.json")          # 可选持久化
MutualAidService.load(".runtime/state.json")
```

根目录另有 `service.py`（`from service import Service, MutualAidService`）、
`app.py` / `main.py`（直接启动 HTTP 服务）作为兼容入口。

## HTTP / 网页服务

`python3 app.py serve --port 8000` 后打开 `http://127.0.0.1:8000/` 即得操作控制台。
身份用请求头 `X-Actor-Role: member|coop|funder` 与 `X-Actor-Code: P01`
（也可用 query `?role=member&as=P01`）。成员只能读/办本人单据，跨户返回 403；
出资代表可查 `/api/quota/statement` 与 `/api/quota/audit?ref=P814`。

主要接口（均返回 JSON，POST 为 JSON 请求体）：

```
GET  /api/health
GET  /api/available                         GET  /api/next-batch?round=B5
GET  /api/quota/statement                   GET  /api/quota/audit?ref=P814
GET  /api/members/P01/view                  GET  /api/members/P01/deficiencies
POST /api/claims                            GET  /api/claims/E1/deficiencies
POST /api/claims/E1/materials               POST /api/claims/E1/correct
POST /api/claims/E1/verify                  POST /api/claims/E1/settle
POST /api/batches/B5/preview                POST /api/batches/B5/approve
POST /api/batches/B5/top-up
POST /api/transfers/receipt                 POST /api/recoveries
POST /api/commitments/C2/cancel             POST /api/commitments/C2/release-excess
POST /api/compensation                      POST /api/suspense/S1/attribute
POST /api/receivables/D2/pay                POST /api/call   {"method": "...", "args": {}}
```

`/api/call` 是方法级调度入口（白名单见 `mutual_aid/server.py: RPC_ALLOWED`），
便于联调与脚本直接调用全部业务动作。加 `--persist` 后每次写操作原子落盘到
`.runtime/state.json`，重启不丢；`--reset` 可从种子重建。

## 代码结构

```
mutual_aid/core.py     纯规则：经营边界、比例分摊、份额损失、凭证拆分、结清公式
mutual_aid/service.py  有状态服务：申请/材料/幂等、批次、承诺与额度流水、清算、角色视角
mutual_aid/server.py   stdlib JSON HTTP + 鉴权 + /api/call
mutual_aid/cli.py      check / demo / demo-paid / serve
mutual_aid/static/     单页控制台（原生 JS，无构建）
tests/                 原有资料检查 + 46 个服务规则/HTTP/持久化用例
```
