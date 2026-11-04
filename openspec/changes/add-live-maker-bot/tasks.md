## 1. 提案与审核
- [x] 1.1 查阅 Ondo 交易 API（签名、下单、撤单、私有 WS、死人开关）
- [x] 1.2 编写 proposal、design、spec、tasks
- [x] 1.3 Claude agent 与 Codex 第一轮审核，并入意见（第二版）
- [x] 1.4 两方确认轮（Claude：可以开发，5 条重要；Codex：3 条阻塞），并入第三版
- [x] 1.5 两方确认第三版没有阻塞问题（Claude：可以开发；Codex：4 项闭合，tasks 措辞已修正）

## 2. 实现（TDD，Codex 执行，主 agent 监督）
- [x] 2.1 `ondo_api.py`：签名（含 WS 登录）、接口白名单、REST 客户端、限速优先级队列、429、错误码分类、校时
- [x] 2.2 `maker_orders.py`：订单状态机、固定 clientOrderId、UNKNOWN 消歧、撤单确认、去重、同步屏障
- [x] 2.3 `maker_risk.py`：最坏敞口、保证金、代码硬上限、守卫状态机、熔断锁（fsync）、窗口累计亏损持久化、死人开关看门狗
- [x] 2.4 `maker.py`：扣除我方挂单后的中心价（第三方盘口）、独立慢锚与漂移守卫（标记价格仅作辅助守卫）、报价与偏移、软上限与硬上限、判据与窗口、窗口末尾降仓、停止流程、单实例锁、运行模式
- [x] 2.5 `alerts.py`：Telegram（自检、合并告警、每小时摘要）、外部心跳
- [x] 2.6 CLI `maker`、`maker-cancel-all`、`maker --reset-halt`；`maker.example.toml`；`deploy/pendle-maker.service`；README 实盘与应急章节
- [x] 2.7 模拟交易所端到端测试（design"测试与验收"中的全部项）

## 3. 标定与验收
- [ ] 3.1 用已采集的数据标定守卫默认值；按实盘规则（8～22bps、偏移）重跑 IBM 回测，确定 gate_comp_max_usd
- [ ] 3.2 服务器部署 collect；dry-run 30 分钟（真实行情）
- [ ] 3.3 沙盒验收实测清单（死人开关 kill -9 与断网、429、撤单延迟、私有消息、保证金、校时、重复 ID）
- [ ] 3.4 小资金实盘（每侧 $50，至少 2 小时），满足量化验收条件，否则延期
- [ ] 3.5 两方复审实现，修复问题
- [ ] 3.6 周六 09:00 正式运行（判据自动控制）

## 4. 收尾
- [ ] 4.1 更新 docs/verification.md、README
- [ ] 4.2 归档变更，同步 specs
