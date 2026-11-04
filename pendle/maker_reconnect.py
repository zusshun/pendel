"""Back off feeds independently, redact logs, and merge urgent alerts within the same failure episode."""

import logging
import random
from dataclasses import dataclass


@dataclass
class ReconnectBackoff:
    failures: int = 0
    connected_at: float | None = None

    def reset_if_stable(self, now):
        if self.connected_at is None or now - self.connected_at < 60:
            return False
        self.failures, self.connected_at = 0, None
        return True

    def failed(self):
        self.connected_at = None
        self.failures += 1
        # Bound exponent computation so prolonged outages cannot build ever-larger integers.
        base = min(60, 3 * 2 ** min(self.failures - 1, 5))
        return min(60, base + random.uniform(-base * .2, base * .2))


class ReconnectFailures:
    """Track sources independently; send at most one urgent alert per unresolved failure episode."""

    def __init__(self, maker):
        self.maker = maker
        self.backoffs, self.latest = {}, {}
        self.urgent_sources = set()

    def connected(self, source):
        state = self.backoffs.setdefault(source, ReconnectBackoff())
        state.connected_at = self.maker.monotonic()

    async def failed(self, source, error):
        from .maker_transport import MakerSecretFilter

        now = self.maker.monotonic()
        for name, state in self.backoffs.items():
            if state.reset_if_stable(now):
                self.latest.pop(name, None)
                self.urgent_sources.discard(name)
        state = self.backoffs.setdefault(source, ReconnectBackoff())
        delay = state.failed()
        error_type, error_message = getattr(error, '_ws_diagnostic',
                                            (type(error).__name__, str(error)))
        record = logging.LogRecord('pendle.maker', logging.WARNING, '', 0,
            error_type + ': ' + error_message, (), None)
        MakerSecretFilter(getattr(self.maker.api, 'credentials', None), self.maker.alerter,
            secrets=(self.maker.settings.healthcheck_url,)).filter(record)
        error_type, _, message = record.getMessage().partition(': ')
        self.maker.log({'事件': '行情连接失败', '来源': source,
            '异常类型': error_type, '异常消息': message,
            '连续失败次数': state.failures, '下次等待秒': delay}, level=logging.WARNING)
        self.latest[source] = (f'{source}：连续失败 {state.failures} 次，'
                               f'{error_type}: {message}，下次等待 {delay:.3f} 秒')
        if self.urgent_sources:
            self.urgent_sources.add(source)
        elif self.maker.settings.mode != 'dry-run' and state.failures >= 5:
            # Lock this failure episode before yielding so three connections cannot send duplicate alerts.
            self.urgent_sources.update(self.latest)
            await self.maker.alerter.alert('行情连接连续失败：\n' + '\n'.join(self.latest.values()),
                                          key='行情重连连续失败', urgent=True)
        return delay


def reconnect_failures(maker):
    """Share alert coalescing across three consumers while keeping independent backoff counts."""
    if not hasattr(maker, '_feed_reconnect'):
        maker._feed_reconnect = ReconnectFailures(maker)
    return maker._feed_reconnect
