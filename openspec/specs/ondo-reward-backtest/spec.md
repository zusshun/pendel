# ondo-reward-backtest Specification

## Purpose
TBD - created by archiving change add-history-reward-backtest. Update Purpose after archive.
## Requirements
### Requirement: 公开历史数据抓取
系统 SHALL 仅通过公开 GET 抓取指定时间窗和标的的 Ondo 逐笔成交、币安 USDⓈ-M 逐笔成交日文件和 Ondo 合约快照，按主键去重存入 SQLite，并记录每次抓取的参数、结果和真实覆盖范围。

#### Scenario: 同时间戳成交排序
- **WHEN** 多笔 Ondo 成交的纳秒时间戳相同
- **THEN** 按 (time_ns, id) 得到稳定顺序，与抓取顺序、重抓、导入来源无关；重复导入幂等

#### Scenario: 币安日文件校验
- **WHEN** 下载的 zip 文件 SHA256 与 CHECKSUM 不一致
- **THEN** 拒绝入库并记录错误

#### Scenario: 币安映射不是同一资产
- **WHEN** 窗口内 Ondo 与映射的币安合约价格偏离中位数超过 max_mapping_dev_bps
- **THEN** 拒绝回测该标的并给出中文错误，不输出结果

#### Scenario: 翻页覆盖窗口
- **WHEN** Ondo 成交翻页到最旧一笔早于窗口起点
- **THEN** 停止翻页，窗口内的成交全部入库且不重复

#### Scenario: 历史不足或接口失败
- **WHEN** 游标耗尽仍未覆盖窗口起点，或接口限流、出错
- **THEN** 记录实际覆盖起点和错误；回测报告标注覆盖不足，不用推测数据补齐

### Requirement: 全市场竞争排名
系统 SHALL 从盘口观测计算每个市场、每个北京时间小时的加权竞争量、价差和一档深度（中位数与 P90），按估算的每小时奖励排序，并标注样本数与采集时段。

#### Scenario: 计算竞争量
- **WHEN** 获得有效 Ondo 双边盘口
- **THEN** 分别按 flat 和 linear 权重计算买卖两侧 ±band 内合格挂单的 USD 值，同时保存中间价

#### Scenario: 盘口未覆盖 band
- **WHEN** 盘口末档仍在 band 以内
- **THEN** 标记 book_band_complete=false，竞争量只作为下界，排名中标为"不确定"

#### Scenario: 导入临时扫描
- **WHEN** 导入每行包含 kind（contracts、depth 或 error）、market、received_ms 及 payload 或 error 的 JSONL，包括尾部被截断的文件
- **THEN** 幂等写入观测与错误，跳过截断的半行并计数

#### Scenario: 工作日与周末分开
- **WHEN** 按北京时间小时统计竞争量
- **THEN** 工作日与周末分别统计，并标出样本数

#### Scenario: 样本不足
- **WHEN** 某个市场、某个小时没有观测
- **THEN** 排名中显示为缺失，不用其他时段补齐

### Requirement: 无前视的成交驱动回测
系统 SHALL 按时间顺序处理 Ondo 成交、币安逐笔、订单生效与撤单生效、对冲到期和分钟快照；数据按事件时间加 data_latency_ms 才算可见，订单按 place_latency_ms 生效；同一输入和配置的结果完全一致。

#### Scenario: 追加未来数据
- **WHEN** 在回测窗口之后追加更多数据
- **THEN** 此前所有事件、成交和奖励都不变

#### Scenario: 单笔成交量守恒
- **WHEN** 一笔 Ondo 成交同时满足我方多张订单的成交条件
- **THEN** 我方各订单成交量之和不超过该笔成交的可分配量

#### Scenario: 库存上限
- **WHEN** 持仓加上同侧挂单的潜在成交会超过 max_inventory_usd
- **THEN** 该侧缩量或不挂单

#### Scenario: 对冲净额与期末
- **WHEN** 待对冲期间出现反向成交，或回测结束时仍有未执行的对冲
- **THEN** 先做净额抵销；期末取消未发送的对冲，两平台持仓分别按各自价格结算

#### Scenario: 穿价与同价成交
- **WHEN** 主动卖单价格低于我方买价
- **THEN** 按我方限价成交 min(可分配量, 剩余量)；等于买价时按 at_price_fill_ratio 成交，报告列出 [0, 0.5, 1] 的敏感性结果；订单生效之前不成交

#### Scenario: 撤单延迟
- **WHEN** 已请求撤单但 cancel_latency_ms 尚未过去
- **THEN** 旧单仍可成交；新单在撤单生效后才发出（cancel_first，本变更唯一的重报模式）

#### Scenario: Post-only 拒单
- **WHEN** 买价不低于估计的卖一，或卖价不高于估计的买一
- **THEN** 该侧不挂单，并计入拒单次数

#### Scenario: 期末两种口径
- **WHEN** 回测结束时仍有持仓
- **THEN** 同时输出账面估值和按 taker 费加半价差平仓两种损益，并分别求 C*

### Requirement: 守卫撤单与恢复
系统 SHALL 在币安价格变动超过阈值时请求撤销全部报价，冷却期内连续无新触发后恢复报价，并报告触发次数和暂停时长。

#### Scenario: 逐笔守卫窗口
- **WHEN** 币安逐笔价格在 guard_window_s 秒内的变动超过 guard_move_bps
- **THEN** 在触发时刻加 guard_latency_ms 后请求撤单；只使用触发时刻之前（含当时）的成交

#### Scenario: 冷却后恢复
- **WHEN** 守卫触发后 guard_cooldown_s 内没有新的触发
- **THEN** 系统恢复报价，已有持仓不受撤单影响

### Requirement: 参数化奖励份额与盈亏平衡
系统 SHALL 在每个 UTC 整分钟按 band、权重函数和双边要求计算我方合格权重，按周奖励池 ÷ 市场数 ÷ 10080 × 份额计算奖励；对每个竞争量情景分别输出，并求出盈亏平衡竞争量。所有奖励参数都必须标注为未经官方确认的假设。

#### Scenario: 超出 band
- **WHEN** 报价相对 reward_mid 的距离（含 reward_mid_error_bps）超过 band_bps
- **THEN** 该报价在本次快照中的权重为 0

#### Scenario: 盈亏平衡无解
- **WHEN** 不靠奖励已盈利、竞争量为 0 仍亏损，或全窗口没有合格挂单
- **THEN** 分别输出 no_rewards_needed、unprofitable_even_at_zero_comp 或 never_eligible，不输出数值解

#### Scenario: 奖励中间价误差
- **WHEN** 历史模式下没有真实盘口中间价
- **THEN** 按 reward_mid_error_bps 情景扩大我方距离，奖励标注为条件性结果

#### Scenario: 上线判据
- **WHEN** 生成报告
- **THEN** 输出每个标的的周末 C*，以及"实时 comp < C* 且盘口完整覆盖 band"的上线判据

#### Scenario: 要求双边
- **WHEN** reward_two_sided 为 true 且只有一侧合格
- **THEN** 本分钟奖励为 0

### Requirement: 分时段与多标的报告
系统 SHALL 按北京时间小时和命名时段输出波动、主动成交额、在线率、模拟成交、markout、费用、对冲成本、库存盈亏、各情景奖励与净收益，并生成多标的 Markdown 对比，同时披露数据覆盖和模型偏差。

#### Scenario: 周末时段汇总
- **WHEN** 用户使用默认命名时段
- **THEN** 报告单独汇总北京时间周六 09:00 至周一 05:00 的结果

### Requirement: 只读与隔离
系统 SHALL 不包含下单、撤单、认证或密钥路径；不得改变现有 record、replay、demo 的行为和数据格式。

#### Scenario: 现有命令回归
- **WHEN** 运行原有测试套件
- **THEN** 全部通过

