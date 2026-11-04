"""Read-only, trade-driven replay without lookahead; retain Decimal for all execution and reward amounts."""

from bisect import bisect_left, bisect_right, insort
from collections import deque
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import heapq
from statistics import median

from .reward import eligible_weight

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)
NS_MS = 1000000
NS_S = 1000000000


def _d(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _ondo_ns(row):
    return int(row['time_ns'])


def _binance_ns(row):
    return int(row['transact_time']) * NS_MS


def _visible_ns(row, event_ns, latency_ns):
    """Live captures respect both fixed latency and receipt time; legacy archives use fixed latency."""
    received = row.get('received_ms')
    return max(event_ns + latency_ns, received * NS_MS) if received is not None else event_ns + latency_ns


def gap_duration_ms(gaps, start_ms, end_ms):
    """Clip to the backtest window and merge overlaps to avoid counting gaps from multiple sources twice."""
    intervals = sorted((max(start_ms, row['start_ms']),
                        min(end_ms, end_ms if row.get('end_ms') is None else row['end_ms']))
                       for row in gaps)
    total, until = 0, start_ms
    for start, end in intervals:
        if end > max(until, start):
            total += end - max(until, start)
            until = end
    return total


def _relevant_gap(row, identity, hedge_mode):
    """Use only market-data channels for this market; support legacy gaps without a source or channel."""
    if not row.get('source') or not row.get('channel'):
        markets = {identity.ondo_market, identity.binance_symbol}
        if hedge_mode == 'lighter_proxy':
            markets.add(identity.symbol)
        return row.get('market') is None or row['market'] in markets
    dependencies = {
        'ondo': (identity.ondo_market, ('tradesPerps', 'depthBooksPerps')),
        'binance': (identity.binance_symbol, ('aggTrade',))}
    if hedge_mode == 'lighter_proxy':
        dependencies['lighter'] = (identity.symbol, ('orderBookOrders',))
    dependency = dependencies.get(row['source'])
    return bool(dependency and row.get('market') in (dependency[0], '*')
                and row.get('channel') in (*dependency[1], '*'))


def validate_mapping(ondo_trades, binance_trades, *, start_ms, end_ms,
                     max_mapping_dev_bps=500):
    """Validate asset mapping against the latest Binance price at each Ondo trade, without future prices."""
    prices = sorted((r for r in binance_trades if r['transact_time'] < end_ms),
                    key=lambda r: (r['transact_time'], r.get('agg_id', 0)))
    trades = sorted((r for r in ondo_trades
                     if start_ms * NS_MS <= _ondo_ns(r) < end_ms * NS_MS),
                    key=lambda r: (_ondo_ns(r), r['id']))
    index, latest, deviations = 0, None, []
    for row in trades:
        while index < len(prices) and _binance_ns(prices[index]) <= _ondo_ns(row):
            latest = _d(prices[index]['price'])
            index += 1
        if latest:
            deviations.append(abs(_d(row['price']) / latest - ONE) * BPS)
    if not deviations:
        raise ValueError('数据不足：没有可用于币安映射校验的同期成交')
    deviation = median(deviations)
    if deviation > _d(max_mapping_dev_bps):
        raise ValueError(f'币安映射校验拒绝：价格偏离中位数 {deviation:.2f} bps，'
                         f'超过上限 {max_mapping_dev_bps} bps')
    return deviation


class _Replay:
    """Drive replay with an incremental queue; retain the latest N basis samples and guard extrema windows."""

    def __init__(self, ondo, binance, settings, symbol, start_ms, end_ms, depths, gaps,
                 liquidation_depths=()):
        self.s, self.symbol = settings, symbol
        self.start, self.end = start_ms * NS_MS, end_ms * NS_MS
        self.precision = settings.for_symbol(symbol)
        self.ondo, self.binance = ondo, binance
        self.depths, self.gaps = depths, gaps
        self.heap, self.sequence, self.decision_times = [], 0, set()
        self.events, self.fills, self.minutes = [], [], []
        self.center_prices = []
        self.boundary_before, self.boundary_time = {}, None
        self.orders, self.order_sequence = {}, 0
        self.basis_queue = {side: deque() for side in ('buy', 'sell')}
        self.basis_values = {side: [] for side in ('buy', 'sell')}
        self.ondo_queue = {side: deque() for side in ('buy', 'sell')}
        self.ondo_values = {side: [] for side in ('buy', 'sell')}
        self.guard_min, self.guard_max = deque(), deque()
        self.reference, self.reference_time = None, None
        self.reference_agg_id = -1
        self.best_bid, self.best_ask, self.depth_mid = None, None, None
        self.best_bid_time = self.best_ask_time = self.depth_mid_time = None
        # Liquidation costs are calibrated after the fact; order book observations do not affect historical quotes, arbitrage checks, or reward midpoints.
        spreads = [_d(row['spread_bps']) for row in liquidation_depths
                   if row.get('spread_bps') is not None]
        spreads = [value for value in spreads if value.is_finite() and ZERO <= value < 2 * BPS]
        self.liquidation_half_spread = median(spreads) / 2 if spreads else settings.ondo_half_spread_bps
        self.liquidation_spread_source = 'depth_observations_median' if spreads else 'config'
        self.liquidation_spread_samples = len(spreads)
        self.center, self.order_center = None, None
        self.mark_ondo, self.mark_lighter = None, None
        self.guard_until, self.guard_active = -1, False
        self.guard_generation = 0
        self.last_guard_interval_end = None
        self.ondo_position = self.lighter_position = ZERO
        self.ondo_cash = self.lighter_cash = ZERO
        self.pending_hedges = deque()
        self.pending_notifications = []
        self.last_time = self.start
        self.online = False
        self.using_fallback = False
        self.online_reason = 'no_reference'
        self.quote_blocks = set()
        self.pnl_determinate = True
        self.totals = {key: ZERO for key in (
            'fill_notional', 'maker_fees', 'hedge_fees', 'hedge_cost', 'basis_pnl',
            'funding_cost', 'guard_pause_ms', 'online_ms', 'fallback_ms',
            'fallback_edge', 'fallback_edge_net', 'max_ondo_notional',
            'max_lighter_notional', 'cancelled_hedge_quantity')}
        self.totals.update(fill_count=0, guard_triggers=0, guard_pause_segments=0,
                           post_only_rejections=0, pickoff_count=0, pickoff_pnl=ZERO,
                           fallback_fill_count=0)
        self.totals['online_breakdown_ms'] = dict.fromkeys((
            'online', 'fallback_online', 'no_reference', 'no_basis', 'guard', 'post_only', 'requote',
            'inventory_limit', 'data_gap'), ZERO)

    def push(self, time_ns, priority, kind, data=None):
        if time_ns < self.end:
            self.sequence += 1
            heapq.heappush(self.heap, (time_ns, priority, self.sequence, kind, data))

    def decision(self, time_ns):
        if self.start <= time_ns < self.end and time_ns not in self.decision_times:
            self.decision_times.add(time_ns)
            self.push(time_ns, 6, 'decision')

    def log(self, now, kind, **data):
        if now >= self.start:
            self.events.append({'time_ms': now // NS_MS, 'time_ns': now,
                                'type': kind, **data})

    def proxy(self):
        return self.reference * (ONE + self.s.lighter_basis_bps / BPS) if self.reference else None

    def reference_fresh(self, now):
        return (self.reference is not None and
                now - self.reference_time <= int(self.s.max_ref_age_s * NS_S))

    def add_sample(self, now, stamp, side, value, queues, values, max_age_s, expiry_kind):
        """Limit each side independently and age samples by event time so visibility latency cannot extend validity."""
        if now - stamp > int(max_age_s * NS_S):
            return
        queue, sorted_values = queues[side], values[side]
        # Capture arrival order may differ from exchange time; use exchange time for age and the latest N samples.
        index = bisect_right([item[0] for item in queue], stamp)
        queue.insert(index, (stamp, value))
        insort(sorted_values, value)
        while len(queue) > self.s.basis_samples:
            _, old = queue.popleft()
            sorted_values.pop(bisect_left(sorted_values, old))
        self.push(stamp + int(max_age_s * NS_S) + 1, 4, expiry_kind)

    def side_median(self, now, side, queues, values, max_age_s):
        """Expire samples individually; the latest timestamp on a side must not renew older samples."""
        queue, sorted_values = queues[side], values[side]
        cutoff = now - int(max_age_s * NS_S)
        while queue and queue[0][0] < cutoff:
            _, old = queue.popleft()
            sorted_values.pop(bisect_left(sorted_values, old))
        if not sorted_values:
            return None
        middle = len(sorted_values) // 2
        return (sorted_values[middle] if len(sorted_values) % 2 else
                (sorted_values[middle - 1] + sorted_values[middle]) / 2)

    def center_state(self, now):
        if self.reference_fresh(now):
            sides = [self.side_median(now, side, self.basis_queue,
                self.basis_values, self.s.max_basis_age_s) for side in ('buy', 'sell')]
            if all(value is not None for value in sides):
                return self.reference * (ONE + sum(sides) / 2), 'binance_basis'
        elif self.s.ref_fallback == 'ondo_mid':
            sides = [self.side_median(now, side, self.ondo_queue,
                self.ondo_values, self.s.max_ondo_mid_age_s) for side in ('buy', 'sell')]
            if all(value is not None for value in sides):
                return sum(sides) / 2, 'ondo_mid'
        return None, None

    def current_center(self, now):
        return self.center_state(now)[0]

    def update_online(self, now):
        """Assign mutually exclusive causes; one valid live side counts as online, with two-sided reward eligibility checked separately."""
        center, source = self.center_state(now)
        self.using_fallback = source == 'ondo_mid'
        if self.in_gap(now):
            reason = 'data_gap'
        elif center is None and not self.reference_fresh(now):
            reason = 'no_reference'
        elif center is None:
            reason = 'no_basis'
        elif self.guard_active:
            reason = 'guard'
        elif any(o['active'] and o['quantity'] > 0 for o in self.orders.values()):
            reason = 'fallback_online' if self.using_fallback else 'online'
        elif self.orders or self.pending_notifications:
            reason = 'requote'
        elif 'post_only' in self.quote_blocks:
            reason = 'post_only'
        elif 'inventory_limit' in self.quote_blocks:
            reason = 'inventory_limit'
        else:
            reason = 'requote'
        self.online_reason = reason
        self.online = reason in ('online', 'fallback_online')

    def record_center(self, now):
        """Record the center price as it becomes visible, retaining invalid values as null without backfilling."""
        if now < self.start:
            return
        center, source = self.center_state(now)
        reference = (self.reference if self.reference is not None and
            now - self.reference_time <= int(self.s.max_ref_age_s * NS_S) else None)
        row = {'time_ms': now // NS_MS, 'time_ns': now, 'price': center,
               'binance_price': reference, 'center_source': source,
               'ref_fallback': source == 'ondo_mid'}
        if self.center_prices and self.center_prices[-1]['time_ns'] == now:
            self.center_prices[-1] = row
        elif (not self.center_prices or any(self.center_prices[-1][key] != row[key]
                                          for key in ('price', 'binance_price', 'center_source'))):
            self.center_prices.append(row)

    def in_gap(self, now):
        return any(row['start_ms'] * NS_MS <= now <
                   (row.get('end_ms') or self.end // NS_MS) * NS_MS for row in self.gaps)

    def accrue(self, now):
        elapsed = max(0, now - max(self.last_time, self.start))
        if elapsed:
            if self.guard_active:
                if self.last_guard_interval_end != max(self.last_time, self.start):
                    self.totals['guard_pause_segments'] += 1
                self.last_guard_interval_end = now
                self.totals['guard_pause_ms'] += Decimal(elapsed) / NS_MS
            if self.online:
                self.totals['online_ms'] += Decimal(elapsed) / NS_MS
            if self.using_fallback:
                self.totals['fallback_ms'] += Decimal(elapsed) / NS_MS
            self.totals['online_breakdown_ms'][self.online_reason] += Decimal(elapsed) / NS_MS
            hours = Decimal(elapsed) / (3600 * NS_S)
            ondo_price = self.center or self.reference or ZERO
            lighter_price = self.proxy() or ZERO
            self.totals['funding_cost'] += hours / BPS * (
                self.ondo_position * ondo_price * self.s.ondo_funding_bps_per_hour +
                self.lighter_position * lighter_price * self.s.lighter_funding_bps_per_hour)
            if self.in_gap(self.last_time) and (self.orders or self.ondo_position or self.lighter_position):
                self.pnl_determinate = False
        self.last_time = now

    def cancel(self, now, reason):
        for order in list(self.orders.values()):
            if order['cancel_time'] is None:
                order['cancel_time'] = now + self.s.cancel_latency_ms * NS_MS
                self.log(now, 'cancel_requested', order_id=order['id'], reason=reason)
                if self.s.cancel_latency_ms == 0:
                    del self.orders[order['id']]
                    self.log(now, 'cancel_effective', order_id=order['id'])
                else:
                    self.push(order['cancel_time'], 0, 'cancel', order['id'])

    def quote(self, now):
        center = self.current_center(now)
        can_quote = bool(center is not None and not self.guard_active and not self.in_gap(now))
        if center is not None:
            self.center = center
        if not can_quote:
            if self.guard_active and center is not None and not self.in_gap(now):
                return
            self.cancel(now, '守卫、断档或参考价过期')
            return
        if self.orders:
            if self.order_center and abs(center / self.order_center - ONE) * BPS > self.s.requote_bps:
                self.cancel(now, '中心价移动')
                if not self.orders:
                    self.quote(now)
            return
        if any(not item['visible'] for item in self.pending_notifications):
            return
        self.order_center = center
        self.quote_blocks.clear()
        tick, step = self.precision.price_tick, self.precision.quantity_step
        for side, sign, rounding in (('buy', ONE, ROUND_FLOOR), ('sell', -ONE, ROUND_CEILING)):
            price = (center * (ONE - sign * self.s.quote_offset_bps / BPS) / tick).to_integral_value(rounding=rounding) * tick
            if price <= 0:
                continue
            # The limit includes current inventory and all potential orders on this side, including pending activation or cancellation.
            potential = sum((o['quantity'] * o['price'] for o in self.orders.values() if o['side'] == side), ZERO)
            capacity = (self.s.max_inventory_usd - sign * self.ondo_position * center - potential) / price
            quantity = min(self.s.quote_notional / price, max(ZERO, capacity))
            quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if quantity <= 0:
                self.quote_blocks.add('inventory_limit')
                continue
            if ((side == 'buy' and self.best_ask is not None and price >= self.best_ask) or
                    (side == 'sell' and self.best_bid is not None and price <= self.best_bid)):
                self.totals['post_only_rejections'] += 1
                self.quote_blocks.add('post_only')
                self.log(now, 'post_only_reject', side=side, price=price)
                continue
            self.order_sequence += 1
            order = {'id': self.order_sequence, 'side': side, 'price': price,
                     'quantity': quantity, 'active': False, 'cancel_time': None}
            self.orders[order['id']] = order
            # Within the same nanosecond, match existing live orders before activating pending orders.
            self.push(now + self.s.place_latency_ms * NS_MS, 3, 'activate', order['id'])
            self.log(now, 'order_sent', order_id=order['id'], side=side, price=price,
                     quantity=quantity, potential_notional=max(ZERO, sign * self.ondo_position * center + potential + quantity * price))

    def add_pending_hedge(self, now, signed_quantity):
        remaining = -signed_quantity
        self.pending_hedges = deque(item for item in self.pending_hedges if item['quantity'])
        for item in self.pending_hedges:
            if remaining and item['quantity'] * remaining < 0:
                offset = min(abs(remaining), abs(item['quantity']))
                direction = ONE if remaining > 0 else -ONE
                item['quantity'] += direction * offset
                remaining -= direction * offset
        if remaining:
            item = {'quantity': remaining}
            self.pending_hedges.append(item)
            self.push(now + self.s.hedge_latency_ms * NS_MS, 3, 'hedge', item)

    def match(self, now, row):
        if now < self.start:
            return
        price, budget = _d(row['price']), _d(row['size'])
        equal_budget = budget * self.s.at_price_fill_ratio
        side = 'buy' if row['aggressor_side'] == 'sell' else 'sell'
        for order in sorted(self.orders.values(), key=lambda o: o['id']):
            if not order['active'] or order['side'] != side or budget <= 0:
                continue
            better = price < order['price'] if side == 'buy' else price > order['price']
            equal = price == order['price']
            if not (better or equal):
                continue
            quantity = min(budget, order['quantity'], equal_budget if equal else budget)
            if quantity <= 0:
                continue
            budget -= quantity
            if equal:
                equal_budget -= quantity
            self.fill(now, order, quantity, 'historical', row['id'])
        self.update_maxima()

    def fill(self, now, order, quantity, fill_type, trade_id):
        """Historical fills and arbitrage pickoffs share inventory, fees, and visible execution reports."""
        order['quantity'] -= quantity
        signed = quantity if order['side'] == 'buy' else -quantity
        notional = quantity * order['price']
        fee = notional * self.s.ondo_maker_fee_bps / BPS
        self.ondo_position += signed
        self.ondo_cash -= signed * order['price'] + fee
        self.totals['fill_count'] += 1
        self.totals['fill_notional'] += notional
        self.totals['maker_fees'] += fee
        if fill_type == 'pickoff':
            self.totals['pickoff_count'] += 1
        center, source = self.center_state(now)
        fill = {'time_ms': now // NS_MS, 'time_ns': now, 'side': order['side'],
                'center': center, 'center_source': source,
                'ref_fallback': source == 'ondo_mid', 'fill_type': fill_type,
                'price': order['price'], 'quantity': quantity, 'notional': notional,
                'fee': fee, 'trade_id': trade_id, 'order_id': order['id']}
        self.fills.append(fill)
        if source == 'ondo_mid':
            edge = (center - order['price']) * signed
            self.totals['fallback_fill_count'] += 1
            self.totals['fallback_edge'] += edge
            self.totals['fallback_edge_net'] += edge - fee
        self.log(now, 'fill', **{k: v for k, v in fill.items() if k not in ('time_ms', 'time_ns')})
        if not order['quantity']:
            del self.orders[order['id']]
        notice = {'quantity': signed, 'visible': False}
        self.pending_notifications.append(notice)
        self.push(now + self.s.data_latency_ms * NS_MS, -1, 'fill_visible', notice)

    def pickoff(self, now):
        """When a Binance price is visible, use the current basis to check whether live orders cover arbitrage fees."""
        center = self.current_center(now)
        if (not self.s.pickoff_enabled or now < self.start or center is None or
                not self.reference_fresh(now) or self.in_gap(now)):
            return
        edge = self.s.pickoff_edge_bps / BPS
        for order in list(self.orders.values()):
            if (not order['active'] or order['quantity'] <= 0 or
                    (order['cancel_time'] is not None and now >= order['cancel_time'])):
                continue
            crossed = (center <= order['price'] * (ONE - edge) if order['side'] == 'buy'
                       else center >= order['price'] * (ONE + edge))
            if crossed:
                self.fill(now, order, order['quantity'], 'pickoff', None)

    def refresh_marks(self, now):
        """Track PnL from cross-venue spread changes on matched hedged positions separately."""
        center = self.current_center(now) or self.center or self.reference
        proxy = self.proxy()
        if center is not None and proxy is not None:
            if self.mark_ondo is not None and self.mark_lighter is not None:
                if self.ondo_position * self.lighter_position < 0:
                    paired = min(abs(self.ondo_position), abs(self.lighter_position))
                    paired *= ONE if self.ondo_position > 0 else -ONE
                    self.totals['basis_pnl'] += paired * ((center - self.mark_ondo) -
                                                         (proxy - self.mark_lighter))
            self.mark_ondo, self.mark_lighter = center, proxy
            self.center = center

    def update_maxima(self):
        self.totals['max_ondo_notional'] = max(self.totals['max_ondo_notional'],
            abs(self.ondo_position) * (self.center or self.reference or ZERO))
        self.totals['max_lighter_notional'] = max(self.totals['max_lighter_notional'],
            abs(self.lighter_position) * (self.proxy() or ZERO))

    def hedge(self, now, item):
        quantity = item['quantity']
        if not quantity:
            return
        # Do not assume proxy hedges succeed during gaps or with stale references; retain exposure and cancel at period end.
        if self.in_gap(now) or self.reference is None or now - self.reference_time > int(self.s.max_ref_age_s * NS_S):
            self.pnl_determinate = False
            self.log(now, 'hedge_unresolved', quantity=quantity)
            return
        mid = self.proxy()
        adverse = (self.s.lighter_half_spread_bps + self.s.hedge_slippage_bps) / BPS
        price = mid * (ONE + (ONE if quantity > 0 else -ONE) * adverse)
        fee = abs(quantity) * price * self.s.lighter_taker_fee_bps / BPS
        self.lighter_position += quantity
        self.lighter_cash -= quantity * price + fee
        self.totals['hedge_fees'] += fee
        self.totals['hedge_cost'] += abs(quantity) * mid * adverse
        item['quantity'] = ZERO
        self.log(now, 'hedge', quantity=quantity, price=price, fee=fee)
        self.update_maxima()

    def guard(self, now, row):
        if not self.reference_fresh(now):
            return
        price = _d(row['price'])
        cutoff = now - int(self.s.guard_window_s * NS_S)
        for queue in (self.guard_min, self.guard_max):
            while queue and queue[0][0] < cutoff:
                queue.popleft()
        while self.guard_min and self.guard_min[-1][1] >= price:
            self.guard_min.pop()
        while self.guard_max and self.guard_max[-1][1] <= price:
            self.guard_max.pop()
        self.guard_min.append((now, price))
        self.guard_max.append((now, price))
        move = (self.guard_max[0][1] / self.guard_min[0][1] - ONE) * BPS
        if move > self.s.guard_move_bps:
            # Extending cooldown within a pause preserves its generation and earliest cancellation time.
            if not self.guard_active:
                self.guard_generation += 1
            self.guard_active = True
            self.guard_until = now + int(self.s.guard_cooldown_s * NS_S)
            if now >= self.start:
                self.totals['guard_triggers'] += 1
            self.log(now, 'guard_trigger', move_bps=move)
            self.push(now + self.s.guard_latency_ms * NS_MS, 4, 'guard_cancel',
                      self.guard_generation)
            self.push(self.guard_until, 4, 'guard_resume', self.guard_until)

    def valuation(self):
        ondo_price = self.center or self.reference or ZERO
        lighter_price = self.proxy() or ZERO
        ondo_mark = self.ondo_cash + self.ondo_position * ondo_price
        lighter_mark = self.lighter_cash + self.lighter_position * lighter_price
        cost = ZERO
        for position, price, spread, fee in (
                (self.ondo_position, ondo_price, self.liquidation_half_spread, self.s.ondo_taker_fee_bps),
                (self.lighter_position, lighter_price, self.s.lighter_half_spread_bps, self.s.lighter_taker_fee_bps)):
            direction = ONE if position >= 0 else -ONE
            execution = price * (ONE - direction * spread / BPS)
            cost += position * (price - execution) + abs(position) * execution * fee / BPS
        common = {'ondo_position': self.ondo_position, 'lighter_position': self.lighter_position,
                  'ondo_cash': self.ondo_cash, 'lighter_cash': self.lighter_cash,
                  'ondo_price': ondo_price, 'lighter_price': lighter_price,
                  'ondo_pnl': ondo_mark, 'lighter_pnl': lighter_mark}
        pnl = ondo_mark + lighter_mark - self.totals['funding_cost']
        return {'mark': {**common, 'pnl': pnl, 'liquidation_cost': ZERO},
                'liquidate': {**common, 'pnl': pnl - cost, 'liquidation_cost': cost,
                    'ondo_half_spread_bps': self.liquidation_half_spread,
                    'ondo_half_spread_source': self.liquidation_spread_source,
                    'ondo_half_spread_sample_count': self.liquidation_spread_samples}}

    def snapshot(self, now):
        center, source = self.center_state(now)
        if center is not None:
            self.center = center
        depth_fresh = (self.depth_mid is not None and
                       now - self.depth_mid_time <= int(self.s.max_depth_age_s * NS_S))
        mid = self.depth_mid if depth_fresh else center
        sides = [[(o['price'], o['quantity']) for o in self.orders.values()
                  if o['active'] and o['side'] == side] for side in ('buy', 'sell')]
        def reward_weight(error):
            return eligible_weight(*sides, mid, band_bps=self.s.band_bps,
                weight_function=self.s.reward_weight, reward_mid_error_bps=error,
                reward_two_sided=self.s.reward_two_sided, min_quote_usd=self.s.min_quote_usd) if mid else ZERO
        weight = reward_weight(self.s.reward_mid_error_bps)
        weights_by_error = {str(error): reward_weight(error)
                           for error in self.s.reward_mid_error_scenarios}
        values = self.valuation()
        self.minutes.append({'time_ms': now // NS_MS, 'my_weight': weight,
            'before_events': dict(self.boundary_before),
            'two_sided': bool(sides[0] and sides[1]),
            'has_inventory': bool(self.ondo_position or self.lighter_position),
            'online': self.online,
            'center_source': source, 'ref_fallback': source == 'ondo_mid',
            'guard_available': self.reference_fresh(now),
            'guard_paused': self.guard_active,
            'ondo_position': self.ondo_position, 'lighter_position': self.lighter_position,
            'ondo_cash': self.ondo_cash, 'lighter_cash': self.lighter_cash,
            'center': center, 'reward_mid': mid,
            'reward_mid_source': 'observed' if depth_fresh else 'quote_center',
            'reward_weights_by_error': weights_by_error,
            'pnl_mark': values['mark']['pnl'], 'pnl_liquidate': values['liquidate']['pnl'],
            **{k: self.totals[k] for k in ('maker_fees', 'hedge_fees', 'hedge_cost',
                                           'basis_pnl', 'funding_cost', 'online_ms', 'guard_pause_ms',
                                           'guard_triggers', 'guard_pause_segments',
                                           'fallback_ms', 'fallback_fill_count',
                                           'fallback_edge', 'fallback_edge_net')},
            'online_breakdown_ms': dict(self.totals['online_breakdown_ms'])})

    def handle(self, now, kind, data):
        if kind == 'binance_visible':
            stamp, agg_id = _binance_ns(data), data.get('agg_id', 0)
            if self.reference_time is None or (stamp, agg_id) >= (self.reference_time, self.reference_agg_id):
                if not self.reference_fresh(now):
                    self.guard_min.clear()
                    self.guard_max.clear()
                self.reference, self.reference_time = _d(data['price']), stamp
                self.reference_agg_id = agg_id
                # Attribute drift from reference price changes to prior holdings before arbitrage pickoffs change positions.
                self.refresh_marks(now)
                self.pickoff(now)
                self.push(now, 4, 'guard', data)
                expiry = self.reference_time + int(self.s.max_ref_age_s * NS_S) + 1
                if expiry > now:
                    self.push(expiry, 4, 'reference_expired')
        elif kind == 'ondo_visible':
            side, stamp, price = data['aggressor_side'], _ondo_ns(data), _d(data['price'])
            if side == 'buy':
                if self.best_ask_time is None or stamp >= self.best_ask_time:
                    self.best_ask, self.best_ask_time = price, stamp
            elif self.best_bid_time is None or stamp >= self.best_bid_time:
                self.best_bid, self.best_bid_time = price, stamp
            self.add_sample(now, stamp, side, price, self.ondo_queue, self.ondo_values,
                            self.s.max_ondo_mid_age_s, 'ondo_mid_expired')
            if (self.reference is not None and
                    now - self.reference_time <= int(self.s.basis_pair_max_age_s * NS_S)):
                self.add_sample(now, stamp, side, price / self.reference - ONE,
                    self.basis_queue, self.basis_values, self.s.max_basis_age_s, 'basis_expired')
        elif kind == 'reference_expired':
            if not self.reference_fresh(now):
                self.guard_active, self.guard_until = False, -1
                self.guard_min.clear()
                self.guard_max.clear()
        elif kind == 'fill_visible':
            data['visible'] = True
            self.log(now, 'fill_visible', quantity=data['quantity'])
            if self.s.hedge_mode == 'lighter_proxy':
                self.add_pending_hedge(now, data['quantity'])
            self.pending_notifications = [item for item in self.pending_notifications if not item['visible']]
        elif kind == 'depth':
            self.depth_mid = _d(data['mid'])
            self.depth_mid_time = now
        elif kind == 'cancel':
            order = self.orders.pop(data, None)
            if order:
                self.log(now, 'cancel_effective', order_id=data)
        elif kind == 'activate':
            order = self.orders.get(data)
            if order:
                side, price = order['side'], order['price']
                if ((side == 'buy' and self.best_ask is not None and price >= self.best_ask) or
                        (side == 'sell' and self.best_bid is not None and price <= self.best_bid)):
                    self.totals['post_only_rejections'] += 1
                    self.quote_blocks.add('post_only')
                    self.log(now, 'post_only_reject', side=side, price=price)
                    del self.orders[data]
                else:
                    order['active'] = True
                    self.log(now, 'order_effective', order_id=data)
        elif kind == 'trade':
            self.match(now, data)
        elif kind == 'hedge':
            self.hedge(now, data)
        elif kind == 'guard':
            self.guard(now, data)
        elif kind == 'guard_cancel':
            if self.guard_active and data == self.guard_generation:
                self.cancel(now, '守卫触发')
        elif kind == 'guard_resume':
            if self.guard_until == data:
                self.guard_active = False
                self.log(now, 'guard_resume')
        elif kind == 'snapshot':
            self.snapshot(now)
        elif kind == 'gap_boundary':
            if self.in_gap(now) and (self.orders or self.ondo_position or self.lighter_position):
                self.pnl_determinate = False
        elif kind == 'decision':
            self.quote(now)
        self.refresh_marks(now)
        if kind not in ('snapshot', 'decision', 'trade'):
            self.decision(now)
        self.update_maxima()
        self.update_online(now)
        self.record_center(now)

    def run(self):
        latency = self.s.data_latency_ms * NS_MS
        for row in self.binance:
            self.push(_visible_ns(row, _binance_ns(row), latency), -2, 'binance_visible', row)
        for row in self.ondo:
            self.push(_visible_ns(row, _ondo_ns(row), latency), -1, 'ondo_visible', row)
            if _ondo_ns(row) >= self.start:
                self.push(_ondo_ns(row), 2, 'trade', row)
        for row in self.depths:
            self.push(row['received_ms'] * NS_MS, -1, 'depth', row)
        for row in self.gaps:
            self.push(row['start_ms'] * NS_MS, 4, 'gap_boundary')
            if row.get('end_ms') is not None:
                self.push(row['end_ms'] * NS_MS, 4, 'gap_boundary')
        for now in range(((self.start + 60000 * NS_MS - 1) // (60000 * NS_MS)) * 60000 * NS_MS,
                         self.end, 60000 * NS_MS):
            self.push(now, 5, 'snapshot')
        self.decision(self.start)
        while self.heap:
            now, _, _, kind, data = heapq.heappop(self.heap)
            if now >= self.start:
                self.accrue(now)
                if now % (60000 * NS_MS) == 0 and now != self.boundary_time:
                    self.boundary_time = now
                    values = self.valuation()
                    self.boundary_before = {
                        'pnl_mark': values['mark']['pnl'],
                        'pnl_liquidate': values['liquidate']['pnl'],
                        **{key: self.totals[key] for key in (
                            'maker_fees', 'hedge_fees', 'hedge_cost', 'basis_pnl',
                            'funding_cost', 'online_ms', 'guard_pause_ms', 'guard_triggers',
                            'guard_pause_segments', 'fallback_ms', 'fallback_fill_count',
                            'fallback_edge', 'fallback_edge_net')},
                        'online_breakdown_ms': dict(self.totals['online_breakdown_ms'])}
            self.handle(now, kind, data)
        self.accrue(self.end)
        self.totals['cancelled_hedge_quantity'] = (abs(
            sum((item['quantity'] for item in self.pending_hedges), ZERO) -
            sum((item['quantity'] for item in self.pending_notifications), ZERO))
            if self.s.hedge_mode == 'lighter_proxy' else ZERO)
        if self.totals['cancelled_hedge_quantity']:
            self.log(self.end, 'hedge_cancelled_at_end',
                     quantity=self.totals['cancelled_hedge_quantity'])
        values = self.valuation()
        self.totals['pickoff_pnl'] = sum(((fill['center'] - fill['price']) *
            fill['quantity'] * (ONE if fill['side'] == 'buy' else -ONE) - fill['fee']
            for fill in self.fills if fill['fill_type'] == 'pickoff'), ZERO)
        self.totals['pickoff_hold_to_end_pnl'] = sum(((values['mark']['ondo_price'] - fill['price']) *
            fill['quantity'] * (ONE if fill['side'] == 'buy' else -ONE) - fill['fee']
            for fill in self.fills if fill['fill_type'] == 'pickoff'), ZERO)
        self.totals['inventory_pnl'] = (values['mark']['pnl'] + self.totals['maker_fees'] +
            self.totals['hedge_fees'] + self.totals['hedge_cost'] + self.totals['funding_cost'] -
            self.totals['basis_pnl'])
        window_ondo = [r for r in self.ondo if self.start <= _ondo_ns(r) < self.end]
        window_binance = [r for r in self.binance if self.start <= _binance_ns(r) < self.end]
        coverage = {'ondo_count': len(window_ondo), 'binance_count': len(window_binance),
            'ondo_start_ms': _ondo_ns(window_ondo[0]) // NS_MS if window_ondo else None,
            'ondo_end_ms': _ondo_ns(window_ondo[-1]) // NS_MS if window_ondo else None,
            'binance_start_ms': window_binance[0]['transact_time'] if window_binance else None,
            'binance_end_ms': window_binance[-1]['transact_time'] if window_binance else None,
            'gaps': self.gaps, 'pnl_determinate': self.pnl_determinate,
            'gap_duration_ms': gap_duration_ms(self.gaps, self.start // NS_MS, self.end // NS_MS),
            'gap_duration_by_source_ms': {source: gap_duration_ms(
                [row for row in self.gaps if row.get('source', 'unknown') == source],
                self.start // NS_MS, self.end // NS_MS)
                for source in sorted({row.get('source', 'unknown') for row in self.gaps})},
            'reward_mid_source': ('mixed' if len({m['reward_mid_source'] for m in self.minutes}) > 1
                                  else self.minutes[0]['reward_mid_source'] if self.minutes else 'quote_center')}
        return {'symbol': self.symbol, 'ref_fallback': self.s.ref_fallback,
            'start_ms': self.start // NS_MS,
            'end_ms': self.end // NS_MS, 'events': self.events, 'fills': self.fills,
            'minutes': self.minutes, 'totals': self.totals, 'valuation': values,
            'center_prices': self.center_prices,
            'coverage': coverage, 'ondo_trades': window_ondo,
            'execution_assumptions': ['公共行情在 max(事件时间加data_latency, 实采received_ms) 后可见，缺接收时间沿用固定延迟；本方成交回报等待data_latency，对冲再等hedge_latency执行。'],
            'assumptions': ['公共行情在 max(事件时间加data_latency, 实采received_ms) 后可见，缺接收时间沿用固定延迟；本方成交回报等待data_latency，对冲再等hedge_latency执行。',
                            '强平半价差按同标的盘口观测价差中位数的一半校准，观测可晚于历史窗口；仅用于事后估值，不参与报价或套利判断。',
                            '基差盈亏按已对冲共同持仓乘两平台估值价差变动归因；其余价格盈亏计入库存盈亏。'],
            'binance_prices': [{'time_ms': r['transact_time'], 'price': _d(r['price'])}
                               for r in window_binance]}


def run_backtest(ondo_trades, binance_trades, settings, *, symbol, start_ms, end_ms,
                 depth_observations=(), gaps=()):
    """Replay a half-open window; earlier trades only warm up state, later trades cannot affect decisions, and order book spreads may calibrate liquidation costs."""
    if type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or end_ms <= start_ms:
        raise ValueError('回测时间窗口无效')
    ondo = sorted((dict(r) for r in ondo_trades if _ondo_ns(r) < end_ms * NS_MS),
                  key=lambda r: (_ondo_ns(r), r['id']))
    binance = sorted((dict(r) for r in binance_trades if r['transact_time'] < end_ms),
                     key=lambda r: (r['transact_time'], r.get('agg_id', 0)))
    deviation = validate_mapping(ondo, binance, start_ms=start_ms, end_ms=end_ms,
        max_mapping_dev_bps=getattr(settings, 'max_mapping_dev_bps', 500))
    identity = settings.for_symbol(symbol)
    liquidation_depths = [dict(r) for r in depth_observations
                          if r.get('market', identity.ondo_market) == identity.ondo_market]
    depths = sorted((r for r in liquidation_depths if r['received_ms'] < end_ms),
                    key=lambda r: (r['received_ms'], r.get('source', '')))
    gap_rows = [dict(r) for r in gaps if r['start_ms'] < end_ms and
                (r.get('end_ms') is None or r['end_ms'] > start_ms)
                and _relevant_gap(r, identity, settings.hedge_mode)]
    result = _Replay(ondo, binance, settings, symbol, start_ms, end_ms, depths, gap_rows,
                     liquidation_depths).run()
    result['coverage']['mapping_deviation_bps'] = deviation
    return result
