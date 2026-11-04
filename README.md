# Ondo／Lighter 做市策略研究

第一版只做单标的公开盘口录制和离线模拟。目录名为 pendle，当前研究对象是 Ondo Perps 与 Lighter，不涉及 Pendle 协议。

Python 3.11+，历史抓取与离线研究只用标准库；常驻实时采集需要可选依赖 `websockets`。无需 API 密钥。除 `record`、`replay`、`demo` 外，提供历史导入、奖励回测及 `collect`／`import-collected` 命令。没有真实下单、撤单、转账或账户接入功能。

## 快速开始

```bash
cd /Users/captain/python/codex/pendle

# 完全离线：生成合成演示行情及模拟报告。
python3 -m pendle demo --db data/demo.sqlite --report data/demo-report.json

# 重放同一会话；应得到相同结果。
python3 -m pendle replay --db data/demo.sqlite --report data/demo-replayed.json

# 录制真实公开行情 60 秒，默认 INTC。
python3 -m pendle record --config config.example.toml --seconds 60 --db data/intc.sqlite

# 只读回放最新会话；也可加 --run-id 指定。
python3 -m pendle replay --db data/intc.sqlite --report data/intc-report.json
```

`record` 正常结束会输出会话 ID、样本数和失败样本数；Ctrl-C 保存已完成样本。数据库保留多个会话，不会覆盖历史数据。默认录制时长为60秒，按Ctrl-C或达到时长后退出，没有后台进程。HTTP请求已在途时，退出还可能等待请求超时。

退出码：0 为命令完成且录制无失败样本；1 为录制已完成但存在失败样本；2 为配置、启动、文件或数据错误；75 为常驻采集达到磁盘用量上限。回放成功只代表报告已生成，是否触发风控需查看 `halted`；启动元数据失败也会把错误留在数据库，缺少有效身份信息的会话不能回放。

## 配置

复制 `config.example.toml` 后修改。首版默认每2秒采样，Ondo 每侧最多50档，Lighter 每侧最多50个订单后聚合同价订单，二者深度口径不同。Lighter 市场 ID 动态解析；未知或非一倍线性合约拒绝模拟。

- `quote_notional`：每侧模拟挂单名义金额；`quote_offset_bps`：距 Ondo 中间价的距离。
- `price_tick`／`quantity_step`：Ondo 的模拟精度假设；Lighter 对冲精度与最小数量／金额来自录制时的市场元数据。
- `cancel_latency_ms`／`hedge_latency_ms`：最低等待假设，不代表实际执行精度。每2秒采样无法重建500毫秒内行情。
- `max_inventory_usd`：分别限制两平台库存及单侧挂单全部成交的潜在 Ondo 库存。
- `max_net_usd`：净敞口与潜在单侧挂单净敞口上限。
- `max_stale_ms`／`max_skew_ms`：数据年龄、断档和两来源接收时间差阈值。
- `max_basis_bps`／`max_jump_bps`：跨平台中间价偏离、相邻观测价格跳变阈值。
- 手续费和额外滑点均按配置计入；资金费为分段持仓的每小时固定费率假设。
- `reward_usdc`：显式填写的总奖励情景，不填即未知。工具不推断官方奖励份额，也不年化。

回放默认使用录制时的配置。若加 `--config` 做策略对比，只允许调整报价、时延、费用、奖励和风险假设；标的、Ondo精度、采样配置固定，避免不同市场数据混用。

回放覆盖文件只需写要修改的键，例如只写 `reward_usdc = "5"`，其余参数仍使用原会话值。SQLite原始盘口逐条读取并增量计算哈希；报告保留事件明细与采样间隔，内存仍会随事件数量增长。长时间采样建议按小时使用不同数据库文件，便于管理磁盘空间和报告体积。

## 模拟规则与报告

报价在双边行情都可见后创建。创建同刻与后续仅触价都不算成交；只有后续盘口严格穿过旧报价才模拟部分成交，按旧限价记账。撤单等待期间旧单仍可成交。对冲使用等待期结束后收到的 Lighter 对手深度，逐档计费，不能用更早的盘口执行。

重复不变盘口不刷新已消耗数量；同一来源价位的多个执行共享剩余深度，只把观察到的数量增加视为补充。数量下降时下调预算；价位消失后重现当作新增可见流动性，这仍是模型假设。对冲低于合约最小订单门槛、深度不足或超出滑点预算时保留残留敞口并停止新报价。

风险停止会锁存至本次回放结束，停止新增报价并请求模拟撤单，后续有效行情只继续处理旧单与降低待对冲风险。撤单不等于平仓，亏损预算不是保证的最大损失。

重点查看 JSON 报告中的：

| 字段 | 含义 |
|---|---|
| `pnl_before_rewards_usdc` | 配置假设下的奖励前模拟损益；必要终值数据缺失时为null |
| `fees_usdc`／`funding_pnl_usdc` | 已模拟手续费／资金费现金流，资金费支出为负 |
| `reward_scenario_usdc`／`scenario_pnl_usdc` | 用户明确输入的奖励情景及对应收益，默认null |
| `break_even_reward_usdc` | 按当前模拟损益覆盖亏损所需的补贴，未含未来退出费用 |
| `positions`／`net_quantity` | 两平台未平仓数量／净数量 |
| `pending_hedges`／`open_quotes` | 尚未执行的对冲与挂单，包括待撤单 |
| `halted`／`risk_reasons` | 是否停止新增报价及原因 |
| `execution_history_incomplete` | 有持仓或挂单时出现缺口，缺口内成交无法确认 |
| `sample_intervals_ms`／`observed_hedge_wait_ms` | 实际采样间隔／观察到的模拟对冲等待时间 |
| `events` | 每次报价、撤单、假设成交、对冲及风险事件 |
| `settings`／`metadata`／`input_sha256` | 配置、录制元数据及输入哈希，供复现 |

**这是离散盘口假设模型，不是实盘收益证明。** 缺少真实排队、逐笔主动成交和样本间行情，穿越模型既可能漏记也可能误记成交。末尾不自动平仓：多仓按该平台最优买价、空仓按最优卖价给账面估值，未保证完整退出深度，未扣未来平仓成本。没有保证金或强平仿真，USD与USDC按1:1处理。

Lighter 所用盘口接口没有可靠源时间，仅保存接收时间；不能证明源报价新鲜。Ondo 保存接口提供的时间。网络错误、限流和无效盘口写入失败样本，不能用旧盘口冒充新行情。

## 服务器部署

`collect` 是 Linux 常驻公开行情服务：一条 Ondo WS 订阅全市场盘口和成交；焦点标的保存降采样全量盘口，所有可交易市场每分钟保存竞争指标；成交全部保存。币安只订阅焦点标的的 aggTrade，Lighter 使用 REST 轮询。Ondo WS 断档时启动全市场 REST 后备扫描。数据按 UTC 小时写入 `data/collect/{source}/{YYYYMMDDTHH}Z-{run_id}.jsonl.gz`，健康统计每分钟写入 `log/collect_{YYYYMMDD}.log`。

以 Debian／Ubuntu 为例，先安装 Python 3.11 和 venv 支持（发行版仓库须提供对应包；已有更新的 Python 也可使用）：

```bash
sudo apt-get update
sudo apt-get install python3.11 python3.11-venv rsync

# 在项目代码目录执行；创建专用的非 root 服务用户。
sudo useradd --system --create-home --home-dir /opt/pendle --shell /usr/sbin/nologin pendle
sudo rsync -a --exclude data/ --exclude log/ --exclude .venv/ ./ /opt/pendle/
sudo chown -R pendle:pendle /opt/pendle
sudo -u pendle python3.11 -m venv /opt/pendle/.venv
cd /opt/pendle
sudo -u pendle .venv/bin/python -m pip install '.[live]'
sudo -u pendle cp collect.example.toml collect.toml
sudo -u pendle mkdir -p data/collect log
```

修改 `/opt/pendle/collect.toml`。相对路径以工作目录为准。示例默认焦点标的为 INTC、DRAM、CRCL、WEN、NKE、RDDT、IBM、DKNG；符号必须大写。配置键 `focus_markets` 兼容旧名 `focus_symbols`。同名资产需人工核实，币安符号默认 `{SYM}USDT`，可在 `[symbol_overrides.INTC]` 等表中覆盖。

| 配置项 | 默认值与作用 |
|---|---|
| `focus_snapshot_s` | 1 秒，焦点市场全量盘口的写盘间隔 |
| `other_snapshot_s` | 60 秒，全市场派生竞争记录的采样间隔 |
| `lighter_poll_s` | 2 秒，焦点标的 Lighter REST 轮询间隔 |
| `contracts_interval_s`／`rest_scan_interval_s` | 300／60 秒，合约快照／Ondo 断档后备扫描间隔 |
| `silence_timeout_s` | 30 秒，按来源、市场、频道检测静默并记录断档 |
| `ondo_ping_s`／`binance_reconnect_s` | 30／82800 秒，Ondo 心跳／币安 23 小时主动重连 |
| `max_http_workers`／`write_queue_size` | 4／1024，HTTP 并发上限／写盘队列容量 |
| `http_timeout_s`／`request_interval_ms` | 5 秒／250 毫秒，HTTP 超时／请求节流间隔 |
| `data_dir`／`log_dir` | `data/collect`／`log`，采集数据和健康日志目录 |
| `max_disk_gb` | 20，采集目录累计磁盘用量上限；达到后冲刷并以 75 退出，不删数据 |
| `band_bps`／`depth`／`max_tick_depth` | 25 bps／100 档／10 个 tick，竞争指标计算范围 |
| `price_tick`／`quantity_step` | 0.01／0.1，默认精度；按标的通过 `symbol_overrides` 覆盖 |

先前台运行 `sudo -u pendle .venv/bin/python -m pendle collect --config collect.toml` 检查环境，按 Ctrl-C 冲刷退出。未安装可选依赖时，`collect` 输出中文安装提示并返回 2，其他命令仍可运行。服务无需开放入站端口。

编辑 `deploy/pendle-collect.service`，把 `User`／`Group` 改为实际非 root 用户，把 `WorkingDirectory` 和 `ExecStart` 中所有 `/PATH/TO/pendle` 替换为 `/opt/pendle`，然后启用：

```bash
sudo cp deploy/pendle-collect.service /etc/systemd/system/pendle-collect.service
sudo systemctl daemon-reload
sudo systemctl enable --now pendle-collect
sudo systemctl status pendle-collect
sudo journalctl -u pendle-collect -f
tail -F log/collect_*.log
```

日志包含各来源消息数、最近有效消息年龄、重连次数和写盘队列长度；持续增长的队列或消息年龄需要检查。systemd 对一般退出使用 `Restart=always`；退出码 75 受 `RestartPreventExitStatus=75` 保护，达到磁盘上限后需归档数据或调整上限，再手动 `systemctl start pendle-collect`。此上限只约束采集目录的累计用量，应为系统其他数据和日志预留磁盘空间。

磁盘占用取决于盘口大小、成交活跃度和压缩率。仅以 8 个焦点市场、每份盘口 4 KiB、每秒一份估算，全量盘口未压缩约 2.6 GiB／天；这不含成交、Lighter 和派生指标，也不代表 gzip 文件大小。先采集 10 分钟，比较 `du -sb data/collect` 的增量，再按实际增量 × 144 估算每天用量并留余量。运行标记文件也在采集目录中，不要在服务运行时改动或删除。

停止与升级时先等待优雅退出；SIGTERM／SIGINT 会冲刷队列并关闭 gzip 文件：

```bash
sudo systemctl stop pendle-collect
sudo systemctl is-active pendle-collect
# 同步新代码时保留 collect.toml、data/、log/ 和 .venv/，再更新依赖。
cd /opt/pendle
sudo -u pendle .venv/bin/python -m pip install --upgrade '.[live]'
# 若修改过单元文件，重新复制并执行 daemon-reload。
sudo systemctl start pendle-collect
```

拉回本地时，可先停止服务获得完整小时文件；服务持续运行时只复制已轮转的文件，稍后再次同步当前小时。导入可重复执行，成交和竞争记录不会重复，gzip 尾部截断时保留可恢复的完整记录：

```bash
# 在本地项目目录执行，替换服务器 SSH 用户和主机。
rsync -av --partial 'user@server:/opt/pendle/data/collect/' data/collect/
python3 -m pendle import-collected --src data/collect --db data/history.sqlite
python3 -m pendle backtest --db data/history.sqlite --config backtest.example.toml \
  --symbols INTC,DRAM,CRCL,WEN,NKE,RDDT,IBM,DKNG \
  --start 2026-11-04T00:00:00Z --end 2026-11-05T00:00:00Z \
  --report data/collected-backtest.json --summary data/collected-backtest.md
```

把回测起止时间改为实际采集区间；区间前应有足够成交用于基差预热。`import-collected` 会把成交、派生竞争记录、合约快照和断档导入历史数据库。回测在相关断档期间不新挂单，报告列出断档时长；断档内已有持仓或挂单时，损益标记为不可判定。

Linux 上执行 10 分钟真实 WS 烟测可使用 GNU `timeout`；正常到时发送 SIGTERM 后，`timeout` 返回 124：

```bash
timeout --signal=TERM --kill-after=90s 10m \
  .venv/bin/python -m pendle collect --config collect.toml
python3 -m pendle import-collected --src data/collect --db data/collect-smoke.sqlite
tail -n 12 log/collect_*.log
du -sh data/collect
```

## 测试与规范

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pendle tests
OPENSPEC_TELEMETRY=0 openspec validate add-ondo-paper-research --strict --no-interactive
```

测试不请求外网、不绑定端口；HTTP 与 WS 通过内存传输或假对象验证。真实公开行情烟测单独运行 `record`。录制数据与生成报告保存在 `data/`，已被 Git 忽略。

设计、实施计划与两个 agent 的提案审核记录在 `docs/`；OpenSpec 提案位于 `openspec/changes/add-ondo-paper-research/`。

官方接口参考：

- [Ondo 盘口](https://docs.ondoperps.xyz/api-reference/market-data/get-order-book-depth)
- [Lighter 订单簿](https://docs.lighter.xyz/api/orders/orderbookorders)
- [Lighter 费用和延迟](https://docs.lighter.xyz/trading/trading-fees)

## 实盘做市机器人（M2）

首版只运行单市场，示例为 `IBM-USD.P`。复制 `maker.example.toml` 为 `maker.toml` 后先使用默认 `dry-run`：此模式只读取公开行情；有 API 密钥时可读取私有状态，完全不发写请求，也不开死人开关。`sandbox` 会在沙盒中执行写操作；沙盒 REST／WS 地址仍待核实。`live` 要求配置 `mode = "live"`、显式 `--live`、守卫完成标定、沙盒验收完成、Telegram 自检成功、无熔断锁、NTP 同步且 REST 响应头 Date 偏差不超过 2 秒，并配置外部健康心跳。

必须使用专用的单市场账户，账户不能有其他市场持仓或其他市场挂单；不要在同一账户手动交易或运行其他策略。对账或发单前发现其他市场非零持仓时会熔断并拒绝新增。全市场挂单查询尚未核实，因此上线前必须人工确认其他市场没有挂单。

API 密钥只开交易权限，设置 IP 白名单（填写运行机器人的服务器公网 IP），**不开提现权限**。沙盒与正式密钥分别保存，禁止把密钥、签名、Telegram token 或健康心跳访问标识提交到版本库。

在服务器 `/home/ubuntu/pendle/secrets/maker.env` 保存 systemd 环境变量，文件内容如下（替换占位值，不要在终端打印真实内容）：

```dotenv
ONDO_API_KEY=替换为本环境的API密钥
ONDO_API_SECRET=替换为本环境的API密钥内容
TELEGRAM_BOT_TOKEN=替换为Telegram机器人token
TELEGRAM_CHAT_ID=替换为Telegram聊天ID
```

```bash
chmod 700 /home/ubuntu/pendle/secrets
chmod 600 /home/ubuntu/pendle/secrets/maker.env
```

也可设置 `secret_file` 和 `telegram_secret_file` 指向权限严格为 **600** 的 JSON 普通文件；两者可以共用一份 `secrets/maker.json`。JSON 格式是 `{"key":"占位密钥","secret":"占位密钥内容","telegram_bot_token":"占位token","telegram_chat_id":"占位chat_id"}`。默认示例注释了这两个路径，因此读取环境变量。环境变量文件和 JSON 密钥文件格式不同，不要把 `.env` 传给 `secret_file`。密钥文件权限过宽、文件损坏或告警自检失败都会拒绝 live。

前台验证与模式切换：

```bash
cd /home/ubuntu/pendle
cp maker.example.toml maker.toml
.venv/bin/python -m pip install '.[live]'
# 向当前 shell 导入本次运行的密钥；配置 secret_file 时可省略。
set -a; . secrets/maker.env; set +a
# 按配置选择环境；默认 dry-run 不写入交易所。
.venv/bin/python -m pendle maker --config maker.toml
# 编辑 mode="sandbox" 后使用同一命令运行沙盒，切换到沙盒密钥。
# 完成全部验收及标定，配置 mode="live"、标定/验收标志及正式密钥后：
.venv/bin/python -m pendle maker --config maker.toml --live
```

前台进程用 SIGTERM／Ctrl-C 停止：禁止新增订单，处理在途订单，按市场全部撤单，并核实挂单为零，再关闭死人开关。实盘服务必须由非 root 用户 `ubuntu` 运行；`deploy/pendle-maker.service` 使用 `/home/ubuntu/pendle`，且 `ExecStart` 显式带 `--live`，仅在正式验收完成后安装：

```bash
sudo cp deploy/pendle-maker.service /etc/systemd/system/pendle-maker.service
sudo systemctl daemon-reload
sudo systemctl enable --now pendle-maker
sudo systemctl status pendle-maker
sudo systemctl stop pendle-maker
```

日志同时写入标准错误和配置的 `data_dir/maker.log`，每分钟记录健康状态；前台运行可直接查看终端，systemd 服务可使用 `sudo journalctl -u pendle-maker -f`。启动失败会输出脱敏后的原因。持仓、窗口盈亏、熔断状态等持久记录在配置的 `data_dir`，默认 `/home/ubuntu/pendle/data/maker/`。不要在服务运行时删除账户锁、熔断锁或窗口盈亏文件，也不要以新账户别名绕过已有锁。

应急时先停止服务，再在相同工作目录、相同运行账户、相同配置和密钥环境下撤单。撤单命令不会自动平掉持仓；撤单结果不确定时应立即在交易所核查，并等待死人开关兜底。`maker-cancel-all` 与人工复位都使用账户实例锁，运行中的机器人尚未退出时会拒绝执行，避免继续补单。

```bash
sudo systemctl stop pendle-maker
sudo systemctl is-active pendle-maker
# 当前 shell 需有同一环境变量，或在配置中使用权限 600 的 JSON 密钥文件。
.venv/bin/python -m pendle maker-cancel-all --config maker.toml
# 核查交易所挂单与持仓、排除熔断原因后，人工复位；保留窗口累计亏损记录。
.venv/bin/python -m pendle maker --config maker.toml --reset-halt
sudo systemctl start pendle-maker
```

`maker --reset-halt` 必须带原实例的 `--config`；默认 dry-run 禁止复位，应急命令不会改变原配置选择的正式／沙盒环境。应急加载允许跳过标定、验收标志和外部心跳前置条件，仍会校验安全地址及金额硬上限；正常 live 启动继续要求全部前置条件通过。熔断退出码为 76，systemd 的 `RestartPreventExitStatus=76` 会阻止自动重启；其他失败最多在 600 秒内重启三次。

配置全部位于 `[maker]` 表，未知字段会报错。金额与 bps 可写十进制字符串，秒数写数字；`maker.example.toml` 列出默认值与待标定注释：

| 用途 | 配置项 |
|---|---|
| 模式、账户与文件 | `mode`、`market`、`account`、`data_dir`、`secret_file`、`telegram_secret_file`、`healthcheck_url` |
| 金额与风险 | `quote_notional`、`soft_cap_usd`、`hard_cap_usd`、`max_position_usd`、`max_account_exposure_usd`、`max_margin_ratio`、`max_window_loss_usd`、`emergency_flatten_loss_usd` |
| 报价与重报 | `offset_bps`、`skew_bps`、`min_offset_bps`、`band_bps`、`band_margin_bps`、`requote_bps`、`min_requote_interval_s` |
| 参考价与守卫 | `anchor_max_age_s`、`anchor_ema_alpha`、`max_anchor_drift_bps`、`max_ext_drift_bps`、`max_ext_age_s`、`max_mark_age_s`、`max_book_age_s`、`max_spread_bps`、`jump_window_s`、`jump_bps`、`cooldown_s`、`stable_s` |
| 判据与对账 | `gate_interval_s`、`gate_comp_max_usd`、`reconcile_interval_s`、`quiet_s` |
| 请求与死人开关 | `request_timeout_s`、`cancel_confirm_timeout_s`、`rate_per_second`、`deadman_timeout_s`、`deadman_ack_match` |
| 行情来源与地址 | `mark_channel`、`binance_symbol`、`binance_ws_url`、`prod_rest_url`、`prod_ws_url`、`sandbox_rest_url`、`sandbox_ws_url` |
| 验收标志 | `guards_calibrated`、`sandbox_verified`、`reduce_only_gtc_verified` |
| 单一周窗口 | `window_start_day`、`window_start_time`、`window_end_day`、`window_end_time`、`timezone` |
| 减仓与消歧预算 | `max_flatten_slippage_bps`、`flatten_attempts`、`flatten_timeout_s`、`unknown_query_attempts`、`unknown_query_interval_s`、`unknown_timeout_s` |
| 循环与健康日志 | `strategy_interval_s`、`health_log_interval_s` |

窗口默认北京时间周六 09:00 至周一 05:00，最后 60 分钟只减仓。每侧默认 $400，偏移 12±4 bps，允许距离 [8,22] bps，软／硬持仓阈值为 $300／$600。单侧 $800、持仓 $1200、账户 $1500 是代码硬上限，不能配置绕过。`gate_comp_max_usd=3500` **待按实盘规则重跑回测标定**；守卫阈值均为保守占位，未标定禁止 live。价格 tick、数量最小单位及最小金额从市场元数据读取。

待沙盒核实的重点包括：沙盒地址和连接数上限、`markPricesPerps` 频道及消息格式、私有订单／成交／持仓／余额消息格式、`deadman_ack_match` 的 ACK 规则、死人开关按账户覆盖与重连行为、ping／REST／公开连接消息是否续期、`kill -9` 和断网后撤单时限、reduceOnly 配 GTC、1 次／秒限流是否覆盖按市场全部撤单、429 与 Retry-After 格式、写请求超时或 5xx 后 clientOrderId 消歧、重复 clientOrderId、撤单延迟及撤单期间成交、盘口推送频率、$400／$800 下实际保证金占用与 marginRatio 量纲、手续费及资金费符号、NTP 与 Date 校时。全部确认后再设置验收标志；正式环境还需以每侧 $50 运行至少 2 小时，确认无未处理 UNKNOWN、无遗留挂单、正式死人开关有效且奖励页面有资格。
