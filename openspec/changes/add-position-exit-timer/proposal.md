# Change: 持仓限时离场与止损

## Why
2026-11-05，IBM 两次挂单成交后持有约 $550 多单，被动减仓只成交了 0.01 股，最后持仓 2 个多小时，由用户手动平仓。做市持仓应当短时间内消化掉，不能长时间暴露在单边价格风险里。用户决定：持仓超过 10 分钟就平仓，并加止损。

## What Changes
- 有持仓时，减仓那一侧的挂单改挂在 `exit_reduce_offset_bps`（生产配置为 10bps，比 min_offset_bps（8）多留 2bps 余量，避免中心价微动就撤单），仍然是 post-only；加仓那一侧维持现有规则。
- 持仓时长达到 `max_hold_s`（生产配置为 600 秒）：撤掉全部挂单，用 IOC 吃单把剩余仓位全部平掉。
- 止损：持仓浮亏达到持仓名义价值的 `stop_loss_bps`（生产配置为 30bps）时，不等计时，立即按同样流程平仓。
- 平仓归零后恢复正常的双边挂单。
- 新增参数默认关闭，不配置时行为与现在一致。

## Impact
- Affected specs: live-maker-bot
- Affected code: pendle/maker.py、pendle/maker_config.py、maker*.example.toml、tests/
