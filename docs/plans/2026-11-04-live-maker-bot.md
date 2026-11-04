# 实盘做市机器人：实施计划

> 执行者：Codex（`codex exec`，workspace-write）。监督：主 agent。依据：`openspec/changes/add-live-maker-bot/` 第三版的 design.md 与 spec；如有冲突，以 design.md 为准。发现设计缺陷时停下来说明，不要扩大范围。

## 通用规则

- 每项都严格按 TDD：先写行为测试，确认失败，再写实现。
- 测试不联网、不绑定端口，交易所、WS、时钟、sleep、文件系统（锁）全部注入假对象。
- **禁止**任何提现、转账、API 密钥管理接口，以及任何真实网络写请求。实现里有接口白名单，并有测试覆盖。
- 注释、日志、错误信息用中文，标识符用英文。现有回测和采集代码的行为不得改变。
- 单个文件写入超过 200 行时分段写入。不要提交 git，不要修改 openspec 和 docs。
- 测试命令：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`。

## 批次 M1：核心安全模块（tasks 2.1～2.3）

1. `pendle/ondo_api.py`：
   - REST 签名与 WS 登录签名，用文档示例做测试。
   - 环境地址可选 prod 和 sandbox。
   - 接口白名单。
   - 异步 REST 客户端，HTTP 传输可以注入。
   - 令牌桶加优先级队列：按市场全部撤单 > 减仓 > 单笔撤单 > 挂单 > 查询。
   - 429 遵守 Retry-After，期间只放行撤单类请求。
   - 错误码分类函数，严格按 design 的表格。
   - 用服务器时间校时，计算偏差。
   - 密钥从环境变量或权限为 600 的文件读取，日志自动遮蔽。
   - 对外发送闸门：Watchdog 可以关闭，停止流程有例外通道。
2. `pendle/maker_orders.py`：
   - 订单状态机：`INTENT → SENT → ACKED/OPEN | REJECTED | UNKNOWN → PARTIAL → FILLED | CANCEL_SENT → CANCELED`。
   - 固定 clientOrderId，永不复用。
   - UNKNOWN 的确认消歧流程（design"订单状态机"第 1～5 条）。
   - 撤单确认后才允许替换单。
   - 成交按 fill id 去重；持仓水位；REST 快照不覆盖更新的 WS 事件。
   - SYNCING 屏障。
   - 停止流程：禁止新增 → 处理在途 → 按市场全部撤单 → 核实挂单为 0。
3. `pendle/maker_risk.py`：
   - 最坏敞口检查（包含所有未终结订单），等于上限时允许、超出时拒绝。
   - 加仓侧限量；账户总敞口；保证金率估算。
   - 代码硬上限：单侧 800、持仓 1200、账户 1500，没有绕过开关。
   - 守卫状态机：冷却和 stable 恢复。
   - 全局熔断：先写锁并 fsync，再停止；写锁失败时退出码 76。
   - 本轮窗口累计亏损持久化（跨午夜、重启都不清零）。
   - 死人开关 Watchdog：每个任务单独的过期阈值；PrivateFeed 看 pong；不健康时关闭闸门。
   - 单实例 flock 锁。

## 批次 M2：策略、告警、部署（tasks 2.4～2.7）

1. `pendle/maker.py`：
   - 第三方盘口（按价位扣除我方剩余量）、中心价、独立慢锚、慢锚 EMA 漂移守卫、可选的币安外部参考守卫、标记价格辅助守卫。
   - 报价：12±4bps，区间 [8, 22]，取整后再检查。
   - 不挂单边；软上限 50%；硬上限 IOC（以中心价或慢锚为基准，滑点 60bps，重试 3 次，60 秒时限）。
   - 判据只用第三方盘口，完整性按 design 定义；单一窗口；窗口最后 60 分钟降仓。
   - 主循环；运行模式 dry-run、sandbox、live 及 live 前置条件。
   - 私有 WS：登录、订阅订单、成交、持仓、余额，死人开关 ACK 后才允许下单。
   - Reconciler 定时对账，断线后补账。
   - 每分钟健康日志，盈亏记录到 `data/maker/`。
2. `pendle/alerts.py`：
   - Telegram 自检（失败时拒绝 live），合并告警，每小时摘要。
   - 有资格时长偏低告警；长时间没有成功报价时告警。
   - 外部心跳：只在健康时发送。
3. CLI：
   - `maker --config maker.example.toml [--live]`
   - `maker-cancel-all --config ...`
   - `maker --reset-halt`
4. 配置与部署：
   - `maker.example.toml`：守卫的默认值先填保守的占位值，并标注"待标定"。
   - `deploy/pendle-maker.service`：`Restart=on-failure`、`StartLimitIntervalSec=600`、`StartLimitBurst=3`、`RestartPreventExitStatus=76`、非 root 运行。
   - README 增加实盘章节：API 密钥只开交易权限、IP 白名单、不开提现；应急停机命令。
5. **模拟交易所端到端测试**：内存撮合，覆盖 design"测试与验收"中的全部项。必须包括以下几项：
   - 只有我方挂单时中心价不随偏移漂移
   - UNKNOWN 消歧过程中不出现第二张单
   - Watchdog 停止续期
   - 先写锁再撤单
   - 告警失败时拒绝 live

## 主 agent 验收

每批结束后，主 agent 在沙箱外运行全量测试并阅读关键代码。之后由主 agent 完成：标定、服务器部署、沙盒实测清单、小资金实盘。
