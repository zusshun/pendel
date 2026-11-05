# Change: 同账户运行第二个单市场实例（RDDT）

## Why
这个周末（2026-11-07～08）是奖励活动的主要窗口。RDDT 的 ±25bps 竞争量约 $1,100，是回测里安全倍数最高的标的。加上它，可以参与的 maker 奖池翻倍。现有机器人只做单市场，一旦发现其他市场有持仓就拒绝下单。

2026-11-05 在正式环境实测：死人开关（cancelAllOrdersAfterPerps）是**账户级**的。断开连接 A 后，只要连接 B 还在 ping，170 秒内都没有撤单。所以两个实例共用一个账户时，必须补上互相看护。

## What Changes
- 新增 `sibling_markets` 配置：只有这些市场上的持仓不再触发"单市场账户存在其他市场持仓"；其他市场的持仓照旧拒绝。
- 账户敞口采用静态预算：各实例只按自己市场的上限运行。部署时保证两个实例的 max_position_usd 之和与 max_account_exposure_usd 之和都在账户承受范围内；保证金率仍使用交易所的账户级数值。
- 每个实例使用独立的 data_dir（实例锁、熔断锁、窗口账本互不覆盖），应急命令使用对应实例的配置。
- 配置了兄弟市场时，窗口账本不记录账户级资金费，避免一个市场的资金费冲抵另一个市场的亏损。资金费量级约每天 $0.1，在本变更中忽略。
- 新增对端心跳：每个实例在 watchdog 健康时写心跳文件，同时读取对端的心跳文件。对端心跳过期时，按对端市场撤单并核实为零，再发紧急告警。代撤成功则本实例继续运行；代撤或核零失败则本实例熔断退出（停止 ping，让账户级死人开关兜底撤单）。
- 多实例模式下，正常退出不关闭死人开关（不发 timeout_seconds=0），以免关掉对端的保护。
- 在公共停止路径（manager.stop）上对重复清扫限频：已核零、且之后没有新活单、延迟受理或新事件时，`stop_sweep_interval_s`（默认 5 秒）内的重复停止直接返回已确认，不发 REST。首次停止、退出、出现新活单或延迟受理时仍立即清扫。
- REST 限流按 API key 计：两个实例用不同的 API key，或者共用一个 key 时把 rate_per_second 各降到 0.5。
- 告警文本带上市场名前缀。
- 部署：先把 IBM 实例升级到新代码，并配置 sibling_markets=[RDDT]、心跳路径；再启动 RDDT 实例（sibling_markets=[IBM]），两边配置互相对应。

## Impact
- Affected specs: live-maker-bot
- Affected code: pendle/maker.py, pendle/maker_config.py, pendle/maker_risk.py, pendle/alerts.py, deploy/, maker.example.toml, tests/
