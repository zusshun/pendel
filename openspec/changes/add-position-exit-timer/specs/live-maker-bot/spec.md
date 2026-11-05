## ADDED Requirements

### Requirement: 持仓限时离场
配置了 `max_hold_s` 时，系统 SHALL 记录本市场持仓从零变为非零（或方向翻转）的时刻；持仓持续达到 `max_hold_s` 时，撤单并核实为零，再用 reduceOnly IOC 把仓位平到零。

#### Scenario: 持仓超时
- **WHEN** 持仓非零的时长达到 `max_hold_s`
- **THEN** 先撤掉本市场全部挂单并核实为零，再发送 reduceOnly IOC 平掉全部仓位，价格受 `max_flatten_slippage_bps` 限制；归零后恢复正常报价

#### Scenario: 重启时已有持仓
- **WHEN** 实例启动时本市场已有持仓
- **THEN** 从启动时刻开始计时

#### Scenario: IOC 未能完全成交
- **WHEN** 离场 IOC 因滑点上限等原因没有完全成交，但撤单和对账都正常
- **THEN** 不熔断，退避后继续尝试；首次失败发一次普通告警

### Requirement: 持仓止损
配置了 `stop_loss_bps` 时，系统 SHALL 用新鲜的中心价或标记价计算本市场持仓的浮动亏损，达到持仓名义价值的 `stop_loss_bps` 时立即按限时离场的流程平仓。

#### Scenario: 浮亏触发止损
- **WHEN** 持仓浮亏 ≥ |持仓| × 开仓均价 × stop_loss_bps / 10000
- **THEN** 不等 `max_hold_s` 计时，立即撤单、核零并用 IOC 平到零

### Requirement: 持仓期间减仓侧收紧
配置了 `exit_reduce_offset_bps` 时，系统 SHALL 在持仓非零期间，把减仓那一侧的挂单放在离中心价 `exit_reduce_offset_bps` 处，仍为 post-only，并满足 min_offset 与 band 的约束。

#### Scenario: 成交后减仓侧靠近中心
- **WHEN** 持仓非零且没有其他守卫阻止报价
- **THEN** 减仓侧的挂单距中心价为 exit_reduce_offset_bps，加仓侧沿用软上限和库存上限的现有规则

#### Scenario: 未配置时行为不变
- **WHEN** 三个参数都未配置
- **THEN** 行为与本变更之前完全一致
