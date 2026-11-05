"""Telegram alerts and external health heartbeats with caller-injected HTTP."""

import asyncio
import json
import logging
import math
import os
import re
import stat
import time
from collections import Counter, deque
from dataclasses import dataclass

from .ondo_api import _read_secret_file


@dataclass(frozen=True, repr=False)
class TelegramCredentials:
    bot_token: str
    chat_id: str

    def __post_init__(self):
        if not all(isinstance(value, str) and value.strip()
                   for value in (self.bot_token, self.chat_id)):
            raise ValueError('Telegram token 和 chat_id 必须配置')

    def __repr__(self):
        return 'TelegramCredentials(bot_token=***, chat_id=***)'


def load_telegram_credentials(*, env=None, secret_file=None, file_reader=_read_secret_file):
    """Read environment variables or the mode-600 JSON credentials file shared with Ondo."""
    env = os.environ if env is None else env
    if secret_file is None:
        return TelegramCredentials(env.get('TELEGRAM_BOT_TOKEN'), env.get('TELEGRAM_CHAT_ID'))
    try:
        mode, content = file_reader(secret_file)
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600 or len(content) > 65536:
            raise ValueError
        payload = json.loads(content)
        return TelegramCredentials(payload['telegram_bot_token'], payload['telegram_chat_id'])
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError('Telegram 密钥文件无效，必须使用权限为 600 的 JSON 普通文件') from None


class Alerter:
    """Use one monotonic time base for all timings; fail safely without HTTP."""

    def __init__(self, credentials=None, *, http=None, monotonic=time.monotonic,
                 healthcheck_url=None, logger=None, alert_min_interval_s=1800.0,
                 summary_interval_s=14400.0, market=''):
        self._credentials = credentials
        self._http, self._monotonic = http, monotonic
        self._healthcheck_url = healthcheck_url
        self.logger = logger or logging.getLogger(__name__)
        self.alert_min_interval_s = alert_min_interval_s
        self.summary_interval_s = summary_interval_s
        self.market = market
        self._last_alert_attempt = None
        self._pending = {}
        self._ordinary_task = None
        self._background_tasks = set()
        self._urgent_tasks = {}
        self._started = None
        self._last_observed = None
        self._previous_qualified = False
        self._qualification = deque()
        self.qualification_ratio = None
        self._last_low_qualification_log = None
        self._last_summary = None
        self._summary_counts = Counter()
        self._summary_baseline = None
        self._period_qualified_seconds = 0.0
        self._last_healthcheck_attempt = None

    def _redact(self, text):
        text = str(text)
        if self._credentials:
            for value in (self._credentials.bot_token, self._credentials.chat_id):
                text = text.replace(value, '***')
        return re.sub(r'(?i)(["\']?(?:sign|signature|ONDO-SIGN)["\']?\s*[:=]\s*["\']?)'
                      r'[0-9a-f]{64}', r'\1***', text)

    async def _request(self, method, url, *, payload=None):
        if self._http is None:
            return False
        try:
            response = await self._http(method, url, json=payload, timeout=10)
            status = getattr(response, 'status', 200 if isinstance(response, dict) else 0)
            body = getattr(response, 'payload', response)
            return 200 <= status < 300 and (
                method == 'GET' or isinstance(body, dict) and body.get('ok') is True)
        except Exception:
            return False

    async def _telegram(self, text):
        if self._credentials is None:
            return False
        if self.market:
            text = f'[{self.market}] ' + str(text)
        url = 'https://api.telegram.org/bot' + self._credentials.bot_token + '/sendMessage'
        success = await self._request('POST', url, payload={
            'chat_id': self._credentials.chat_id, 'text': self._redact(text)[:4096]})
        if not success:
            self.logger.warning('Telegram 告警发送失败')
        return success

    async def self_test(self):
        """Live startup must check the result and refuse to start on failure."""
        return await self._telegram('实盘做市告警通道自检')

    async def alert(self, reason, *, key=None, urgent=False):
        """Log gate observations, queue ordinary alerts, and await separate HTTP for urgent alerts."""
        if urgent:
            task = asyncio.create_task(self._telegram('紧急告警：' + str(reason)))
            self._urgent_tasks[task] = self._monotonic() + 10
            task.add_done_callback(lambda completed: self._urgent_tasks.pop(completed, None))
            # A wait timeout or caller cancellation leaves HTTP running; shutdown uses the same deadline.
            done, _ = await asyncio.wait((task,), timeout=10)
            return task.result() if done else False
        if key in ('判据变化', 'low-qualification'):
            self._summary_counts[key] += 1
            if key == 'low-qualification':
                now = self._monotonic()
                if (self._last_low_qualification_log is not None
                        and now - self._last_low_qualification_log < 600):
                    return False
                self._last_low_qualification_log = now
            self.logger.info(self._redact(reason))
            return False
        self._pending[str(key if key is not None else reason)] = str(reason)
        return self._schedule_alerts()

    def _start_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task):
        self._background_tasks.discard(task)
        if task is self._ordinary_task:
            self._ordinary_task = None
        if not task.cancelled() and task.exception() is not None:
            self.logger.warning('后台告警发送异常')

    async def flush_urgent(self, *, timeout_s=10):
        """Wait for urgent HTTP within its remaining shutdown budget, without restarting the ten-second timeout."""
        await asyncio.sleep(0)
        tasks = tuple(self._urgent_tasks)
        if not tasks:
            return True
        remaining = max(self._urgent_tasks[task] for task in tasks) - self._monotonic()
        _, pending = await asyncio.wait(tasks,
            timeout=max(0, min(float(timeout_s), remaining, 10)))
        return not pending

    def _schedule_alerts(self):
        now = self._monotonic()
        sending = self._ordinary_task is not None and not self._ordinary_task.done()
        limited = (self._last_alert_attempt is not None
                   and now - self._last_alert_attempt < self.alert_min_interval_s)
        if sending or not self._pending or limited:
            return False
        self._last_alert_attempt = now
        batch, self._pending = self._pending, {}
        self._ordinary_task = self._start_background(self._send_alert_batch(batch))
        return True

    async def _send_alert_batch(self, batch):
        success = False
        try:
            success = await self._telegram('做市告警：\n' + '\n'.join(batch.values()))
        finally:
            if not success:
                # Restore only this batch; newer content for the same key always takes precedence.
                self._pending = {**batch, **self._pending}

    @staticmethod
    def _summary_snapshot(metrics):
        """Copy cumulative observations so later mutations cannot change period baselines."""
        return {'qualified': metrics.get('有资格时长秒'),
                'elapsed': metrics.get('观测时长秒'),
                'fills': metrics.get('成交数', 0),
                'exits': {reason: metrics.get('守卫及停报原因', {}).get(reason, {}).get('次数', 0)
                          for reason in ('持仓超时离场', '止损离场')},
                'pauses': {reason: row.get('时长秒', 0)
                           for reason, row in metrics.get('守卫及停报原因', {}).items()}}

    def _format_summary(self, metrics, snapshot, now):
        """Render at most eight lines, using deltas for period counts and durations."""
        baseline = self._summary_baseline
        elapsed = now - self._last_summary
        qualified = self._period_qualified_seconds
        if all(row[key] is not None for row in (baseline, snapshot)
               for key in ('qualified', 'elapsed')):
            elapsed = max(0, snapshot['elapsed'] - baseline['elapsed'])
            qualified = max(0, snapshot['qualified'] - baseline['qualified'])
        ratio = f'{min(1, qualified / elapsed):.1%}' if elapsed > 0 else '未知'
        pauses = [(reason, seconds - baseline['pauses'].get(reason, 0))
                  for reason, seconds in snapshot['pauses'].items()]
        pauses = sorted(((reason, seconds) for reason, seconds in pauses if seconds > 0),
                        key=lambda row: (-row[1], row[0]))[:3]

        def text(value):
            value = '未知' if value is None else ' '.join(self._redact(value).split())
            return value if len(value) <= 160 else value[:159] + '…'

        stops = '；'.join(f'{text(reason)} {seconds:.0f}秒' for reason, seconds in pauses) or '无'
        fills = max(0, snapshot['fills'] - baseline['fills'])
        exits = '；'.join(f'{reason}：{max(0, count-baseline["exits"].get(reason, 0))}次'
                         for reason, count in snapshot['exits'].items()
                         if count > baseline['exits'].get(reason, 0))
        weight = metrics.get('竞争量权重', 'flat')
        competition = metrics.get('竞争量' + weight)
        gate = metrics.get('当前判据', metrics.get('判据'))
        gate_text = '通过' if gate is True or gate == '通过' else (
            '不通过' if gate is False or gate == '不通过' else '未知')
        lines = ['做市摘要：',
                 f'持仓：{text(metrics.get("持仓"))}；未实现盈亏：{text(metrics.get("未实现盈亏"))} 美元',
                 f'窗口已实现盈亏：{text(metrics.get("窗口已实现盈亏"))}；'
                 f'窗口总盈亏：{text(metrics.get("窗口总盈亏"))} 美元',
                 f'本期有资格时长占比：{ratio}；判据变化：{self._summary_counts["判据变化"]}次；'
                 f'低资格观测：{self._summary_counts["low-qualification"]}次',
                 '停报前三：' + stops,
                 f'本期成交：{fills}笔' + ('；' + exits if exits else ''),
                 f'竞争量（{text(weight)}）：{text(competition)} 美元；当前判据：{gate_text}']
        if metrics.get('外部心跳风险'):
            lines.append(text(metrics['外部心跳风险']))
        return '\n'.join(lines)

    async def observe(self, *, healthy, waiting, qualified, last_quote, summary=None):
        """Call each strategy cycle; waiting must reflect an explicit, explainable wait state."""
        now = self._monotonic()
        metrics = {} if summary is None else summary
        if self._started is None:
            self._started = self._last_summary = now
            self._summary_baseline = self._summary_snapshot(metrics)
        if self._last_observed is not None and now > self._last_observed:
            self._qualification.append((self._last_observed, now, self._previous_qualified))
            if self._previous_qualified:
                self._period_qualified_seconds += now - self._last_observed
        self._last_observed, self._previous_qualified = now, bool(qualified)
        cutoff = now - 1800
        while self._qualification and self._qualification[0][1] <= cutoff:
            self._qualification.popleft()
        elapsed = min(now - self._started, 1800)
        seconds = sum(end - max(start, cutoff) for start, end, eligible
                      in self._qualification if eligible)
        self.qualification_ratio = seconds / elapsed if elapsed > 0 else None
        if elapsed >= 1800 and self.qualification_ratio < 0.5:
            await self.alert('最近 30 分钟有资格挂单时长低于 50%', key='low-qualification')

        valid_quote = (isinstance(last_quote, (int, float)) and not isinstance(last_quote, bool)
                       and math.isfinite(last_quote) and last_quote <= now)
        age = now - (last_quote if valid_quote else self._started)
        if not waiting and age > 600:
            await self.alert('最近 10 分钟没有成功报价', key='no-successful-quote')
        self._schedule_alerts()

        if now - self._last_summary >= self.summary_interval_s:
            snapshot = self._summary_snapshot(metrics)
            self._start_background(self._telegram(self._format_summary(metrics, snapshot, now)))
            self._last_summary = now
            self._summary_baseline = snapshot
            self._period_qualified_seconds = 0.0
            self._summary_counts.clear()

        recent_quote = valid_quote and age <= 600
        due = self._last_healthcheck_attempt is None or now - self._last_healthcheck_attempt >= 60
        if self._healthcheck_url and healthy and (waiting or recent_quote) and due:
            self._last_healthcheck_attempt = now
            if not await self._request('GET', self._healthcheck_url):
                self.logger.warning('外部健康心跳发送失败')
