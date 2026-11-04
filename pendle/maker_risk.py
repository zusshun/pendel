"""Synchronous risk checks, persistent halts, and liveness supervision for live market making."""

import asyncio
import fcntl
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR


class RiskError(ValueError):
    """Risk inputs or persistent state are invalid; the caller must stop adding orders."""


class RiskRejected(RiskError):
    """The proposed order exceeds the risk budget."""


def _decimal(value, *, positive=False, nonnegative=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if (not result.is_finite() or (positive and result <= 0)
                or (nonnegative and result < 0)):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RiskError('风险参数必须是有效有限数字') from exc


def _side(value):
    if value not in ('buy', 'sell'):
        raise RiskError('订单方向必须为 buy 或 sell')
    return value


def _name(value):
    if not isinstance(value, str) or not value:
        raise RiskError('市场或记录标识不能为空')
    return value


@dataclass(frozen=True)
class RiskLimits:
    quote_notional: Decimal = Decimal('400')
    max_position_usd: Decimal = Decimal('1200')
    max_account_exposure_usd: Decimal = Decimal('1200')
    max_margin_ratio: Decimal = Decimal('.3')
    hard_cap_usd: Decimal = Decimal('600')

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _decimal(getattr(self, name), positive=True))
        if (self.quote_notional > 800 or self.max_position_usd > 1200
                or self.max_account_exposure_usd > 1500 or self.max_margin_ratio > 1
                or self.hard_cap_usd > self.max_position_usd):
            raise RiskError('配置超过代码风险硬上限')


@dataclass(frozen=True)
class MarketRisk:
    market: str
    position: Decimal
    mark_price: Decimal
    initial_margin_rate: Decimal = Decimal('.1')
    unrealized_pnl: Decimal = Decimal(0)

    def __post_init__(self):
        _name(self.market)
        for name in ('position', 'unrealized_pnl'):
            object.__setattr__(self, name, _decimal(getattr(self, name)))
        for name in ('mark_price', 'initial_margin_rate'):
            object.__setattr__(self, name, _decimal(getattr(self, name), positive=True))
        if self.initial_margin_rate > 1:
            raise RiskError('初始保证金率不得超过一')


ACTIVE_STATES = frozenset(('INTENT', 'SENT', 'ACKED', 'OPEN', 'PARTIAL',
                           'UNKNOWN', 'CANCEL_SENT'))
TERMINAL_STATES = frozenset(('FILLED', 'CANCELED', 'REJECTED', 'CONFIRMED_ABSENT', 'ABSENT'))


@dataclass(frozen=True)
class RiskOrder:
    market: str
    side: str
    remaining: Decimal
    price: Decimal
    state: str

    def __post_init__(self):
        _name(self.market)
        _side(self.side)
        object.__setattr__(self, 'remaining', _decimal(self.remaining, nonnegative=True))
        object.__setattr__(self, 'price', _decimal(self.price, positive=True))
        if self.state not in ACTIVE_STATES | TERMINAL_STATES:
            raise RiskError('未知订单状态不能释放风险预算')


@dataclass(frozen=True)
class RiskReport:
    exposures: dict
    account_exposure: Decimal
    margin_required: Decimal
    margin_ratio: Decimal


class RiskManager:
    """Use one synchronous input snapshot; do not yield between checking and sending."""

    def __init__(self, limits=None):
        self.limits = limits or RiskLimits()

    def _snapshot(self, markets, orders):
        markets, orders = tuple(markets), tuple(orders)
        states = {row.market: row for row in markets}
        if len(states) != len(markets) or not states:
            raise RiskError('市场风险快照为空或重复')
        sides = {name: [row.position * row.mark_price, row.position * row.mark_price]
                 for name, row in states.items()}
        for order in orders:
            if order.market not in states:
                raise RiskError('挂单缺少对应市场估值')
            if order.state in ACTIVE_STATES:
                notional = order.remaining * max(order.price, states[order.market].mark_price)
                sides[order.market][0 if order.side == 'buy' else 1] += (
                    notional if order.side == 'buy' else -notional)
        return states, sides

    @staticmethod
    def _exposures(sides):
        return {name: max(Decimal(0), buy, -sell) for name, (buy, sell) in sides.items()}

    @staticmethod
    def _margin(states, exposures):
        # Pending sandbox verification: market margin uses the larger side notional times the initial rate, plus unrealized losses; profits do not offset it.
        return sum((exposures[name] * row.initial_margin_rate
                    + max(Decimal(0), -row.unrealized_pnl)
                    for name, row in states.items()), Decimal(0))

    def worst_exposures(self, markets, orders):
        """Use the larger worst-case side notional per market; never net different markets against one another."""
        _, sides = self._snapshot(markets, orders)
        return self._exposures(sides)

    def check_order(self, market, side, quantity, price, markets, orders, equity_usd,
                    available_balance_usd, *, reduce_only=False, exchange_margin_ratio=None):
        side = _side(side)
        quantity, price = _decimal(quantity, positive=True), _decimal(price, positive=True)
        equity = _decimal(equity_usd, positive=True)
        available = _decimal(available_balance_usd, nonnegative=True)
        if type(reduce_only) is not bool:
            raise RiskError('reduce_only 必须是布尔值')
        orders = tuple(orders)
        states, sides = self._snapshot(markets, orders)
        if market not in states:
            raise RiskError('拟下单缺少对应市场估值')
        position = states[market].position
        if reduce_only:
            if ((side == 'buy' and position >= 0) or (side == 'sell' and position <= 0)
                    or quantity > abs(position)):
                raise RiskRejected('减仓订单不得加仓或反转持仓')
        elif quantity * price > self.limits.quote_notional:
            raise RiskRejected('拟下单金额超过单侧报价限额')
        previous_sides = {name: tuple(endpoints) for name, endpoints in sides.items()}
        previous_margin = self._margin(states, self._exposures(sides))
        notional = quantity * max(price, states[market].mark_price)
        sides[market][0 if side == 'buy' else 1] += notional if side == 'buy' else -notional
        exposures = self._exposures(sides)
        account = sum(exposures.values(), Decimal(0))
        margin = self._margin(states, exposures)
        # When already over limit, exempt only genuine reductions with no other active orders; neither worst-case side endpoint nor margin may increase.
        safe_reduction = (reduce_only and not any(order.state in ACTIVE_STATES for order in orders)
            and margin <= previous_margin and all(
                max(Decimal(0), endpoints[0]) <= max(Decimal(0), previous_sides[name][0])
                and max(Decimal(0), -endpoints[1]) <= max(Decimal(0), -previous_sides[name][1])
                for name, endpoints in sides.items()))
        # Recompute both final endpoints at send time; reversal quotes must also respect the hard reduction threshold.
        if not safe_reduction and any(value > self.limits.hard_cap_usd for value in exposures.values()):
            raise RiskRejected('两侧最终持仓敞口超过硬上限')
        if not safe_reduction and any(value > self.limits.max_position_usd for value in exposures.values()):
            raise RiskRejected('最坏持仓敞口超过上限')
        if not safe_reduction and account > self.limits.max_account_exposure_usd:
            raise RiskRejected('账户总敞口超过上限')
        incremental_margin = max(Decimal(0), margin - previous_margin)
        margin_ratio = (margin / equity if exchange_margin_ratio is None else
                        _decimal(exchange_margin_ratio, nonnegative=True) + incremental_margin / equity)
        if ((not safe_reduction and margin_ratio > self.limits.max_margin_ratio)
                or incremental_margin > available):
            raise RiskRejected('保证金率或可用余额不足')
        return RiskReport(exposures, account, margin, margin_ratio)

    def limit_adding_quantity(self, market, side, desired, base_increment, markets, orders,
                              *, price=None):
        """Limit quantity by the final signed endpoint on this side, retaining one minimum unit for the reversal portion."""
        side = _side(side)
        desired = _decimal(desired, nonnegative=True)
        increment = _decimal(base_increment, positive=True)
        states, sides = self._snapshot(markets, orders)
        if market not in states:
            raise RiskError('加仓限量缺少市场估值')
        row = states[market]
        valuation = max(row.mark_price, _decimal(price, positive=True)) if price is not None else row.mark_price
        # Negative endpoints represent room to reduce first; do not clamp to zero and discard genuine reduction capacity.
        current = sides[market][0 if side == 'buy' else 1] * (1 if side == 'buy' else -1)
        budget = (self.limits.hard_cap_usd - current) / valuation - increment
        desired = min(desired, max(Decimal(0), budget))
        return (desired / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


class AlreadyRunning(RiskError):
    """Another instance is already running for this account."""


class DurableFiles:
    """Inject os/flock for all file operations; completed writes include fsync of the parent directory."""

    def __init__(self, *, fs=os, flock=fcntl.flock):
        self.fs, self.flock = fs, flock

    def exists(self, path):
        try:
            fd = self.fs.open(str(path), os.O_RDONLY)
        except FileNotFoundError:
            return False
        self.fs.close(fd)
        return True

    def read_text(self, path):
        fd = self.fs.open(str(path), os.O_RDONLY)
        try:
            parts = []
            while True:
                data = self.fs.read(fd, 65536)
                if not data:
                    return b''.join(parts).decode('utf-8')
                parts.append(data)
        finally:
            self.fs.close(fd)

    def _write(self, path, payload):
        fd = self.fs.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                count = self.fs.write(fd, payload[offset:])
                if count <= 0:
                    raise OSError('持久状态写入未前进')
                offset += count
            self.fs.fsync(fd)
        finally:
            self.fs.close(fd)

    def _sync_directory(self, directory):
        fd = self.fs.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            self.fs.fsync(fd)
        finally:
            self.fs.close(fd)

    def _save(self, path, value, *, exclusive):
        path = str(path)
        parent = os.path.dirname(path) or '.'
        self.fs.makedirs(parent, exist_ok=True)
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        temporary = path if exclusive else path + '.tmp-' + uuid.uuid4().hex
        try:
            self._write(temporary, payload)
            if not exclusive:
                self.fs.replace(temporary, path)
            self._sync_directory(parent)
        except BaseException:
            # Retain even partial halt files: any existing file must block trading after restart.
            if not exclusive:
                try:
                    self.fs.unlink(temporary)
                except FileNotFoundError:
                    pass
            raise

    def create_halt(self, path, value):
        self._save(path, value, exclusive=True)

    def write_json(self, path, value):
        self._save(path, value, exclusive=False)

    def acquire_instance(self, path):
        path = str(path)
        self.fs.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        fd = self.fs.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.fs.close(fd)
            raise AlreadyRunning('同一账户已有机器人实例') from exc
        except BaseException:
            self.fs.close(fd)
            raise
        return fd

    def release_instance(self, fd):
        try:
            self.flock(fd, fcntl.LOCK_UN)
        finally:
            self.fs.close(fd)


class SingleInstanceLock:
    def __init__(self, path, *, files=None):
        self.path, self.files = path, files or DurableFiles()
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            raise AlreadyRunning('当前实例已经持有账户锁')
        self.handle = self.files.acquire_instance(self.path)
        return self

    def release(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.files.release_instance(handle)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


class WindowLossLedger:
    """The caller explicitly identifies windows; midnight and process restarts never reset them implicitly."""

    def __init__(self, window_id, path, *, files=None):
        self.window_id, self.path = _name(window_id), path
        self.files = files or DurableFiles()
        self.events, self.unrealized_pnl, self.source_ms = {}, Decimal(0), -1
        if self.files.exists(path):
            try:
                state = json.loads(self.files.read_text(path))
                if state['version'] != 1 or state['window_id'] != window_id:
                    raise ValueError
                for event_id, value in state['events'].items():
                    _name(event_id)
                    if len(value) != 3:
                        raise ValueError
                    self.events[event_id] = tuple(_decimal(item) for item in value)
                self.unrealized_pnl = _decimal(state['unrealized_pnl'])
                self.source_ms = state['source_ms']
                if type(self.source_ms) is not int or self.source_ms < -1:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise RiskError('窗口账本损坏或窗口标识不匹配，禁止隐式清零') from exc

    def _save(self, events, unrealized, source_ms):
        self.files.write_json(self.path, {'version': 1, 'window_id': self.window_id,
            'events': {key: [str(item) for item in value] for key, value in events.items()},
            'unrealized_pnl': str(unrealized), 'source_ms': source_ms})

    def record(self, event_id, *, realized_pnl=0, fee=0, funding=0):
        _name(event_id)
        # Pending sandbox verification: positive fees mean expense, negative fees mean rebates, and positive funding means income.
        event = (_decimal(realized_pnl), _decimal(fee), _decimal(funding))
        if event_id in self.events:
            if self.events[event_id] != event:
                raise RiskError('同一盈亏事件出现矛盾内容')
            return False
        events = {**self.events, event_id: event}
        self._save(events, self.unrealized_pnl, self.source_ms)
        self.events = events
        return True

    def set_unrealized(self, value, *, source_ms):
        value = _decimal(value)
        if type(source_ms) is not int or source_ms < 0:
            raise RiskError('浮盈快照时间必须为非负毫秒整数')
        if source_ms < self.source_ms:
            return False
        if source_ms == self.source_ms:
            if value != self.unrealized_pnl:
                raise RiskError('同一水位的浮盈快照互相矛盾')
            return False
        self._save(self.events, value, source_ms)
        self.unrealized_pnl, self.source_ms = value, source_ms
        return True

    @property
    def loss_usd(self):
        pnl = sum((realized - fee + funding for realized, fee, funding in self.events.values()),
                  self.unrealized_pnl)
        return max(Decimal(0), -pnl)

    def exceeds(self, limit):
        return self.loss_usd > _decimal(limit, nonnegative=True)


def _seconds(value):
    value = float(_decimal(value, nonnegative=True))
    if not math.isfinite(value):
        raise RiskError('时间阈值无效')
    return value


async def _wait_cleanup(task):
    """Shield cleanup from repeated cancellation, then return the pending cancellation to the caller."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class Guard:
    """Market guard; block/resume change only this market barrier and never reopen the global send gate."""

    def __init__(self, cooldown_s, stable_s, *, stop, block=lambda: None,
                 resume=lambda: None, monotonic=time.monotonic):
        self.cooldown_s, self.stable_s = _seconds(cooldown_s), _seconds(stable_s)
        self.stop, self.block, self.resume, self.monotonic = stop, block, resume, monotonic
        self.state, self.reason = 'RUNNING', None
        self.triggered_at, self.stable_since = None, None
        self.generation = 0

    async def trigger(self, reason):
        self.block()
        self.generation += 1
        generation = self.generation
        self.reason, self.triggered_at = _name(reason), self.monotonic()
        self.state, self.stable_since = 'STOPPING', None
        try:
            stopped = await self.stop(reason)
        except BaseException:
            if generation == self.generation:
                self.state = 'STOP_FAILED'
            raise
        if generation == self.generation:
            self.state = 'COOLDOWN' if stopped is True else 'STOP_FAILED'
        return stopped is True

    async def observe(self, healthy, reason='守卫条件再次触发'):
        if type(healthy) is not bool:
            raise RiskError('守卫观测必须为布尔值')
        if not healthy:
            await self.trigger(reason)
            return False
        if self.state == 'RUNNING':
            return True
        if self.state in ('STOPPING', 'STOP_FAILED'):
            return False
        now = self.monotonic()
        if now - self.triggered_at < self.cooldown_s:
            return False
        if self.stable_since is None:
            self.stable_since, self.state = now, 'STABILIZING'
        if now - self.stable_since < self.stable_s:
            return False
        self.resume()
        self.state = 'RUNNING'
        return True


@dataclass(frozen=True)
class HaltResult:
    reason: str
    persisted: bool
    stopped: bool
    exit_code: int = 76
    errors: tuple = ()


class HaltController:
    """Block new orders synchronously, then persist the halt lock; caller cancellation must not interrupt stop cleanup."""

    def __init__(self, path, *, gate, stop, alert, files=None, clock=time.time,
                 monitor_after_halt=False):
        self.path, self.gate, self.stop, self.alert = path, gate, stop, alert
        self.files, self.clock = files or DurableFiles(), clock
        self.monitor_after_halt = monitor_after_halt
        self._task = None
        self.exit_code = None

    def assert_can_start(self):
        if self._task is not None or self.files.exists(self.path):
            raise RiskError('熔断锁存在或状态不确定，必须人工复位')

    async def _finish(self, reason, persisted, errors):
        stopped = False
        try:
            stopped = await self.stop(reason) is True
            if not stopped:
                errors.append('停止流程未确认挂单清空')
        except Exception as exc:
            errors.append('停止流程失败：' + type(exc).__name__)
        finally:
            # Keep monitoring only after the lock is persisted and orders are confirmed absent; never reopen a gate closed by Watchdog.
            if (not self.monitor_after_halt or not persisted or not stopped
                    or not self.gate.is_open):
                self.gate.finish_stop()
            self.exit_code = 76
        try:
            await self.alert('全局熔断：' + reason + ('；' + '；'.join(errors) if errors else ''))
        except Exception as exc:
            errors.append('紧急告警失败：' + type(exc).__name__)
        return HaltResult(reason, persisted, stopped, 76, tuple(errors))

    async def trigger(self, reason, *, permanent=False):
        if self._task is None:
            reason = _name(reason)
            if self.monitor_after_halt:
                self.gate.halt_trading()
            else:
                self.gate.close()
            errors, persisted = [], False
            try:
                self.files.create_halt(self.path, {'reason': reason, 'time_ms': int(self.clock() * 1000)})
                persisted = True
            except Exception as exc:
                errors.append('熔断锁持久化失败：' + type(exc).__name__)
            # Failed stopping exhausted the cancellation budget: do not yield between persisting the lock and permanently closing the gate, including for queued pings or reconnects.
            if permanent:
                self.gate.finish_stop()
            self._task = asyncio.create_task(self._finish(reason, persisted, errors))
        elif permanent:
            self.gate.finish_stop()
        return await _wait_cleanup(self._task)


class Watchdog:
    """Renew only on critical-task heartbeats and private pongs; after failure, this instance can never resume sending."""

    def __init__(self, *, gate, stop, ping, alert, reconciler_interval_s,
                 strategy_stale_s=15, order_manager_stale_s=15, private_stale_s=15,
                 check_interval_s=10, ping_timeout_s=5, monotonic=time.monotonic,
                 sleep=asyncio.sleep, required_tasks=None):
        self.gate, self.stop, self.ping, self.alert = gate, stop, ping, alert
        self.monotonic, self.sleep = monotonic, sleep
        self.thresholds = {'Strategy': _seconds(strategy_stale_s),
            'OrderManager': _seconds(order_manager_stale_s),
            'PrivateFeed': _seconds(private_stale_s),
            'Reconciler': _seconds(reconciler_interval_s) + 30}
        if required_tasks is not None:
            required_tasks = frozenset(required_tasks)
            if not required_tasks or required_tasks - self.thresholds.keys():
                raise RiskError('必需监督任务必须非空且全部已注册')
            self.thresholds = {name: threshold for name, threshold in self.thresholds.items()
                               if name in required_tasks}
        self.check_interval_s = _seconds(check_interval_s)
        self.ping_timeout_s = _seconds(ping_timeout_s)
        if self.check_interval_s <= 0 or any(value <= 0 for value in self.thresholds.values()):
            raise RiskError('存活阈值和检查周期必须为正')
        if not 0 < self.ping_timeout_s <= self.check_interval_s:
            raise RiskError('续期超时必须为正且不能超过检查周期')
        self.last_seen = {}
        self.tripped, self.failure_reason = False, None
        self._stop_task = None
        self.errors = []
        self._background = set()

    def beat(self, task_name):
        if task_name not in self.thresholds or task_name == 'PrivateFeed':
            raise RiskError('私有连接必须用 pong 记录存活，其他任务名必须已注册')
        self.last_seen[task_name] = self.monotonic()

    def pong(self):
        self.last_seen['PrivateFeed'] = self.monotonic()

    def unhealthy_tasks(self):
        now = self.monotonic()
        return tuple(name for name, threshold in self.thresholds.items()
                     if name not in self.last_seen or now - self.last_seen[name] > threshold
                     or now < self.last_seen[name])

    async def _stop(self, reason):
        try:
            if await self.stop(reason) is not True:
                self.errors.append('停止流程未确认挂单清空')
        except Exception as exc:
            self.errors.append('停止流程失败：' + type(exc).__name__)
        finally:
            self.gate.finish_stop()
        try:
            await self.alert('死人开关停止续期：' + reason)
        except Exception as exc:
            self.errors.append('紧急告警失败：' + type(exc).__name__)

    async def _trip(self, reason):
        if not self.tripped:
            # Do not await before creating the stop task; ordinary sends and concurrent checks become invalid immediately.
            self.tripped, self.failure_reason = True, reason
            self.gate.close()
            self._stop_task = asyncio.create_task(self._stop(reason))
        await _wait_cleanup(self._stop_task)
        return False

    async def check_once(self, *, renew=True):
        if self.tripped:
            return False
        if not self.gate.is_open:
            return False
        unhealthy = self.unhealthy_tasks()
        if unhealthy:
            return await self._trip('关键任务不健康：' + '、'.join(unhealthy))
        if not renew:
            return True
        sending = asyncio.create_task(self.ping())
        timer = asyncio.create_task(self.sleep(self.ping_timeout_s))
        try:
            done, _ = await asyncio.wait((sending, timer), return_when=asyncio.FIRST_COMPLETED)
            if sending not in done:
                timer.result()
                raise TimeoutError('续期发送超时')
            sending.result()
        except asyncio.CancelledError:
            await self._trip('看门狗检查被取消')
            raise
        except Exception as exc:
            return await self._trip('续期发送失败：' + type(exc).__name__)
        finally:
            self._cancel_background(sending)
            self._cancel_background(timer)
        return not self.tripped

    def _cancel_background(self, task):
        # The sender may ignore cancellation, so do not await it; every actual send must still check the same gate.
        if not task.done():
            task.cancel()
        self._background.add(task)

        def done(completed):
            self._background.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

    async def run(self):
        try:
            while not self.tripped:
                started = self.monotonic()
                if not await self.check_once():
                    break
                await self.sleep(max(0, self.check_interval_s - (self.monotonic() - started)))
        except (asyncio.CancelledError, Exception) as exc:
            await self._trip('看门狗运行退出：' + type(exc).__name__)
            raise
