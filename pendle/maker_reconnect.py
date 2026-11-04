"""行情连接分别退避，日志脱敏并合并同一故障期的紧急告警。"""

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
        # 限制指数的计算规模，长期断网不会构造不断增大的整数。
        base = min(60, 3 * 2 ** min(self.failures - 1, 5))
        return min(60, base + random.uniform(-base * .2, base * .2))


class ReconnectFailures:
    """状态按来源独立；一个未恢复的故障期最多发一次 urgent。"""

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
            # 在让出事件循环前锁定此故障期，避免三条连接同时产生重复告警。
            self.urgent_sources.update(self.latest)
            await self.maker.alerter.alert('行情连接连续失败：\n' + '\n'.join(self.latest.values()),
                                          key='行情重连连续失败', urgent=True)
        return delay


def reconnect_failures(maker):
    """三个消费协程共享告警合并状态，分别保留自己的退避计数。"""
    if not hasattr(maker, '_feed_reconnect'):
        maker._feed_reconnect = ReconnectFailures(maker)
    return maker._feed_reconnect
