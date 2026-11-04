# 历史奖励回测 + 实时采集：实施计划

> 执行者：Codex（`codex exec`，workspace-write 沙箱）。监督：主 agent（Claude Code），负责每批验收、沙箱外全量测试、外部网络验收和审核协调。
> 依据：`openspec/changes/add-history-reward-backtest/` 的 design.md 与 specs。若与本计划冲突，以 design.md 为准；发现设计缺陷时停下并在输出中说明，不要自行扩大范围。

## 通用规则

- 每项都按 TDD：先写行为测试并运行确认失败，再写最小实现，再复跑。
- Python 3.11+。P0 只用标准库；`collector.py` 可以在函数内按需导入 `websockets`。
- 代码注释、文档字符串、日志、错误信息用中文；标识符和 JSON 键名用英文。
- 新增测试不访问外网、不绑定端口。HTTP、WS、时钟、sleep 一律通过参数注入假对象。
- 现有 `pendle/market.py`、`storage.py`、`paper.py`、`recorder.py`、`config.py` 的行为不得改变，只能复用。`cli.py` 只能新增子命令。
- 测试命令：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`。`tests/test_market.py` 中绑定本机端口的旧测试如果因沙箱失败，可以忽略，主 agent 会在沙箱外运行。
- 每批结束时输出：修改的文件清单、测试结果摘要、未完成项和已知问题。不要提交 git。
- 单个文件写入超过 200 行时分段写入。

## 批次 A（P0 基础，tasks 2.1–2.5）

1. `pendle/bt_config.py`：
   - `BacktestSettings` 和 `CollectSettings` 为冻结 dataclass，与 design 的参数名一致；使用 Decimal，拒绝未知键，拒绝非有限值和负数。
   - 支持按标的覆盖 `price_tick`、`quantity_step` 和币安符号映射（默认 `{SYM}USDT`）。
   - 新增 `backtest.example.toml`，带中文注释。默认值：`quote_notional=800`、`max_inventory_usd=1600`、`band_bps=25`、`pool_usdc_per_week=110000`、maker 费 1bps、taker 费 2.5bps。
2. `pendle/history.py`：
   - `HistoryStore`：SQLite 表结构与 design 一致，包括唯一键、索引和事务。
   - `fetch_ondo_trades(client, market, start_ms, end_ms, …)`：游标翻页到最旧一笔早于 start 时停止，按 `id` 去重。时间解析为 time_ns（纳秒字符串转整数）。记录 fetches 和真实覆盖范围。按 `request_interval_ms` 限速，429 尊重 `RateLimited`。
   - 合约快照入库。
   - 提供按时间顺序迭代的读取接口，顺序键为 (time_ns, id)。
3. `pendle/binance.py`：
   - 日 zip 与 `.CHECKSUM` 下载：二进制 GET，单文件上限 200MiB。SHA256 不一致时拒绝。
   - 解析 aggTrades CSV，兼容有表头和无表头两种格式，存入 `binance_trades`。
   - 下载函数通过注入实现，测试用内存中的 zip。
4. `pendle/scan_import.py`：
   - 导入 `data/depth_scan/*.jsonl` 格式：每行包含 `kind`、`market`、`received_ms`，以及 `payload` 或 `error`。导入幂等；尾部截断的半行跳过并计数。
   - 对每条 depth 计算并写入 `depth_observations`：mid、买一卖一、末档距离、`book_band_complete`、flat 与 linear 两种权重下买卖两侧的加权 USD、距 mid 第 k 个 tick 的深度（k ≤ 10）。
5. `pendle/reward.py`：纯函数。
   - 距离与权重：flat、linear，band 外为 0，加上 `reward_mid_error_bps`。
   - 单分钟份额与奖励额。
   - 恒定 comp 下的 C*：二分法或解析解，边界返回 `no_rewards_needed`、`unprofitable_even_at_zero_comp`、`never_eligible`。
   - 所需奖励池。

## 批次 B（P0 回测与报告，tasks 2.6–2.9）

1. `pendle/backtest.py`：
   - 实现 design"回测事件模型"第 1～10 条，以及固定的同一时刻处理顺序。
   - 输入为迭代器（Ondo 成交、币安成交）加上配置；输出逐事件记录和逐分钟记录（我方合格权重、是否双边、有无持仓），供 reward 和 report 使用。
   - 必须有的测试：
     - 追加未来数据不改变此前结果
     - 单笔成交量守恒
     - 订单生效前不成交
     - 撤单延迟期间仍可成交
     - post-only 拒单
     - 库存上限含潜在挂单
     - 对冲净额与期末取消
     - 守卫冷却后恢复
     - 两种期末口径
2. `pendle/bt_report.py`：
   - 按北京时间（UTC+8）小时分桶，工作日与周末分开；命名时段默认"周末：周六09:00–周一05:00"，可配置。
   - 指标：主动成交额、实现波动、在线率、守卫暂停时长、成交次数与名义额、markout（10s/60s/300s，事后分析）、费用、对冲成本、基差盈亏、库存盈亏、各 comp 情景的奖励与净收益、同价比例敏感性。
   - 输出 C*（mark 和 liquidate 两种口径）、所需奖励池、上线判据、最大名义值与本金之比、覆盖与断档，以及全部假设声明。
   - 竞争排名：数据来自 depth_observations，按市场、北京时间小时、工作日或周末计算中位数和 P90，排序，标出样本数和"不确定"。
   - 输出 JSON 和 Markdown 摘要（中文表格），并生成多标的对比。
3. CLI 新增子命令：
   - `fetch-history --symbols INTC,DRAM,CRCL --start ISO --end ISO --db data/history.sqlite`
   - `import-scan --src data/depth_scan --db …`
   - `competition --db … --report … --summary …`
   - `backtest --db … --config backtest.toml --symbols … --start … --end … --report … --summary …`
   - 错误信息用中文，退出码与现有约定一致。
   - 要有离线端到端测试。

## 批次 C（P1 采集，tasks 4.1–4.3）

按 design"实时采集服务"实现 `pendle/collector.py`、`import-collected`、`collect.example.toml`、`deploy/pendle-collect.service`，以及 pyproject 中的可选依赖 `live`。在 README 补充中文部署说明。

测试用假的 WS 连接对象（异步迭代器加可控异常）和假时钟，不启动真实服务。

## 主 agent 验收（每批之后）

沙箱外运行全量测试、compileall、`openspec validate --strict`，阅读 diff，抽查关键逻辑。批次 B 完成后，执行外部网络验收（tasks 3.2）并撰写中间结论；批次 C 完成后，做 10 分钟的真实 WS 烟测。
