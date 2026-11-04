# 设计：Ondo 实盘做市机器人（第三版）

日期：2026-11-04。第二版合并了两份审核的阻塞问题。第三版合并确认轮意见：Codex 的 3 条阻塞（熔断锁顺序、续期封锁、标记价格自我引用）和偏移冲突，以及 Claude 的 5 条重要（独立慢锚、加仓侧限量、reduceOnly 错误处理、软上限数量、单边挂单）。

**首版原则**：单市场、小金额、宁可不挂单也不冒险。周五没有通过量化验收就延期，不按日历放行。

## API 事实（审核 agent 核实文档，必须在沙盒和小资金实盘中验证）

- **环境**：正式 `https://api.ondoperps.xyz`，**沙盒** `https://api.ondoperps-sandbox.xyz`（接口相同，演示资金）。WS 地址分别对应。
- **REST 签名**：`ONDO-SIGN = hex(HMAC-SHA256(secret, ts_ms + METHOD + path?query + body))`。时间戳只允许落后 10 秒、超前 1 秒。
- **限流**：公测期**每个 API key 默认 1 次/秒**，提额要发邮件申请。标准默认值是下单、撤单各 30 次/秒，其他 10 次/秒。WS 限 25 条/秒。**设计按 1 次/秒计算。**
- **下单**：`POST /v1/perps/orders`，`postOnly` 会撮合时返回 `post_only_has_match`；`clientOrderId` 不超过 64 个字符，重复时返回 `clientOrderID_collision`。
- **撤单**：
  - 按市场全部撤单：`DELETE /v1/perps/orders?market=`，不占撤单配额。
  - 按 ID 撤单：`DELETE /v1/perps/orders/{id}`，也支持 `client:{clientOrderId}`。
  - 批量撤单：`DELETE /v1/perps/orders/batch?orderIDs=`。
- **私有 WS 登录**：`{"op":"login","args":{"key","time","sign"}}`，其中 `sign = HMAC(secret, time + "ondo_perps_ws_login")`，成功返回 `loggedIn`。连接数有上限。
  - 频道：`ordersPerps`、`fillsPerps`、`positionsPerps`、`balancePerps`。
  - 订单和成交频道只推事件，没有初始快照，初始状态用 REST 拉取。
- **死人开关** `cancelAllOrdersAfterPerps`（`timeout_seconds`），参照 Spot 文档的推断，需实测：
  - 客户端发出的消息（包括 ping）会续期。
  - 计时器按账户计，新连接上开启会覆盖旧的；断线后计时器仍有效。
  - `timeout_seconds=0` 关闭。
  - 被撤的单 `cancelReason=cancelAfterTimeout`。
- **保证金**：全仓。每个市场占用 = max(买侧, 卖侧) + 浮亏，挂单也计入。10x 市场初始保证金率 10%、维持 5%。最小下单量为一个 `baseIncrement`。
- **周末机制**：标记价格被限制在上周五收盘价 ±1/最大杠杆以内；内部预言机跟随盘口的 impact 买卖价，**我方挂单会影响它**。标记价格可以从公开频道 `markPricesPerps` 获取，名称实现时核实。

## 查询接口核实（主 agent 于 2026-11-04 核对文档，M1 之后补充）

| 用途 | 接口 | 要点 |
|---|---|---|
| 鉴权请求头 | `ONDO-KEY-ID`、`ONDO-TIMESTAMP`（毫秒）、`ONDO-SIGN` | M1 误写成 `ONDO-KEY`、`ONDO-TS`，M2 修正 |
| 单个订单 | `GET /v1/perps/orders/{orderID}`，支持 `client:{cid}` | 查不到时返回 **400 `order_not_found`**，不是 404 |
| 挂单列表 | `GET /v1/perps/orders?market=&status=open&limit=&cursor=` | 按时间倒序，用 `pageInfo.nextCursor` 翻页，必须翻完 |
| 持仓 | `GET /v1/perps/positions` | 字段 `direction`（long/short/neutral）、`netQuantity`、`averageEntryPrice`、`markPrice`、`unrealizedPnl`、`usedMargin`、`liquidationPrice`。**没有时间戳** |
| 余额与保证金 | `GET /v1/perps/balance` | `marginBalance`、`usedMargin`、`availableMargin`、`marginRatio`（量纲需沙盒核实）、`underLiquidation`、`totalTradingFees`、`totalFundingPayments` |
| 成交 | `GET /v1/perps/fills?market=&startTime=&endTime=&cursor=` | `id`、`orderId`、`clientOrderId`、`price`、`size`、`side`、`fee`、`pnl`、`time`、`isMaker` |
| 杠杆 | `GET /v1/perps/leverage?market=` | `result[].leverage` |
| 服务器时间 | **不存在**（`GET /status` 不带时间） | 改为两项检查：服务器 NTP 同步状态（`timedatectl` 显示已同步），以及 REST 响应头 Date 与本地时间相差不超过 2 秒；运行中出现 `timestamp_too_far` 就熔断 |

- **持仓水位的修正**：持仓快照没有交易所时间，所以对账按以下步骤进行：
  1. 进入 SYNCING，停止新单。
  2. 等到 `quiet_s`（默认 3 秒）内没有新成交。
  3. 拉取持仓快照，与本地按成交推算的持仓对比。
  4. 一致就恢复；不一致就以快照为准，再等一个 quiet_s 复核一次，两次一致后才恢复，否则保持 SYNCING 并告警。
- **保证金率**：优先使用交易所返回的 `marginRatio`；交易所的值不可用时，才用本地估算并告警。

## 首版范围

- **单市场**（默认 IBM），每侧默认 $400（可配置，代码上限 $800）。持仓上限默认 $1200，代码上限也是 $1200；账户总敞口默认 $1200，代码上限 $1500。
- **砍掉**：币安守卫（改为可选的只读参考）、多窗口（只保留一个窗口）、回测重构（实盘逻辑独立实现，回测不变）、估算奖励份额（只记录原始数据）、第二个市场。
- **不能砍**：订单状态机与幂等、撤单确认、死人开关实测与存活绑定、下单前的敞口检查、错误码分类、带优先级的限速队列、扣除我方挂单、熔断锁、强制告警、外部心跳。

## 进程结构

单进程 asyncio，单实例。同一账户只能运行一个实例：用 `flock` 锁住 `data/maker/{account}.lock`，拿不到锁就拒绝启动。

- **任务**：PublicFeed（盘口、成交、标记价格）、PrivateFeed（订单、成交、持仓、余额）、Strategy、OrderManager、Reconciler、Watchdog、Alerter。
- **状态**：
  - `AccountState` 是唯一状态源。
  - 成交按 fill id 去重，订单按 orderId 和 clientOrderId 去重。
  - 每个订单记录最后更新的交易所时间；REST 快照不能覆盖更新的 WS 事件。
  - **持仓水位**：REST 持仓快照带有交易所时间。之后的 WS 成交在快照基础上增量计算；早于快照的成交不再计入持仓，只用于盈亏与去重。快照持仓与本地推算不一致时，以快照为准，并进入 SYNCING。
  - **成交补账**：私有连接中断后，用 REST 成交查询补齐断线期间的成交，按 fill id 去重。
- **同步屏障**：启动、私有 WS 重连、REST 与 WS 出现矛盾、出现 UNKNOWN 订单时，进入 `SYNCING`：
  1. 禁止新挂单。
  2. 用 REST 拉取订单、持仓、余额。
  3. 用 clientOrderId 查清在途订单。
  4. 一致后回到 `RUNNING`。

## 订单状态机（必须）

`INTENT → SENT → (ACKED/OPEN | REJECTED | UNKNOWN) → PARTIAL → (FILLED | CANCEL_SENT → CANCELED)`

- **固定 clientOrderId**：每个意图有一个固定 id（`{run}-{mkt}-{side}-{seq}`），id 永不复用。写请求超时、网络错误、5xx 时都进入 UNKNOWN：
  1. 用 `client:{id}` 查询，最多 `unknown_query_attempts` 次（默认 3），间隔 3 秒。
  2. 仍然查不到时，**保持 UNKNOWN**，该市场进入 SYNCING（两侧都不发新单），执行确认消歧：
     - 先按市场全部撤单（`DELETE /v1/perps/orders?market=`），能撤掉任何延迟受理的旧单。
     - 再用 REST 列出该市场的挂单，**确认为 0**，并且 `client:{id}` 仍然查不到。
     - 两者都满足后，才把这张单标记为"已确认不存在"，并告警。
  3. **确认之前，同一市场不发新单**；重试必须沿用同一个 id。
  4. 之后如果延迟受理的订单出现在私有推送或对账里，按"遗留挂单"立即撤销并告警。
  5. 从进入 UNKNOWN 起超过 60 秒仍无法确认，就熔断。
- **先撤后挂**：撤单必须确认进入终态（WS 事件或 REST 查询）后，才能挂替换单。撤单超过 `cancel_confirm_timeout_s` 仍未确认时，升级为按市场全部撤单；还不行就熔断。
- **停止流程**（守卫、熔断、退出、窗口结束共用）：
  1. 禁止新增订单。
  2. 等在途请求有结果，或超时后查询。
  3. 按市场全部撤单。
  4. 用 REST 核实挂单数为 0。
  5. 记录日志。

## 下单前的风险检查（每次发单前同步执行）

- **最坏敞口**：
  - 买侧 = 持仓 + 所有未终结的买单（OPEN、PARTIAL、SENT、UNKNOWN、CANCEL_SENT）剩余量 + 新单。
  - 卖侧同理。
  - 两侧名义值都必须不超过 `max_position_usd`，账户合计不超过 `max_account_exposure_usd`。
- **保证金**：按交易所规则估算新单后的保证金率，必须不超过 `max_margin_ratio`（默认 0.3），并且余额足够。
- **代码硬上限**：`quote_notional ≤ 800`、`max_position_usd ≤ 1200`、`max_account_exposure_usd ≤ 1500`。**没有任何绕过开关**，超限时拒绝启动。

## 报价

- **参考中心价** `ref_mid`：
  - 先从公开盘口里按价位扣除我方挂单的剩余量（以私有订单状态为准），得到"第三方盘口"，取它的买一卖一中点。
  - 扣除不确定时（我方订单处于 SENT、UNKNOWN 或 CANCEL_SENT，或盘口比订单状态更旧），本节拍不更新报价、不通过判据。
- **独立慢锚** `anchor`：最近一次第三方盘口两侧都非空时的 `ref_mid`，记录时间。
  - **不使用标记价格作为回退。** 周末标记价格取预言机、EMA、盘口三者的中位数，我方是唯一挂单时它会跟着我方报价走。标记价格只作为辅助守卫。
  - 第三方盘口某一侧为空时，中心价冻结为 `anchor`，**不叠加库存偏移**。
  - `anchor` 的年龄超过 `anchor_max_age_s`（默认 600）时，两侧一起撤单并停报，直到第三方盘口恢复。
  - `|ref_mid − anchor_ema| > max_anchor_drift_bps`（默认 15）时触发守卫，其中 `anchor_ema` 是 anchor 的慢速 EMA。
  - 币安报价新鲜时，额外检查 `|ref_mid − 币安价 × 滚动基差| > max_ext_drift_bps`。这只作为守卫，不参与报价。
- **判据的完整性**：第三方盘口的末档超出 band，或者该侧在 band 内为空但盘口还没到 100 档，都视为该侧"完整"。判据只用第三方盘口计算，我方挂单永远不计入竞争量。
- **标记价格**：两侧到标记价格的距离也要在 `[min_offset_bps, band_bps − band_margin_bps]` 之内；偏离过大就进入守卫，作为辅助。
- **价格**：`offset_bps`（默认 **12**）加库存偏移 `skew = clamp(pos/soft_cap, −1, 1) × skew_bps`（默认 4）。两侧距离落在 [8, 16] bps，满足 min_offset 8 和上限 22。多头时买价更远、卖价更近。取整到 tick 后**再检查一次** band、post-only（不能跨越第三方对手一档）和金额。
- **不挂单边**：除了软上限时主动缩单，任何一侧不合规时，**两侧一起撤单**，避免只有风险没有奖励。
- **加仓侧限量**：加仓那一侧的数量 = min(报价额, `hard_cap_usd` − 该侧最坏敞口 − 一个最小单位)，确保硬上限只在异常时才会触发。
- **重报**：目标价与当前挂单差超过 `requote_bps`（默认 3）且距上次重报超过 `min_requote_interval_s`（默认 10）时才重报，以适配 1 次/秒的限流。
- **post-only 被拒**：退避 2 秒、4 秒、8 秒；连续 5 次被拒就进入守卫冷却。
- **软上限**：持仓 ≥ `soft_cap_usd`（默认 $300）时，加仓那一侧缩到报价额的 **50%**，挂在 band 边缘（22bps），保留双边资格。官方如果按两侧较小量计分，最小单就拿不到奖励；周五在奖励页面核实后再调整。减仓那一侧挂被动减仓单：如果沙盒实测 reduceOnly 配 GTC 可用就用它；否则用普通 post-only 单，数量不超过持仓。
- **硬上限**：持仓 ≥ `hard_cap_usd`（默认 $600）时：
  1. 停报，撤掉加仓那一侧的单。
  2. 用 reduceOnly IOC 减到 soft_cap 以下，价格限定在中心价（ref_mid；一侧为空时用慢锚）外 `max_flatten_slippage_bps`（默认 60）以内。
  3. 最多重试 3 次、总时限 60 秒，每次按实际剩余持仓核验。
  4. 失败就熔断，并持续发紧急告警。

## 报价中心修正（M13，依据 2026-11-04 服务器 dry-run 实测）

- **实测问题**：IBM 在非美股时段第三方价差常为 30～120bps（中位数 37～100bps），这正是竞争低的时段。原来的 max_spread_bps=20 加上标记价格辅助守卫，让机器人在这些时段停报：dry-run 中竞争量只有 $596，有资格挂单的时长却为 0%。
- **规则**：
  - 第三方两侧都有单，并且价差 ≤ narrow_spread_bps（20）时，center = 第三方中间价；它和标记价的偏差超过 max_mid_mark_bps（8）就触发守卫。
  - 否则 center = 新鲜的标记价格（必须明确不处于价格发现阶段），我方成为最优买卖价。
  - max_spread_bps 改为只防极端情况，取 300。
- **安全约束不变**：
  - 慢锚 EMA、漂移守卫、jump 守卫对实际使用的 center 生效。
  - 币安 IBMUSDT 作为独立外部守卫，不参与定价。
  - post-only 按第三方对手价检查。
  - 库存偏移只调整两侧距离，不移动 center。
- **残留风险**：周末标记价格的三项中位数里含有盘口一项，我方报价会让 mark 向我方报价轻微靠拢。由于报价对称，再加上漂移守卫的上限，这个影响有界；对应的测试要覆盖 1 小时的漂移上界。
- **max_mark_age_s 改为 10**：实测约 2 秒推送一次，偶尔超过 5 秒。

## 限速队列

- 全局令牌桶，默认 `1/s`，可配置。
- 优先级：按市场全部撤单 > reduceOnly 减仓 > 单笔撤单 > 挂单 > 对账查询。
- 遇到 429 时遵守 `Retry-After`，期间只允许撤单类请求。

## 错误码分类

| 类别 | 错误码 | 处理 |
|---|---|---|
| 重算 | `post_only_has_match` | 退避后重算 |
| 守卫 | `order_price_outside_safe_bounds`、`mark_price_not_available`、`insufficient_liquidity`、5xx | 进入守卫冷却 |
| 立即熔断 | 401（各类 auth）、`ip_not_permitted`、`key_doesnt_have_scope`、`account_in_liquidation`、`insufficient_margin`、`net_position_too_large`、`reduce_only_flipping_position`、`reduce_only_increasing_position`、`clientOrderID_collision` 但查不到对应订单 | 熔断并告警 |
| 校时后重试 | `timestamp_too_far` | 校时后重试 1 次；仍失败就熔断 |
| 对账后继续 | `reduce_only_no_open_position`、撤单时订单已终结（`order_already_*`、404） | 正常竞态，对账后继续 |
| reduceOnly 参数 | `reduce_only_invalid_tif` | 切换到回退方式（post-only，数量不超过持仓），并告警一次 |
| 未知 | `must_use_delayed_path`、`trading_disabled` 及其他 | 先进入守卫；实测含义后再归类 |

## 守卫与熔断

- **守卫**：撤销该市场全部挂单，冷却 `cooldown_s`；之后连续 `stable_s` 没有触发才恢复。触发条件：
  - `ref_mid` 在 `jump_window_s` 内变动超过 `jump_bps`
  - ref_mid 与标记价格偏离过大
  - 盘口超过 `max_book_age_s` 没有更新
  - 价差超过 `max_spread_bps`
  - 私有或公开 WS 断线
  - 对账矛盾
  - 进入 SYNCING 状态
- **默认值**：周四用已采集的数据，扣除我方挂单后的盘口推送间隔、周末波动、撤单尾部延迟来标定，写进 `maker.example.toml`，并注明依据。标定之前不能 live。
- **全局熔断**：
  - 触发条件：
    - **本轮窗口累计**亏损超过 `max_window_loss_usd`（已实现加浮动，含手续费和资金费；持久化保存，重启后不清零、跨午夜不清零）
    - 保证金率超过 `max_margin_ratio`
    - 硬上限减仓失败
    - 立即熔断类错误
    - 熔断锁写入失败（这时拒绝交易）
  - 熔断后：
    - **先写锁文件并 fsync**，确认成功后再执行停止流程，然后持续告警。写锁失败时，仍然执行停止流程并告警，最后以退出码 76 退出；只要锁的状态不确定，就不允许任何新单。
    - 必须用 `maker --reset-halt` 人工复位。
    - 熔断后如果仍有持仓，继续监控，并每 15 分钟告警一次，不自动平仓。只有亏损超过 `emergency_flatten_loss_usd` 时，才执行带滑点上限的 IOC 减仓。

## 死人开关（必须实测后才能 live）

- 在私有连接上订阅 `cancelAllOrdersAfterPerps`，`timeout_seconds` 默认 30。**收到 ACK 之后才允许发第一笔订单。**
- **续期封锁**：所有对外发送（私有 WS、公开 WS、REST）都经过同一个闸门。Watchdog 判定不健康时：
  - 闸门关闭，所有发送停止，也不重连任何连接。
  - **唯一例外**是停止流程本身：按市场全部撤单，以及核实挂单为 0 的 REST 请求，各最多重试 3 次。撤单本来就是目标，即使这些请求会续期计时器，也不影响安全。
  - 停止流程完成或重试用尽后，闸门完全关闭，等待死人开关兜底，并紧急告警。
- **实测项**：沙盒中分别测试 REST 请求、公开连接消息、私有连接 ping 是否会续期。如果某类消息续期而闸门没有覆盖，就拒绝 live。
- **续期与存活绑定**：
  - Watchdog 每 10 秒检查关键任务（Strategy、OrderManager、PrivateFeed、Reconciler）的最近心跳。
  - 每个任务有自己的过期阈值：Strategy 和 OrderManager 默认 15 秒，Reconciler 为它的周期加 30 秒。PrivateFeed 的存活看它最近一次 ping 往返（pong），不看业务消息的多少，因为冷清时可能很久没有业务消息。全部都在阈值内，才发 ping 续期。
  - 任何关键任务卡住，就停止续期，让交易所撤单，同时本地触发停止流程。
- **重连**：重连后重新登录、重新开启死人开关，收到 ACK 前处于 SYNCING。
- **退出**：先执行停止流程，确认挂单为 0 后，再把 `timeout_seconds` 设为 0 关闭。
- **实测项**：
  - `kill -9` 后在 timeout 内被撤单
  - 断网后被撤单
  - ping 是否续期
  - 重连后计时器的行为

## 判据与窗口

- **判据**：每 `gate_interval_s`（默认 300）用扣除我方挂单后的盘口计算 ±band 加权竞争量。
  - 扣除不确定或 band 覆盖不完整时，判为"不通过"。
  - 阈值 `gate_comp_max_usd`：周四按实盘规则（offset 8～22bps、库存偏移）重跑回测得到 C* 后确定。C* 取 offset 5bps 与 10bps 两者中较小的，再乘 0.2。暂定 IBM 约 $3,500，以重跑结果为准。
- **窗口**：默认北京时间周六 09:00 至周一 05:00。
  - **窗口最后 60 分钟**只挂被动 reduceOnly 单，把持仓降到 0。
  - 窗口结束时执行停止流程；还有持仓就紧急告警，并继续监控。

## 告警与监控（live 必须）

- **Telegram 必须配置**：live 模式启动时发一条自检消息，失败就拒绝启动。
- **告警内容**：
  - 熔断、守卫（合并后最多 5 分钟一条）、断线、对账矛盾、判据状态变化
  - 有资格挂单的时长占比：连续 30 分钟低于 50% 时告警
  - 最近一次成功报价超过 10 分钟
  - 每小时摘要：持仓、盈亏、有资格时长、判据、错误数
- **外部心跳**：配置 `healthcheck_url`（例如 healthchecks.io），每分钟 GET 一次；只有在"关键任务健康，且最近 10 分钟内有成功报价或明确处于等待状态"时才发。进程或服务器挂掉时，由外部服务通知用户。
- **systemd**：`Restart=on-failure`，配 `StartLimitIntervalSec=600` 和 `StartLimitBurst=3`；熔断退出码 76 不重启。

## 运行模式

- **dry-run**（默认）：连接公开频道。如果提供了密钥，也只读连接私有频道，并且不开死人开关、不执行任何写请求。只记录拟下单。
- **sandbox**：连接沙盒地址，执行全部写操作。
- **live**：必须同时满足 `--live` 参数、配置中 `mode = "live"`、Telegram 自检成功、熔断锁不存在、校时检查通过（NTP 已同步，且与响应头 Date 相差不超过 2 秒）。

## 密钥与安全

- 从环境变量或权限为 600 的 `secret_file` 读取。日志自动遮蔽密钥和签名。
- **接口白名单**：只允许下单、撤单、查询订单、查询持仓、查询余额、查询杠杆、查询市场、查询服务器时间。
- 要求 API 密钥只开交易权限、开 IP 白名单、不开提现权限。

## 测试与验收

- **单元测试与模拟交易所**（不联网、不绑定端口）：
  - 状态机全部转移，包括 UNKNOWN 消歧
  - 撤单确认前不补单
  - 敞口检查等于上限时的边界
  - 部分成交时撤单
  - 重复事件和乱序的 REST 快照
  - 限速优先级与 429
  - 错误码分类
  - 扣除我方挂单的各种情形
  - 自我引用不发散：盘口里只有我方挂单时，中心价不随偏移漂移
  - 硬上限 IOC 零成交和部分成交
  - 熔断锁持久化与写入失败
  - Watchdog 停止续期
  - 窗口末尾减仓
  - 告警失败时拒绝 live
  - 单实例锁
- **沙盒验收**（周五上午）：实测清单全部完成，每一项都要有时限和可观察的结果，记录到 `docs/verification.md`。包括：
  - reduceOnly 配 GTC 是否允许
  - 连接数上限
  - 1 次/秒的限流是否也限制按市场全部撤单
  - REST 请求和公开连接的消息是否会续期死人开关
  - 目标额度（$400 和 $800）下实际占用的保证金
  - 5xx 或超时后用 clientOrderId 查询的行为
  - 死人开关：`kill -9`、断网
  - 429 的返回格式和实际额度
  - 撤单延迟分布
  - 撤单期间的成交
  - 重复 clientOrderId
  - 私有消息格式
  - 盘口推送频率
  - 保证金占用
  - 校时
- **小资金实盘**（周五下午）：每侧 $50，至少 2 小时。必须满足：0 次未处理的 UNKNOWN、0 条遗留挂单、死人开关在正式环境被 `kill -9` 验证一次、奖励页面显示我方有资格。**任何一项不满足就延期。**
