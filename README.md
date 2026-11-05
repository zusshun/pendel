# Ondo / Lighter Market-Making Research

The first version records public order books for a single symbol and runs offline simulations. The directory is named pendle, but the current research covers Ondo Perps and Lighter, with no connection to the Pendle protocol.

Requires Python 3.11+. Historical downloads and offline research use only the standard library; continuous live collection requires the optional `websockets` dependency. No API keys are needed. Alongside `record`, `replay`, and `demo`, the research tools provide historical imports, reward backtests, and the `collect` / `import-collected` commands. These tools do not place or cancel real orders, transfer funds, or access accounts.

## Quick Start

```bash
cd /Users/captain/python/codex/pendle

# Fully offline: generate synthetic demo market data and a simulation report.
python3 -m pendle demo --db data/demo.sqlite --report data/demo-report.json

# Replay the same session; the result should be identical.
python3 -m pendle replay --db data/demo.sqlite --report data/demo-replayed.json

# Record 60 seconds of real public market data (INTC by default).
python3 -m pendle record --config config.example.toml --seconds 60 --db data/intc.sqlite

# Read-only replay of the latest session; pass --run-id to pick one.
python3 -m pendle replay --db data/intc.sqlite --report data/intc-report.json
```

On normal completion, `record` prints the session ID, sample count, and failed sample count; Ctrl-C saves completed samples. The database retains multiple sessions without overwriting history. Recording defaults to 60 seconds and exits on Ctrl-C or when the duration ends, with no background process. An in-flight HTTP request may delay shutdown until its timeout.

Exit codes: 0 means the command completed with no failed recording samples; 1 means recording completed with failed samples; 2 indicates a configuration, startup, file, or data error; 75 means continuous collection reached its disk limit. Successful replay means a report was generated; check `halted` for risk stops. Startup metadata failures are also stored in the database, and sessions without valid market identity metadata cannot be replayed.

## Configuration

Copy `config.example.toml` and adjust it. The first version samples every 2 seconds by default. Ondo returns up to 50 price levels per side; Lighter returns up to 50 orders per side before aggregating orders at the same price, so the depth measures differ. Lighter market IDs are resolved dynamically; unknown contracts and contracts other than linear contracts with a multiplier of one are rejected.

- `quote_notional`: simulated quote notional per side; `quote_offset_bps`: distance from the Ondo midpoint.
- `price_tick` / `quantity_step`: simulated Ondo precision assumptions. Lighter hedge precision and minimum quantity/notional come from market metadata captured during recording.
- `cancel_latency_ms` / `hedge_latency_ms`: minimum wait assumptions, not execution timing guarantees. Sampling every 2 seconds cannot reconstruct market changes within 500 milliseconds.
- `max_inventory_usd`: limits each venue's inventory and potential Ondo inventory if all quotes on one side fill.
- `max_net_usd`: limits net exposure and potential net exposure from quotes on one side.
- `max_stale_ms` / `max_skew_ms`: thresholds for data age, gaps, and receive-time differences between sources.
- `max_basis_bps` / `max_jump_bps`: thresholds for cross-venue midpoint divergence and price jumps between consecutive observations.
- Fees and extra slippage follow the configuration; funding assumes a fixed hourly rate applied to each holding interval.
- `reward_usdc`: an explicit total-reward scenario; omitted means unknown. The tool neither estimates official reward shares nor annualizes returns.

Replay uses the recorded configuration by default. With `--config` for strategy comparisons, only quoting, latency, fee, reward, and risk assumptions may change. The symbol, Ondo precision, and sampling settings remain fixed to prevent mixing incompatible market data.

Replay override files need only contain changed keys, such as `reward_usdc = "5"`; other parameters retain their session values. Raw SQLite order books are read one at a time and hashed incrementally. Reports retain event details and sampling intervals, so memory still grows with event count. For long recordings, use separate hourly database files to manage disk usage and report size.

## Simulation Rules and Reports

Quotes are created only after market data from both venues is visible. Neither the creation-time snapshot nor later price touches count as fills. A later order book must strictly cross an existing quote to simulate a partial fill, booked at the original limit price. Orders can still fill while cancellation is pending. Hedges use opposing Lighter depth received after the wait period, with costs calculated level by level; earlier books cannot be used for execution.

Repeated unchanged order books do not replenish consumed quantity. Executions at the same source and price share remaining depth; only observed quantity increases replenish it. Quantity decreases reduce the available budget. A price level that disappears and returns is treated as newly visible liquidity, which remains a modeling assumption. If a hedge is below the contract's minimum order size, lacks depth, or exceeds the slippage budget, residual exposure remains and new quoting stops.

A risk stop stays latched until replay ends, blocks new quotes, and requests simulated cancellations. Subsequent valid market data only processes existing orders and reduces pending hedge risk. Cancellation does not close positions, and the loss budget is not a guaranteed maximum loss.

Key fields in the JSON report:

| Field | Meaning |
|---|---|
| `pnl_before_rewards_usdc` | Simulated P&L before rewards under the configured assumptions; null if required terminal valuation data is missing |
| `fees_usdc` / `funding_pnl_usdc` | Simulated fees / funding cash flows; funding payments are negative |
| `reward_scenario_usdc` / `scenario_pnl_usdc` | User-supplied reward scenario and resulting P&L; null by default |
| `break_even_reward_usdc` | Subsidy needed to cover the current simulated loss, excluding future exit costs |
| `positions` / `net_quantity` | Open quantities on both venues / net quantity |
| `pending_hedges` / `open_quotes` | Unexecuted hedges and open quotes, including pending cancellations |
| `halted` / `risk_reasons` | Whether new quoting has stopped and why |
| `execution_history_incomplete` | A gap occurred while positions or quotes were open; fills within the gap cannot be confirmed |
| `sample_intervals_ms` / `observed_hedge_wait_ms` | Actual sampling intervals / observed simulated hedge wait times |
| `events` | Every quote, cancellation, assumed fill, hedge, and risk event |
| `settings` / `metadata` / `input_sha256` | Configuration, recording metadata, and input hash for reproducibility |

**This model makes assumptions from discrete order book snapshots; it does not demonstrate live profitability.** Without real queue positions, individual aggressive trades, or market data between samples, the crossing model can miss fills or report false fills. Positions are not automatically closed at the end: longs are marked at the venue's best bid and shorts at its best ask, without ensuring sufficient exit depth or deducting future closing costs. Margin and liquidation are not simulated; USD and USDC are treated as 1:1.

The Lighter order book endpoint has no reliable source timestamp, so only receive time is stored; source quote freshness cannot be established. Ondo retains timestamps provided by its API. Network errors, rate limits, and invalid books are recorded as failed samples; old books are never presented as new market data.

## Server Deployment

`collect` is a continuous public market data service for Linux. One Ondo WS connection subscribes to order books and trades across all markets. It saves downsampled full books for focus symbols, competition metrics every minute for all tradable markets, and all trades. Binance subscriptions cover only aggTrade for focus symbols; Lighter uses REST polling. Ondo WS gaps trigger an all-market REST fallback scan. Data is written to hourly UTC files at `data/collect/{source}/{YYYYMMDDTHH}Z-{run_id}.jsonl.gz`; health statistics are written every minute to `log/collect_{YYYYMMDD}.log`.

On Debian / Ubuntu, install Python 3.11 and venv support first (the distribution's repositories must provide these packages; a newer installed Python also works):

```bash
sudo apt-get update
sudo apt-get install python3.11 python3.11-venv rsync

# Run from the project directory; create a dedicated non-root service user.
sudo useradd --system --create-home --home-dir /opt/pendle --shell /usr/sbin/nologin pendle
sudo rsync -a --exclude data/ --exclude log/ --exclude .venv/ ./ /opt/pendle/
sudo chown -R pendle:pendle /opt/pendle
sudo -u pendle python3.11 -m venv /opt/pendle/.venv
cd /opt/pendle
sudo -u pendle .venv/bin/python -m pip install '.[live]'
sudo -u pendle cp collect.example.toml collect.toml
sudo -u pendle mkdir -p data/collect log
```

Edit `/opt/pendle/collect.toml`. Relative paths use the working directory. The example focus symbols are INTC, DRAM, CRCL, WEN, NKE, RDDT, IBM, and DKNG; symbols must be uppercase. The `focus_markets` key supports the legacy name `focus_symbols`. Manually verify assets with matching names. Binance symbols default to `{SYM}USDT` and can be overridden in tables such as `[symbol_overrides.INTC]`.

| Setting | Default and purpose |
|---|---|
| `focus_snapshot_s` | 1 second; write interval for full focus-market order books |
| `other_snapshot_s` | 60 seconds; sampling interval for derived competition records across all markets |
| `lighter_poll_s` | 2 seconds; Lighter REST polling interval for focus symbols |
| `contracts_interval_s` / `rest_scan_interval_s` | 300 / 60 seconds; contract snapshots / fallback scans during Ondo gaps |
| `silence_timeout_s` | 30 seconds; detect silence and record gaps by source, market, and channel |
| `ondo_ping_s` / `binance_reconnect_s` | 30 / 82800 seconds; Ondo heartbeat / proactive Binance reconnection after 23 hours |
| `max_http_workers` / `write_queue_size` | 4 / 1024; HTTP concurrency limit / write queue capacity |
| `http_timeout_s` / `request_interval_ms` | 5 seconds / 250 milliseconds; HTTP timeout / request throttle interval |
| `data_dir` / `log_dir` | `data/collect` / `log`; collection data and health log directories |
| `max_disk_gb` | 20; total disk usage limit for the collection directory. Flush and exit with 75 when reached; retain data |
| `band_bps` / `depth` / `max_tick_depth` | 25 bps / 100 levels / 10 ticks; competition metric calculation range |
| `price_tick` / `quantity_step` | 0.01 / 0.1; default precision, overridden per symbol through `symbol_overrides` |

First check the environment in the foreground with `sudo -u pendle .venv/bin/python -m pendle collect --config collect.toml`; press Ctrl-C to flush and exit. If optional dependencies are missing, `collect` prints installation instructions in Chinese and returns 2; other commands remain available. The service needs no inbound ports.

Edit `deploy/pendle-collect.service`: set `User` / `Group` to the actual non-root user and replace every `/PATH/TO/pendle` in `WorkingDirectory` and `ExecStart` with `/opt/pendle`, then enable the service:

```bash
sudo cp deploy/pendle-collect.service /etc/systemd/system/pendle-collect.service
sudo systemctl daemon-reload
sudo systemctl enable --now pendle-collect
sudo systemctl status pendle-collect
sudo journalctl -u pendle-collect -f
tail -F log/collect_*.log
```

Logs include message counts per source, age of the latest valid message, reconnect counts, and write queue length. Investigate steadily growing queues or message ages. systemd uses `Restart=always` for ordinary exits; `RestartPreventExitStatus=75` blocks restart at the disk limit. Archive data or adjust the limit, then run `systemctl start pendle-collect` manually. This limit covers only total usage in the collection directory; reserve space for other system data and logs.

Disk usage depends on book size, trading activity, and compression. With 8 focus markets, 4 KiB per book, and one book per second, full books alone would use about 2.6 GiB per day uncompressed. This excludes trades, Lighter, and derived metrics, and does not estimate gzip size. Collect for 10 minutes, measure the increase with `du -sb data/collect`, then multiply by 144 to estimate daily usage and allow extra capacity. Run marker files also live in the collection directory; do not modify or delete them while the service is running.

Wait for graceful shutdown before stopping or upgrading; SIGTERM / SIGINT flush the queue and close gzip files:

```bash
sudo systemctl stop pendle-collect
sudo systemctl is-active pendle-collect
# When syncing new code, keep collect.toml, data/, log/ and .venv/, then update dependencies.
cd /opt/pendle
sudo -u pendle .venv/bin/python -m pip install --upgrade '.[live]'
# If the unit file changed, copy it again and run daemon-reload.
sudo systemctl start pendle-collect
```

For local downloads, stop the service first to obtain complete hourly files. If it stays running, copy rotated files and sync the current hour again later. Imports are repeatable without duplicating trades or competition records; complete recoverable records are retained if a gzip tail is truncated:

```bash
# Run from the local project directory; replace the SSH user and host of your server.
rsync -av --partial 'user@server:/opt/pendle/data/collect/' data/collect/
python3 -m pendle import-collected --src data/collect --db data/history.sqlite
python3 -m pendle backtest --db data/history.sqlite --config backtest.example.toml \
  --symbols INTC,DRAM,CRCL,WEN,NKE,RDDT,IBM,DKNG \
  --start 2026-11-04T00:00:00Z --end 2026-11-05T00:00:00Z \
  --report data/collected-backtest.json --summary data/collected-backtest.md
```

Set the backtest start and end times to the actual collection interval, with enough preceding trades to warm up the basis estimate. `import-collected` imports trades, derived competition records, contract snapshots, and gaps into the historical database. Backtests place no new quotes during relevant gaps and report gap duration. P&L is marked indeterminate if positions or quotes were open during a gap.

On Linux, use GNU `timeout` for a 10-minute live WS smoke test; after sending SIGTERM at the deadline, `timeout` returns 124:

```bash
timeout --signal=TERM --kill-after=90s 10m \
  .venv/bin/python -m pendle collect --config collect.toml
python3 -m pendle import-collected --src data/collect --db data/collect-smoke.sqlite
tail -n 12 log/collect_*.log
du -sh data/collect
```

## Tests and Specifications

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pendle tests
OPENSPEC_TELEMETRY=0 openspec validate add-ondo-paper-research --strict --no-interactive
```

Tests make no external network requests and bind no ports; HTTP and WS behavior is checked with in-memory transports or fakes. Run `record` separately for a real public market data smoke test. Recordings and generated reports are stored in `data/`, which Git ignores.

Design notes, implementation plans, and proposal reviews by two agents are in `docs/`; the OpenSpec proposal is in `openspec/changes/add-ondo-paper-research/`.

Official API references:

- [Ondo order book depth](https://docs.ondoperps.xyz/api-reference/market-data/get-order-book-depth)
- [Lighter order book](https://docs.lighter.xyz/api/orders/orderbookorders)
- [Lighter fees and latency](https://docs.lighter.xyz/trading/trading-fees)

## Live Market-Making Bot (M2)

The first version runs one market, with `IBM-USD.P` as the example. Copy `maker.example.toml` to `maker.toml` and start with the default `dry-run`. This mode reads public market data and, when API keys are available, private state; it sends no write requests and does not arm the dead man's switch. `sandbox` performs writes in the sandbox; its REST / WS endpoints still need verification. `live` requires `mode = "live"`, an explicit `--live`, calibrated guards, completed sandbox acceptance checks, a successful Telegram self-test, no halt lock, NTP synchronization, REST Date header skew within 2 seconds, and an external health heartbeat.

Use a dedicated single-market account with no positions or open orders in other markets. Do not trade manually or run other strategies in the same account. A nonzero position in another market detected before reconciliation or order submission triggers a halt and blocks new orders. The all-market open-order query remains unverified, so manually confirm that other markets have no open orders before launch.

Grant API keys trading permissions only and configure an IP allowlist using the bot server's public IP. **Do not enable withdrawals.** Store sandbox and production keys separately. Never commit keys, signatures, Telegram tokens, or health heartbeat access tokens to the repository.

Store systemd environment variables in `/home/ubuntu/pendle/secrets/maker.env` on the server, using the following format (replace placeholders; do not print real values in the terminal):

```dotenv
ONDO_API_KEY=<API key for this environment>
ONDO_API_SECRET=<API secret for this environment>
TELEGRAM_BOT_TOKEN=<Telegram bot token>
TELEGRAM_CHAT_ID=<Telegram chat id>
```

```bash
chmod 700 /home/ubuntu/pendle/secrets
chmod 600 /home/ubuntu/pendle/secrets/maker.env
```

Alternatively, point `secret_file` and `telegram_secret_file` to regular JSON files with permissions set to exactly **600**; both can share `secrets/maker.json`. The JSON format is `{"key":"<api key>","secret":"<api secret>","telegram_bot_token":"<bot token>","telegram_chat_id":"<chat id>"}`. Both paths are commented out in the default example, so environment variables are used. Environment files and JSON secret files use different formats; do not pass an `.env` file to `secret_file`. Overly permissive secret files, corrupt files, or failed alert self-tests prevent live startup.

Foreground verification and mode switching:

```bash
cd /home/ubuntu/pendle
cp maker.example.toml maker.toml
.venv/bin/python -m pip install '.[live]'
# Export this run's secrets into the current shell; skip this if secret_file is configured.
set -a; . secrets/maker.env; set +a
# The environment comes from the config; the default dry-run never writes to the exchange.
.venv/bin/python -m pendle maker --config maker.toml
# Set mode="sandbox" and use the same command with sandbox keys to run against the sandbox.
# After all acceptance checks and calibration, with mode="live", the calibration/acceptance flags and production keys set:
.venv/bin/python -m pendle maker --config maker.toml --live
```

Stop the foreground process with SIGTERM / Ctrl-C. It blocks new orders, handles in-flight orders, cancels all orders in the market, verifies zero open orders, then disarms the dead man's switch. With `sibling_markets` configured, it keeps the account-wide dead man's switch armed. The live service must run as the non-root user `ubuntu`. `deploy/pendle-maker.service` uses `/home/ubuntu/pendle` and explicitly passes `--live` in `ExecStart`; install it only after production acceptance checks are complete:

```bash
sudo cp deploy/pendle-maker.service /etc/systemd/system/pendle-maker.service
sudo systemctl daemon-reload
sudo systemctl enable --now pendle-maker
sudo systemctl status pendle-maker
sudo systemctl stop pendle-maker
```

To run RDDT on the same account, upgrade IBM first and enable the commented sibling keys in `maker.toml`, pointing at RDDT's heartbeat. Copy `maker-rddt.example.toml` to `maker-rddt.toml`, complete calibration and live acceptance, and confirm the two heartbeat mappings match. Keep IBM's `data_dir="data/maker"` and RDDT's `data_dir="data/maker-rddt"` separate so their instance locks, halt locks, and loss ledgers cannot overwrite each other. Install `deploy/pendle-maker-rddt.service` using the same commands above with `pendle-maker-rddt` as the service name, then start RDDT only after IBM runs the upgraded configuration. Use each instance's original configuration for `maker-cancel-all` and `--reset-halt` (for RDDT, `--config maker-rddt.toml`).

Sibling limits are static per instance: keep the sums of both `max_position_usd` budgets and both `max_account_exposure_usd` budgets within the account's capacity; margin ratio remains account-wide. Sibling mode excludes account-level funding from window loss ledgers. REST limits apply per API key: use separate trading keys, or set `rate_per_second=0.5` in both configurations when sharing one key. To select separate keys, configure `secret_file` and matching instance key environments for emergency commands; the supplied service units share `secrets/maker.env` by default. A stale peer is cancelled and checked for zero orders; failed peer cancellation halts the surviving instance and stops its pings while keeping the dead man's switch armed.

Logs go to both standard error and the configured `data_dir/maker.log`, with health status recorded every minute. Watch the terminal in foreground mode or use `sudo journalctl -u pendle-maker -f` for systemd. Startup failures report sanitized reasons. Positions, window P&L, halt state, and other persistent records are stored in the configured `data_dir`, defaulting to `/home/ubuntu/pendle/data/maker/`. Do not delete account locks, halt locks, or window P&L files while the service is running, or bypass existing locks with a new account alias.

In an emergency, stop the service first, then cancel orders from the same working directory, under the same user, with the same configuration and key environment. The cancellation command does not close positions. If cancellation is uncertain, check the exchange immediately and wait for the dead man's switch as a fallback. Both `maker-cancel-all` and manual reset acquire the account instance lock and refuse to run until the bot exits, preventing further quote replenishment.

```bash
sudo systemctl stop pendle-maker
sudo systemctl is-active pendle-maker
# The current shell needs the same environment variables, or use a JSON secret file with permission 600 in the config.
.venv/bin/python -m pendle maker-cancel-all --config maker.toml
# After checking exchange orders and positions and resolving the halt cause, reset manually; the window loss ledger is kept.
.venv/bin/python -m pendle maker --config maker.toml --reset-halt
sudo systemctl start pendle-maker
```

`maker --reset-halt` requires the original instance's `--config`; reset is blocked in the default dry-run mode. Emergency commands retain the production / sandbox environment selected by the configuration. Emergency loading may skip calibration, acceptance flags, and external heartbeat prerequisites, but still validates safe endpoints and hard amount limits. Normal live startup requires every prerequisite. A halt exits with code 76, and systemd's `RestartPreventExitStatus=76` prevents automatic restart; other failures allow at most three restarts within 600 seconds.

All settings belong in the `[maker]` table; unknown fields are rejected. Amounts and bps may be decimal strings, while seconds use numbers. `maker.example.toml` lists defaults and notes on values requiring calibration:

| Purpose | Settings |
|---|---|
| Mode, account, and files | `mode`, `market`, `account`, `data_dir`, `secret_file`, `telegram_secret_file`, `healthcheck_url` |
| Sibling instances and heartbeat guards | `sibling_markets`, `heartbeat_path`, `peer_heartbeat_paths`, `peer_stale_s`, `peer_cancel_timeout_s`, `stop_sweep_interval_s` |
| Amounts and risk | `quote_notional`, `soft_cap_usd`, `hard_cap_usd`, `max_position_usd`, `max_account_exposure_usd`, `max_margin_ratio`, `max_window_loss_usd`, `emergency_flatten_loss_usd` |
| Quoting and requoting | `offset_bps`, `skew_bps`, `min_offset_bps`, `band_bps`, `band_margin_bps`, `requote_bps`, `min_requote_interval_s` |
| Reference prices and guards | `anchor_max_age_s`, `anchor_ema_alpha`, `max_anchor_drift_bps`, `max_ext_drift_bps`, `max_ext_age_s`, `max_mark_age_s`, `max_book_age_s`, `max_spread_bps`, `jump_window_s`, `jump_bps`, `cooldown_s`, `stable_s` |
| Gate and reconciliation | `gate_interval_s`, `gate_comp_max_usd`, `reconcile_interval_s`, `quiet_s` |
| Requests and dead man's switch | `request_timeout_s`, `cancel_confirm_timeout_s`, `rate_per_second`, `deadman_timeout_s`, `deadman_ack_match` |
| Market data sources and endpoints | `mark_channel`, `binance_symbol`, `binance_ws_url`, `prod_rest_url`, `prod_ws_url`, `sandbox_rest_url`, `sandbox_ws_url` |
| Acceptance flags | `guards_calibrated`, `sandbox_verified`, `reduce_only_gtc_verified` |
| Single weekly window | `window_start_day`, `window_start_time`, `window_end_day`, `window_end_time`, `timezone` |
| Position reduction and ambiguity resolution budgets | `max_flatten_slippage_bps`, `flatten_attempts`, `flatten_timeout_s`, `unknown_query_attempts`, `unknown_query_interval_s`, `unknown_timeout_s` |
| Loop and health logging | `strategy_interval_s`, `health_log_interval_s` |

The default window runs from Saturday 09:00 to Monday 05:00 Beijing time, with position reduction only in the final 60 minutes. Defaults are $400 per side, offsets of 12±4 bps within [8,22] bps, and soft / hard position thresholds of $300 / $600. Hard limits of $800 per side, $1200 per position, and $1500 per account are enforced in code and cannot be bypassed through configuration. `gate_comp_max_usd=3500` **requires calibration by rerunning backtests under live trading rules**. Guard thresholds are conservative placeholders; live mode is blocked until calibration. Price ticks, quantity increments, and minimum notional come from market metadata.

Key sandbox checks include endpoint addresses and connection limits; the `markPricesPerps` channel and message format; private order, fill, position, and balance formats; ACK rules for `deadman_ack_match`; account-wide dead man's switch coverage and reconnection behavior; whether ping, REST, or public connection messages renew it; cancellation deadlines after `kill -9` or network loss; reduceOnly with GTC; whether the one-request-per-second limit includes market-wide cancellation; 429 and Retry-After formats; clientOrderId ambiguity resolution after write timeouts or 5xx responses; duplicate clientOrderId handling; cancellation latency and fills during cancellation; order book update frequency; actual margin usage at $400 / $800 and marginRatio units; fee and funding signs; and NTP / Date clock checks. Set acceptance flags only after all checks pass. In production, also run at $50 per side for at least 2 hours and confirm no unresolved UNKNOWN states, no leftover orders, a working production dead man's switch, and eligibility on the rewards page.
