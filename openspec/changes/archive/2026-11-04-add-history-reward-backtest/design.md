# 设计：历史奖励回测 + 服务器实时采集

日期：2026-11-04。第三版，已合并 Claude 两轮审核、Codex 一轮审核，以及"服务器上必须接 WS"的要求。只读公开数据，不涉及交易。

## 已实测的数据源

| 数据 | 接口与事实 |
|---|---|
| Ondo 逐笔（REST） | `GET api.ondoperps.xyz/v1/perps/trades?market=&limit=1000&cursor=`，无需认证。最新在前，游标为 base32 JSON。回到 10-31 01:00Z 约 31～42 页。时间为纳秒精度，扫单时同一时间戳有多笔 |
| Ondo 合约 | `/v1/perps/contracts`：87 个市场，约 71～76 个可交易，maker 0.0001、taker 0.00025，含 `fundingRate`、`indexPrice` |
| Ondo 盘口（REST） | `/v1/perps/depth?market=&depth=100`，最多 100 档（200 或 0 返回 400），只有当前快照 |
| Ondo WS | `wss://api.ondoperps.xyz/ws`。`depthBooksPerps` 和 `tradesPerps` **无法按市场过滤，会推送全部市场**。盘口是全量快照（每侧最多 100 档），每市场约 256ms 一条，单条约 4KB，未压缩合计约 1MB/s。客户端发 `{"op":"ping"}`，空闲 180 秒断开，25 次/秒 |
| 币安历史逐笔 | `data.binance.vision/data/futures/um/daily/aggTrades/{SYM}/{SYM}-aggTrades-{DATE}.zip` 及 `.CHECKSUM`，INTC、CRCL、DRAM 在 10-31～11-02 都有。REST 只能查最近 2 天 |
| 币安 WS | `wss://fstream.binance.com/stream?streams=intcusdt@aggTrade/...`，单个连接最长 24 小时 |
| Lighter | REST `orderBookOrders` 可用。WS `wss://mainnet.zklighter.elliot.ai/stream` 先推快照再推增量，带 offset/nonce。历史 K 线 403 |

另有实测：
- 周末 INTC 的 Ondo 价格相对币安有约 −21bps 基差。
- 工作日 ±25bps 内的竞争量：INTC 约 $0.9M～4M，CRCL 约 $0.14M～0.43M。

## 范围拆分

**P0**（11-05 晚前，回测结论）：
- 三个标的（INTC、DRAM、CRCL）必做。
- 另加初步扫描中竞争最低、币安有对应合约的 WEN、NKE、RDDT、IBM、DKNG（主 agent 已于 11-04 备份原始数据，见 `data/raw/`）。
  - 实测：非美股时段 ±25bps 竞争量只有约 $0.5k～$18k，而 INTC 约 $4.1M。
- 中心价只用 `binance_basis` 一种。
- 守卫只用币安逐笔。
- 重报只做 `cancel_first`。
- 同价成交用固定比例，跑敏感性网格。
- 对冲：`none` 和 `lighter_proxy`。
- 期末估值：两种口径。
- 竞争量：情景值，或从现有扫描 JSONL 导入的实测值。

**P1**（11-06 晚前，服务器采集）：`collect`、`import-collected`（WS 格式）、systemd 部署。

**延期**：K 线抓取与 K 线守卫、按排队份额计算同价成交、`place_first`、`ondo_trades` 与 `index` 中心价、Lighter WS 本地盘口维护。补抓 10-24 那个周末只在时间允许时做。

## 模块

- `bt_config.py`：`BacktestSettings` 和 `CollectSettings`，均为冻结 dataclass。读 TOML，未知键拒绝，用 Decimal 校验。
- `history.py`：SQLite 存储，每个写入批次一个事务；接口调用通过注入的 HTTP 客户端进行，复用 `market.HttpClient`。各表：
  - `fetches`：请求参数、结果、真实覆盖范围。
  - `ondo_trades`：主键 `id`。
    - 时间顺序的唯一稳定键是 `(time_ns, id)`。同一纳秒内的真实先后不可知，用 id 作稳定次序，与抓取顺序、重抓、WS 导入都无关。
    - 重复导入是幂等的。
  - `binance_trades`：主键 (symbol, agg_id)。
  - `contracts`：快照。
  - `depth_observations`：唯一键 (market, source, received_ms)，按 (market, received_ms) 建索引。字段：
    - mid、买一卖一、末档距离
    - `book_band_complete`：末档是否超出 band
    - flat 和 linear 两种权重下买卖两侧的加权竞争量
    - 距中间价第 k 个 tick 的档位深度（k 取 0～N）
  - `collector_gaps`：来源、市场、频道、开始、结束、原因。
- `binance.py`：日 zip 下载（二进制 GET，上限 200MiB）。SHA256 必须与 CHECKSUM 一致，否则拒绝。
- **币安映射校验**：同名符号不一定是同一资产。实测 BB 在币安是 BounceBit，Ondo 上是股票。
  - 回测开始前，计算窗口内 Ondo 与币安价格偏离的中位数，超过 `max_mapping_dev_bps`（默认 500）时拒绝该标的，并给出中文错误。
  - 映射可以在配置中手动覆盖。
- `scan_import.py`：幂等导入现有临时扫描的 JSONL。这一格式写入 spec：每行一个 JSON，包含 `kind`（`contracts`、`depth` 或 `error`）、`market`、`received_ms`，以及 `payload` 或 `error`。
- `reward.py`：纯函数：权重、单分钟份额、奖励额、恒定竞争量下的盈亏平衡 C*、所需奖励池。
- `backtest.py`：确定性事件回放，每个标的独立；时钟和数据都由调用方注入。
- `bt_report.py`：分时段汇总、工作日与周末分开、命名时段、markout、多标的 Markdown、竞争排名、上线判据。
- `collector.py`（P1）：asyncio 服务。

## 回测事件模型

**可见性**：
- 交易所事件时间加上 `data_latency_ms` 才算"可见"，决策只能用当时已可见的数据。
- 订单发出后过 `place_latency_ms` 才生效；撤单请求后过 `cancel_latency_ms` 才生效。

**同一时刻的处理顺序**：撤单生效 → 订单生效 → Ondo 成交判定 → 对冲到期 → 守卫 → 奖励快照 → 新决策。

**因果性测试**：在窗口之后追加更多数据，此前所有事件和结果都不得改变。

1. **报价中心**（B4 修订）：`quote_center = 币安最新可见逐笔价 × (1 + basis)`。
   - **分侧基差**：每笔可见的 Ondo 成交与当时可见的币安价配对，币安价年龄不超过 `basis_pair_max_age_s`。按主动方向分为买、卖两组，每组保留最近 `basis_samples` 个、年龄不超过 `max_basis_age_s` 的样本。`basis = (买组中位数 + 卖组中位数) / 2`，两组都非空才有效，不要求买卖同时发生。这样可以消除宽盘口造成的双峰。
   - 演变：
     - 第一版：要求 300 秒窗口内有成交，冷门市场几乎不报价。
     - B3：用买卖同时出现时的中点，冷门市场在线率仍只有 3%～12%，且只在活跃时段挂单，带来选择偏差。
     - B4：改为分侧基差。
   - **参考价回退** `ref_fallback`：
     - `ondo_mid`（默认）：币安参考价超过 `max_ref_age_s` 时，center 改用 Ondo 自身的买卖两侧成交价中位数的中点（年龄不超过 `max_ondo_mid_age_s`）。回退期间守卫与 pickoff 不可用，报告单列回退时长和这段时间的 edge。
     - `none`：无参考价时不报价。
     - 首表并列两种口径。
   - 冷门市场在周末大量时段币安无成交（NKE 44 小时中有 40.6 小时连续超过 2 分钟无成交）；实盘会按 Ondo 盘口报价，所以用 ondo_mid 回退更接近实盘。
2. **奖励中间价**：`reward_mid` 与报价中心分开。
   - 历史模式：`reward_mid = quote_center`，并用 `reward_mid_error_bps` 情景计算我方到中间价的距离 `d + error`，所以历史回测的奖励只是条件性结果。
   - 有采集盘口时：`reward_mid` 用真实盘口中间价。
3. **Post-only 检查**：用最近可见的主动买成交价估算卖一、主动卖成交价估算买一。买价 ≥ 卖一估计（卖侧对称）时，该侧不挂单，并计入拒单次数。这个估计可能过时，报告中注明。
4. **报价与重报**：
   - 价格：`bid = floor_tick(center×(1−offset))`，`ask = ceil_tick(center×(1+offset))`。数量按 `quote_notional`。`price_tick` 和 `quantity_step` 按标的配置。
   - 重报：`|center − 当前报价中心| > requote_bps` 时请求撤单，撤单生效后才发新单（`cancel_first`）。
5. **成交判定**：对每笔 Ondo 成交 (p, q, side)，以主动卖为例：
   - `p < bid`：成交 `min(q 的剩余可分配量, 订单剩余)`。
   - `p == bid`：按 `at_price_fill_ratio` 成交。
   - **单笔成交量守恒**：每笔成交有一个可分配预算，我方所有订单共用，不能重复消费。
   - 按我方限价记账。
   - 敏感性：`at_price_fill_ratios = [0, 0.5, 1]`，报告中逐项列出。
6. **库存上限**：持仓名义值，加上同侧挂单全部成交后的潜在名义值，超过 `max_inventory_usd` 时缩量或不挂单。
7. **对冲**：
   - `none`：不对冲。
   - `lighter_proxy`：成交后过 `hedge_latency_ms` 执行，执行价为 `币安价 × (1 + lighter_basis_bps)`，再按不利方向加 `lighter_half_spread_bps + hedge_slippage_bps`，计 taker 费。待对冲量先做净额抵销。基差漂移造成的盈亏单列。
8. **守卫**：用可见的币安逐笔，在 `guard_window_s` 内最大变动超过 `guard_move_bps` 时，请求撤销全部报价。连续 `guard_cooldown_s` 无新触发后恢复。报告给出触发次数和暂停时长。
9. **费用与资金费**：Ondo 按 maker 费；资金费按每小时 bps 参数计提（P1 可改用采集到的 `fundingRate`）。
10. **期末**：
    - 未发出的对冲直接取消，不假设已完成。
    - 两个平台的实际持仓分别按各自价格结算：
      - `mark`：Ondo 按 center，Lighter 按代理价。
      - `liquidate`：两边都加 taker 费和半个价差。

## 奖励模型（参数化，未经官方确认）

- **快照**：每个 UTC 整分钟。已生效、撤单尚未生效的报价，若 `d ≤ band_bps` 即计入。
- **权重**：`flat` 时 w=1；`linear` 时 w = 1−d/band。
- **资格**：`reward_two_sided` 要求双边；`min_quote_usd` 为最小挂单额。
- **单分钟奖励** = `pool ÷ reward_markets ÷ 10080 × my ÷ (my + comp)`。
- **comp 来源**：
  - `scenarios`：情景网格。
  - `observed`：该市场在同一北京时间小时的中位数，工作日与周末分开统计。这是外生情景，报告中注明采集时段。
  - 当 `book_band_complete=false` 时，comp 只是下界、奖励只是上界，排名中标记为"不确定"。
- **C\***：在恒定竞争量 c 下，求 `奖励(c) = 不含奖励的亏损`，`mark` 和 `liquidate` 两种口径各求一次。边界情况分别输出：
  - `no_rewards_needed`：不靠奖励已盈利
  - `unprofitable_even_at_zero_comp`：竞争量为 0 仍亏
  - `never_eligible`：全窗口没有合格挂单
- **所需奖励池**：在给定竞争量下，达到盈亏平衡所需的每市场周奖励池。
- **上线判据**（周六 09:00 使用）：实时 comp < 该标的周末 C*，且 `book_band_complete=true`。

## 盈亏拆解与决策口径（B2 修订）

- **markout**：基准是成交后 h 秒的 `quote_center`，已做基差调整；原始币安价只作为辅助列。
- **拆解**：
  - 价差收益：Σ 方向 ×（成交时 center − 成交价）× 数量
  - 逆向选择：Σ 方向 ×（center[t+300s] − 成交时 center）× 数量
  - 库存漂移：账面损益 − 价差收益 − 逆向选择 + 费用
- **edge 损益**：价差收益 + 逆向选择 − 费用 − 对冲成本。它剔除了长周期的库存方向运气，是主决策口径；同时也给出按实际账面和强平口径计算的 C*。
- **对冲**：报告同时跑 `none` 和 `lighter_proxy` 两种对冲模式，并列展示。
- **竞争量情景**：默认 [1000, 3000, 10000, 30000, 100000, 300000, 1000000, 4000000]。另外给出该标的"工作日非美股时段（北京时间 05:00–21:30）实测竞争量中位数"作为周末的参考代理，并明确标注。
- **在线率拆解**：按"无参考价、无基差、守卫、post-only 拒单、撤单或重报空档、库存上限"统计各自占的时长。
- **守卫**：区分原始触发次数和暂停段数。
- **报告体积**：事件明细默认不输出，用 `--events` 开启。

## 审核后补充（B3）

- **pickoff**：在可见的币安价格事件上，若 center 越过我方挂单价超过 `pickoff_edge_bps`，就视为被套利者以我方限价吃掉剩余全部数量。用来弥补"历史上没有更紧的挂单，因此也没有套利成交"造成的乐观偏差。
- **markout 延后估计**：缺样时取目标时刻之后第一个有效 center，最多延后 `markout_max_delay_s`，并标注笔数。
- **奖励中间价误差情景**：`reward_mid_error_scenarios = [0, 10, 20]`。
- **强平半价差**：按标的实测价差中位数的一半；没有观测时用配置值。
- **同一纳秒**：先判定成交，再让订单生效。
- **盘口中间价**：`max_depth_age_s` 过期后不再使用。

## 竞争排名

数据来自 depth_observations。对每个市场、每个北京时间小时，分工作日和周末，计算以下指标的中位数和 P90：
- 加权竞争量
- 价差
- 一档深度
- 完整度

按估算的每小时奖励排序，同时标出样本数。

## 实时采集服务（P1）

- **单个 Ondo WS 连接**：订阅全市场 `depthBooksPerps` 和 `tradesPerps`。写盘前先降采样：
  - 焦点市场：每 `focus_snapshot_s`（默认 1 秒）保存一份全量盘口。
  - 其他市场：每分钟只保存派生的竞争指标和 tick 深度。
  - 成交：全部保存。
- **REST**：全市场扫描只在 WS 断档时作为后备。合约快照每 5 分钟一次。
- **币安**：焦点标的的 `@aggTrade` 流，地址为 `wss://fstream.binance.com/market/stream?streams=`（实测：旧的 `/stream` 和 `/ws` 能连上但不推送数据），地址可配置。23 小时时主动重连。健康日志会对长时间零消息的来源给出警告。
- **Lighter**：P1 只用 REST 轮询焦点标的（`lighter_poll_s`），WS 延期。
- **并发**：
  - 同步的 HTTP 调用经有界线程池执行（`asyncio.to_thread`），不阻塞 WS。
  - 写盘由独立队列负责。
  - HTTP、WS、时钟和等待器都可以注入，便于测试。
- **断档**（C2 修订，依据真实烟测）：
  - 断档只在连接级或频道级产生，覆盖以下情况：断线、整个连接或整个频道超过 `silence_timeout_s` 没有消息、进程启动时上一次异常退出。
  - 单个市场超过 `market_stale_s` 没有更新，只记为 `market_stale`，不算断档，因为冷门市场的成交本来就稀疏。第一版按市场判定，10 分钟产生了 652 条误判。
  - 断档在收到第一条有效消息时才结束。
  - 回测在断档期间不新挂单；如果断档期间有持仓或挂单，标记"损益不可判定"，不把这段时间当作已成功对冲。
- **存储**：
  - 文件路径为 `data_dir/{source}/{YYYYMMDDTHH}Z-{run_id}.jsonl.gz`，按小时轮转。
  - 导入时容忍文件尾部被截断的半行。
  - 磁盘用量达到 `max_disk_gb` 时，以专用退出码 75 退出；systemd 单元配置 `RestartPreventExitStatus=75`，避免反复重启。
- **日志**：写到 `log/collect_{YYYYMMDD}.log`，每分钟一行健康统计。
- **部署**：`deploy/pendle-collect.service`（非 root 运行，`Restart=always`）；README 写明部署步骤；WS 依赖作为可选依赖 `live` 安装。

## 已知偏差（报告必须披露）

- 中心价是近似值，基差会漂移。
- 穿价判定忽略了我方订单对价格路径的影响。
- 同价成交比例是假设。
- 竞争量来自本周实测，不是上周末的真实值。
- Lighter 对冲是代理价格。
- 奖励规则未经官方确认。
- 没有模拟保证金：名义盈利不能证明 $1000 本金可以执行。报告会给出各平台的最大名义值与本金之比。
- 一个周末只是一个样本。

## 测试

- 只用 unittest，新增测试不绑定端口；HTTP、WS、时钟都通过注入的假对象模拟。
- 覆盖以下场景：
  - 翻页与覆盖记录
  - 排序稳定键、重复导入幂等、后补成交
  - CHECKSUM 不符
  - 可见性与订单生效延迟
  - 追加未来数据不改变此前结果
  - post-only、穿价成交、同价比例、单笔成交量守恒、撤单延迟
  - 库存上限、对冲延迟与净额抵销、基差、期末取消待对冲
  - 守卫与冷却
  - flat 和 linear 权重、双边要求、band 外权重为 0、reward_mid 误差、band 覆盖不完整
  - C* 的各种边界情况、所需奖励池
  - 北京时间分桶，以及工作日与周末拆分
  - 扫描 JSONL 导入、截断尾部
  - collector 的重连、静默、断档、轮转、磁盘退出码、优雅退出
  - CLI 离线端到端
- 真实网络抓取和 WS 烟测作为外部验收单独运行，不进入单元测试。
