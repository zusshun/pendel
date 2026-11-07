"""Single source of truth for live orders, synchronization barriers, and confirmed cancellation.

Exchange inputs use normalized fields; untimed positions require a quiet period and consistency checks.
This module opens no network connections; an injected API performs all sends.
"""

import asyncio
import inspect
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .market import number
from .ondo_api import exchange_time_ms


class OrdersBlocked(ValueError):
    """The current state blocks new orders."""


class OrderStateError(ValueError):
    """An order event or state transition is invalid."""


class OrderStatus(str, Enum):
    INTENT = 'INTENT'
    SENT = 'SENT'
    ACKED = 'ACKED'
    OPEN = 'OPEN'
    REJECTED = 'REJECTED'
    UNKNOWN = 'UNKNOWN'
    PARTIAL = 'PARTIAL'
    FILLED = 'FILLED'
    CANCEL_SENT = 'CANCEL_SENT'
    CANCELED = 'CANCELED'
    ABSENT = 'ABSENT'


TERMINAL = frozenset({OrderStatus.REJECTED, OrderStatus.FILLED,
                      OrderStatus.CANCELED, OrderStatus.ABSENT})


def _position_identity(position):
    """Exclude valuation and observation fields from the economic position identity."""
    quantity = number(position['quantity'])
    direction = position.get('direction',
                             'long' if quantity > 0 else 'short' if quantity < 0 else 'neutral')
    return (direction, number(position.get('netQuantity', abs(quantity))),
            number(position['averageEntryPrice']) if 'averageEntryPrice' in position else None)


@dataclass
class Order:
    client_order_id: str
    market: str
    side: str
    quantity: Decimal
    price: Decimal
    reduce_only: bool = False
    time_in_force: str = 'GTC'
    post_only: bool = True
    status: OrderStatus = OrderStatus.INTENT
    order_id: str | None = None
    filled: Decimal = Decimal(0)
    event_filled: Decimal = Decimal(0)
    fill_total: Decimal = Decimal(0)
    exchange_ms: int = -1
    source: str = ''
    unknown_since: float | None = None
    cancel_since: float | None = None
    confirmed_absent: bool = False
    legacy: bool = False
    last_error_code: str | None = None
    last_error_category: str | None = None
    last_error_status: int | None = None
    last_error_reason: str | None = None
    emergency: bool = False
    exchange_only: bool = False

    @property
    def remaining(self):
        return max(Decimal(0), self.quantity - self.filled)

    @property
    def terminal(self):
        return self.status in TERMINAL


class AccountState:
    """Update synchronously within one asyncio event loop; update methods contain no await."""

    def __init__(self, markets, *, monotonic=time.monotonic):
        self.markets = tuple(markets)
        if not self.markets or len(set(self.markets)) != len(self.markets):
            raise OrderStateError('市场必须非空且不重复')
        self.orders, self.order_ids, self.fills = {}, {}, {}
        self.positions = {market: Decimal(0) for market in self.markets}
        self.position_details = {}
        self.account_positions = []
        self.position_bases = {market: (Decimal(0), -1, '') for market in self.markets}
        self.balances, self.balance_ms = {}, -1
        self.balance_source = ''
        self.realized_pnl, self.fees, self.funding = Decimal(0), Decimal(0), Decimal(0)
        self.stopping, self.deadman_acked = False, False
        self.generations = {market: 0 for market in self.markets}
        self.modes = {market: 'SYNCING' for market in self.markets}
        self.sync_reasons = {}
        self.revision = 0
        self.balance_revision = 0
        # Verification of zero open orders tracks only target-market orders and positions, unaffected by balances or other markets.
        self.market_revisions = {market: 0 for market in self.markets}
        self.monotonic = monotonic
        self.last_fill_monotonic = {market: None for market in self.markets}
        self._untimed_positions = set()
        self._position_order_coverage = {}
        self._position_event_contents = {}

    def market_mode(self, market):
        return self.modes[market]

    def begin_sync(self, market, reason):
        self.generations[market] += 1
        self.sync_reasons[market] = reason
        self.modes[market] = 'STOPPING' if self.stopping else 'SYNCING'
        return self.generations[market]

    def deadman_ack(self):
        self.deadman_acked = True

    def private_disconnected(self):
        self.deadman_acked = False
        for market in self.markets:
            self.begin_sync(market, '私有连接中断，等待重新确认死人开关')

    def require_running(self, market):
        if (self.stopping or not self.deadman_acked or self.modes[market] != 'RUNNING'
                or self.has_unaccounted_fills(market)):
            raise OrdersBlocked('同步或停止屏障禁止新增订单')

    def has_unaccounted_fills(self, market):
        """When cumulative order fills arrive first, retain risk budget until fills or positions account for them."""
        watermark = self.position_bases[market][1]
        for order in self.orders.values():
            covered_at, covered_quantity = self._position_order_coverage.get(
                order.client_order_id, (-1, Decimal(0)))
            if (order.market != market or order.exchange_ms <= watermark
                    or order.exchange_ms <= covered_at and order.event_filled <= covered_quantity):
                continue
            accounted = sum((fill[2].copy_abs() for fill in self.fills.values()
                             if fill[3] == order.client_order_id and fill[1] <= order.exchange_ms), Decimal(0))
            if order.event_filled > accounted:
                return True
        return False

    def active_orders(self, market=None):
        return tuple(order for order in self.orders.values()
                     if not order.terminal and (market is None or order.market == market))

    def note_position_event(self, market, position=None):
        """Only direction, net quantity, or entry price changes invalidate private position verification."""
        if position is not None:
            content = _position_identity(position)
            # REST or fills may still lag this observation; repeats must not restart synchronization.
            if self._position_event_contents.get(market) == content:
                return False
            self._position_event_contents[market] = content
        self.market_revisions[market] += 1
        return True

    def apply_position_snapshot(self, market, quantity, time_ms, *, source='rest'):
        quantity, time_ms = number(quantity), _timestamp(time_ms)
        old, watermark, old_source = self.position_bases[market]
        if time_ms < watermark:
            return False
        if time_ms == watermark and old_source == 'ws' and source == 'rest':
            if quantity != old:
                self.begin_sync(market, '同毫秒 REST 与私有持仓快照矛盾')
            return False
        expected = old + sum((fill[2] for fill in self.fills.values()
                              if fill[0] == market and watermark < fill[1] <= time_ms), Decimal(0))
        if quantity != expected:
            self.begin_sync(market, '持仓快照与本地推算矛盾')
        position = quantity + sum((fill[2] for fill in self.fills.values()
                                   if fill[0] == market and fill[1] > time_ms), Decimal(0))
        changed = (quantity != old or source != old_source
                   or position != self.positions[market])
        if quantity != old or position != self.positions[market]:
            self._position_event_contents.pop(market, None)
        self.position_bases[market] = (quantity, time_ms, source)
        self.positions[market] = position
        if changed:
            self.revision += 1
            self.market_revisions[market] += 1
        return True

    def apply_untimed_position_snapshot(self, market, quantity):
        """Called only by REST reconciliation with no concurrent private updates during the quiet period."""
        quantity = number(quantity)
        if quantity != self.positions[market]:
            self._position_event_contents.pop(market, None)
        self.positions[market] = quantity
        self._untimed_positions.add(market)
        for order in self.orders.values():
            if order.market == market:
                self._position_order_coverage[order.client_order_id] = (
                    order.exchange_ms, max(order.event_filled, order.fill_total))
        self.revision += 1
        self.market_revisions[market] += 1

    def apply_balance_snapshot(self, balance, *, source='rest'):
        at = _timestamp(balance['time_ms']) if 'time_ms' in balance else None
        if at is not None and at < self.balance_ms:
            return False
        for name in ('equity', 'available'):
            number(balance[name])
        if at == self.balance_ms and self.balance_source == 'ws' and source == 'rest':
            if any(number(balance[name]) != number(self.balances[name]) for name in ('equity', 'available')):
                for market in self.markets:
                    self.begin_sync(market, '同毫秒 REST 与私有余额快照矛盾')
            return False
        changed = ({key: value for key, value in balance.items() if key != 'time_ms'}
                   != {key: value for key, value in self.balances.items() if key != 'time_ms'})
        self.balances = dict(balance)
        if at is not None:
            self.balance_ms = at
        self.balance_source = source
        if changed:
            # Valuation fields (unrealized PnL, margin) change every ~80 ms while a position is open; they must
            # not invalidate order/position snapshots, so balances keep their own revision (2026-11-07).
            self.balance_revision += 1
        return True

    def apply_fill(self, fill_id, market, side, quantity, time_ms, *,
                   client_order_id=None, realized_pnl='0', fee='0', funding='0'):
        if not isinstance(fill_id, str) or not fill_id or side not in ('buy', 'sell'):
            raise OrderStateError('成交标识或方向无效')
        quantity, at = number(quantity), _timestamp(time_ms)
        pnl, fee, funding = number(realized_pnl), number(fee), number(funding)
        if quantity <= 0 or market not in self.positions:
            raise OrderStateError('成交数量或市场无效')
        signed = quantity if side == 'buy' else -quantity
        value = (market, at, signed, client_order_id, pnl, fee, funding)
        if fill_id in self.fills:
            if self.fills[fill_id] != value:
                self.begin_sync(market, '重复成交标识的内容矛盾')
                raise OrderStateError('重复成交标识的内容矛盾')
            return False
        order = self.orders.get(client_order_id)
        next_filled = None
        if order:
            if (order.market, order.side) != (market, side):
                self.begin_sync(order.market, '成交方向或市场与意图矛盾')
                raise OrderStateError('成交方向或市场与意图矛盾')
            after_watermark = sum((fill[2].copy_abs() for fill in self.fills.values()
                                   if fill[3] == client_order_id and fill[1] > order.exchange_ms), Decimal(0))
            if at > order.exchange_ms:
                after_watermark += quantity
            next_filled = max(order.fill_total + quantity, order.event_filled + after_watermark)
            if next_filled > order.quantity:
                self.begin_sync(market, '累计成交超过原始订单量')
                raise OrderStateError('累计成交超过原始订单量')
        self.fills[fill_id] = value
        self.last_fill_monotonic[market] = self.monotonic()
        position_before_fill = self.positions[market]
        included = self._position_order_coverage.get(client_order_id, (-1, Decimal(0)))[1]
        if market in self._untimed_positions:
            previous = order.fill_total if order else Decimal(0)
            delta = max(Decimal(0), previous + quantity - included) - max(Decimal(0), previous - included)
            self.positions[market] += delta if side == 'buy' else -delta
        elif at > self.position_bases[market][1]:
            self.positions[market] += signed
        content = self._position_event_contents.get(market)
        if content is not None and self.positions[market] != position_before_fill:
            observed_quantity = (content[1] if content[0] == 'long'
                                 else -content[1] if content[0] == 'short' else Decimal(0))
            # Preserve an observation that arrived before its fill; invalidate only a conflicting quantity.
            if self.positions[market] != observed_quantity:
                self._position_event_contents.pop(market, None)
        self.realized_pnl += pnl
        self.fees += fee
        self.funding += funding
        if order:
            order.fill_total += quantity
            order.filled = next_filled
            if order.filled == order.quantity:
                order.status = OrderStatus.FILLED
            elif not order.terminal and order.status != OrderStatus.CANCEL_SENT:
                order.status = OrderStatus.PARTIAL
        self.revision += 1
        self.market_revisions[market] += 1
        return True

    def apply_order_event(self, row, *, source='ws', timestamp=None):
        try:
            oid, market, side = (row[key] for key in ('orderId', 'market', 'side'))
            cid = row.get('clientOrderId')
            exchange_only = cid is None or cid == ''
            if exchange_only:
                cid = self.order_ids.get(oid, 'order:' + oid)
            quantity, price = number(row['size']), number(row['price'])
            filled, at = number(row['filledSize']), _order_event_ms(row, timestamp)
            raw_status = row['status']
            if not isinstance(raw_status, str):
                raise ValueError
            status = OrderStatus({'fullyfilled': 'FILLED'}.get(raw_status.lower(), raw_status.upper()))
            reduce_only = row.get('reduceOnly', False)
            if (not isinstance(cid, str) or not cid or not isinstance(oid, str) or not oid
                    or market not in self.positions or side not in ('buy', 'sell')
                    or quantity <= 0 or price <= 0 or not 0 <= filled <= quantity
                    or type(reduce_only) is not bool
                    or status not in {OrderStatus.ACKED, OrderStatus.OPEN, OrderStatus.PARTIAL,
                                      OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
                    or (status == OrderStatus.FILLED and filled != quantity)):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderStateError('订单事件字段无效') from exc
        known_cid = self.order_ids.get(oid)
        if known_cid is not None and known_cid != cid:
            known = self.orders[known_cid]
            if known.exchange_only and cid not in self.orders:
                # Keep the confirmed exchange ID when a legacy order later recovers its client ID.
                cid = known_cid
            else:
                self.begin_sync(market, '交易所订单标识冲突')
                raise OrderStateError('交易所订单标识冲突')
        order = self.orders.get(cid)
        if order is None:
            order = Order(cid, market, side, quantity, price, reduce_only=reduce_only,
                          legacy=True, exchange_only=exchange_only)
            self.orders[cid] = order
            self.begin_sync(market, '发现不属于当前意图的遗留订单')
        if (order.market, order.side, order.quantity, order.price) != (market, side, quantity, price):
            self.begin_sync(market, '订单事件与意图矛盾')
            raise OrderStateError('订单事件与意图矛盾')
        if order.order_id and order.order_id != oid:
            self.begin_sync(market, '一个意图对应多个交易所订单')
            raise OrderStateError('一个意图对应多个交易所订单')
        if at < order.exchange_ms:
            return order
        if at == order.exchange_ms:
            # Millisecond truncation merges updates: preserve terminal states and maximum cumulative fills before comparing sources.
            if order.terminal and status not in TERMINAL:
                status = order.status
            filled = max(filled, order.event_filled)
            if (status == order.status and filled == order.event_filled
                    and source == 'rest' and order.source == 'ws'):
                return order
        if filled < order.event_filled:
            self.begin_sync(market, '累计成交量倒退')
            return order
        after_watermark = sum((fill[2].copy_abs() for fill in self.fills.values()
                               if fill[3] == cid and fill[1] > at), Decimal(0))
        next_filled = max(filled + after_watermark, order.fill_total)
        if next_filled > quantity:
            self.begin_sync(market, '订单成交水位超过数量')
            raise OrderStateError('订单成交水位超过数量')
        if order.terminal and status not in TERMINAL:
            if at == order.exchange_ms and not order.confirmed_absent:
                return order
            order.legacy = True
            self.begin_sync(market, '终态之后出现遗留挂单')
        if order.terminal and status in TERMINAL and order.status == OrderStatus.FILLED:
            return order
        was_canceling = order.status == OrderStatus.CANCEL_SENT
        order.order_id, order.exchange_ms, order.source = oid, at, source
        self.order_ids[oid] = cid
        order.event_filled = filled
        order.filled = next_filled
        order.status = (OrderStatus.CANCEL_SENT if was_canceling and status not in TERMINAL else status)
        if order.filled == quantity:
            order.status = OrderStatus.FILLED
        if self.has_unaccounted_fills(market):
            self.begin_sync(market, '订单累计成交尚未由成交账本或持仓水位覆盖')
        self.revision += 1
        self.market_revisions[market] += 1
        return order


def _timestamp(value):
    if type(value) is not int or value < 0:
        raise OrderStateError('交易所时间必须为非负毫秒整数')
    return value


def _order_event_ms(row, timestamp=None):
    """Take the latest order-lifecycle or outer WS time, with fallback to legacy normalized millisecond input."""
    values = [row[key] for key in ('createdAt', 'filledAt', 'canceledAt')
              if row.get(key) is not None]
    if timestamp is not None:
        values.append(timestamp)
    if values:
        return max(exchange_time_ms(value) for value in values)
    return _timestamp(row['time_ms'])


async def _ignore(reason):
    return None


async def _warn_rejection(reason):
    logging.getLogger('pendle.maker').warning(reason)


async def _wait_cleanup(task):
    """Shield safety cleanup from repeated cancellation, then propagate cancellation to the caller."""
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


class OrderManager:
    """API adapters must return normalized orders; run risk_check synchronously immediately before sending.

    risk_check(order, account_state) must raise on rejection and exclude this order
    from aggregate risk inputs while retaining all other INTENT orders. M2 injects the risk adapter.
    """

    def __init__(self, state, api, *, risk_check, monotonic=time.monotonic,
                 sleep=asyncio.sleep, alert=_ignore, halt=_ignore, stop_failed=_ignore,
                 reject=_warn_rejection, run_id='maker',
                 request_timeout_s=5, unknown_query_attempts=3,
                 unknown_query_interval_s=3, cancel_confirm_timeout_s=10, quiet_s=3,
                 unknown_timeout_s=60, stop_sweep_interval_s=0):
        try:
            durations = [float(number(value)) for value in
                         (request_timeout_s, cancel_confirm_timeout_s, unknown_query_interval_s, quiet_s, unknown_timeout_s)]
            stop_sweep_interval_s = float(number(stop_sweep_interval_s))
        except ValueError as exc:
            raise OrderStateError('订单管理器安全参数无效') from exc
        if (not callable(risk_check) or type(unknown_query_attempts) is not int
                or not 1 <= unknown_query_attempts <= 3 or durations[-1] > 60
                or not math.isfinite(stop_sweep_interval_s) or stop_sweep_interval_s < 0
                or any(not math.isfinite(value) or value <= 0 for value in durations)):
            raise OrderStateError('订单管理器安全参数无效')
        self.state, self.api, self.risk_check = state, api, risk_check
        self.monotonic, self.sleep, self.alert, self.halt = monotonic, sleep, alert, halt
        self.stop_failed = stop_failed
        self.reject = reject
        self.run_id = str(run_id)[:8] + '-' + uuid.uuid4().hex[:16]
        self.sequence = 0
        (self.request_timeout_s, self.cancel_confirm_timeout_s,
         self.unknown_query_interval_s, self.quiet_s, self.unknown_timeout_s) = durations
        self.state.monotonic = monotonic
        self._quiet_since = {market: monotonic() for market in self.state.markets}
        self._position_confirmation = {}
        self.unknown_query_attempts = unknown_query_attempts
        self.pending, self.background = {}, set()
        self._stop_task = None
        self._stop_cleanup_task = None
        self._local_stop_confirmed = False
        self._halting = False
        self.stop_sweep_interval_s = stop_sweep_interval_s
        self._stop_confirmed_at = None
        self._stop_confirmed_revisions = None

    def _require_emergency(self, market, candidate=None):
        gate = getattr(self.api, 'gate', None)
        if (gate is None or not gate.is_open or not getattr(gate, 'trading_halted', False)
                or not self.state.stopping or not self.state.deadman_acked
                or self.state.market_mode(market) != 'HALTED'
                or any(order is not candidate for order in self.state.active_orders())
                or any(self.state.has_unaccounted_fills(name) for name in self.state.markets)
                or any(not task.done() and cid != getattr(candidate, 'client_order_id', None)
                       for cid, task in self.pending.items())
                or candidate is None and getattr(self.api, 'in_flight_writes', 0)):
            raise OrdersBlocked('紧急减仓必须在健康通信、已核实撤单和持仓的 HALTED 屏障内执行')

    def create_intent(self, market, side, quantity, price, *, reduce_only=False,
                      time_in_force='GTC', post_only=True, emergency=False):
        if type(emergency) is not bool:
            raise OrderStateError('紧急交易标记必须为布尔值')
        if emergency:
            self._require_emergency(market)
        else:
            self.state.require_running(market)
        quantity, price = number(quantity), number(price)
        if (side not in ('buy', 'sell') or quantity <= 0 or price <= 0
                or type(reduce_only) is not bool or type(post_only) is not bool
                or time_in_force not in ('GTC', 'IOC')
                or (time_in_force == 'IOC' and (not reduce_only or post_only))
                or (not post_only and not reduce_only)
                or emergency and (not reduce_only or time_in_force != 'IOC' or post_only)):
            raise OrderStateError('订单方向、数量或价格无效')
        if any(order.side == side for order in self.state.active_orders(market)):
            raise OrdersBlocked('同侧存在未终结订单，必须先确认撤单')
        self.sequence += 1
        safe_market = re.sub(r'[^A-Za-z0-9_-]', '_', market)
        cid = f'{self.run_id}-{safe_market}-{side}-{self.sequence}'
        if not re.fullmatch(r'^[A-Za-z0-9_-]{1,64}$', cid):
            raise OrderStateError('订单意图标识只能包含字母、数字、下划线或连字符，长度须为 1 至 64 字符')
        order = Order(cid, market, side, quantity, price, reduce_only, time_in_force, post_only,
                      emergency=emergency)
        self.state.orders[cid] = order
        return order

    async def _wait(self, task, seconds, *, cancel=True):
        # Let immediately completed fake transports or local results run before starting a timeout timer.
        await asyncio.sleep(0)
        if task.done():
            return task.result()
        timer = asyncio.create_task(self.sleep(max(0, seconds)))
        try:
            done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            raise TimeoutError('订单安全操作等待超时')
        finally:
            timer.cancel()
            if cancel and not task.done():
                task.cancel()
                self._track(task)
            await asyncio.gather(timer, return_exceptions=True)

    def _track(self, task):
        self.background.add(task)
        def finished(completed):
            self.background.discard(completed)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(finished)

    async def _call(self, awaitable, *, deadline=None):
        seconds = self.request_timeout_s
        if deadline is not None:
            seconds = min(seconds, max(0, deadline - self.monotonic()))
        return await self._wait(asyncio.create_task(awaitable), seconds)

    def _unknown(self, order):
        if order.terminal:
            return
        if order.status != OrderStatus.CANCEL_SENT:
            order.status = OrderStatus.UNKNOWN
        if order.unknown_since is None:
            order.unknown_since = self.monotonic()
        self.state.begin_sync(order.market, '写请求结果未知')

    def _before_send(self, order, *, timestamp_retry=False, intent=None):
        if order.emergency:
            self._require_emergency(order.market, order)
        else:
            self.state.require_running(order.market)
        actual = (order.client_order_id, order.market, order.side, order.quantity, order.price,
                  order.reduce_only, order.time_in_force, order.post_only, order.emergency)
        if intent is not None and actual != intent:
            raise OrderStateError('排队期间订单意图发生变化，禁止发送')
        if order.status != OrderStatus.INTENT and not (timestamp_retry and order.status == OrderStatus.SENT):
            raise OrderStateError('只允许发送尚未发送的固定意图')
        result = self.risk_check(order, self.state)
        if inspect.isawaitable(result):
            if hasattr(result, 'close'):
                result.close()
            raise OrderStateError('风险检查必须同步执行')
        if result is False:
            raise OrdersBlocked('风险检查拒绝发送')
        order.status = OrderStatus.SENT

    async def submit(self, order):
        if self.state.orders.get(order.client_order_id) is not order or order.status != OrderStatus.INTENT:
            raise OrderStateError('意图已发送或不属于当前账户')
        if order.client_order_id in self.pending:
            raise OrderStateError('意图已有在途请求')
        task = asyncio.create_task(self._send(order))
        self.pending[order.client_order_id] = task
        try:
            await self._wait(task, self.request_timeout_s, cancel=False)
        except (TimeoutError, asyncio.CancelledError):
            self._unknown(order)
            if asyncio.current_task().cancelling():
                raise
        finally:
            if task.done():
                self.pending.pop(order.client_order_id, None)
        return order

    async def _trip_halt(self, reason):
        self._block_halt()
        await self.halt(reason)

    def _block_halt(self):
        self._halting = True
        self.state.stopping = True
        for market in self.state.markets:
            self.state.modes[market] = 'STOPPING'
        gate = getattr(self.api, 'gate', None)
        if gate is not None:
            gate.close()

    def _halt_after_stop_error(self, exc):
        if getattr(exc, 'category', None) != 'halt':
            return False
        if not self._halting:
            self._block_halt()
            # Let the current stop task exit, then start another stop after persisting the halt lock to avoid self-waiting.
            self._track(asyncio.create_task(self.halt('停止流程返回立即熔断类错误')))
            return True
        return False

    async def _handle_error(self, exc, reason):
        if getattr(exc, 'category', None) == 'halt':
            await self._trip_halt(reason)

    async def _send(self, order):
        intent = (order.client_order_id, order.market, order.side, order.quantity, order.price,
                  order.reduce_only, order.time_in_force, order.post_only, order.emergency)
        body = {'market': order.market, 'side': order.side, 'size': str(order.quantity),
                'type': 'limit',
                'price': str(order.price), 'clientOrderId': order.client_order_id,
                'reduceOnly': order.reduce_only, 'postOnly': order.post_only,
                'timeInForce': order.time_in_force}
        try:
            options = {'emergency': True} if order.emergency else {}
            row = await self.api.place_order(body, before_send=lambda **kwargs:
                                             self._before_send(order, intent=intent, **kwargs), **options)
            await self.handle_order_event(row, source='rest')
            if self.state.stopping and not order.emergency:
                await self.alert('停止期间收到延迟受理结果，必须再次核实撤单')
                if self._stop_task is not None and self._stop_task.done():
                    self._track(asyncio.create_task(self._late_stop()))
        except asyncio.CancelledError:
            self._unknown(order)
            raise
        except Exception as exc:
            order.last_error_code = getattr(exc, 'code', None)
            order.last_error_category = getattr(exc, 'category', 'guard')
            order.last_error_status = getattr(exc, 'status', None)
            if (400 <= getattr(exc, 'status', 0) < 500
                    and not getattr(exc, 'response_format', False)):
                order.last_error_reason = (
                    f'下单被拒绝：HTTP {exc.status}，error_code={order.last_error_code or "未知"}，'
                    f'error={getattr(exc, "error", None) or "未提供"}')
                await self.reject(order.last_error_reason)
            if order.status == OrderStatus.INTENT:
                order.status = OrderStatus.REJECTED
            elif getattr(exc, 'category', None) == 'halt':
                if getattr(exc, 'status', 0) >= 500 or getattr(exc, 'response_format', False):
                    self._unknown(order)
                elif order.status in (OrderStatus.SENT, OrderStatus.UNKNOWN):
                    order.status = OrderStatus.REJECTED
                self.pending.pop(order.client_order_id, None)
                await self._trip_halt(order.last_error_reason or '下单返回立即熔断类错误')
            elif getattr(exc, 'response_format', False):
                self._unknown(order)
            elif (getattr(exc, 'code', None) == 'clientOrderID_collision'
                  and getattr(exc, 'category', None) == 'reconcile'):
                self._unknown(order)
                found = await self._call(self.api.get_order(order.client_order_id))
                if found is None:
                    self.pending.pop(order.client_order_id, None)
                    await self._trip_halt('clientOrderId 冲突且找不到对应订单')
                else:
                    await self.handle_order_event(found, source='rest')
            elif 0 < getattr(exc, 'status', 0) < 500:
                if order.status in (OrderStatus.SENT, OrderStatus.UNKNOWN):
                    order.status = OrderStatus.REJECTED
                category = getattr(exc, 'category', 'guard')
                if category not in ('recompute', 'reduce_only_fallback', 'reconcile'):
                    self.state.begin_sync(order.market, order.last_error_reason or '下单返回守卫类错误')
            else:
                self._unknown(order)
            await self.alert('下单结果异常，已封锁未确认意图')

    async def _late_stop(self):
        await asyncio.sleep(0)
        gate = getattr(self.api, 'gate', None)
        if gate is None or gate.is_open:
            await self.stop('延迟受理结果再次全撤')
        else:
            await self.alert('发送闸门已封锁，延迟受理订单等待死人开关兜底')

    async def _unknown_expired(self, order, deadline):
        if self.monotonic() >= deadline:
            self.state.begin_sync(order.market, 'UNKNOWN 超过确认时限')
            await self._trip_halt('UNKNOWN 超过确认时限仍未确认')
            return True
        return False

    async def resolve_unknown(self, order):
        if order.status != OrderStatus.UNKNOWN:
            return order.status != OrderStatus.SENT
        deadline = order.unknown_since + self.unknown_timeout_s
        try:
            for attempt in range(self.unknown_query_attempts):
                if await self._unknown_expired(order, deadline):
                    return False
                found = await self._call(self.api.get_order(order.client_order_id), deadline=deadline)
                if await self._unknown_expired(order, deadline):
                    return False
                if found is not None:
                    await self.handle_order_event(found, source='rest')
                    return True
                if attempt + 1 < self.unknown_query_attempts:
                    await self.sleep(min(self.unknown_query_interval_s, deadline - self.monotonic()))
            if await self._unknown_expired(order, deadline):
                return False
            await self._call(self.api.cancel_all(order.market), deadline=deadline)
            open_orders = await self._call(self.api.get_open_orders(order.market), deadline=deadline)
            if open_orders:
                return False
            found = await self._call(self.api.get_order(order.client_order_id), deadline=deadline)
            if await self._unknown_expired(order, deadline):
                return False
            if found is not None:
                await self.handle_order_event(found, source='rest')
                return True
            # An in-flight order coroutine may be accepted after cancellation; one empty snapshot cannot prove absence.
            pending = self.pending.get(order.client_order_id)
            if ((pending is not None and not pending.done())
                    or getattr(self.api, 'in_flight_writes', 0)):
                return False
            if order.status == OrderStatus.UNKNOWN:
                order.status, order.confirmed_absent = OrderStatus.ABSENT, True
                await self.alert('订单经全撤与双重查询，已确认不存在')
            return order.terminal
        except Exception as exc:
            await self._handle_error(exc, 'UNKNOWN 查询或确认撤单发生鉴权等致命错误')
            await self._unknown_expired(order, deadline)
            return False

    async def handle_order_event(self, row, *, source='ws', timestamp=None):
        order = self.state.apply_order_event(row, source=source, timestamp=timestamp)
        if order.legacy and not order.terminal:
            await self.cancel(order)
            await self.alert('发现延迟受理或遗留挂单，立即撤销')
        return order

    def _get_order(self, order):
        if order.exchange_only:
            return self.api.get_order(order.order_id, by_order_id=True)
        return self.api.get_order(order.client_order_id)

    async def cancel(self, order):
        if order.terminal:
            return True
        order.status = OrderStatus.CANCEL_SENT
        if order.cancel_since is None:
            order.cancel_since = self.monotonic()
        try:
            request = (self.api.cancel_order(order.order_id, by_order_id=True) if order.exchange_only
                       else self.api.cancel_order(order.client_order_id))
            await self._call(request)
        except Exception as exc:
            # Cancellation responses do not prove a terminal state; 404, already closed, and timeout cases need query or private confirmation.
            self.state.begin_sync(order.market, '撤单结果待确认')
            await self._handle_error(exc, '撤单返回立即熔断类错误')
        return order.terminal

    async def confirm_cancel(self, order):
        if order.terminal:
            return True
        if order.status != OrderStatus.CANCEL_SENT:
            raise OrderStateError('订单尚未发出撤单请求')
        deadline = order.cancel_since + self.cancel_confirm_timeout_s
        while self.monotonic() < deadline:
            try:
                found = await self._call(self._get_order(order), deadline=deadline)
                if found is not None:
                    await self.handle_order_event(found, source='rest')
                if order.terminal:
                    return True
            except Exception as exc:
                await self._handle_error(exc, '撤单确认返回立即熔断类错误')
                if self.state.stopping:
                    return False
            await self.sleep(min(1, max(0, deadline - self.monotonic())))
        try:
            await self._call(self.api.cancel_all(order.market))
            found = await self._call(self._get_order(order))
            if found is not None:
                await self.handle_order_event(found, source='rest')
            if order.terminal:
                return True
        except Exception as exc:
            await self._handle_error(exc, '升级撤单返回立即熔断类错误')
        self.state.begin_sync(order.market, '撤单升级后仍无终态确认')
        await self._trip_halt('撤单确认超时，按市场全撤后仍未确认')
        return False

    async def _wait_for_position_quiet(self, market):
        while True:
            last = self.state.last_fill_monotonic[market]
            since = max(self._quiet_since[market], last if last is not None else self._quiet_since[market])
            remaining = self.quiet_s - (self.monotonic() - since)
            if remaining <= 0:
                return
            await self.sleep(remaining)

    async def _reconcile_untimed_position(self, market, quantity):
        quantity = number(quantity)
        fill_ids = frozenset(key for key, fill in self.state.fills.items() if fill[0] == market)
        confirmation = (quantity, fill_ids)
        previous = self._position_confirmation.get(market)
        if previous is not None:
            if previous == confirmation:
                self.state.apply_untimed_position_snapshot(market, quantity)
                self._position_confirmation.pop(market, None)
                return True
        elif quantity == self.state.positions[market] and not self.state.has_unaccounted_fills(market):
            self.state.apply_untimed_position_snapshot(market, quantity)
            return True
        self.state.apply_untimed_position_snapshot(market, quantity)
        self._position_confirmation[market] = confirmation
        self._quiet_since[market] = self.monotonic()
        await self.alert('无时间戳持仓与本地账本不一致，已采用快照并等待 quiet 后复核')
        return False

    async def reconcile(self, market, *, monitor_halted=False):
        gate = getattr(self.api, 'gate', None)
        if type(monitor_halted) is not bool or monitor_halted and (
                gate is None or not gate.is_open or not getattr(gate, 'trading_halted', False)
                or not self.state.stopping):
            self.state.begin_sync(market, '熔断监控对账通道不可用')
            return False
        generation = self.state.begin_sync(market, 'REST 同步中')
        try:
            before_orders = self.state.revision
            rows = await self._call(self.api.get_open_orders(market))
            if not isinstance(rows, list):
                raise OrderStateError('挂单快照必须为完整列表')
            if self.state.revision != before_orders:
                logging.getLogger('pendle.maker').warning('对账未通过：市场=%s 原因=读取挂单快照期间私有状态发生变化', market)
                self.state.begin_sync(market, '读取挂单快照期间私有状态发生变化')
                return False
            listed = set()
            for row in rows:
                parsed = await self.handle_order_event(row, source='rest')
                listed.add(parsed.client_order_id)
            for order in tuple(self.state.active_orders(market)):
                missing = order.client_order_id not in listed
                if missing or order.status in (OrderStatus.SENT, OrderStatus.UNKNOWN, OrderStatus.CANCEL_SENT):
                    found = await self._call(self._get_order(order))
                    if found is not None:
                        await self.handle_order_event(found, source='rest')
                    if missing and not order.terminal:
                        self.state.begin_sync(market, '本地活单缺失于完整 REST 挂单快照')
                        return False
            await self._wait_for_position_quiet(market)
            before_positions = self.state.revision
            positions = await self._call(self.api.get_positions())
            if self.state.revision != before_positions:
                logging.getLogger('pendle.maker').warning('对账未通过：市场=%s 原因=读取持仓快照期间私有状态发生变化', market)
                self.state.begin_sync(market, '读取持仓快照期间私有状态发生变化')
                return False
            self.state.account_positions = [dict(row) for row in positions]
            matches = [row for row in positions if row['market'] == market]
            if len(matches) > 1:
                raise OrderStateError('持仓快照中同一市场不得重复')
            # Full snapshots omit zero positions; synthesize an untimed neutral zero position and retain the quiet-period verification barrier.
            pos = matches[0] if matches else {
                'market': market, 'direction': 'neutral', 'quantity': Decimal(0),
                'unrealized_pnl': Decimal(0)}
            if 'time_ms' in pos:
                self.state.apply_position_snapshot(market, pos['quantity'], pos['time_ms'])
                position_confirmed = True
            else:
                number(pos['unrealized_pnl'])
                position_confirmed = await self._reconcile_untimed_position(market, pos['quantity'])
            if position_confirmed:
                content = self.state._position_event_contents.get(market)
                confirmed = _position_identity(pos)
                # A genuine REST rebase must allow a previously seen conflicting WS position to resynchronize.
                if content is not None and any(old != new for old, new in zip(content, confirmed)
                                               if old is not None and new is not None):
                    self.state._position_event_contents.pop(market, None)
                self.state.position_details[market] = dict(pos)
            before_balance = self.state.revision
            before_balance_values = self.state.balance_revision
            balance = await self._call(self.api.get_balances())
            if self.state.revision != before_balance:
                logging.getLogger('pendle.maker').warning('对账未通过：市场=%s 原因=读取余额快照期间私有状态发生变化', market)
                self.state.begin_sync(market, '读取余额快照期间私有状态发生变化')
                return False
            # A private balance push during the read is newer than this untimed REST snapshot; keep it.
            if self.state.balance_revision == before_balance_values:
                self.state.apply_balance_snapshot(balance)
            unresolved = any(order.status in (OrderStatus.INTENT, OrderStatus.SENT,
                             OrderStatus.UNKNOWN, OrderStatus.CANCEL_SENT)
                             for order in self.state.active_orders(market))
            invalid_monitor = monitor_halted and (not gate.is_open or not gate.trading_halted)
            failed = [name for name, bad in (
                ('持仓未确认', not position_confirmed), ('熔断监控通道无效', invalid_monitor),
                ('停止中', self.state.stopping and not monitor_halted),
                ('死人开关未确认', not self.state.deadman_acked), ('存在未决订单', unresolved),
                ('存在未入账成交', self.state.has_unaccounted_fills(market)),
                ('同步代数变化', generation != self.state.generations[market])) if bad]
            if failed:
                # Record why reconciliation did not settle; silent False results hid a stuck exit (2026-11-07).
                logging.getLogger('pendle.maker').warning(
                    '对账未通过：市场=%s 原因=%s 本地持仓=%s 快照持仓=%s', market, '、'.join(failed),
                    self.state.positions[market], pos['quantity'])
                return False
            self.state.modes[market] = 'HALTED' if monitor_halted else 'RUNNING'
            return True
        except Exception as exc:
            self.state.begin_sync(market, 'REST 同步失败，继续封锁新增')
            await self._handle_error(exc, '对账返回立即熔断类错误')
            return False

    def confirm_local_stop(self):
        """Confirm a local stop only for dry-run without write requests; invoke no API or alerts."""
        self.state.stopping = True
        for market in self.state.markets:
            self.state.modes[market] = 'STOPPING'
        self._stop_task = asyncio.get_running_loop().create_future()
        self._stop_task.set_result(True)
        self._local_stop_confirmed = True
        return True

    def _reuse_stop_confirmation(self, was_stopping):
        """Reuse a completed sweep only while all target-market zero evidence remains valid."""
        gate = getattr(self.api, 'gate', None)
        return (self.stop_sweep_interval_s > 0 and was_stopping
                and self._stop_confirmed_at is not None
                and self.monotonic() - self._stop_confirmed_at < self.stop_sweep_interval_s
                and self._stop_task is not None and self._stop_task.done()
                and not self._stop_task.cancelled() and self._stop_task.exception() is None
                and self._stop_task.result() is True
                and self._stop_confirmed_revisions == self.state.market_revisions
                and not self.state.active_orders()
                and not any(not task.done() for task in self.pending.values())
                and not getattr(self.api, 'in_flight_writes', 0)
                and (gate is None or gate.is_open))

    async def stop(self, reason='停止', *, force=False):
        was_stopping = self.state.stopping
        self._local_stop_confirmed = False
        self.state.stopping = True
        for market in self.state.markets:
            self.state.modes[market] = 'STOPPING'
        if (self._stop_cleanup_task is None or self._stop_cleanup_task.done()):
            if not force and self._reuse_stop_confirmation(was_stopping):
                return True
            self._stop_confirmed_at = None
            self._stop_confirmed_revisions = None
            self._stop_task = asyncio.create_task(self._stop(reason))
            self._stop_cleanup_task = asyncio.create_task(
                self._finish_stop(self._stop_task, reason))
        return await _wait_cleanup(self._stop_cleanup_task)

    async def _finish_stop(self, task, reason):
        stopped = await task
        if stopped is not True:
            # Wait for the stop task to exit before a global halt so halt cleanup cannot wait on itself when stopping again.
            await self.stop_failed(reason)
            if self.stop_failed is _ignore:
                await self.alert('停止流程无法确认清空，等待死人开关并紧急告警')
        return stopped

    async def _stop(self, reason):
        gate = getattr(self.api, 'gate', None)
        scheduled_halt = False
        try:
            unknowns = {order.client_order_id: order for order in self.state.active_orders()
                        if order.status == OrderStatus.UNKNOWN}
            can_query = gate is None or gate.is_open
            waited_for_writes = False
            attempts = {market: 0 for market in self.state.markets}
            confirmed = {}

            def zero_evidence_valid(market):
                return (confirmed.get(market) == self.state.market_revisions[market]
                        and not self.state.active_orders(market))

            while True:
                all_empty = True
                for market in self.state.markets:
                    if zero_evidence_valid(market):
                        continue
                    confirmed.pop(market, None)
                    empty = False
                    while attempts[market] < 3:
                        attempts[market] += 1
                        before = self.state.market_revisions[market]
                        try:
                            await self._call(self.api.cancel_all(market, stop=True))
                            rows = await self._call(self.api.get_open_orders(market, stop=True))
                            if not isinstance(rows, list):
                                raise OrderStateError('停止核实必须得到完整挂单列表')
                            # Cancel accepted orders first, then wait for in-flight writes; later rounds cancel delayed acceptances.
                            if not waited_for_writes:
                                waited_for_writes = True
                                writes = [task for task in self.pending.values() if not task.done()]
                                if writes:
                                    waiting = asyncio.ensure_future(asyncio.gather(*writes, return_exceptions=True))
                                    try:
                                        await self._wait(waiting, self.request_timeout_s, cancel=False)
                                    except TimeoutError:
                                        pass
                            # SENT may become UNKNOWN while waiting; preserve the requirement to resolve its state.
                            unknowns.update((order.client_order_id, order)
                                for order in self.state.active_orders() if order.status == OrderStatus.UNKNOWN)
                            inflight = (any(not task.done() for task in self.pending.values())
                                        or getattr(self.api, 'in_flight_writes', 0))
                            if not rows and not inflight and before == self.state.market_revisions[market]:
                                unresolved = False
                                for order in unknowns.values():
                                    if order.market != market or order.terminal:
                                        continue
                                    if not can_query:
                                        unresolved = True
                                        continue
                                    # After an empty open-order snapshot, confirm UNKNOWN with two cid queries; one empty list cannot release risk.
                                    absent = True
                                    for _proof in range(2):
                                        proof_revision = self.state.market_revisions[market]
                                        found = await self._call(self.api.get_order(order.client_order_id))
                                        if (self.state.market_revisions[market] != proof_revision
                                                or any(not task.done() for task in self.pending.values())
                                                or getattr(self.api, 'in_flight_writes', 0)):
                                            absent, unresolved = False, True
                                            break
                                        if found is not None:
                                            self.state.apply_order_event(found, source='rest')
                                            absent = False
                                            unresolved = unresolved or not order.terminal
                                            break
                                    if absent and order.status == OrderStatus.UNKNOWN:
                                        order.status, order.confirmed_absent = OrderStatus.ABSENT, True
                                    elif absent:
                                        # A late ACK may turn UNKNOWN into OPEN; dual queries and verification of zero open orders still prove cancellation.
                                        order.status = OrderStatus.CANCELED
                                if unresolved:
                                    continue
                                for order in self.state.active_orders(market):
                                    order.status = (OrderStatus.REJECTED if order.status == OrderStatus.INTENT
                                                    else OrderStatus.CANCELED)
                                empty = not self.state.active_orders(market)
                                confirmed[market] = self.state.market_revisions[market]
                                break
                        except Exception as exc:
                            # Record why a stop attempt failed; silent retries made production STOP_FAILED undiagnosable.
                            logging.getLogger('pendle.maker').warning(
                                '停止流程第 %d 次撤单核零失败：市场=%s 异常=%s status=%s error_code=%s',
                                attempts[market], market, type(exc).__name__, getattr(exc, 'status', None),
                                getattr(exc, 'code', None))
                            if getattr(exc, 'category', None) == 'halt':
                                scheduled_halt = self._halt_after_stop_error(exc)
                                return False
                    all_empty = all_empty and empty
                # Allow at most three cancel-all attempts per market; reuse the remaining budget if later market waits or final alerts invalidate earlier evidence.
                if not all_empty:
                    logging.getLogger('pendle.maker').warning(
                        '停止流程三次撤单核零均未成功：本地活动单=%d 在途写=%s',
                        len(self.state.active_orders()), bool(any(not task.done() for task in self.pending.values())
                                                         or getattr(self.api, 'in_flight_writes', 0)))
                    return False
                if not all(zero_evidence_valid(market) for market in self.state.markets):
                    continue
                await self.alert('停止流程已核实挂单为零')
                # Do not await between the final check and successful return; late events during alerts could leave open orders.
                if all(zero_evidence_valid(market) for market in self.state.markets):
                    if self.stop_sweep_interval_s > 0:
                        self._stop_confirmed_at = self.monotonic()
                        self._stop_confirmed_revisions = dict(self.state.market_revisions)
                    return True
        finally:
            if gate is not None and not gate.is_open and not scheduled_halt:
                gate.finish_stop()

    async def drain(self):
        """Reap completed write requests before exit; local cancellation does not count as exchange cancellation."""
        tasks = [task for task in self.pending.values() if task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending = {cid: task for cid, task in self.pending.items() if not task.done()}
        if self.state.stopping:
            gate = getattr(self.api, 'gate', None)
            if gate is None or gate.is_open:
                return await self.stop('处理延迟受理结果')
            await self.alert('发送已封锁，无法再次核实远端挂单为零，等待死人开关撤单')
            return False
        return not self.pending

    async def check_timeouts(self):
        """Called by the OrderManager heartbeat loop; retries do not reset the absolute UNKNOWN deadline."""
        for order in self.state.active_orders():
            if order.unknown_since is not None and order.status == OrderStatus.UNKNOWN:
                await self._unknown_expired(order, order.unknown_since + self.unknown_timeout_s)
            elif (order.status == OrderStatus.CANCEL_SENT and order.cancel_since is not None
                  and self.monotonic() >= order.cancel_since + self.cancel_confirm_timeout_s):
                await self.confirm_cancel(order)

    def prepare_resume(self):
        """Return False while cleanup is in flight; resume only into a new reconciliation barrier."""
        gate = getattr(self.api, 'gate', None)
        if (self._stop_task is None
                or self._stop_task.done() and (self._stop_task.cancelled()
                    or self._stop_task.exception() is not None or self._stop_task.result() is not True)
                or not self._local_stop_confirmed and not self.state.deadman_acked
                or (gate is not None and (
                    not gate.is_open or getattr(gate, 'trading_halted', False)))):
            raise OrdersBlocked('停止尚未确认或闸门已关闭，禁止恢复')
        # Orders may still be cancelling while a sweep is in flight; check them only once it settles.
        if (not self._stop_task.done() or any(not task.done() for task in self.pending.values())
                or getattr(self.api, 'in_flight_writes', 0)):
            return False
        if self.state.active_orders():
            raise OrdersBlocked('停止尚未确认或闸门已关闭，禁止恢复')
        self.state.stopping = False
        self._stop_task = None
        self._local_stop_confirmed = False
        self._stop_confirmed_at = None
        self._stop_confirmed_revisions = None
        for market in self.state.markets:
            self.state.begin_sync(market, '守卫恢复前重新对账')
        return True
