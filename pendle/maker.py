"""Live strategy for a single market; all sends use the order manager and global gate."""

import asyncio
import json
import logging
import math
import signal
import sys
import time
from copy import copy
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from .market import number, parse_ondo
from .maker_orders import AccountState, OrderManager, OrderStatus, OrdersBlocked
from .maker_risk import (Guard, HaltController, MarketRisk, RiskLimits, RiskManager,
                         RiskOrder, RiskRejected, SingleInstanceLock, Watchdog,
                         WindowLossLedger, DurableFiles, _wait_cleanup)
from .ondo_api import ApiError, SendGate
from .maker_reconnect import reconnect_failures

D = Decimal
BPS = D(10000)
HEALTHCHECK_WAIVER_WARNING = ('未配置外部心跳：服务器或网络整体故障时不会收到告警；'
                            '死人开关会撤销挂单，但已有持仓无人处理')


@dataclass(frozen=True)
class MarketRules:
    tick: Decimal
    increment: Decimal
    min_notional: Decimal = D(0)
    max_position_base_size: Decimal | None = None
    default_leverage: Decimal | None = None
    max_leverage: Decimal | None = None

    def __post_init__(self):
        for key in ('tick', 'increment', 'min_notional'):
            object.__setattr__(self, key, number(getattr(self, key)))
        if self.tick <= 0 or self.increment <= 0 or self.min_notional < 0:
            raise ValueError('交易精度与最小金额无效')
        for key in ('max_position_base_size', 'default_leverage', 'max_leverage'):
            value = getattr(self, key)
            if value is not None:
                value = number(value)
                if value <= 0 or key != 'max_position_base_size' and value < 1:
                    raise ValueError('市场持仓上限或杠杆无效')
                object.__setattr__(self, key, value)
        if (self.default_leverage is not None and self.max_leverage is not None
                and self.default_leverage > self.max_leverage):
            raise ValueError('默认杠杆超过市场最大杠杆')


@dataclass(frozen=True)
class Quote:
    side: str
    price: Decimal
    quantity: Decimal
    reduce_only: bool = False


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    center: Decimal | None = None
    quotes: tuple = ()
    competition: Decimal | None = None
    competition_flat: Decimal | None = None
    competition_linear: Decimal | None = None
    center_source: str | None = None


@dataclass(frozen=True)
class Window:
    active: bool
    reducing: bool
    window_id: str


def window_state(settings, at):
    at = at.astimezone(ZoneInfo(settings.timezone))
    hour, minute = map(int, settings.window_start_time.split(':'))
    start = (at - timedelta(days=(at.weekday()-settings.window_start_day) % 7)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if start > at:
        start -= timedelta(days=7)
    hour, minute = map(int, settings.window_end_time.split(':'))
    end = (start + timedelta(days=(settings.window_end_day-settings.window_start_day) % 7)).replace(
        hour=hour, minute=minute)
    if end <= start:
        end += timedelta(days=7)
    active = start <= at < end
    return Window(active, active and end-at <= timedelta(hours=1), start.isoformat())


def risk_orders(state, exclude=None):
    return [RiskOrder(o.market, o.side, o.remaining, o.price, o.status.value)
            for o in state.active_orders() if o is not exclude]


def base_position_capacity(rules, state, market, side, exclude=None):
    """The exchange base-size cap is separate from USD caps; all same-side nonterminal remaining quantities consume capacity."""
    if rules.max_position_base_size is None:
        return None
    direction = 1 if side == 'buy' else -1
    pending = sum((order.remaining for order in state.active_orders(market)
                   if order is not exclude and order.side == side), D(0))
    return max(D(0), rules.max_position_base_size - direction*state.positions[market] - pending)


class QuoteEngine:
    def __init__(self, settings, *, clock=time.time):
        self.settings = settings
        self.clock = clock
        self.anchor = self.anchor_at = self.ema = None
        self.ema_at = self.prior_ema = None
        self.mark = self.external = self.basis = None
        # None is reserved for in-memory state that bypasses public ingestion; live data must provide valid source times.
        self.mark_source_times = self.discovery_source_times = None
        # Invalid times may invalidate freshness but must not erase the ordering baseline for accepted values.
        self.mark_accepted_source_time = None
        self.mark_ws_seen = self.mark_rest_seen = None
        self.public_health = None
        self.oracle = None
        self.in_price_discovery = None
        self.price_discovery_seen = None
        self.price_discovery_invalid = False
        self.subscription_failures = set()
        self.history = deque()
        self.prior_history = ()
        self.center_source = self.source_since = None
        self.slow_anchor = self.slow_anchor_at = self.slow_drift_bps = None
        self.slow_prior_anchor = None
        self.mark_reference = None

    def _public_is_healthy(self, now, public_healthy):
        if public_healthy is not None:
            return public_healthy
        return self.public_health(now) if self.public_health is not None else True

    def _source_times_are_sane(self, times):
        wall = self.clock()
        return times is None or (bool(times) and all(
            -2 <= wall-stamp <= self.settings.mark_source_max_age_s for stamp in times))

    def ws_mark_seen(self):
        # In-memory callers may inject marks directly; REST receipt times must never count as WS updates.
        if self.mark_ws_seen is None and self.mark_rest_seen is None and self.mark is not None:
            return self.mark[1]
        return self.mark_ws_seen

    def mark_is_fresh(self, now, public_healthy=None):
        if self.mark is None or not self._source_times_are_sane(self.mark_source_times):
            return False
        seen = self.ws_mark_seen()
        ws_valid = (self._public_is_healthy(now, public_healthy) and seen is not None
                    and 0 <= now-seen <= self.settings.mark_quiet_max_s)
        rest_valid = (self.mark_rest_seen is not None
                      and 0 <= now-self.mark_rest_seen <= self.settings.mark_rest_confirm_s)
        return ws_valid or rest_valid

    def discovery_is_fresh(self, now, public_healthy=None):
        if (type(self.in_price_discovery) is not bool or self.price_discovery_invalid
                or self.price_discovery_seen is None
                or not self._public_is_healthy(now, public_healthy)
                or not 0 <= now-self.price_discovery_seen <= self.settings.discovery_quiet_max_s):
            return False
        return self._source_times_are_sane(self.discovery_source_times)

    def third_party(self, book, state, exclude=None):
        sides = [dict(book.bids), dict(book.asks)]
        for order in state.active_orders(self.settings.market):
            if order is exclude:
                continue
            if (order.status in (OrderStatus.INTENT, OrderStatus.SENT, OrderStatus.UNKNOWN,
                                 OrderStatus.CANCEL_SENT)
                    or book.source_ms is None or book.source_ms < order.exchange_ms):
                raise OrdersBlocked('我方扣除存在不确定性')
            levels = sides[0 if order.side == 'buy' else 1]
            if levels.get(order.price, D(0)) < order.remaining:
                raise OrdersBlocked('公开盘口尚未覆盖我方剩余量')
            levels[order.price] -= order.remaining
        return tuple(tuple((p, q) for p, q in levels.items() if q > 0) for levels in sides)

    def observe_center(self, book, state, *, now):
        """Observe the center price from valid data; quote pauses and price discovery never freeze the short EMA or ultra-slow anchor."""
        cfg = self.settings
        if self.subscription_failures:
            return Decision(False, '关键公开频道订阅失败，双侧停止报价')
        if type(self.in_price_discovery) is not bool or self.price_discovery_invalid:
            return Decision(False, '价格发现状态未知或无效，双侧停止报价')
        if not self.mark_is_fresh(now):
            return Decision(False, '缺少新鲜标记价格，双侧停止报价')
        if not self.discovery_is_fresh(now):
            return Decision(False, '缺少新鲜价格发现状态确认，双侧停止报价')
        try:
            bids, asks = self.third_party(book, state)
        except OrdersBlocked as exc:
            return Decision(False, str(exc))
        spread = mid = None
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            spread = (asks[0][0]-bids[0][0])/mid*BPS
        source = self.center_source
        if source is None:
            source = 'third_party_mid' if spread is not None and spread <= cfg.narrow_spread_bps else 'mark'
        elif now-self.source_since >= cfg.source_min_dwell_s:
            if source == 'third_party_mid' and (spread is None or spread > cfg.narrow_spread_bps):
                source = 'mark'
            elif source == 'mark' and spread is not None and spread <= cfg.narrow_spread_bps-5:
                source = 'third_party_mid'
        if source != self.center_source:
            self.center_source, self.source_since = source, now
        if source == 'third_party_mid' and mid is None:
            return Decision(False, '中心价来源最短停留等待', center_source=source)
        center = mid if source == 'third_party_mid' else self.mark[0]
        # Update the ultra-slow anchor by elapsed time; guard rejection must not freeze the actual center price.
        if self.slow_anchor is None:
            self.slow_anchor = center
            self.slow_prior_anchor = center
            self.slow_drift_bps = D(0)
        else:
            # Rechecks at the same instant use the prior anchor so an earlier observation cannot relax the guard.
            if now != self.slow_anchor_at:
                self.slow_prior_anchor = self.slow_anchor
            self.slow_drift_bps = abs(center/self.slow_prior_anchor-1)*BPS
            elapsed = max(0, now-self.slow_anchor_at)
            alpha = D(str(-math.expm1(-elapsed/cfg.slow_anchor_tau_s)))
            self.slow_anchor += alpha*(center-self.slow_anchor)
        self.slow_anchor_at = now
        # The short EMA also tracks the actual center price in real time; guard rejection must not freeze observations.
        if self.ema is None:
            self.ema = self.prior_ema = center
        else:
            # Rechecks at the same instant use the prior EMA so one observation cannot relax the drift guard.
            if now != self.ema_at:
                self.prior_ema = self.ema
            elapsed = max(0, now-self.ema_at)
            alpha = D(str(-math.expm1(-elapsed/cfg.anchor_ema_tau_s)))
            self.ema += alpha*(center-self.ema)
        self.ema_at = now
        while self.history and now-self.history[0][0] > cfg.jump_window_s:
            self.history.popleft()
        # Save history before appending this observation; rejected center prices still enter later jump windows.
        self.prior_history = tuple(self.history)
        if not self.history or self.history[-1] != (now, center):
            self.history.append((now, center))
        if source == 'mark' and self.mark_reference is None:
            self.mark_reference = center
        return center, source, spread, bids, asks

    def evaluate(self, book, state, rules, *, now, guard_only=False, existing_prices=False,
                 market_only=False):
        cfg = self.settings
        observed = self.observe_center(book, state, now=now)
        if isinstance(observed, Decision):
            return observed
        center, source, spread, bids, asks = observed
        center_values = {'center_source': source}
        if self.in_price_discovery:
            return Decision(False, '市场处于价格发现，双侧停止报价', center, **center_values)
        fresh_mark = True
        # Continuous mark phases only guard against catastrophic cumulative drift; pauses do not reset them. M14 symmetric quotes are the main defense against self-reference.
        cumulative = (abs(center/self.mark_reference-1)*BPS
                      if source == 'mark' else D(0))
        if (self.slow_drift_bps > cfg.max_slow_drift_bps
                or cumulative >= cfg.max_mark_phase_drift_bps):
            return Decision(False, '超慢锚漂移守卫，双侧停止报价', center, **center_values)
        if spread is not None and spread > cfg.max_spread_bps:
            return Decision(False, '第三方价差超过阈值', center, **center_values)
        if source == 'third_party_mid' and abs(center/self.mark[0]-1)*BPS > cfg.max_mid_mark_bps:
            return Decision(False, '中间价与标记价格偏离超过阈值', center, **center_values)
        # Both sources apply guards using the short EMA and history from before this observation.
        if abs(center/self.prior_ema-1)*BPS > cfg.max_anchor_drift_bps:
            return Decision(False, '偏离独立慢锚', center, **center_values)
        if any(abs(center/old-1)*BPS > cfg.jump_bps for _, old in self.prior_history):
            return Decision(False, '中心价跳变', center, **center_values)
        if self.external is not None and 0 <= now-self.external[1] <= cfg.max_ext_age_s:
            ext = self.external[0]
            if self.basis is None:
                self.basis = center/ext
            elif abs(center/(ext*self.basis)-1)*BPS > cfg.max_ext_drift_bps:
                return Decision(False, '外部参考偏离', center, **center_values)
            else:
                self.basis += cfg.anchor_ema_alpha*(center/ext-self.basis)
        self.anchor, self.anchor_at = center, now
        band = cfg.band_bps/BPS
        competition_flat = competition_linear = D(0)
        for levels, raw in ((bids, book.bids), (asks, book.asks)):
            inside = [(p,q) for p,q in levels if abs(p/center-1) <= band]
            complete = (bool(raw) and abs(raw[-1][0]/center-1) > band
                        or not inside and len(raw) < 100)
            if not complete:
                return Decision(False, '第三方盘口覆盖不完整', center, **center_values)
            competition_flat += sum((p*q for p,q in inside), D(0))
            competition_linear += sum((p*q*max(D(0), 1-abs(p/center-1)/band) for p,q in inside), D(0))
        competition = competition_flat if cfg.gate_weight == 'flat' else competition_linear
        competition_values = {**center_values, 'competition': competition, 'competition_flat': competition_flat,
                              'competition_linear': competition_linear}
        if competition > cfg.gate_comp_max_usd:
            return Decision(False, '竞争量超过判据', center, **competition_values)
        if source == 'third_party_mid':
            self.mark_reference = None
        # Validate fixed orders by actual price and remaining quantity; hypothetical two-sided quotes must not constrain retention or sending.
        if market_only:
            return Decision(True, '判据通过', center, **competition_values)
        position = state.positions[cfg.market]
        pos_usd = position*center
        # The mark may fully track our own order book midpoint; keep offsets symmetric and adjust only quantity for inventory.
        skew = (D(0) if source == 'mark' else
                max(D(-1), min(D(1), pos_usd/cfg.soft_cap_usd))*cfg.skew_bps)
        quotes = []
        risk = RiskManager(RiskLimits(quote_notional=cfg.quote_notional,
            hard_cap_usd=cfg.hard_cap_usd, max_position_usd=cfg.max_position_usd,
            max_account_exposure_usd=cfg.max_account_exposure_usd, max_margin_ratio=cfg.max_margin_ratio))
        existing = ({order.side: order for order in state.active_orders(cfg.market)}
                    if existing_prices else {})
        for side, offset, opposite in (('buy',cfg.offset_bps+skew,asks), ('sell',cfg.offset_bps-skew,bids)):
            adding = position == 0 or (position > 0) == (side == 'buy')
            soft = abs(pos_usd) >= cfg.soft_cap_usd
            soft_edge = adding and soft and source == 'third_party_mid'
            if soft_edge:
                offset = cfg.band_bps-cfg.band_margin_bps
            price = center*(1 + (-offset if side == 'buy' else offset)/BPS)
            # Orders adding exposure at the soft cap sit near the band edge; round inward, then recheck minimum offset and post-only.
            rounding = (ROUND_CEILING if side == 'buy' else ROUND_FLOOR) if soft_edge else (
                ROUND_FLOOR if side == 'buy' else ROUND_CEILING)
            price = (price/rules.tick).to_integral_value(rounding=rounding)*rules.tick
            amount = cfg.quote_notional*(D('.5') if adding and soft else 1)
            quantity = (amount/price/rules.increment).to_integral_value(rounding=ROUND_FLOOR)*rules.increment
            # Fixed-order and existing two-sided checks reserve no hypothetical same-side order; actual sends still check all active orders.
            if not guard_only:
                quantity = risk.limit_adding_quantity(cfg.market, side, quantity, rules.increment,
                    [MarketRisk(cfg.market, position, max(center, self.mark[0]) if fresh_mark else center)],
                    risk_orders(state), price=price)
            if not adding and soft:
                quantity = min(quantity, (abs(position)/rules.increment).to_integral_value(
                    rounding=ROUND_FLOOR)*rules.increment)
            # Check the current price when retaining valid orders; theoretical quotes still set requote thresholds, and sends validate new prices by default.
            old = existing.get(side)
            capacity = base_position_capacity(rules, state, cfg.market, side, exclude=old)
            # Fixed orders in guard_only are checked separately at send time; do not reserve another hypothetical same-side order.
            if capacity is not None and (not guard_only or existing_prices):
                quantity = min(quantity, (capacity/rules.increment).to_integral_value(
                    rounding=ROUND_FLOOR)*rules.increment)
                checked_base = old.remaining if old is not None else quantity
                if checked_base <= 0 or checked_base > capacity:
                    return Decision(False, '交易所最大基础持仓量限制，双侧停止报价',
                                    center, **competition_values)
            checked_price = old.price if old is not None else price
            checked_quantity = old.remaining if old is not None else quantity
            distance = abs(checked_price/center-1)*BPS
            if (not cfg.min_offset_bps <= distance <= cfg.band_bps-cfg.band_margin_bps
                    or checked_quantity <= 0 or old is None and quantity*price < rules.min_notional
                    or opposite and (checked_price >= opposite[0][0] if side == 'buy'
                                     else checked_price <= opposite[0][0])):
                return Decision(False, '取整后任一侧不合规', center, **competition_values)
            if fresh_mark:
                mark = self.mark[0]
                if (not self.mark_is_fresh(now) or
                        not cfg.min_offset_bps <= abs(checked_price/mark-1)*BPS <= cfg.band_bps-cfg.band_margin_bps):
                    return Decision(False, '标记价格辅助守卫', center, **competition_values)
            quotes.append(Quote(side, price, quantity, soft and not adding and cfg.reduce_only_gtc_verified))
        return Decision(True, '判据通过', center, tuple(quotes), **competition_values)


class Maker:
    """Safety orchestrator; synchronous ticks and actual send callbacks both enforce risk checks."""

    def __init__(self, settings, *, api, alerter, rules=None, files=None,
                 monotonic=time.monotonic, clock=time.time, sleep=asyncio.sleep,
                 connect=None, logger=None, public_http=None):
        self.settings, self.api, self.alerter = settings, api, alerter
        self.files, self.monotonic, self.clock, self.sleep = files or DurableFiles(), monotonic, clock, sleep
        self.connect, self.logger, self.rules = connect, logger, rules
        self.public_http = public_http
        self.initial_margin_rate = D('.1')
        self.reconcile_lock = asyncio.Lock()
        self.gate = api.gate if api is not None else SendGate()
        self.state = AccountState([settings.market])
        self.engine, self.risk = QuoteEngine(settings, clock=clock), RiskManager(settings.risk_limits)
        self.engine.public_health = self.public_is_healthy
        self.book, self.book_seen = None, None
        self.public_disconnected = False
        self.mark_rest_last_poll = None
        self._mark_rest_polling = False
        self.mark_older_counts = Counter()
        self.private_ws, self.public_ws = None, None
        self.public_pong_seen = None
        self._public_pong_event = asyncio.Event()
        self._public_ping_deadline = None
        self.public_subscriptions = set()
        self.private_logged_in,self.deadman_requested=False,False
        self.deadman_disabled = False
        self._deadman_disable_requested = False
        self._deadman_disabled_event = asyncio.Event()
        self.ack_wait_started, self.ack_wait_phase = None, '登录'
        self.stop_event = asyncio.Event()
        self.reconcile_event = asyncio.Event()
        self.last_quote = self.last_health = self.last_requote = None
        self.last_decision = self.decision_seen = None
        self.book_intervals = deque(maxlen=10000)
        self.observation_started = self.observation_at = self.monotonic()
        self.observed_qualified, self.observed_reason = False, '等待首次策略评估'
        self.qualified_seconds = 0.0
        self.pause_counts, self.pause_seconds = Counter(), Counter()
        self.observed_center_source = None
        self.center_source_seconds = {'third_party_mid': 0.0, 'mark': 0.0}
        self.last_stop_reason = None
        self.last_fill_ms = 0
        self.ledger = None
        self.ledgers, self.perwindow_fill_sync_ms = {}, {}
        self.last_position_rows = []
        self.waiting, self.qualified = True, False
        self.market_data_wait_reason = None
        self.reject_count, self.backoff_until = 0, 0
        self.gate_wait_until,self.last_gate_status=0,None
        self.passive_reduce_unavailable=False
        self._shutdown = False
        self._stop_failed = False
        self._order_alerts = deque()
        self.exit_code, self.started = 0, False
        self.halt_result, self.emergency_attempted, self.emergency_active = None,False,False
        self.last_halt_alert = self.last_residual_alert = self.last_reconcile_at = None
        self.instance = SingleInstanceLock(settings.instance_lock_path, files=self.files)
        self.manager = OrderManager(self.state, api, risk_check=self.check_order,
            monotonic=monotonic, sleep=sleep, alert=self.alert, halt=self.trip_halt,
            stop_failed=self.stop_failed, reject=self.record_order_rejection,
            request_timeout_s=settings.request_timeout_s,
            unknown_query_attempts=settings.unknown_query_attempts, unknown_timeout_s=settings.unknown_timeout_s,
            unknown_query_interval_s=settings.unknown_query_interval_s,
            cancel_confirm_timeout_s=settings.cancel_confirm_timeout_s, quiet_s=settings.quiet_s)
        self.halt = HaltController(settings.halt_path, gate=self.gate, stop=self.stop_orders,
            alert=self.urgent_alert, files=self.files, clock=clock,monitor_after_halt=True)
        self.guard = Guard(settings.cooldown_s, settings.stable_s,
            stop=self.stop_orders, block=lambda: self.state.begin_sync(settings.market,'守卫屏障'),
            resume=self.manager.prepare_resume, monotonic=monotonic)
        # Dry-run starts no order-writing loop; without credentials, it also skips private feeds and reconciliation.
        required_tasks = {'Strategy'}
        if settings.mode != 'dry-run':
            required_tasks.add('OrderManager')
        if settings.mode != 'dry-run' or api is not None:
            required_tasks.update(('PrivateFeed', 'Reconciler'))
        self.watchdog = Watchdog(gate=self.gate, stop=self.stop_orders, ping=self.ping,
            alert=self.urgent_alert, reconciler_interval_s=settings.reconcile_interval_s,
            monotonic=monotonic, sleep=sleep, required_tasks=required_tasks)

    async def alert(self, reason):
        # An order coroutine must not await a stop task that includes itself; queue its alerts after cancellation.
        current = asyncio.current_task()
        if current in self.manager.pending.values():
            self._order_alerts.append(reason)
            return False
        if current is self.manager._stop_task:
            await self._flush_order_alerts()
        return await self.alerter.alert(reason)

    async def _flush_order_alerts(self):
        while self._order_alerts:
            await self.alerter.alert(self._order_alerts.popleft())

    async def urgent_alert(self, reason):
        return await self.alerter.alert(reason, urgent=True)

    async def record_order_rejection(self, reason):
        """Log rejections from every order path and include them in health statistics for quote-stop reasons."""
        self.log({'事件': '下单被拒绝', '原因': reason}, level=logging.WARNING)
        self.record_quote_state(False, reason, event=True)

    async def stop_failed(self, reason):
        """Halt after the stop task finishes so stopping and halting cannot wait on each other."""
        if self._stop_failed:
            return
        self._stop_failed = True
        previous_halt_finished = self.halt._task is not None and self.halt._task.done()
        if self.halt._task is None:
            await self.trip_halt('STOP_FAILED：'+reason, permanent=True)
        # An existing halt task has already attempted lock persistence; it sends the merged urgent alert.
        self.gate.finish_stop()
        self.exit_code = 76
        if previous_halt_finished:
            await self.urgent_alert('STOP_FAILED：'+reason)
        self.request_stop()

    async def trip_halt(self, reason, *, permanent=False):
        result = await self.halt.trigger(reason, permanent=permanent)
        self.halt_result=result
        if result.persisted and result.stopped and self.gate.is_open:
            for market in self.state.markets:
                self.state.modes[market]='HALTED'
        self.exit_code = result.exit_code
        if not result.persisted or not result.stopped or not self.gate.is_open:
            self.request_stop()
        return result

    async def startup(self, *, live=False):
        cfg = self.settings
        if (cfg.mode == 'live') != bool(live):
            raise ValueError('live 必须同时配置 mode=live 并传入 --live')
        self.instance.acquire()
        try:
            self.halt.assert_can_start()
            if cfg.mode == 'live':
                if (not cfg.guards_calibrated or not cfg.sandbox_verified
                        or not (cfg.healthcheck_url or cfg.healthcheck_waived)):
                    raise ValueError('live 前必须标定守卫、验收沙盒，并配置外部心跳或明确放弃')
                if not await self.alerter.self_test():
                    raise ValueError('Telegram 自检失败，拒绝 live')
                if self.api is None:
                    raise ValueError('live 缺少交易密钥')
                await self.api.sync_time()
            elif cfg.mode == 'sandbox' and self.api is None:
                raise ValueError('sandbox 缺少交易密钥')
            if self.rules is None:
                await self.load_rules()
            window = window_state(cfg, datetime.fromtimestamp(self.clock(), ZoneInfo(cfg.timezone)))
            self.ledger = self.ledger_for_window(window.window_id)
            self.started = True
            if cfg.mode == 'live' and not cfg.healthcheck_url and cfg.healthcheck_waived:
                await self.urgent_alert(HEALTHCHECK_WAIVER_WARNING)
            # Allow task startup grace before the first check; reconnects must not refresh these health timestamps.
            for name in self.watchdog.thresholds:
                self.watchdog.last_seen[name] = self.monotonic()
            self.ack_wait_started = self.monotonic()
        except BaseException:
            self.instance.release()
            raise

    def ledger_for_window(self, window_id):
        """Isolate ledgers by account and window; late fills must not switch the active risk ledger."""
        if window_id not in self.ledgers:
            path = Path(self.settings.data_dir)/(
                'pnl-'+self.settings.account+'-'+window_id[:10]+'.json')
            self.ledgers[window_id] = WindowLossLedger(window_id, path, files=self.files)
        return self.ledgers[window_id]

    def check_order(self, order, state):
        return self.validate_resting_or_pending(order, state, now=self.monotonic())

    def validate_resting_or_pending(self, order, state, *, now):
        """Share full validation between sending and retaining orders; count the candidate only once at its remaining quantity."""
        cfg, remaining = self.settings, order.remaining
        if self.stop_event.is_set():
            raise OrdersBlocked('进程正在退出，禁止新增')
        if cfg.mode == 'dry-run':
            raise OrdersBlocked('dry-run 禁止全部写请求')
        if any(row['market'] != cfg.market and number(row['quantity']) != 0
               for row in state.account_positions):
            raise RiskRejected('单市场账户存在其他市场持仓')
        if (not self.gate.is_open or self.book is None or self.book_seen is None
                or not 0 <= now-self.book_seen <= cfg.max_book_age_s):
            raise OrdersBlocked('行情过期或发送闸门关闭')
        if (self.rules is None or remaining <= 0 or order.price <= 0
                or order.price % self.rules.tick or remaining % self.rules.increment
                or remaining*order.price < self.rules.min_notional):
            raise RiskRejected('固定订单不满足交易精度或最小金额')
        capacity = base_position_capacity(self.rules, state, cfg.market, order.side, exclude=order)
        if capacity is not None and remaining > capacity:
            raise RiskRejected('固定订单超过交易所最大基础持仓量')
        if order.time_in_force == 'IOC':
            center=self.reduction_center(exclude=order)
            if (not order.reduce_only or order.post_only or center is None
                    or order.side=='sell' and order.price<center*(1-cfg.max_flatten_slippage_bps/BPS)
                    or order.side=='buy' and order.price>center*(1+cfg.max_flatten_slippage_bps/BPS)):
                raise RiskRejected('IOC 固定限价超过最新第三方中心的滑点上限')
        else:
            window=window_state(cfg,datetime.fromtimestamp(self.clock(),ZoneInfo(cfg.timezone)))
            if not window.active:
                raise OrdersBlocked('固定挂单排队期间已越过交易窗口')
            if window.reducing and not order.reduce_only:
                raise OrdersBlocked('窗口最后一小时禁止发送非 reduceOnly 挂单')
            snapshot=copy(state)
            # Published orders still count in order book subtraction; exclude unpublished intents only from the order book snapshot.
            snapshot.orders={cid:existing for cid,existing in state.orders.items()
                if existing is not order or order.status in (
                    OrderStatus.OPEN,OrderStatus.PARTIAL,OrderStatus.ACKED)}
            engine=copy(self.engine)
            engine.history=self.engine.history.copy()
            decision=engine.evaluate(self.book,snapshot,self.rules,now=now,guard_only=True,
                                     market_only=True)
            if not decision.eligible or decision.center is None:
                raise OrdersBlocked('发送前最新盘口复核不通过：'+decision.reason)
            center=decision.center
            bids,asks=self.engine.third_party(self.book,snapshot)
            opposite=asks if order.side=='buy' else bids
            # Theoretical prices set ordinary requote thresholds, not the inner compliance boundary for fixed orders.
            direction = -1 if order.side == 'buy' else 1
            distance=direction*(order.price/center-1)*BPS
            if (not order.post_only or not cfg.min_offset_bps<=distance<=cfg.band_bps-cfg.band_margin_bps
                    or opposite and (order.price>=opposite[0][0] if order.side=='buy'
                                     else order.price<=opposite[0][0])):
                raise RiskRejected('固定挂单不满足最新 band、距离或 post-only 条件')
            position=state.positions[cfg.market]
            adding=position==0 or (position>0)==(order.side=='buy')
            soft=abs(position*center)>=cfg.soft_cap_usd
            amount=cfg.quote_notional*(D('.5') if adding and soft else 1)
            # A fixed price may remain valid, but quantity must be limited again using that price and current active orders.
            quantity=(amount/order.price/self.rules.increment).to_integral_value(
                rounding=ROUND_FLOOR)*self.rules.increment
            quantity=self.risk.limit_adding_quantity(cfg.market,order.side,quantity,self.rules.increment,
                [MarketRisk(cfg.market,position,max(center,self.engine.mark[0]))],
                risk_orders(state,exclude=order),price=order.price)
            if soft and not adding:
                quantity=min(quantity,(abs(position)/self.rules.increment).to_integral_value(
                    rounding=ROUND_FLOOR)*self.rules.increment)
            if remaining>quantity:
                raise RiskRejected('固定挂单数量超过最新单侧报价额、软上限或最终持仓容量')
            if self.engine.mark is not None:
                mark,seen=self.engine.mark
                if (not self.engine.mark_is_fresh(now) or not cfg.min_offset_bps
                        <=direction*(order.price/mark-1)*BPS<=cfg.band_bps-cfg.band_margin_bps):
                    raise RiskRejected('固定挂单不满足最新标记价格辅助守卫')
        balance = state.balances
        if not balance or balance.get('under_liquidation') is not False:
            raise RiskRejected('余额不完整或账户正在清算')
        # Value positions at the reference price; RiskManager separately accounts for each active or new order at its higher limit price.
        price = max(center, self.engine.mark[0] if self.engine.mark else D(0))
        rows = [MarketRisk(cfg.market, state.positions[cfg.market], price,
                           initial_margin_rate=self.initial_margin_rate,unrealized_pnl=self.unrealized())]
        active = risk_orders(state, exclude=order)
        ratio=balance.get('margin_ratio')
        if ratio is None:
            ratio=self.current_margin_ratio(exclude=order,state=state,center=center)
        return self.risk.check_order(order.market, order.side, remaining, order.price,
            rows, active, balance['equity'], balance['available'], reduce_only=order.reduce_only,
            exchange_margin_ratio=ratio)

    def unrealized(self):
        return sum((number(row.get('unrealized_pnl',0)) for row in self.last_position_rows), D(0))

    def current_margin_ratio(self, exclude=None, *, state=None, center=None):
        state=self.state if state is None else state
        balance=state.balances
        if balance.get('margin_ratio') is not None:
            return number(balance['margin_ratio'])
        if not balance or number(balance.get('equity',0))<=0:
            return None
        if center is None:
            center=self.reduction_center(exclude=exclude) or self.engine.anchor
        used=max(D(0),number(balance.get('used_margin',0)))
        if center is not None:
            market=MarketRisk(self.settings.market,state.positions[self.settings.market],center,
                initial_margin_rate=self.initial_margin_rate,unrealized_pnl=self.unrealized())
            exposures=self.risk.worst_exposures([market],risk_orders(state,exclude=exclude))
            used=max(used,self.risk._margin({market.market:market},exposures))
        return used/number(balance['equity'])

    async def stop_orders(self, reason):
        self.last_stop_reason = reason
        self.record_quote_state(False, reason, event=True)
        self.qualified = False
        if self._stop_failed:
            return False
        if self.settings.mode == 'dry-run':
            stopped = self.manager.confirm_local_stop()
            self.log({'事件':'已确认停止','模式':'dry-run','原因':reason})
            return stopped
        stopped = await self.manager.stop(reason)
        await self._flush_order_alerts()
        return stopped

    async def reconcile_once(self):
        async with self.reconcile_lock:
            return await self._reconcile_once()

    async def _reconcile_once(self):
        if self.api is None or not self.gate.is_open:
            return False
        cfg = self.settings
        try:
            query_window_id = self.ledger.window_id if self.ledger else None
            start_ms = int(datetime.fromisoformat(query_window_id).timestamp()*1000) if query_window_id else 0
            previous_sync = self.perwindow_fill_sync_ms.get(query_window_id, start_ms)
            # The latest WS fill does not prove REST reconciliation is complete; the first query for a new window must start at its beginning.
            fills = await self.api.get_fills(cfg.market, start_time=max(start_ms,previous_sync-10000))
            for fill in fills:
                await self.record_fill(fill)
            if self.gate.trading_halted:
                ok=await self.manager.reconcile(cfg.market,monitor_halted=True)
            else:
                ok=await self.manager.reconcile(cfg.market)
            if any(row['market'] != cfg.market and number(row['quantity']) != 0
                   for row in self.state.account_positions):
                await self.trip_halt('单市场账户存在其他市场持仓')
                return False
            self.last_position_rows = [self.state.position_details[cfg.market]] if hasattr(
                self.state,'position_details') and cfg.market in self.state.position_details else []
            self.watchdog.beat('Reconciler')
            if ok:
                self.last_reconcile_at=self.monotonic()
                if query_window_id is not None:
                    self.perwindow_fill_sync_ms[query_window_id] = max(
                        previous_sync, start_ms, max((fill['time_ms'] for fill in fills), default=start_ms))
            if ok and self.ledger and self.last_position_rows:
                self.ledger.set_unrealized(self.unrealized(), source_ms=int(self.clock()*1000))
                self.record_funding()
            balance = self.state.balances
            if balance.get('margin_ratio') is None:
                await self.alerter.alert('交易所保证金率不可用，使用保守本地估算',key='保证金估算')
            return ok
        except ApiError as exc:
            if exc.category == 'halt':
                await self.trip_halt('对账接口返回立即熔断错误')
            else:
                await self.guard.trigger('对账接口失败')
            return False

    async def record_fill(self, fill):
        fields = {key:fill[key] for key in ('fill_id','market','side','quantity','time_ms')}
        fields.update({key:fill.get(key,0 if key != 'client_order_id' else None)
                       for key in ('client_order_id','realized_pnl','fee')})
        self.state.apply_fill(**fields)
        self.last_fill_ms = max(self.last_fill_ms, fill['time_ms'])
        occurred = datetime.fromtimestamp(fill['time_ms']/1000, ZoneInfo('UTC'))
        window = window_state(self.settings, occurred)
        ledger = self.ledger_for_window(window.window_id)
        ledger.record(fill['fill_id'],realized_pnl=fill.get('realized_pnl',0),fee=fill.get('fee',0))

    async def tick(self):
        try:
            await self._tick()
        finally:
            self.observe_quote_state()

    async def _tick(self):
        cfg, now = self.settings, self.monotonic()
        # Settle the previous source interval first; early returns such as missing data must not reuse the old center price.
        self.accrue_quote_time()
        self.observed_center_source = None
        self.market_data_wait_reason = None
        self.watchdog.beat('Strategy')
        if self.stop_event.is_set():
            return
        window = window_state(cfg,datetime.fromtimestamp(self.clock(),ZoneInfo(cfg.timezone)))

        def observe_paused_center():
            # Update only the valid in-memory center price; risk pauses must not freeze the short EMA or ultra-slow anchor, or trigger sends.
            if (self.book is not None and self.book_seen is not None
                    and 0 <= now-self.book_seen <= cfg.max_book_age_s):
                observed = self.engine.observe_center(self.book, self.state, now=now)
                if not isinstance(observed, Decision):
                    self.observed_center_source = observed[1]

        if not self.gate.is_open:
            observe_paused_center()
            self.waiting, self.qualified = False, False
            return
        if self.halt.exit_code is not None:
            observe_paused_center()
            self.waiting,self.qualified=True,False
            await self.monitor_halted_tick()
            return
        if window.active and self.ledger is not None and self.ledger.window_id!=window.window_id:
            if not await self.stop_orders('新窗口开始前清理旧单'):
                observe_paused_center()
                await self.trip_halt('窗口轮换撤单无法确认')
                return
            self.ledger=self.ledger_for_window(window.window_id)
        balance = self.state.balances
        margin_ratio=self.current_margin_ratio()
        if balance.get('under_liquidation') or (margin_ratio is not None
                and margin_ratio > cfg.max_margin_ratio):
            observe_paused_center()
            await self.trip_halt('账户清算状态或交易所保证金率超过上限')
            return
        if self.ledger and self.ledger.exceeds(cfg.max_window_loss_usd):
            observe_paused_center()
            await self.trip_halt('本轮窗口累计亏损超过上限')
            return
        self.waiting = not window.active
        if not window.active:
            observe_paused_center()
            await self.stop_orders('窗口外停止报价')
            if self.state.positions[cfg.market] and (self.last_residual_alert is None
                    or now-self.last_residual_alert>=900):
                await self.alerter.alert('窗口结束仍有持仓，继续监控',key='窗口残余持仓',urgent=True)
                self.last_residual_alert=now
            return
        active_orders = self.state.active_orders(cfg.market)
        if self.book is None or self.book_seen is None:
            if active_orders:
                await self.guard.trigger('公开盘口过期')
            else:
                self.market_data_wait_reason = '等待首个盘口'
                self.waiting, self.qualified = True, False
            return
        if now-self.book_seen > cfg.max_book_age_s:
            await self.guard.trigger('公开盘口过期')
            return
        if self.engine.mark is not None and not self.engine.mark_is_fresh(now):
            await self.guard.trigger('缺少新鲜标记价格，双侧停止报价')
            return
        if (self.engine.price_discovery_seen is not None
                and not self.engine.discovery_is_fresh(now)):
            await self.guard.trigger('缺少新鲜价格发现状态确认，双侧停止报价')
            return
        # Wait if initial data is incomplete; existing active orders still require full guard checks and cancellation.
        if not active_orders:
            if self.engine.mark is None:
                self.market_data_wait_reason = '等待首个标记价格'
            elif self.engine.price_discovery_seen is None:
                self.market_data_wait_reason = '等待首个价格发现状态'
            if self.market_data_wait_reason is not None:
                self.waiting, self.qualified = True, False
                return
        decision = self.engine.evaluate(self.book,self.state,self.rules,now=now,
            guard_only=bool(self.state.active_orders(cfg.market)),
            existing_prices=bool(self.state.active_orders(cfg.market)))
        self.last_decision, self.decision_seen = decision, now
        self.observed_center_source = decision.center_source
        flatten_center=self.reduction_center()
        if (cfg.mode != 'dry-run' and flatten_center is not None
                and abs(self.state.positions[cfg.market]*flatten_center) >= cfg.hard_cap_usd):
            await self.flatten(flatten_center)
            return
        if not decision.eligible:
            self.waiting, self.qualified = True, False
            if decision.reason in ('竞争量超过判据','第三方盘口覆盖不完整') and now>=self.gate_wait_until:
                self.gate_wait_until=now+cfg.gate_interval_s
            # Cancel all first when eligibility requires both sides to stop; the stop flow continues resolving UNKNOWN orders.
            await self.guard.trigger(decision.reason)
            if self.last_gate_status is not False:
                await self.alerter.alert('报价判据不通过：'+decision.reason,key='判据变化')
                self.last_gate_status=False
            return
        # Syncing, ACK waits, eligibility waits, backoff, and requote throttling must not delay cancellation of invalid orders.
        try:
            for order in self.state.active_orders(cfg.market):
                self.validate_resting_or_pending(order,self.state,now=now)
        except (OrdersBlocked,RiskRejected) as exc:
            await self.stop_orders('既有挂单发送前复核不通过，先撤销双边：'+str(exc))
            return
        if now<self.gate_wait_until:
            self.waiting,self.qualified=True,False
            return
        if self.last_gate_status is not True:
            await self.alerter.alert('报价判据通过',key='判据变化')
            self.last_gate_status=True
        if (cfg.mode != 'dry-run' and not self.state.deadman_acked
                and (self.guard.state != 'RUNNING' or self.state.stopping)):
            return
        if self.guard.state != 'RUNNING':
            if not await self.guard.observe(True):
                return
            if cfg.mode != 'dry-run' and not await self.reconcile_once():
                return
        if self.state.stopping:
            if cfg.mode == 'dry-run':
                self.state.stopping = False
            else:
                self.manager.prepare_resume()
                if not await self.reconcile_once():
                    return
        self.waiting = False
        quotes = decision.quotes
        position = self.state.positions[cfg.market]
        if window.reducing:
            if self.passive_reduce_unavailable:
                self.waiting=True
                await self.stop_orders('窗口末尾 reduceOnly GTC 不可用，停止报价并等待人工减仓')
                await self.alerter.alert('reduceOnly GTC 被拒，窗口末尾无法安全被动减仓',key='窗口降仓不可用')
                return
            if not position:
                self.waiting=True
                await self.stop_orders('窗口末尾已归零')
                return
            side = 'sell' if position > 0 else 'buy'
            quotes = tuple(Quote(q.side,q.price,min(q.quantity,abs(position)),True)
                           for q in quotes if q.side == side)
        if cfg.mode == 'dry-run':
            self.qualified = True
            self.log({'事件':'拟报价','中心价':str(decision.center),
                '中心价来源':decision.center_source,
                **self.competition_metrics(),
                '报价':[{'方向':q.side,'价格':str(q.price),'数量':str(q.quantity)} for q in quotes]})
            return
        active = self.state.active_orders(cfg.market)
        desired = {q.side:q for q in quotes}
        incomplete=bool(active) and ({o.side for o in active}!=set(desired) or len(active)!=len(desired))
        if incomplete:
            await self.stop_orders('任一侧终结，先撤销残留单边')
            return
        if self.state.market_mode(cfg.market) != 'RUNNING' or not self.state.deadman_acked:
            return
        if now < self.backoff_until:
            return
        needs_change = not active or any(o.side not in desired or o.reduce_only != desired[o.side].reduce_only
            or abs(o.price/desired[o.side].price-1)*BPS > cfg.requote_bps for o in active)
        if not needs_change:
            self.qualified = len(active) == len(quotes)
            return
        if active:
            if self.last_requote is not None and now-self.last_requote < cfg.min_requote_interval_s:
                return
            # Send one market-wide cancellation per ordinary requote cycle; reconcile this tick and price from the latest state on the next.
            if await self.stop_orders('合规挂单偏离理论价超过重报阈值，按市场撤销双边'):
                self.last_requote = self.monotonic()
                if self.state.deadman_acked and self.gate.is_open and not self.gate.trading_halted:
                    self.manager.prepare_resume()
                    await self.reconcile_once()
            return
        for q in quotes:
            order = self.manager.create_intent(cfg.market,q.side,q.quantity,q.price,reduce_only=q.reduce_only)
            await self.manager.submit(order)
            if order.status not in (OrderStatus.OPEN,OrderStatus.ACKED,OrderStatus.PARTIAL):
                detail = '：' + order.last_error_reason if order.last_error_reason else ''
                if order.last_error_category == 'reduce_only_fallback':
                    self.settings=replace(self.settings,reduce_only_gtc_verified=False)
                    self.engine.settings=self.settings
                    self.passive_reduce_unavailable=True
                    await self.stop_orders('切换被动减仓参数前撤销双边' + detail)
                    await self.alert('reduceOnly GTC 不可用，切换持仓数量内的普通 post-only 回退')
                elif order.last_error_category == 'recompute':
                    self.reject_count += 1
                    self.backoff_until = self.monotonic()+min(8,2**min(self.reject_count,3))
                    if self.reject_count >= 5:
                        await self.guard.trigger('post-only 连续拒绝五次' + detail)
                    else:
                        await self.stop_orders('post-only 拒绝，撤销另一侧等待重算' + detail)
                elif self.gate.is_open:
                    await self.guard.trigger('任一侧下单失败，撤销双边' + detail)
                return
        self.reject_count = 0
        self.last_quote = self.monotonic()
        self.last_requote = self.last_quote
        self.qualified = True

    async def flatten(self, center):
        try:
            async with asyncio.timeout(self.settings.flatten_timeout_s):
                return await self._flatten(center)
        except TimeoutError:
            await self.trip_halt('硬上限减仓超过总时限')
            return False

    async def _flatten(self, center):
        cfg = self.settings
        if cfg.mode == 'dry-run':
            return False
        deadline = self.monotonic()+cfg.flatten_timeout_s
        if not await self.stop_orders('达到硬上限，先撤单再 IOC 减仓'):
            await self.trip_halt('硬上限撤单无法确认')
            return False
        self.manager.prepare_resume()
        if not await self.reconcile_once():
            await self.trip_halt('硬上限减仓前对账失败')
            return False
        for _ in range(cfg.flatten_attempts):
            if self.monotonic() >= deadline or not self.gate.is_open:
                break
            center = self.reduction_center()
            if center is None:
                await self.trip_halt('硬上限减仓缺少新鲜中心价，停止 IOC 尝试')
                return False
            position = self.state.positions[cfg.market]
            if abs(position)*center < cfg.soft_cap_usd:
                return True
            quantity = abs(position)-(cfg.soft_cap_usd/center-self.rules.increment)
            quantity = min(abs(position),(quantity/self.rules.increment).to_integral_value(
                rounding=ROUND_CEILING)*self.rules.increment)
            side = 'sell' if position > 0 else 'buy'
            bound = center*(1+(-1 if side == 'sell' else 1)*cfg.max_flatten_slippage_bps/BPS)
            price = (bound/self.rules.tick).to_integral_value(
                rounding=ROUND_CEILING if side == 'sell' else ROUND_FLOOR)*self.rules.tick
            order = self.manager.create_intent(cfg.market,side,quantity,price,
                reduce_only=True,time_in_force='IOC',post_only=False)
            await self.manager.submit(order)
            if order.status == OrderStatus.UNKNOWN:
                await self.manager.resolve_unknown(order)
            if not order.terminal:
                break
            if not await self.reconcile_once():
                break
        if abs(self.state.positions[cfg.market])*center < cfg.soft_cap_usd:
            return True
        await self.trip_halt('IOC 减仓预算耗尽或仍未降至软上限以下')
        return False

    def reduction_center(self, exclude=None):
        """Use a fresh third-party midpoint each cycle for reduction slippage; fall back to an unexpired slow anchor only when a side is missing."""
        cfg,now=self.settings,self.monotonic()
        if self.book is None or self.book_seen is None or now-self.book_seen>cfg.max_book_age_s:
            return None
        try:
            bids,asks=self.engine.third_party(self.book,self.state,exclude=exclude)
            if bids and asks:
                return (bids[0][0]+asks[0][0])/2
        except OrdersBlocked:
            return None
        if self.engine.anchor_at is not None and now-self.engine.anchor_at<=cfg.anchor_max_age_s:
            return self.engine.anchor
        return None

    async def monitor_halted_tick(self):
        try:
            async with asyncio.timeout(self.settings.flatten_timeout_s):
                return await self._monitor_halted_tick()
        except TimeoutError:
            self.emergency_attempted=True
            self.emergency_active=False
            self.state.stopping=True
            await self.stop_orders('紧急减仓总时限耗尽，撤单确认后禁止再次自动尝试')
            await self.alerter.alert('紧急 IOC 已超过总时限，等待人工处理',urgent=True)

    async def _monitor_halted_tick(self):
        """Keep ordinary trading locked; emergency access gets a single budget of three attempts within sixty seconds."""
        cfg,now=self.settings,self.monotonic()
        position=self.state.positions[cfg.market]
        if (not position or self.emergency_attempted or self.ledger is None
                or not self.ledger.exceeds(cfg.emergency_flatten_loss_usd)):
            return
        safe=(self.halt_result is not None and self.halt_result.persisted and self.halt_result.stopped
            and self.gate.is_open and self.gate.trading_halted and not self.watchdog.unhealthy_tasks()
            and self.state.market_mode(cfg.market)=='HALTED' and self.state.deadman_acked
            and self.last_reconcile_at is not None and now-self.last_reconcile_at<=cfg.reconcile_interval_s
            and self.book_seen is not None and now-self.book_seen<=cfg.max_book_age_s
            and self.engine.anchor_at is not None and now-self.engine.anchor_at<=cfg.anchor_max_age_s
            and not self.state.active_orders() and not any(not t.done() for t in self.manager.pending.values())
            and not getattr(self.api,'in_flight_writes',0)
            and (cfg.mode!='live' or getattr(self.api,'clock_verified',False)))
        if not safe:
            await self.alerter.alert('紧急亏损达到阈值，但无法确认安全减仓前置条件',key='紧急减仓封锁')
            return
        self.emergency_attempted,self.emergency_active=True,True
        deadline=now+cfg.flatten_timeout_s
        try:
            for _ in range(cfg.flatten_attempts):
                if self.monotonic()>=deadline or self.stop_event.is_set() or not self.gate.is_open:
                    break
                if self.watchdog.unhealthy_tasks():
                    await self.watchdog.check_once()
                    break
                center=self.reduction_center()
                if center is None or self.last_reconcile_at is None or (
                        self.monotonic()-self.last_reconcile_at>cfg.reconcile_interval_s):
                    break
                position=self.state.positions[cfg.market]
                quantity=(abs(position)/self.rules.increment).to_integral_value(rounding=ROUND_FLOOR)*self.rules.increment
                if quantity<=0:
                    return
                side='sell' if position>0 else 'buy'
                bound=center*(1+(-1 if side=='sell' else 1)*cfg.max_flatten_slippage_bps/BPS)
                price=(bound/self.rules.tick).to_integral_value(
                    rounding=ROUND_CEILING if side=='sell' else ROUND_FLOOR)*self.rules.tick
                order=self.manager.create_intent(cfg.market,side,quantity,price,reduce_only=True,
                    time_in_force='IOC',post_only=False,emergency=True)
                await self.manager.submit(order)
                if order.status==OrderStatus.UNKNOWN:
                    await self.manager.resolve_unknown(order)
                if not order.terminal or not await self.reconcile_once():
                    break
                self.watchdog.beat('Strategy')
                if not self.state.positions[cfg.market]:
                    await self.alerter.alert('熔断后紧急 IOC 已确认归零',urgent=True)
                    return
            await self.alerter.alert('熔断后紧急 IOC 预算耗尽，禁止自动再次尝试',urgent=True)
        finally:
            self.emergency_active=False
            self.state.stopping=True

    def accrue_quote_time(self):
        now = self.monotonic()
        elapsed = max(0, now-self.observation_at)
        if self.observed_center_source is not None:
            self.center_source_seconds[self.observed_center_source] += elapsed
        if self.observed_qualified:
            self.qualified_seconds += elapsed
        elif self.observed_reason is not None:
            self.pause_seconds[self.observed_reason] += elapsed
        self.observation_at = now

    def record_quote_state(self, qualified, reason=None, *, event=False):
        """Counts measure triggers; repeated triggers do not double-count the same quote-stop interval."""
        self.accrue_quote_time()
        if reason is not None and (event or reason != self.observed_reason):
            self.pause_counts[reason] += 1
        self.observed_qualified, self.observed_reason = qualified, reason

    def observe_quote_state(self):
        if not self.gate.is_open:
            reason = '全局发送封锁'
        elif self.halt.exit_code is not None:
            reason = self.halt_result.reason if self.halt_result else '全局熔断'
        elif self.guard.state != 'RUNNING':
            reason = self.guard.reason or '守卫恢复等待'
        elif self.state.stopping:
            reason = self.last_stop_reason or '停止核零等待'
        elif self.market_data_wait_reason is not None:
            reason = self.market_data_wait_reason
        elif self.settings.mode != 'dry-run' and not self.state.deadman_acked:
            reason = '等待死人开关 ACK'
        elif self.settings.mode != 'dry-run' and self.state.market_mode(self.settings.market) != 'RUNNING':
            reason = '账户同步屏障'
        elif self.monotonic() < self.backoff_until:
            reason = 'post-only 拒绝退避'
        elif self.qualified and not self.waiting and not self.state.stopping:
            self.last_stop_reason = None
            self.record_quote_state(True)
            return
        elif self.waiting:
            reason = self.last_stop_reason or '判据或窗口等待'
        else:
            reason = '等待同步或合规报价'
        self.record_quote_state(False, reason)

    def competition_metrics(self):
        decision = self.last_decision
        return {'竞争量权重': self.settings.gate_weight,
                '竞争量flat': (str(decision.competition_flat) if decision is not None
                              and decision.competition_flat is not None else None),
                '竞争量linear': (str(decision.competition_linear) if decision is not None
                                and decision.competition_linear is not None else None)}

    def observation_metrics(self):
        """Accumulate eligibility and quote-stop durations by elapsed time; use the latest health-log interval for order book update gaps."""
        self.accrue_quote_time()
        now = self.monotonic()
        while self.book_intervals and self.book_intervals[0][0] < now-self.settings.health_log_interval_s:
            self.book_intervals.popleft()
        intervals = sorted(gap for _, gap in self.book_intervals)
        deviation = None
        if (self.book is not None and self.book_seen is not None
                and 0 <= now-self.book_seen <= self.settings.max_book_age_s
                and self.engine.mark_is_fresh(now)):
            try:
                bids, asks = self.engine.third_party(self.book, self.state)
                if bids and asks:
                    deviation = str((self.engine.mark[0]/((bids[0][0]+asks[0][0])/2)-1)*BPS)
            except OrdersBlocked:
                pass
        elapsed = max(0, now-self.observation_started)
        book_age = None if self.book_seen is None else max(0, now-self.book_seen)
        return {**self.competition_metrics(), '标记价格偏差bps': deviation,
                '中心价来源': self.observed_center_source,
                '超慢锚偏差bps': (str(self.engine.slow_drift_bps)
                                 if self.engine.slow_drift_bps is not None else None),
                '无偏移模式': self.observed_center_source == 'mark',
                '中心价来源时长秒': dict(self.center_source_seconds),
                '盘口更新间隔最大秒': max(intervals+[book_age]) if book_age is not None else None,
                '盘口更新间隔P90秒': intervals[(9*len(intervals)+9)//10-1] if intervals else None,
                '盘口更新间隔样本数': len(intervals), '盘口距今秒': book_age,
                '守卫及停报原因': {reason: {'次数': self.pause_counts[reason], '时长秒': seconds}
                                  for reason, seconds in self.pause_seconds.items()},
                '观测时长秒': elapsed,
                '有资格时长占比': self.qualified_seconds/elapsed if elapsed else None}

    def log(self, row, *, level=logging.INFO):
        if self.logger is not None:
            getattr(self.logger, logging.getLevelName(level).lower())(
                json.dumps(row,ensure_ascii=False))

    def request_stop(self, signum=None):
        self.state.stopping = True
        self.stop_event.set()

    async def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        try:
            stopped = await self.stop_orders('收到退出信号，安全停止')
            if stopped and self.private_ws is not None and self.gate.is_open and self.settings.mode != 'dry-run':
                if not await self.disable_deadman():
                    self.exit_code = self.exit_code or 76
            if not stopped:
                self.exit_code = self.exit_code or 76
        finally:
            self.gate.finish_stop()
            self.instance.release()

    @staticmethod
    def _deadman_ack_matches(message, timeout):
        """Accept only observed subscription acknowledgments; booleans and numeric strings cannot stand in for timeout seconds."""
        data = message.get('data')
        return (message.get('type') == 'subscribed'
                and message.get('channel') == 'cancelAllOrdersAfterPerps'
                and isinstance(data, dict) and type(data.get('timeout_seconds')) is int
                and data['timeout_seconds'] == timeout)

    async def disable_deadman(self):
        """After confirming cancellation, disable the subscription and await acknowledgment of this timeout_seconds=0 request."""
        self._deadman_disable_requested = True
        self._deadman_disabled_event.clear()

        async def confirm():
            await self.send(self.private_ws, {'op': 'subscribe',
                'channel': 'cancelAllOrdersAfterPerps', 'timeout_seconds': 0})
            consumer = getattr(self, '_private_feed_task', None)
            if (not self._deadman_disabled_event.is_set()
                    and (consumer is None or consumer.done()) and hasattr(self.private_ws, '__aiter__')):
                async for message in self.private_ws:
                    await self.on_private(message)
                    if self._deadman_disabled_event.is_set():
                        break
            await self._deadman_disabled_event.wait()

        confirming = asyncio.create_task(confirm())
        timer = asyncio.create_task(self.sleep(self.settings.deadman_ack_timeout_s))
        try:
            done, _ = await asyncio.wait((confirming, timer), return_when=asyncio.FIRST_COMPLETED)
            if confirming in done:
                confirming.result()
            if self._deadman_disabled_event.is_set():
                self.deadman_disabled = True
                self.deadman_requested = self.state.deadman_acked = False
                return True
        except Exception:
            pass
        finally:
            self._deadman_disable_requested = False
            for task in (confirming, timer):
                task.cancel()
            await asyncio.gather(confirming, timer, return_exceptions=True)
        reason = '死人开关关闭 ACK 未确认，保留交易所超时兜底'
        self.log({'事件': '死人开关关闭失败', '原因': reason}, level=logging.WARNING)
        await self.urgent_alert(reason)
        return False

    async def send(self, ws, value):
        return await self.gate.send_ws(ws.send,json.dumps(value,separators=(',',':')))

    async def ping(self):
        if self.private_ws is None:
            raise RuntimeError('私有连接不存在')
        await self.send(self.private_ws,{'op':'ping'})

    async def _public_heartbeat(self, ws, receiving):
        """Public heartbeats keep unauthenticated connections alive, independently of private renewal and data freshness."""
        missed = 0
        next_ping = self.monotonic() + self.settings.public_ping_s

        async def ping_and_wait():
            # Keep this adjacent to sending; a late pong from the prior cycle cannot acknowledge this one.
            self._public_pong_event.clear()
            self._public_ping_deadline = self.monotonic() + 10
            await self.send(ws, {'op': 'ping'})
            await self._public_pong_event.wait()

        while not receiving.done() and self.gate.is_open and not self.stop_event.is_set():
            await self.sleep(max(0, next_ping-self.monotonic()))
            if receiving.done() or not self.gate.is_open or self.stop_event.is_set():
                return
            next_ping = self.monotonic() + self.settings.public_ping_s
            waiting = asyncio.create_task(ping_and_wait())
            timer = asyncio.create_task(self.sleep(10))
            try:
                done, _ = await asyncio.wait((waiting, timer), return_when=asyncio.FIRST_COMPLETED)
                if waiting in done:
                    waiting.result()
                    missed = 0
                else:
                    timer.result()
                    missed += 1
            finally:
                self._public_ping_deadline = None
                waiting.cancel()
                timer.cancel()
                await asyncio.gather(waiting, timer, return_exceptions=True)
            if missed >= 2:
                raise TimeoutError('公开连接连续两次心跳未在 10 秒内收到 pong')

    async def _public_feed(self, ws):
        """When either receiver or heartbeat ends, reap the other; pass failures to the existing backoff flow."""
        async def receive():
            async for message in ws:
                await self.on_public(message)
                if self.stop_event.is_set() or not self.gate.is_open:
                    break

        receiving = asyncio.create_task(receive())
        heartbeat = asyncio.create_task(self._public_heartbeat(ws, receiving))
        try:
            done, _ = await asyncio.wait((receiving, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            receiving.cancel()
            heartbeat.cancel()
            await asyncio.gather(receiving, heartbeat, return_exceptions=True)

    def public_is_healthy(self, now):
        """Only ongoing updates to this market order book prove public connection health; wait for a fresh order book after reconnecting."""
        return (not self.public_disconnected and self.book_seen is not None
                and 0 <= now-self.book_seen <= self.settings.max_book_age_s)

    def _mark_source_times(self, row, message=None):
        """Parse and validate source time; ingestion also orders by lastUpdatedTime independently of validity."""
        source_times, errors = [], []
        fields = [('lastUpdatedTime', row)]
        if message is not None:
            fields.append(('timestamp', message))
        wall = self.clock()
        for field, payload in fields:
            try:
                source_time = datetime.fromisoformat(payload.get(field).replace('Z', '+00:00'))
                if source_time.tzinfo is None:
                    raise ValueError
                stamp = source_time.timestamp()
            except (AttributeError, TypeError, ValueError, OverflowError, OSError):
                errors.append(field+' 缺失或无法解析')
                continue
            source_times.append(stamp)
            if not -2 <= wall-stamp <= self.settings.mark_source_max_age_s:
                errors.append(field+' 异常过旧或超前')
        if errors:
            raise ValueError('；'.join(errors))
        return tuple(source_times)

    def _mark_is_older(self, source_time, *, origin):
        accepted = self.engine.mark_accepted_source_time
        if accepted is not None and source_time < accepted:
            self.mark_older_counts[origin] += 1
            return True
        return False

    def _mark_conflict_reason(self, source_time, mark):
        if (source_time == self.engine.mark_accepted_source_time
                and self.engine.mark is not None and mark != self.engine.mark[0]):
            return ('标记价格源时间相同但价格不同：'
                    f'{self.engine.mark[0]} → {mark}，采用后到值')
        return None

    async def _reject_mark_time(self, detail, *, ws):
        if ws:
            self.engine.mark_source_times = ()
            self.engine.discovery_source_times = ()
        reason = '标记价格时间异常：'+detail+'，双侧停止报价'
        if ws:
            await self.guard.trigger(reason)
        else:
            # Rejecting this REST confirmation must not alter prior valid A/B state; their original expiry clocks keep running.
            reason = '标记价格时间异常：'+detail+'，拒绝本次 REST 确认'
        if self.logger is not None:
            self.logger.warning(reason)
        await self.alerter.alert(reason, key='标记价格时间异常')

    async def poll_mark_rest_once(self):
        """Throttle public read-only confirmations separately, outside the signed API queue and trading token budget."""
        cfg, now = self.settings, self.monotonic()
        if (self.public_http is None or self._mark_rest_polling or not cfg.mark_channel
                or self.stop_event.is_set() or not self.gate.is_open):
            return False
        seen = self.engine.ws_mark_seen()
        quiet_since = seen if seen is not None else self.observation_started
        if (now-quiet_since < cfg.mark_rest_poll_after_s
                or self.mark_rest_last_poll is not None
                and now-self.mark_rest_last_poll < cfg.mark_rest_poll_interval_s):
            return False
        # Consume this cycle's rate budget at request start; failures and slow requests must not cause rapid retries.
        self.mark_rest_last_poll, self._mark_rest_polling = now, True
        try:
            async with asyncio.timeout(cfg.request_timeout_s):
                response = await self.public_http('GET',
                    cfg.rest_url.rstrip('/')+'/v1/perps/mark_prices', timeout=cfg.request_timeout_s)
            if self.stop_event.is_set() or not self.gate.is_open:
                return False
            payload = response.payload
            if (response.status != 200 or not isinstance(payload, dict)
                    or payload.get('success') is not True
                    or not isinstance(payload.get('result'), dict)):
                raise ValueError('公开 REST 标记价格响应无效')
            row = payload['result'].get(cfg.market)
            if not isinstance(row, dict):
                raise ValueError('公开 REST 缺少本市场标记价格')
            try:
                source_times = self._mark_source_times(row)
            except ValueError as error:
                await self._reject_mark_time(str(error), ws=False)
                return False
            if self._mark_is_older(source_times[0], origin='rest'):
                return False
            mark = number(row['markPrice'])
            oracle = number(row['oraclePrice']) if 'oraclePrice' in row else None
            if mark <= 0 or oracle is not None and oracle <= 0:
                raise ValueError('公开 REST 标记价格或预言机价格无效')
            conflict = self._mark_conflict_reason(source_times[0], mark)
            now = self.monotonic()
            # REST has no inPriceDiscovery; price confirmation must never extend WS state or clear true.
            self.engine.mark = (mark, now)
            self.engine.oracle = (oracle, now) if oracle is not None else None
            self.engine.mark_source_times = source_times
            self.engine.mark_accepted_source_time = source_times[0]
            self.engine.mark_rest_seen = now
            self.log({'事件': 'REST 标记价格确认', '市场': cfg.market,
                      'markPrice': str(mark)}, level=logging.DEBUG)
            if conflict:
                if self.logger is not None:
                    self.logger.warning(conflict)
                await self.alerter.alert(conflict, key='标记价格同源时间冲突')
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ApiError, TimeoutError) as error:
            self.log({'事件': 'REST 标记价格确认失败', '异常类型': type(error).__name__},
                     level=logging.WARNING)
            return False
        finally:
            self._mark_rest_polling = False

    async def mark_rest_loop(self):
        """Run confirmation requests alongside the strategy loop so HTTP waits never block order book guards or cancellation."""
        while self.gate.is_open and not self.stop_event.is_set():
            await self.poll_mark_rest_once()
            await self.sleep(min(1.0, self.settings.mark_rest_poll_after_s,
                                 self.settings.mark_rest_poll_interval_s))

    async def on_public(self, message):
        cfg = self.settings
        if isinstance(message,str):
            message = json.loads(message)
        if not isinstance(message,dict):
            raise ValueError('公开推送必须是对象')
        channel = message.get('channel')
        critical = {'depthBooksPerps', cfg.mark_channel} - {''}
        if message.get('type') == 'subscribed':
            if isinstance(channel, str):
                self.public_subscriptions.add(channel)
                self.engine.subscription_failures.discard(channel)
                if critical <= self.public_subscriptions:
                    self.engine.subscription_failures.discard('未知频道')
            return
        if message.get('type') == 'error':
            channel = channel if isinstance(channel, str) and channel else '未知频道'
            reason = '公开频道订阅失败：' + channel
            if channel in critical or channel == '未知频道':
                self.public_subscriptions.discard(channel)
                if channel == '未知频道':
                    self.public_subscriptions.clear()
                self.engine.subscription_failures.add(channel)
                await self.guard.trigger(reason)
            await self.alerter.alert(reason, key='公开订阅失败')
            return
        if message.get('type') == 'pong' or message.get('op') == 'pong':
            self.public_pong_seen = self.monotonic()
            if (self._public_ping_deadline is not None
                    and self.public_pong_seen <= self._public_ping_deadline):
                self._public_pong_event.set()
            return
        rows = message.get('data', [])
        if not isinstance(rows, list):
            raise ValueError('公开行情 data 必须是数组')
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('公开行情元素必须是对象')
            if row.get('market') != cfg.market:
                continue
            if channel == 'depthBooksPerps':
                # Market making may quote around a fresh mark in a sparse third-party order book; other parsers still require both sides.
                book = parse_ondo({'success':True,'result':row},cfg.market,int(self.clock()*1000),
                                  allow_empty=True)
                if (book.source_ms is None or self.clock()*1000-book.source_ms > cfg.max_book_age_s*1000
                        or book.source_ms-self.clock()*1000 > 2000):
                    raise ValueError('交易所盘口时间过期或超前')
                if self.book is None or self.book.source_ms is None or book.source_ms >= self.book.source_ms:
                    now = self.monotonic()
                    if self.book_seen is not None:
                        self.book_intervals.append((now, max(0, now-self.book_seen)))
                    self.book, self.book_seen = book,now
                    self.public_disconnected = False
            elif channel == cfg.mark_channel:
                try:
                    source_times = self._mark_source_times(row, message)
                except ValueError as error:
                    await self._reject_mark_time(str(error), ws=True)
                    continue
                if self._mark_is_older(source_times[0], origin='ws'):
                    # Count fully late messages only; they cannot extend price or price-discovery validity.
                    continue
                discovery = row.get('inPriceDiscovery')
                if type(discovery) is not bool:
                    # Pause quotes immediately on invalid messages; preserve valid state and both clocks until an explicit boolean restores it.
                    self.engine.price_discovery_invalid = True
                    reason = 'inPriceDiscovery 字段缺失或类型无效，价格发现状态未确认，双侧停止报价'
                    await self.guard.trigger(reason)
                    if self.logger is not None:
                        self.logger.warning(reason)
                    await self.alerter.alert(reason, key='价格发现状态异常')
                    continue
                mark = number(row['markPrice'])
                if mark <= 0:
                    raise ValueError('标记价格无效')
                oracle = number(row['oraclePrice']) if 'oraclePrice' in row else None
                if oracle is not None and oracle <= 0:
                    raise ValueError('预言机价格无效')
                conflict = self._mark_conflict_reason(source_times[0], mark)
                now = self.monotonic()
                if self.engine.mark is None or mark != self.engine.mark[0]:
                    # A different WS value cannot reuse prior confirmation; same-price updates retain independent REST validity.
                    self.engine.mark_rest_seen = None
                self.engine.mark_source_times = self.engine.discovery_source_times = tuple(source_times)
                self.engine.mark_accepted_source_time = source_times[0]
                self.engine.mark = (mark,now)
                self.engine.mark_ws_seen = now
                self.engine.oracle = (oracle,now) if oracle is not None else None
                self.engine.in_price_discovery = discovery
                self.engine.price_discovery_seen = now
                self.engine.price_discovery_invalid = False
                self.log({'事件':'标记价格','市场':cfg.market,'markPrice':str(mark),
                          'oraclePrice':str(oracle) if oracle is not None else None,
                          'inPriceDiscovery':discovery}, level=logging.DEBUG)
                if discovery:
                    await self.guard.trigger('市场处于价格发现，双侧停止报价')
                if conflict:
                    if self.logger is not None:
                        self.logger.warning(conflict)
                    await self.alerter.alert(conflict, key='标记价格同源时间冲突')
            elif channel == 'tradesPerps':
                price, quantity = number(row['price']), number(row['size'])
                if price <= 0 or quantity <= 0 or row['aggressor_side'] not in ('buy', 'sell'):
                    raise ValueError('公开成交字段无效')
                self.log({'事件':'公开成交','市场':cfg.market,'价格':str(price),
                          '数量':str(quantity),'主动方向':row['aggressor_side']}, level=logging.DEBUG)

    async def _private_input_failure(self, detail, *, subscription=False):
        """Private field contracts await sandbox samples; incomplete input always waits for REST reconciliation."""
        reason = ('私有频道订阅失败' if subscription else '私有推送字段无效')
        reason += '（待沙盒样本）：'+detail
        if detail.startswith(('ordersPerps', 'fillsPerps')):
            reason += '（待小额实盘样本）'
        if subscription:
            self.state.private_disconnected()
            self.private_logged_in, self.deadman_requested = False, False
            if self.ack_wait_started is None:
                self.ack_wait_started = self.monotonic()
            self.ack_wait_phase = '订阅'
        else:
            self.state.begin_sync(self.settings.market, reason)
        self.reconcile_event.set()
        if self.logger is not None:
            self.logger.warning(reason)
        await self.alerter.alert(reason, key='私有订阅失败' if subscription else '私有字段异常')

    async def on_private(self, message):
        from .ondo_api import normalize_balance, normalize_fill, normalize_position
        if isinstance(message,str):
            try:
                message=json.loads(message)
            except (ValueError, TypeError, RecursionError):
                await self._private_input_failure('消息不是有效 JSON 对象')
                return
        if not isinstance(message,dict):
            await self._private_input_failure('消息必须是对象')
            return
        if message.get('op') == 'pong' or message.get('type') == 'pong':
            self.watchdog.pong()
            return
        if message.get('error_code') == 'timestamp_too_far':
            await self.trip_halt('运行中私有连接报告时间戳偏差')
            return
        if message.get('type') == 'error' or message.get('op') == 'error':
            await self._private_input_failure('交易所拒绝私有频道订阅', subscription=True)
            return
        if message.get('op') == 'loggedIn' or message.get('type') == 'loggedIn':
            if not self.private_logged_in or self.ack_wait_started is None:
                self.ack_wait_started, self.ack_wait_phase = self.monotonic(), '死人开关'
            self.private_logged_in=True
            self.deadman_requested=False
            self.state.deadman_acked=False
            for channel in ('ordersPerps','fillsPerps','positionsPerps','balancePerps'):
                await self.send(self.private_ws,{'op':'subscribe','channel':channel})
            if self.settings.mode != 'dry-run':
                await self.send(self.private_ws,{'op':'subscribe','channel':'cancelAllOrdersAfterPerps',
                    'timeout_seconds':self.settings.deadman_timeout_s})
                self.deadman_requested=True
            else:
                self.ack_wait_started = None
            await self.ping()
            return
        if self._deadman_disable_requested and self._deadman_ack_matches(message, 0):
            self._deadman_disabled_event.set()
            return
        rule = self.settings.deadman_ack_match

        def matches(path, expected):
            value = message
            for key in path.split('.'):
                if not isinstance(value, dict) or key not in value:
                    return False
                value = value[key]
            return type(value) is type(expected) and value == expected

        if (self.private_logged_in and self.deadman_requested and rule
                and not self._deadman_disable_requested
                and self._deadman_ack_matches(message, self.settings.deadman_timeout_s)
                and all(matches(k, v) for k, v in rule.items())):
            if self.settings.mode != 'dry-run':
                self.state.deadman_ack()
                self.ack_wait_started = None
                self.reconcile_event.set()
            return
        if message.get('type') == 'subscribed' or message.get('op') == 'subscribed':
            if not isinstance(message.get('channel'), str) or not message['channel']:
                await self._private_input_failure('订阅确认缺少频道名称')
            return
        channel = message.get('channel')
        if not isinstance(channel,str) or not channel:
            await self._private_input_failure('缺少频道名称')
            return
        if channel not in ('balancePerps','fillsPerps','ordersPerps','positionsPerps'):
            return
        rows=message.get('data')
        if isinstance(rows,dict):
            rows=[rows]
        if not isinstance(rows,list):
            await self._private_input_failure(channel+' 的 data 必须是数组或单个对象')
            return
        if channel == 'balancePerps' and not rows:
            self.state.apply_balance_snapshot(normalize_balance(rows), source='ws')
            return
        if channel == 'positionsPerps':
            try:
                if any(not isinstance(row, dict) or not isinstance(row.get('market'), str)
                       or not row['market'] for row in rows):
                    raise ValueError
                rows = [normalize_position(row) for row in rows]
            except (ApiError, KeyError, TypeError, ValueError, OverflowError):
                await self._private_input_failure(channel+' 缺少关键字段或字段值无效')
                return
        if channel == 'positionsPerps' and not any(
                row['market'] == self.settings.market for row in rows):
            # Omission from a full snapshot means zero for this market; retain the sync barrier pending REST verification.
            position = normalize_position({'market': self.settings.market,
                                           'direction': 'neutral', 'netQuantity': '0'})
            if self.state.note_position_event(self.settings.market, position):
                self.state.positions[self.settings.market] = D(0)
                self.state.begin_sync(self.settings.market, '私有空持仓快照等待 quiet 对账')
                self.reconcile_event.set()
            return
        for row in rows:
            try:
                if not isinstance(row,dict):
                    raise ValueError('频道数据元素必须是对象')
                market = row.get('market')
                if channel != 'balancePerps':
                    if not isinstance(market,str) or not market:
                        raise ValueError('缺少有效市场名称')
                    if market != self.settings.market:
                        continue
                if channel == 'balancePerps':
                    balance = normalize_balance(row)
                    for key in ('used_margin','margin_ratio'):
                        if balance.get(key) is not None and number(balance[key]) < 0:
                            raise ValueError('保证金字段无效')
                    if ('under_liquidation' in balance
                            and type(balance['under_liquidation']) is not bool):
                        raise ValueError('清算状态必须是布尔值')
                    self.state.apply_balance_snapshot(balance,source='ws')
                elif channel == 'fillsPerps':
                    # Await small live-trading samples; retain existing field validation and fallback paths.
                    fill = normalize_fill(row,market=self.settings.market)
                    # Validate the ledger time range first so invalid times cannot leave fills accounted for without a ledger write.
                    try:
                        datetime.fromtimestamp(fill['time_ms']/1000, ZoneInfo('UTC'))
                    except OSError:
                        raise ValueError('成交时间超出系统支持范围') from None
                    await self.record_fill(fill)
                    self.reconcile_event.set()
                elif channel == 'ordersPerps':
                    await self.manager.handle_order_event(row, source='ws',
                                                          timestamp=message.get('timestamp'))
                elif channel == 'positionsPerps':
                    position = normalize_position(row)
                    if self.state.note_position_event(self.settings.market, position):
                        self.state.begin_sync(self.settings.market,'私有持仓事件等待 quiet 对账')
                        self.reconcile_event.set()
            except (ApiError, KeyError, TypeError, ValueError, OverflowError):
                await self._private_input_failure(channel+' 缺少关键字段或字段值无效')

    async def feed(self, private=False):
        cfg = self.settings
        source = '私有' if private else '公开'
        reconnect = reconnect_failures(self)
        if private:
            self._private_feed_task = asyncio.current_task()
        while not self.stop_event.is_set() and self.gate.is_open:
            ws = None
            delay = None
            cancelled = False
            try:
                if private:
                    if self.ack_wait_started is None:
                        self.ack_wait_started = self.monotonic()
                    self.ack_wait_phase = '登录'
                    self.private_logged_in,self.deadman_requested=False,False
                    self.state.private_disconnected()
                ws = await self.gate.connect(self.connect,cfg.ws_url,ping_interval=None,
                                             max_size=5*1024*1024,close_timeout=2)
                reconnect.connected(source)
                if private:
                    self.private_ws=ws
                    self.private_logged_in,self.deadman_requested=False,False
                    self.state.private_disconnected()
                    await self.send(ws,self.api.ws_login())
                else:
                    self.public_ws=ws
                    self.public_pong_seen = None
                    self._public_pong_event.clear()
                    for channel in ('depthBooksPerps','tradesPerps',cfg.mark_channel):
                        if channel:
                            await self.send(ws,{'op':'subscribe','channel':channel,'limit':100})
                if private:
                    async for message in ws:
                        await self.on_private(message)
                        if not self.gate.is_open:
                            break
                else:
                    await self._public_feed(ws)
                if not self.stop_event.is_set():
                    raise OSError('行情连接结束')
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as error:
                if private:
                    self.state.private_disconnected()
                    if self.ack_wait_started is None:
                        self.ack_wait_started = self.monotonic()
                    self.ack_wait_phase = '登录'
                    self.private_logged_in,self.deadman_requested=False,False
                else:
                    self.public_disconnected = True
                if self.settings.mode != 'dry-run' and self.gate.is_open:
                    await self.guard.trigger('私有连接中断' if private else '公开连接中断')
                # Repeated failures may send urgent alerts; complete guard cancellations first so alerts cannot block risk cleanup.
                delay = await reconnect.failed(source, error)
                await self.alerter.alert('私有连接中断' if private else '公开连接中断',key='行情断线')
            finally:
                if private and self.private_ws is ws:
                    self.private_ws = None
                    self.private_logged_in = False
                elif not private and self.public_ws is ws:
                    self.public_ws = None
                    self.public_disconnected = True
                if ws is not None:
                    # A close frame is also a send; once the gate closes, leave socket release to process exit.
                    if self.gate.is_open:
                        try:
                            await ws.close()
                        except Exception as error:
                            # Cleanup failures must not mask cancellation or count the same disconnect twice.
                            if not cancelled and not asyncio.current_task().cancelling():
                                if delay is None:
                                    delay = await reconnect.failed(source, error)
                                else:
                                    from .maker_transport import MakerSecretFilter
                                    error_type, message = getattr(error, '_ws_diagnostic',
                                        (type(error).__name__, str(error)))
                                    record = logging.LogRecord('pendle.maker', logging.WARNING,
                                        '', 0, error_type + ': ' + message, (), None)
                                    MakerSecretFilter(getattr(self.api, 'credentials', None),
                                        self.alerter, secrets=(cfg.healthcheck_url,)).filter(record)
                                    error_type, _, message = record.getMessage().partition(': ')
                                    self.log({'事件': '行情连接关闭失败', '来源': source,
                                        '异常类型': error_type, '异常消息': message,
                                        '连续失败次数': reconnect.backoffs[source].failures,
                                        '下次等待秒': delay}, level=logging.WARNING)
            if self.gate.is_open and not self.stop_event.is_set():
                await self.sleep(delay)

    async def binance_feed(self):
        cfg=self.settings
        reconnect = reconnect_failures(self)
        while self.gate.is_open and not self.stop_event.is_set():
            ws=None
            delay=None
            cancelled=False
            try:
                ws=await self.gate.connect(self.connect,cfg.binance_ws_url+cfg.binance_symbol.lower()+'@aggTrade',
                    ping_interval=None,max_size=1048576,close_timeout=2)
                reconnect.connected('币安')
                async for raw in ws:
                    row=json.loads(raw).get('data',{})
                    if row.get('s') == cfg.binance_symbol and number(row['p']) > 0:
                        self.engine.external=(number(row['p']),self.monotonic())
                    if not self.gate.is_open or self.stop_event.is_set():
                        break
                if not self.stop_event.is_set():
                    raise OSError('行情连接结束')
            except asyncio.CancelledError:
                cancelled=True
                raise
            except Exception as error:
                self.engine.external=None
                delay = await reconnect.failed('币安', error)
            finally:
                if ws is not None and self.gate.is_open:
                    try:
                        await ws.close()
                    except Exception as error:
                        if not cancelled and not asyncio.current_task().cancelling():
                            if delay is None:
                                delay = await reconnect.failed('币安', error)
                            else:
                                from .maker_transport import MakerSecretFilter
                                error_type, message = getattr(error, '_ws_diagnostic',
                                    (type(error).__name__, str(error)))
                                record = logging.LogRecord('pendle.maker', logging.WARNING,
                                    '', 0, error_type + ': ' + message, (), None)
                                MakerSecretFilter(getattr(self.api, 'credentials', None),
                                    self.alerter, secrets=(cfg.healthcheck_url,)).filter(record)
                                error_type, _, message = record.getMessage().partition(': ')
                                self.log({'事件': '行情连接关闭失败', '来源': '币安',
                                    '异常类型': error_type, '异常消息': message,
                                    '连续失败次数': reconnect.backoffs['币安'].failures,
                                    '下次等待秒': delay}, level=logging.WARNING)
            if self.gate.is_open and not self.stop_event.is_set():
                await self.sleep(delay)

    async def load_rules(self):
        from .ondo_api import MARKETS_PATH, LEVERAGE_PATH
        if self.api is not None:
            payload=await self.api.request('GET',MARKETS_PATH)
        elif self.public_http is not None:
            self.gate.check()
            response=await self.public_http('GET',self.settings.rest_url+MARKETS_PATH)
            if response.status != 200:
                raise ValueError('无法核实市场精度')
            payload=response.payload
        else:
            raise ValueError('缺少读取交易精度的传输')
        try:
            result = payload['result']
            nested = isinstance(result, dict)
            pairs = result['perps']['tradingPairs'] if nested else result
            if not isinstance(pairs, list) or any(not isinstance(row, dict) for row in pairs):
                raise ValueError
            rows = [row for row in pairs if row.get('market') == self.settings.market]
            if payload.get('success') is not True or len(rows) != 1:
                raise ValueError
            row = rows[0]
            if (any(row.get(k) is True for k in ('disabled','isClosed','reduceOnly'))
                    or row.get('tradingState', 'TRADING') != 'TRADING'):
                raise ValueError('市场当前不支持双边交易')
            # Support old snapshots; live perps metadata must provide default leverage, tiered leverage, and the base-position cap.
            default = row['defaultLeverage'] if nested else row.get('defaultLeverage')
            cap = row['maxPositionBaseSize'] if nested else row.get('maxPositionBaseSize')
            margins = row['marginInfo'] if nested else row.get('marginInfo', [])
            if not isinstance(margins, list) or nested and not margins:
                raise ValueError
            leverages = [number(info['maxLeverage']) for info in margins]
            if any(value < 1 for value in leverages) or nested and (default is None or cap is None):
                raise ValueError
            rules = MarketRules(row['quoteIncrement'], row['baseIncrement'],
                row.get('minNotional', 0), cap, default, max(leverages) if leverages else None)
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError('市场精度、杠杆或持仓上限缺失或无效') from None
        if self.api is not None:
            leverage=await self.api.request('GET',LEVERAGE_PATH,params={'market':self.settings.market})
            try:
                rows = leverage['result']
                if (leverage.get('success') is not True or not isinstance(rows, list)
                        or any(not isinstance(row, dict)
                               or not isinstance(row.get('market', self.settings.market), str)
                               or not row.get('market', self.settings.market) for row in rows)):
                    raise ValueError
                matches = [row for row in rows
                           if row.get('market', self.settings.market) == self.settings.market]
                if len(matches) > 1:
                    raise ValueError
                # Use the public default when market leverage is unset; conservatively assume 1x for old snapshots without a default.
                value = number(matches[0]['leverage']) if matches else rules.default_leverage or D(1)
                if value < 1:
                    raise ValueError
                self.initial_margin_rate = 1/value
            except (KeyError, TypeError, AttributeError, ValueError):
                raise ValueError('无法核实市场杠杆') from None
        elif rules.default_leverage is not None:
            self.initial_margin_rate = 1/rules.default_leverage
            self.log({'事件':'初始保证金率估算','来源':'无 API 密钥，按 1/defaultLeverage 估算',
                      '初始保证金率':str(self.initial_margin_rate)})
        self.rules = rules

    def record_funding(self):
        """Use the prior cumulative funding in the persistent ledger as the baseline for idempotent recovery after a post-write crash."""
        total=self.state.balances.get('totalFundingPayments')
        if total is None:
            return
        total=number(total)
        entries=[key for key in self.ledger.events if key.startswith('funding:')]
        if entries:
            previous=number(entries[-1].rsplit(':',1)[-1])
            if previous == total:
                return
            delta=total-previous
        else:
            delta=D(0)
        # Pending sandbox verification: positive totalFundingPayments means expense, while positive ledger funding means income.
        self.ledger.record('funding:'+str(int(self.clock()*1000))+':'+str(total),funding=-delta)

    async def strategy_loop(self):
        while not self.stop_event.is_set():
            await self.tick()
            await self.sleep(self.settings.strategy_interval_s)

    async def order_loop(self):
        while not self.stop_event.is_set() and self.gate.is_open:
            if self.emergency_active:
                self.watchdog.beat('OrderManager')
                await self.sleep(1)
                continue
            await self.manager.check_timeouts()
            for order in tuple(self.state.active_orders()):
                if order.status == OrderStatus.UNKNOWN:
                    await self.manager.resolve_unknown(order)
            if not self.gate.trading_halted:
                await self.manager.drain()
            self.watchdog.beat('OrderManager')
            await self.sleep(1)

    async def reconcile_loop(self):
        while not self.stop_event.is_set() and self.gate.is_open:
            if self.state.deadman_acked or self.settings.mode == 'dry-run':
                ok=await self.reconcile_once()
                if not ok and not self.state.stopping and self.settings.mode!='dry-run':
                    await self.guard.trigger('对账尚未一致')
            self.reconcile_event.clear()
            sleeper=asyncio.create_task(self.sleep(self.settings.reconcile_interval_s))
            wake=asyncio.create_task(self.reconcile_event.wait())
            try:
                await asyncio.wait((sleeper,wake),return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleeper.cancel()
                wake.cancel()
                await asyncio.gather(sleeper,wake,return_exceptions=True)

    async def monitor_loop(self):
        while not self.stop_event.is_set():
            unhealthy_tasks = list(self.watchdog.unhealthy_tasks())
            healthy=not unhealthy_tasks and self.gate.is_open and self.halt.exit_code is None
            metrics = self.observation_metrics()
            summary = {'持仓':str(self.state.positions[self.settings.market]),
                       '亏损':str(self.ledger.loss_usd) if self.ledger else '未知',
                       '判据':self.observed_qualified,'错误数':self.reject_count}
            if (self.settings.mode == 'live' and not self.settings.healthcheck_url
                    and self.settings.healthcheck_waived):
                summary['外部心跳风险'] = HEALTHCHECK_WAIVER_WARNING
            summary.update(metrics)
            await self.alerter.observe(healthy=healthy,waiting=self.waiting,
                qualified=self.observed_qualified,last_quote=self.last_quote,
                summary=summary)
            if self.last_health is None or self.monotonic()-self.last_health>=self.settings.health_log_interval_s:
                self.log({'事件':'健康状态','关键任务健康':healthy,'不健康任务':unhealthy_tasks,'等待':self.waiting,
                    '持仓':str(self.state.positions[self.settings.market]),
                    '有资格':self.observed_qualified,'模式':self.settings.mode, **metrics})
                self.last_health=self.monotonic()
            if self.halt.exit_code is not None and (self.last_halt_alert is None
                    or self.monotonic()-self.last_halt_alert>=900):
                await self.alerter.alert('熔断后等待人工处理，残余持仓：'+str(
                    self.state.positions[self.settings.market]),key='熔断残余持仓',urgent=True)
                self.last_halt_alert=self.monotonic()
            await self.sleep(1)

    async def watchdog_loop(self):
        while self.gate.is_open and not self.stop_event.is_set():
            await self.sleep(1)
            if self.stop_event.is_set():
                return
            # Read-only private connections also ping regularly; pongs are the only proof of PrivateFeed liveness.
            if self.settings.mode == 'dry-run':
                if self.private_ws is not None and self.private_logged_in and (not hasattr(self, '_last_renewal')
                        or self.monotonic()-self._last_renewal >= self.watchdog.check_interval_s):
                    try:
                        await asyncio.wait_for(self.ping(), timeout=self.watchdog.ping_timeout_s)
                    except Exception as error:
                        # Read-only mode lets the feed back off and reconnect; actual pongs still determine liveness.
                        self.log({'事件': '只读私有心跳失败', '异常类型': type(error).__name__},
                                 level=logging.WARNING)
                    finally:
                        self._last_renewal = self.monotonic()
                continue
            if not await self.watchdog.check_once(renew=False):
                self.exit_code=76
                self.request_stop()
                return
            if (not self.state.deadman_acked and self.ack_wait_started is not None
                    and self.monotonic()-self.ack_wait_started >= self.settings.deadman_ack_timeout_s):
                await self.watchdog._trip(self.ack_wait_phase+' ACK 超时，停止交易')
                self.exit_code=76
                self.request_stop()
                return
            if self.state.deadman_acked and (not hasattr(self, '_last_renewal')
                    or self.monotonic()-self._last_renewal >= self.watchdog.check_interval_s):
                if not await self.watchdog.check_once():
                    self.exit_code=76
                    self.request_stop()
                    return
                self._last_renewal = self.monotonic()

    async def run(self, *, live=False):
        tasks,installed=[],[]
        try:
            await self.startup(live=live)
            loop=asyncio.get_running_loop()
            for sig in (signal.SIGTERM,signal.SIGINT):
                try:
                    loop.add_signal_handler(sig,self.request_stop,sig)
                    installed.append(sig)
                except (NotImplementedError,RuntimeError):
                    pass
            jobs=[self.feed(),self.strategy_loop(),self.monitor_loop(),self.mark_rest_loop()]
            if self.api is not None:
                jobs.extend((self.feed(True),self.reconcile_loop(),self.watchdog_loop()))
                if self.settings.mode!='dry-run':
                    jobs.append(self.order_loop())
            if self.settings.binance_symbol:
                jobs.append(self.binance_feed())
            tasks=[asyncio.create_task(job) for job in jobs]
            stop=asyncio.create_task(self.stop_event.wait())
            try:
                done,_=await asyncio.wait([stop,*tasks],return_when=asyncio.FIRST_COMPLETED)
                if not self.stop_event.is_set():
                    for task in done:
                        task.result()
                    if not self.gate.is_open:
                        self.exit_code=76
                    else:
                        raise RuntimeError('关键任务意外结束')
            finally:
                stop.cancel()
                await asyncio.gather(stop,return_exceptions=True)
        except asyncio.CancelledError:
            self.exit_code=76
            raise
        except Exception as error:
            from .maker_transport import MakerSecretFilter
            prefix = '运行失败：' if self.started else '启动失败：'
            record = logging.LogRecord('pendle.maker', logging.ERROR, '', 0,
                prefix+str(error), (), None)
            MakerSecretFilter(getattr(self.api, 'credentials', None), self.alerter,
                secrets=(self.settings.healthcheck_url,)).filter(record)
            message = record.getMessage()
            if self.logger is not None:
                self.logger.error(message)
            print(message, file=sys.stderr)
            if self.started and self.settings.mode!='dry-run':
                result = await self.trip_halt(message)
                # Halt tasks are idempotent; an existing halt still requires separate reporting of this run failure.
                if result.reason != message:
                    await self.urgent_alert(message)
            else:
                self.exit_code=2
        finally:
            # Cleanup, task reaping, and urgent HTTP waits share cancellation protection, even under repeated cancellation.
            await _wait_cleanup(asyncio.create_task(self._finish_run(tasks, installed)))
        return self.exit_code

    async def _finish_run(self, tasks, installed):
        self.request_stop()
        try:
            # Block the strategy first, then handle in-flight requests and cancellation; keep the private consumer alive until cleanup ends.
            if self.started:
                await self.shutdown()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if hasattr(self.alerter, 'flush_urgent'):
                    await self.alerter.flush_urgent(timeout_s=10)
            finally:
                for sig in installed:
                    asyncio.get_running_loop().remove_signal_handler(sig)


def run_maker(settings, *, live=False, **dependencies):
    from .maker_transport import run_maker as run
    return run(settings,live=live,**dependencies)


def run_cancel_all(settings, **dependencies):
    from .maker_transport import run_cancel_all as run
    return run(settings,**dependencies)


def reset_halt(settings, **dependencies):
    from .maker_transport import reset_halt as reset
    return reset(settings,**dependencies)
