# 最终验证记录

日期：2026-11-03，北京时间。

## 范围与审核

已完成单标的公开行情录制、SQLite持久化、确定性离线模拟、风险停止和三条CLI命令。提案与实现分别经两个独立agent审核，重要意见全部按失败测试→修复→复核闭合。无实盘交易、账户密钥或后台任务。

## 命令与结果

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`：43项通过，0失败，0错误。
- `python3 -m compileall -q pendle tests`：退出码0。
- `OPENSPEC_TELEMETRY=0 openspec validate add-ondo-paper-research --strict --no-interactive`：通过。
- `python3 -m pendle demo --db data/demo.sqlite --report data/demo-report.json`：7组离线合成行情，包含模拟成交与延迟对冲。
- `python3 -m pendle replay --db data/demo.sqlite --report data/demo-replayed.json`：与同会话演示报告逐字段完全一致。
- `python3 -m pendle record --config config.example.toml --seconds 10 --db data/intc-smoke.sqlite`：真实官方匿名GET成功，5组双平台样本，失败样本0，原始响应均已保存。
- `python3 -m pendle replay --db data/intc-smoke.sqlite --report data/intc-smoke-report.json`：真实数据成功回放，0次风险停止；此次短时样本没有模拟成交，不能由此评估收益。

SQLite输入SHA256、运行参数和合约元数据随报告保存。data目录为本地生成产物，Git忽略。

## 已知限制

这是离散盘口假设模型，不能重建真实排队与样本间成交，也不证明实盘收益。Lighter盘口无可靠源时间；费用／资金费是显式情景参数；奖励默认未知，不年化；BBO期末估值不保证退出深度且不含未来退出费用；无保证金／强平仿真。事件报告仍随时长增长，建议分段录制。

本机HTTP集成测试需允许127.0.0.1临时监听；默认受限沙箱禁止绑定端口时会失败，在获得本机测试权限后全量通过。

---

# 变更 add-history-reward-backtest 验证记录

日期：2026-11-04，北京时间。开发由 Codex（`codex exec`，workspace-write）完成，主 agent 在每个批次后，于沙箱外独立验收。

## 批次与测试

| 批次 | 内容 | 沙箱外全量测试 |
|---|---|---|
| A | 配置、历史库、币安日文件、扫描导入、奖励 | 131 项通过 |
| B / B2 | 回测、报告、CLI；修正参考价时效、markout 口径、盈亏拆解 | 206 → 240 项通过 |
| C | collector、import-collected、systemd、可选依赖 | 286 项通过 |
| B3 | pickoff、延后 markout、中间价误差情景、按标的计算强平价差 | 312 项通过 |
| B4 + C2 | 分侧基差与 ondo_mid 回退；币安 WS 新地址、连接级断档 | 345 项通过 |
| D | 两方最终审核的 9 条修复（pickoff 即时口径、接收时间可见性、精度来源、band 一致性、Lighter 缓存、增量磁盘统计、单调时钟等） | **383 项通过** |

`python3 -m compileall -q pendle tests`、`openspec validate add-history-reward-backtest --strict` 都通过。

## 外部验收

- **历史数据**：8 个标的上周末的 Ondo 原始成交页，以及币安 aggTrades 日文件，都在 `data/raw/`，币安文件已用 SHA256 与 CHECKSUM 校验。建库 12 秒，扫描导入约 4 秒。8 个标的、32 种组合的回测约 5.7 分钟（B 版本约 18 秒；现在每个标的要跑 2 种回退 × 2 种对冲 × 3 档同价比例）。
- **真实 WS 烟测**：
  - 第一次（10 分钟）：SIGTERM 后退出码 0，立即退出。发现两个问题：币安 0 条消息，原因是旧地址不再推送；以及 652 条误判断档。
  - C2 修复后（3 分钟）：币安 1070 条消息，断档 6 条，即三个来源启动时各开、各关一次。
  - D 修复后（90 秒）：Lighter 不支持的 NKE、RDDT、DKNG 各只警告一次，错误记录为 0；市场精度已导入（87 个市场）。
  - 数据量：降采样后 Ondo 约 1.3MB/10 分钟，约 190MB/天。
- **全市场竞争扫描**：从 2026-11-04 01:22 起，每 5 分钟一轮，持续到 11-09 08:00，覆盖整个周末。中途切换为 depth=100。

## 已知限制

- 竞争量只有工作日数据（周末代理值）。
- 奖励规则未经官方确认。
- 只有一个周末样本。
- 没有模拟保证金和强平。
- Lighter 对冲是代理价格。
- 冷门市场周末币安成交稀疏，大量时段要靠 Ondo 自身回退，回退期间守卫不可用。
- 详见 `docs/reports/2026-11-04-final-conclusion.md`。
