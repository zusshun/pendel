## ADDED Requirements

### Requirement: 安全的实盘接入
系统 SHALL 仅调用白名单内的 Ondo Perps 下单、撤单与查询接口，使用 HMAC-SHA256 签名；密钥只从环境变量或权限为 600 的文件读取，永不写入日志；不包含提现、转账或密钥管理调用；同一账户同时只能运行一个实例。

#### Scenario: 密钥文件权限过宽
- **WHEN** secret_file 的权限不是 600
- **THEN** 拒绝启动并给出中文错误

#### Scenario: 非白名单接口
- **WHEN** 代码尝试请求不在白名单中的路径
- **THEN** 抛出错误，不发出请求

#### Scenario: 第二个实例
- **WHEN** 同一账户已有实例持有锁
- **THEN** 新实例拒绝启动

### Requirement: 运行模式与硬上限
系统 SHALL 默认以 dry-run 运行，不发出任何写请求；sandbox 模式只连接沙盒；live 模式必须同时满足 --live 参数、配置中 mode=live、告警自检成功、不存在熔断锁、校时检查通过（服务器 NTP 已同步且与交易所响应头 Date 相差不超过 2 秒）。单侧报价不超过 $800、持仓不超过 $1200、账户敞口不超过 $1500 是代码内置上限，没有绕过开关。

#### Scenario: 配置超过上限
- **WHEN** 配置的 quote_notional、max_position_usd 或 max_account_exposure_usd 超过代码上限
- **THEN** 拒绝启动

#### Scenario: live 自检失败
- **WHEN** Telegram 自检失败或校时偏差过大
- **THEN** 拒绝以 live 模式启动

### Requirement: 订单状态机与幂等
系统 SHALL 为每个订单意图固定一个 clientOrderId；写请求结果未知时标记为 UNKNOWN，用 client:{id} 查清之前，同一侧不发新单，重试沿用同一个 id；撤单确认进入终态之前，不挂替换单。

#### Scenario: 下单超时
- **WHEN** 下单请求超时或返回 5xx，交易所实际可能已受理
- **THEN** 用 clientOrderId 查询结果；查清之前不发新单，不会出现两张单

#### Scenario: 查询不到
- **WHEN** 多次用 clientOrderId 查询都找不到订单
- **THEN** 保持 UNKNOWN，该市场两侧都不发新单；按市场全部撤单，并用 REST 确认挂单为 0 后，才标记为已确认不存在并告警；该 id 永不复用；超过 60 秒仍无法确认就熔断

#### Scenario: 延迟受理的旧单
- **WHEN** 已确认不存在的订单之后出现在私有推送或对账中
- **THEN** 立即撤销并告警

#### Scenario: 撤单期间成交
- **WHEN** 撤单请求已发出，确认之前订单部分成交
- **THEN** 按实际成交更新持仓，确认撤单进入终态后，才按新持仓计算替换单

### Requirement: 状态一致性与同步屏障
系统 SHALL 按 fill id 和 order id 去重；REST 快照不得覆盖更新的 WS 事件；启动、私有连接重连、出现矛盾或 UNKNOWN 时进入 SYNCING，禁止新挂单，对账一致后才恢复。

#### Scenario: 重复成交事件
- **WHEN** 同一成交被 WS 推送两次
- **THEN** 只计账一次

#### Scenario: 私有连接重连
- **WHEN** 私有 WS 断开后重连
- **THEN** 重新登录、重新开启死人开关，在 SYNCING 状态下完成订单、持仓、余额对账后才继续报价

### Requirement: 下单前的敞口与保证金检查
系统 SHALL 在每次发单前同步检查：持仓加上全部未终结的同侧订单（含在途、UNKNOWN、待撤）再加新单的名义值，不超过 max_position_usd；账户合计不超过 max_account_exposure_usd；新单后的估算保证金率不超过 max_margin_ratio。

#### Scenario: 刚好等于上限
- **WHEN** 新单后最坏敞口恰好等于上限
- **THEN** 允许；超过一个最小单位就拒绝

### Requirement: 扣除我方挂单后的报价
系统 SHALL 从公开盘口中按价位扣除我方挂单的剩余量，得到第三方盘口，用其买一卖一中点作为参考中心价；扣除不确定时不更新报价、不通过判据；不得以标记价格作为中心价的回退；两侧距离在 [min_offset_bps, band_bps − band_margin_bps] 之内，取整后再次检查；参考中心价偏离独立慢锚或外部参考过大时触发守卫。

#### Scenario: 盘口里只有我方挂单
- **WHEN** 扣除我方挂单后第三方盘口一侧为空
- **THEN** 中心价冻结为独立慢锚，不叠加库存偏移；慢锚超过 anchor_max_age_s 时两侧一起撤单并停报

#### Scenario: 一侧不合规
- **WHEN** 除软上限主动缩单外，任一侧报价不合规
- **THEN** 两侧一起撤单，不保留单边挂单

#### Scenario: 加仓侧限量
- **WHEN** 加仓一侧整笔成交会让持仓超过 hard_cap_usd
- **THEN** 该侧数量缩到不超过 hard_cap_usd 减去最坏敞口再减一个最小单位

#### Scenario: 库存偏移
- **WHEN** 持有多头
- **THEN** 买价离中心更远、卖价更近，且两侧仍在区间内

#### Scenario: post-only 被拒
- **WHEN** 交易所返回 post_only_has_match
- **THEN** 退避后重算；连续 5 次被拒就进入守卫冷却，不改用 taker 单

### Requirement: 库存上限与减仓
系统 SHALL 在持仓达到软上限时，把加仓一侧缩到报价额的 50% 并挂在 band 边缘以保留双边，同时在减仓一侧挂被动减仓单（沙盒确认 reduceOnly 配 GTC 可用时用 reduceOnly，否则用数量不超过持仓的普通 post-only 单）；达到硬上限时停报，用带滑点上限的 reduceOnly IOC 减仓，最多重试 3 次、总时限 60 秒，每次按实际剩余持仓核验；减仓失败时熔断。

#### Scenario: IOC 零成交或部分成交
- **WHEN** reduceOnly IOC 没有成交或只成交一部分
- **THEN** 按剩余持仓重试；预算用尽仍超过上限就熔断并持续告警

### Requirement: 带优先级的限速与错误分类
系统 SHALL 用令牌桶限速（默认 1 次/秒），优先级为：按市场全部撤单 > 减仓 > 单笔撤单 > 挂单 > 查询；遇到 429 时遵守 Retry-After；错误码按重算、守卫、立即熔断、校时后重试、未知归入守卫这几类处理。

#### Scenario: 429 期间需要撤单
- **WHEN** 处于 Retry-After 等待期，守卫要求撤单
- **THEN** 撤单类请求优先，挂单请求暂停

#### Scenario: 正常竞态
- **WHEN** 返回 reduce_only_no_open_position，或撤单时订单已经终结
- **THEN** 对账后继续，不熔断、不进入守卫

#### Scenario: 鉴权失败
- **WHEN** 返回 401、ip_not_permitted 或 key_doesnt_have_scope
- **THEN** 立即熔断并告警

### Requirement: 守卫、熔断与死人开关
系统 SHALL 在价格跳变、偏离标记价格、盘口过期、价差过大、断线、对账矛盾时执行停止流程（禁止新增 → 处理在途 → 按市场全部撤单 → 核实挂单为 0）并冷却；在本轮窗口累计亏损超限（含费用与资金费，持久化保存）、保证金率超限、减仓失败、立即熔断类错误时全局熔断，写入经 fsync 的锁文件，必须人工复位。死人开关收到 ACK 后才允许下单；只有关键任务全部健康时才续期。

#### Scenario: 策略任务卡住
- **WHEN** 任一关键任务的心跳超过 task_stale_s
- **THEN** 停止续期死人开关，并在本地执行停止流程

#### Scenario: 进程被强杀
- **WHEN** 进程收到 kill -9
- **THEN** 交易所在 timeout_seconds 内撤销所有挂单

#### Scenario: 熔断锁写入失败
- **WHEN** 无法持久化熔断锁
- **THEN** 仍然执行停止流程并告警，以退出码 76 退出，不允许任何新单

#### Scenario: 熔断的顺序
- **WHEN** 触发全局熔断
- **THEN** 先写锁文件并 fsync，再执行停止流程；中途崩溃后重启时锁仍然存在

#### Scenario: 不健康时封锁续期
- **WHEN** Watchdog 判定任一关键任务不健康
- **THEN** 私有 WS、公开 WS、REST 的所有发送都停止，也不重连；只有停止流程中的撤单和核实请求例外（各最多重试 3 次），之后完全封锁，等待死人开关兜底

#### Scenario: 重启后亏损累计
- **WHEN** 窗口内进程重启
- **THEN** 窗口累计亏损从持久化记录恢复，不清零

### Requirement: 判据与时间窗口
系统 SHALL 只在配置的窗口内报价，并且只在扣除我方挂单后的加权竞争量低于 gate_comp_max_usd、盘口完整覆盖 band 时报价；窗口最后 60 分钟只挂被动 reduceOnly 单；窗口结束时执行停止流程，仍有持仓时继续监控并告警。

#### Scenario: 竞争量超限
- **WHEN** 实时竞争量超过阈值，或扣除不确定
- **THEN** 判为不通过，执行停止流程，下一个周期再判断

#### Scenario: 窗口结束仍有持仓
- **WHEN** 窗口结束时持仓不为 0
- **THEN** 撤销所有挂单，每 15 分钟告警，继续监控风险

### Requirement: 强制告警与外部心跳
系统 SHALL 在 live 模式下必须配置 Telegram 并在启动时自检；告警覆盖熔断、守卫、断线、对账矛盾、判据变化、有资格时长偏低、长时间没有成功报价，以及每小时摘要；只有在关键任务健康，且最近有成功报价或明确处于等待状态时，才向外部心跳地址发送心跳。

#### Scenario: 进程活着但不报价
- **WHEN** 超过 10 分钟没有成功报价，且不处于判据等待或窗口外
- **THEN** 告警，并停止发送外部心跳
