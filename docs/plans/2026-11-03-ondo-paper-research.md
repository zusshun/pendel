# Ondo／Lighter 录制与模拟实施计划

> **执行要求：** 主 agent 按任务推进并检查；使用 test-driven-development、verification-before-completion 和 requesting-code-review。用户已选择当前会话内完成，无需重复询问执行方式。

**目标：** 建立可独立运行、可验证且不接触账户的单标的研究工具。

**架构：** 标准库 HTTP 读取公开盘口，SQLite 保存可回放样本，确定性模拟器消费过去可见数据，CLI 输出含假设的报告。

**技术栈：** Python 3.11+、Decimal、urllib、concurrent.futures、sqlite3、tomllib、argparse、unittest。

---

每项先编写行为测试并运行确认预期失败，再实施最小代码、复跑测试、整理代码。代码命令从项目根目录执行。不要启动实盘或后台常驻任务。

## 任务 1：配置与数据接入

文件：`pendle/config.py`、`pendle/market.py`、`tests/test_market.py`、`tests/test_config.py`。

1. 测试 `Settings.from_file` 验证未知键、NaN、负数、风险限制和时延；`parse_ondo`／`parse_lighter` 校验并聚合同价盘口；市场 ID 唯一解析且 multiplier=1。
2. 执行 `python3 -m unittest discover -s tests -v`，确认功能缺失导致失败。
3. 使用 frozen dataclass、Decimal 实施校验；公开 GET 禁止自定义交易 URL／方法，不处理密钥；HTTP 错误不输出敏感请求信息。
4. 测试本地 HTTP 的合法 JSON、失败标识、429 和超时路径，确认错误可解释。

## 任务 2：存储与录制

文件：`pendle/storage.py`、`pendle/recorder.py`、`tests/test_recorder.py`。

1. 测试成功／失败样本、两端接收时间差、会话重开后原始字段保留、有限时长退出。
2. 运行上述测试确认失败，再实施两线程采样和逐样本提交。
3. 再运行测试；确保失败不会悄悄缓存旧数据，Ctrl-C 后已保存样本可读取。

## 任务 3：模拟状态与执行

文件：`pendle/paper.py`、`tests/test_paper.py`。

1. 构造小型已知盘口序列，测试创建样本不成交、触价不成交、后续穿越部分成交、撤单等待时成交和延迟对冲。
2. 运行并确认预期失败；用一组报价和待对冲状态实施确定性回放。
3. 对冲按数量步长与对手盘口深度执行；不足时保留净敞口，不伪造完全成交。

## 任务 4：风险与核算

文件：`pendle/paper.py`、`tests/test_risk.py`。

1. 先测试 stale/skew/gap、跳变、价差、订单潜在库存、残留敞口、费用、资金费、未知估值与未给定奖励。
2. 风险锁存、停止新增报价、执行已观察到的残留订单和降低敞口；报告保留结束状态。
3. 输出每笔模拟事件与明确模型限制；同一数据回放两次 JSON 一致。

## 任务 5：命令与文档

文件：`pendle/cli.py`、`pendle/__main__.py`、`pendle/__init__.py`、`tests/test_cli.py`、`config.example.toml`、`README.md`、`pyproject.toml`、`.gitignore`。

1. 通过 subprocess 先测试 `demo` 生成 SQLite 和 JSON，`replay` 重现结果，非法配置中文退出，record 有限运行。
2. 实施参数解析；输出路径来自 CLI，不隐式覆盖录制输入；默认奖励缺省，测试不访问外网。
3. 编写中文运行示例、模型假设和当前功能边界。

## 任务 6：端到端、审查与收尾

1. `python3 -m unittest discover -s tests -v`，`python3 -m compileall -q pendle tests`。
2. `OPENSPEC_TELEMETRY=0 openspec validate add-ondo-paper-research --strict --no-interactive`。
3. `python3 -m pendle demo --db data/demo.sqlite --report data/demo-report.json`，再回放比较。
4. 官方只读接口 `record` 约 10 秒，核对保存的时间和错误，再 `replay` 生成独立报告。
5. 两位 agent 审查实现；主 agent 判断、回归测试和修复，再核对 tasks 与 spec。
6. 保存中文验证记录并 git 提交；不部署、不运行后台任务。

## 审核新增验收

必须先写失败测试覆盖：重复盘口不增加成交／对冲量；多笔执行共享深度；标的／版本／顺序不匹配拒绝；Lighter 独立接收时刻早于对冲到期不执行；maker按旧限价记账；终值按BBO账面估值且不保证退出；429等待遵守Retry-After（上限60秒）。配置与元数据哈希或完整快照随报告保存，以便复现。

附加测试：固定同时间事件顺序；活动挂单时缺口设置execution_history_incomplete；跨平台净零仍受单端库存限制；资金费正多付空收、用变化前持仓计提；记录实际模拟对冲等待。
