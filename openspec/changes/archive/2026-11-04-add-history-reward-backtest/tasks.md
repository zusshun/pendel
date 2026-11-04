## 1. 提案与审核
- [x] 1.1 归档上一变更，核实公开数据源可用性
- [x] 1.2 编写 proposal、design、spec delta、tasks
- [x] 1.3 Claude agent 第一轮审核，并入意见和 WS／服务器要求（第二版）
- [x] 1.4 Codex 第一轮审核 + Claude 复核，并入意见（第三版）
- [x] 1.5 两方确认第三版无阻塞问题（Claude 已确认；Codex 第 6、10 条已按主 agent 决策处理并确认）

## 2. P0 实现（TDD，Codex 执行，主 agent 监督）
- [x] 2.1 `bt_config.py`：BacktestSettings、CollectSettings 及校验；`backtest.example.toml`
- [x] 2.2 `history.py`：SQLite 表结构与事务；Ondo 成交翻页、(time_ns, id) 稳定排序、覆盖记录；合约快照
- [x] 2.3 `binance.py`：日 zip 下载与 CHECKSUM 校验、aggTrades 入库
- [x] 2.4 `scan_import.py`：临时扫描 JSONL 幂等导入，计算 depth_observations（加权竞争量、tick 深度、band 覆盖完整度）
- [x] 2.5 `reward.py`：权重、分钟份额、reward_mid 误差、C* 及边界、所需奖励池
- [x] 2.6 `backtest.py`：可见性与订单生效延迟、基差中心价、post-only、报价与 cancel_first 重报、成交判定（单笔成交量守恒、同价比例）、库存上限
- [x] 2.7 `backtest.py`：对冲（none、lighter_proxy）、守卫与冷却、费用、资金费、期末两种口径
- [x] 2.8 `bt_report.py`：北京时间分时（工作日／周末）、命名时段、markout、同价比例敏感性、竞争排名、上线判据、多标的 Markdown
- [x] 2.9 CLI：`fetch-history`、`import-scan`、`competition`、`backtest`

## 3. P0 验证
- [x] 3.1 全量 unittest（含原有 43 项）、compileall、`openspec validate --strict`。新增测试不绑定端口；旧的 `tests/test_market.py` 端口测试不修改，由主 agent 在沙箱外执行全量测试覆盖
- [x] 3.2 外部验收：抓取 2026-10-31 00:30Z～11-01 21:10Z 的 INTC、DRAM、CRCL（时间允许再加排名前列的市场和 10-24 那个周末），导入扫描数据并回测
- [x] 3.3 中间结论报告：各标的周末 C*、实测竞争量对比、周六 09:00 上线判据

## 4. P1 实时采集
- [x] 4.1 `collector.py`：单个 Ondo WS 连接（全市场盘口与成交、降采样）、币安 aggTrade、Lighter REST 轮询、REST 后备扫描；线程池与写盘队列；ping、静默检测、断档、轮转、退出码 75、优雅退出、健康日志
- [x] 4.2 `import-collected`：导入 WS 格式数据（含截断尾部），接入回测的 observed 竞争量、reward_mid 与断档
- [x] 4.3 `collect.example.toml`、`deploy/pendle-collect.service`、可选依赖 `live`、README 部署说明
- [x] 4.4 外部验收：本地用真实公开 WS 烟测 10 分钟，检查重连、轮转和磁盘占用

## 5. 收尾
- [x] 5.1 Claude agent 与 Codex agent 复审实现，修复问题
- [x] 5.2 全量验证，更新 docs/verification.md 和最终结论报告
- [x] 5.3 归档变更，同步 specs/（spec.md + design.md）
