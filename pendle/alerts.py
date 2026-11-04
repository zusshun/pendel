"""Telegram 告警和外部健康心跳；HTTP 由调用方注入。"""

import asyncio
import json
import logging
import math
import os
import re
import stat
import time
from collections import deque
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
    """读取环境变量或与 Ondo 共用的权限 600 JSON 密钥文件。"""
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
    """时间参数均使用 monotonic 同一时基；没有 HTTP 时安全失败。"""

    def __init__(self, credentials=None, *, http=None, monotonic=time.monotonic,
                 healthcheck_url=None, logger=None):
        self._credentials = credentials
        self._http, self._monotonic = http, monotonic
        self._healthcheck_url = healthcheck_url
        self.logger = logger or logging.getLogger(__name__)
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
        self._last_summary = None
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
        url = 'https://api.telegram.org/bot' + self._credentials.bot_token + '/sendMessage'
        success = await self._request('POST', url, payload={
            'chat_id': self._credentials.chat_id, 'text': self._redact(text)[:4096]})
        if not success:
            self.logger.warning('Telegram 告警发送失败')
        return success

    async def self_test(self):
        """live 启动方必须检查返回值，失败时拒绝启动。"""
        return await self._telegram('实盘做市告警通道自检')

    async def alert(self, reason, *, key=None, urgent=False):
        """普通告警仅入队，返回是否启动发送；紧急告警等待独立的 HTTP。"""
        if urgent:
            task = asyncio.create_task(self._telegram('紧急告警：' + str(reason)))
            self._urgent_tasks[task] = self._monotonic() + 10
            task.add_done_callback(lambda completed: self._urgent_tasks.pop(completed, None))
            # wait 超时或调用者取消都不取消 HTTP；退出清理沿用同一个截止时间。
            done, _ = await asyncio.wait((task,), timeout=10)
            return task.result() if done else False
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
        """退出前等待紧急 HTTP 的剩余预算，不重复等待已耗尽的十秒。"""
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
                   and now - self._last_alert_attempt < 300)
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
                # 只恢复此批次，发送期间同 key 的较新内容始终优先。
                self._pending = {**batch, **self._pending}

    async def observe(self, *, healthy, waiting, qualified, last_quote, summary=None):
        """策略周期调用；waiting 必须来自可解释的明确等待状态。"""
        now = self._monotonic()
        if self._started is None:
            self._started = self._last_summary = now
        if self._last_observed is not None and now > self._last_observed:
            self._qualification.append((self._last_observed, now, self._previous_qualified))
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

        if now - self._last_summary >= 3600:
            self._last_summary = now
            metrics = {} if summary is None else summary
            self._start_background(self._telegram(
                '每小时摘要：\n' + json.dumps(metrics, ensure_ascii=False, default=str)))

        recent_quote = valid_quote and age <= 600
        due = self._last_healthcheck_attempt is None or now - self._last_healthcheck_attempt >= 60
        if self._healthcheck_url and healthy and (waiting or recent_quote) and due:
            self._last_healthcheck_attempt = now
            if not await self._request('GET', self._healthcheck_url):
                self.logger.warning('外部健康心跳发送失败')
