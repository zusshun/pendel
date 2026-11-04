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
