# Ondo／Lighter 做市策略研究

第一版只做单标的公开盘口录制和离线模拟。目录名为 pendle，当前研究对象是 Ondo Perps 与 Lighter，不涉及 Pendle 协议。

Python 3.11+，仅标准库，无需安装依赖或提供 API 密钥。三个命令：`record`、`replay`、`demo`。没有真实下单、撤单、转账或账户接入功能。

## 快速开始

```bash
cd /Users/captain/python/codex/pendle

# 完全离线：生成合成演示行情及模拟报告。
python3 -m pendle demo --db data/demo.sqlite --report data/demo-report.json

# 重放同一会话；应得到相同结果。
python3 -m pendle replay --db data/demo.sqlite --report data/demo-replayed.json

# 录制真实公开行情 60 秒，默认 INTC。
python3 -m pendle record --config config.example.toml --seconds 60 --db data/intc.sqlite

# 只读回放最新会话；也可加 --run-id 指定。
python3 -m pendle replay --db data/intc.sqlite --report data/intc-report.json
```

`record` 正常结束会输出会话 ID、样本数和失败样本数；Ctrl-C 保存已完成样本。数据库保留多个会话，不会覆盖历史数据。默认录制时长为60秒，按Ctrl-C或达到时长后退出，没有后台进程。HTTP请求已在途时，退出还可能等待请求超时。

退出码：0 为命令完成且录制无失败样本；1 为录制已完成但存在失败样本；2 为配置、启动、文件或数据错误。回放成功只代表报告已生成，是否触发风控需查看 `halted`；启动元数据失败也会把错误留在数据库，缺少有效身份信息的会话不能回放。

## 配置

复制 `config.example.toml` 后修改。首版默认每2秒采样，Ondo 每侧最多50档，Lighter 每侧最多50个订单后聚合同价订单，二者深度口径不同。Lighter 市场 ID 动态解析；未知或非一倍线性合约拒绝模拟。

- `quote_notional`：每侧模拟挂单名义金额；`quote_offset_bps`：距 Ondo 中间价的距离。
- `price_tick`／`quantity_step`：Ondo 的模拟精度假设；Lighter 对冲精度与最小数量／金额来自录制时的市场元数据。
- `cancel_latency_ms`／`hedge_latency_ms`：最低等待假设，不代表实际执行精度。每2秒采样无法重建500毫秒内行情。
- `max_inventory_usd`：分别限制两平台库存及单侧挂单全部成交的潜在 Ondo 库存。
- `max_net_usd`：净敞口与潜在单侧挂单净敞口上限。
- `max_stale_ms`／`max_skew_ms`：数据年龄、断档和两来源接收时间差阈值。
- `max_basis_bps`／`max_jump_bps`：跨平台中间价偏离、相邻观测价格跳变阈值。
- 手续费和额外滑点均按配置计入；资金费为分段持仓的每小时固定费率假设。
- `reward_usdc`：显式填写的总奖励情景，不填即未知。工具不推断官方奖励份额，也不年化。

回放默认使用录制时的配置。若加 `--config` 做策略对比，只允许调整报价、时延、费用、奖励和风险假设；标的、Ondo精度、采样配置固定，避免不同市场数据混用。

回放覆盖文件只需写要修改的键，例如只写 `reward_usdc = "5"`，其余参数仍使用原会话值。SQLite原始盘口逐条读取并增量计算哈希；报告保留事件明细与采样间隔，内存仍会随事件数量增长。长时间采样建议按小时使用不同数据库文件，便于管理磁盘空间和报告体积。

## 模拟规则与报告

报价在双边行情都可见后创建。创建同刻与后续仅触价都不算成交；只有后续盘口严格穿过旧报价才模拟部分成交，按旧限价记账。撤单等待期间旧单仍可成交。对冲使用等待期结束后收到的 Lighter 对手深度，逐档计费，不能用更早的盘口执行。

重复不变盘口不刷新已消耗数量；同一来源价位的多个执行共享剩余深度，只把观察到的数量增加视为补充。数量下降时下调预算；价位消失后重现当作新增可见流动性，这仍是模型假设。对冲低于合约最小订单门槛、深度不足或超出滑点预算时保留残留敞口并停止新报价。

风险停止会锁存至本次回放结束，停止新增报价并请求模拟撤单，后续有效行情只继续处理旧单与降低待对冲风险。撤单不等于平仓，亏损预算不是保证的最大损失。

重点查看 JSON 报告中的：

| 字段 | 含义 |
|---|---|
| `pnl_before_rewards_usdc` | 配置假设下的奖励前模拟损益；必要终值数据缺失时为null |
| `fees_usdc`／`funding_pnl_usdc` | 已模拟手续费／资金费现金流，资金费支出为负 |
| `reward_scenario_usdc`／`scenario_pnl_usdc` | 用户明确输入的奖励情景及对应收益，默认null |
| `break_even_reward_usdc` | 按当前模拟损益覆盖亏损所需的补贴，未含未来退出费用 |
| `positions`／`net_quantity` | 两平台未平仓数量／净数量 |
| `pending_hedges`／`open_quotes` | 尚未执行的对冲与挂单，包括待撤单 |
| `halted`／`risk_reasons` | 是否停止新增报价及原因 |
| `execution_history_incomplete` | 有持仓或挂单时出现缺口，缺口内成交无法确认 |
| `sample_intervals_ms`／`observed_hedge_wait_ms` | 实际采样间隔／观察到的模拟对冲等待时间 |
| `events` | 每次报价、撤单、假设成交、对冲及风险事件 |
| `settings`／`metadata`／`input_sha256` | 配置、录制元数据及输入哈希，供复现 |

**这是离散盘口假设模型，不是实盘收益证明。** 缺少真实排队、逐笔主动成交和样本间行情，穿越模型既可能漏记也可能误记成交。末尾不自动平仓：多仓按该平台最优买价、空仓按最优卖价给账面估值，未保证完整退出深度，未扣未来平仓成本。没有保证金或强平仿真，USD与USDC按1:1处理。

Lighter 所用盘口接口没有可靠源时间，仅保存接收时间；不能证明源报价新鲜。Ondo 保存接口提供的时间。网络错误、限流和无效盘口写入失败样本，不能用旧盘口冒充新行情。

## 测试与规范

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pendle tests
OPENSPEC_TELEMETRY=0 openspec validate add-ondo-paper-research --strict --no-interactive
```

测试不请求外网，包含本机临时 HTTP 服务（用于验证客户端）。真实公开行情烟测单独运行 `record`。录制数据与生成报告保存在 `data/`，已被 Git 忽略。

设计、实施计划与两个 agent 的提案审核记录在 `docs/`；OpenSpec 提案位于 `openspec/changes/add-ondo-paper-research/`。

官方接口参考：

- [Ondo 盘口](https://docs.ondoperps.xyz/api-reference/market-data/get-order-book-depth)
- [Lighter 订单簿](https://docs.lighter.xyz/api/orders/orderbookorders)
- [Lighter 费用和延迟](https://docs.lighter.xyz/trading/trading-fees)
