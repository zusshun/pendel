"""Deterministic hypothetical fill model for discrete order books, without account or live trading connections."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from . import __version__
from .config import Settings
from .market import resolve_lighter, validate_ondo_contract
from .storage import SCHEMA_VERSION

D = Decimal
BPS = D(10000)
ZERO = D(0)


def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


class DepthBudget:
    """Identical observations do not replenish budgets; add only positive increases in visible quantity per price level."""

    def __init__(self):
        self.observed = {'bids': {}, 'asks': {}}
        self.remaining = {'bids': {}, 'asks': {}}

    def update(self, book):
        for side in ('bids', 'asks'):
            old, remaining = self.observed[side], self.remaining[side]
            fresh = dict(getattr(book, side))
            self.remaining[side] = {
                price: min(size, remaining.get(price, ZERO) + max(ZERO, size - old.get(price, ZERO)))
                for price, size in fresh.items()}
            self.observed[side] = fresh

    def take(self, side, price, quantity):
        actual = min(quantity, self.remaining[side].get(price, ZERO))
        self.remaining[side][price] = self.remaining[side].get(price, ZERO) - actual
        return actual


@dataclass
class Quote:
    side: str
    price: Decimal
    quantity: Decimal
    created_ms: int
    cancel_ms: int | None = None


@dataclass
class Pending:
    quantity: Decimal
    due_ms: int
    created_ms: int


class Simulator:
    def __init__(self, settings, metadata):
        self.s = settings
        self.hedge_step = D(1).scaleb(-metadata['lighter_market']['supported_size_decimals'])
        self.hedge_min_base = D(metadata['lighter_market']['min_base_amount'])
        self.hedge_min_quote = D(metadata['lighter_market']['min_quote_amount'])
        self.positions = {'ondo': ZERO, 'lighter': ZERO}
        self.cash = {'ondo': ZERO, 'lighter': ZERO}
        self.fees = self.funding = ZERO
        self.latest = {}
        self.budgets = {venue: DepthBudget() for venue in self.positions}
        self.quotes, self.pending, self.events, self.reasons = [], [], [], []
        self.halted = self.incomplete = False
        self.last_event_ms = self.previous_ms = None
        self.previous_mid = None
        self.gaps, self.waits = [], []
        self.max_net = ZERO
        self.quote_time_ms = 0
        self.valuation_valid = False

    def event(self, kind, at, **fields):
        self.events.append({'type': kind, 'at_ms': at,
                            **{key: str(value) if isinstance(value, D) else value
                               for key, value in fields.items()}})

    def halt(self, reason, at):
        self.halted = True
        if reason not in self.reasons:
            self.reasons.append(reason)
            self.event('halt', at, reason=reason)
        self.cancel(at)

    def cancel(self, at):
        for quote in self.quotes:
            if quote.cancel_ms is None:
                quote.cancel_ms = at + self.s.cancel_latency_ms
                self.event('cancel_request', at, side=quote.side, effective_ms=quote.cancel_ms)

    def expire(self, at):
        remaining = []
        for quote in self.quotes:
            if quote.cancel_ms is not None and quote.cancel_ms <= at:
                self.event('cancelled', at, side=quote.side, remaining_quantity=quote.quantity)
            elif quote.quantity > 0:
                remaining.append(quote)
        self.quotes = remaining

    def accrue(self, at):
        if self.last_event_ms is not None:
            if at < self.last_event_ms:
                raise ValueError('来源事件时间倒退')
            hours = D(at - self.last_event_ms) / D(3600000)
            for venue, rate in (('ondo', self.s.ondo_funding_bps_per_hour),
                                ('lighter', self.s.lighter_funding_bps_per_hour)):
                if venue in self.latest:
                    payment = -self.positions[venue] * self.latest[venue].mid * rate / BPS * hours
                    self.cash[venue] += payment
                    self.funding += payment
        self.last_event_ms = at

    def execute(self, venue, signed_quantity, price, at, kind, **extra):
        fee_bps = self.s.ondo_maker_fee_bps if venue == 'ondo' else self.s.lighter_taker_fee_bps
        fee = abs(signed_quantity) * price * fee_bps / BPS
        self.positions[venue] += signed_quantity
        self.cash[venue] -= signed_quantity * price + fee
        self.fees += fee
        self.event(kind, at, side='buy' if signed_quantity > 0 else 'sell',
                   quantity=abs(signed_quantity), price=price, fee_usdc=fee, **extra)
        self.check_positions(at)

    def fill_maker(self, book):
        for quote in list(self.quotes):
            if book.received_ms <= quote.created_ms:
                continue
            if quote.cancel_ms is not None and book.received_ms > quote.cancel_ms:
                continue
            side = 'asks' if quote.side == 'buy' else 'bids'
            for price, _ in getattr(book, side):
                crossed = price < quote.price if quote.side == 'buy' else price > quote.price
                if not crossed:
                    break
                available = self.budgets['ondo'].remaining[side].get(price, ZERO)
                quantity = floor_step(min(quote.quantity, available), self.s.quantity_step)
                if quantity <= 0:
                    continue
                self.budgets['ondo'].take(side, price, quantity)
                quote.quantity -= quantity
                signed = quantity if quote.side == 'buy' else -quantity
                self.execute('ondo', signed, quote.price, book.received_ms, 'maker_fill')
                self.queue_hedge(signed, book.received_ms)
                if quote.quantity == 0:
                    break

    def queue_hedge(self, quantity, at):
        """Offset known opposing fills first so old tasks cannot reopen positions after net exposure reaches zero."""
        for item in self.pending:
            if item.quantity * quantity < 0:
                offset = min(abs(item.quantity), abs(quantity))
                direction = D(1) if quantity > 0 else D(-1)
                item.quantity += direction * offset
                quantity -= direction * offset
                self.event('hedge_netted', at, quantity=offset)
        self.pending = [item for item in self.pending if item.quantity]
        if quantity:
            self.pending.append(Pending(quantity, at + self.s.hedge_latency_ms, at))

    def hedge(self, book):
        due = [item for item in self.pending if item.due_ms <= book.received_ms]
        if not due:
            return
        quantity = sum((item.quantity for item in due), ZERO)
        current_net = sum(self.positions.values(), ZERO)
        if quantity and (quantity * current_net <= 0 or abs(quantity) > abs(current_net)):
            self.halt('hedge_not_reducing', book.received_ms)
            return
        created = min(item.created_ms for item in due)
        self.pending = [item for item in self.pending if item.due_ms > book.received_ms]
        if quantity == 0:
            self.event('hedge_netted', book.received_ms)
            return
        target = -quantity
        side = 'asks' if target > 0 else 'bids'
        levels = getattr(book, side)
        best = levels[0][0]
        needed = floor_step(abs(target), self.hedge_step)
        if needed < self.hedge_min_base or needed * best < self.hedge_min_quote:
            self.pending.append(Pending(quantity, book.received_ms, created))
            self.halt('hedge_minimum', book.received_ms)
            return
        executed = ZERO
        for price, _ in levels:
            effective = price * (1 + self.s.extra_slippage_bps / BPS * (1 if target > 0 else -1))
            slippage = abs(effective - best) / best * BPS
            if slippage > self.s.max_hedge_slippage_bps:
                break
            available = self.budgets['lighter'].remaining[side].get(price, ZERO)
            actual = floor_step(min(needed - executed, available), self.hedge_step)
            if actual <= 0:
                continue
            self.budgets['lighter'].take(side, price, actual)
            signed = actual if target > 0 else -actual
            wait = book.received_ms - created
            self.execute('lighter', signed, effective, book.received_ms, 'hedge_fill', wait_ms=wait)
            self.waits.append(wait)
            executed += actual
            if executed >= needed:
                break
        residual = quantity + (executed if target > 0 else -executed)
        if residual:
            self.pending.append(Pending(residual, book.received_ms, created))
        if executed < needed:
            self.halt('hedge_depth', book.received_ms)

    def marked_pnl(self):
        if any(self.positions[venue] and venue not in self.latest for venue in self.positions):
            return None
        value = sum(self.cash.values(), ZERO)
        for venue, quantity in self.positions.items():
            if quantity:
                price = self.latest[venue].bids[0][0] if quantity > 0 else self.latest[venue].asks[0][0]
                value += quantity * price
        return value

    def check_positions(self, at):
        for venue, quantity in self.positions.items():
            if venue in self.latest and abs(quantity) * self.latest[venue].mid > self.s.max_inventory_usd:
                self.halt('inventory', at)
        if self.latest:
            reference = self.latest.get('ondo', self.latest.get('lighter')).mid
            exposure = abs(sum(self.positions.values(), ZERO)) * reference
            self.max_net = max(self.max_net, exposure)
            if exposure > self.s.max_net_usd:
                self.halt('net_exposure', at)
        pnl = self.marked_pnl()
        if pnl is not None and pnl < -self.s.max_loss_usd:
            self.halt('loss_budget', at)

    def decide(self, at, ondo):
        self.check_potential(self.quotes, ondo.mid, at)
        if self.halted:
            self.cancel(at)
            self.expire(at)
            return
        prices = {
            'buy': (ondo.mid * (1 - self.s.quote_offset_bps / BPS) / self.s.price_tick
                    ).to_integral_value(rounding=ROUND_FLOOR) * self.s.price_tick,
            'sell': (ondo.mid * (1 + self.s.quote_offset_bps / BPS) / self.s.price_tick
                     ).to_integral_value(rounding=ROUND_CEILING) * self.s.price_tick}
        if prices['buy'] <= 0 or prices['buy'] >= ondo.asks[0][0] or prices['sell'] <= ondo.bids[0][0]:
            self.halt('post_only', at)
            return
        if len(self.quotes) == 2 and all(q.cancel_ms is None and q.price == prices[q.side]
                                          and q.quantity > 0 for q in self.quotes):
            return
        self.cancel(at)
        self.expire(at)
        if self.quotes:
            return
        candidates = [Quote(side, price, floor_step(self.s.quote_notional / price,
                                                   self.s.quantity_step), at)
                      for side, price in prices.items()]
        if any(q.quantity <= 0 for q in candidates):
            self.halt('quote_size', at)
            return
        self.check_potential(candidates, ondo.mid, at)
        if self.halted:
            return
        self.quotes = candidates
        for quote in candidates:
            self.event('quote', at, side=quote.side, price=quote.price, quantity=quote.quantity)

    def check_potential(self, quotes, mid, at):
        buys = sum((q.quantity for q in quotes if q.side == 'buy'), ZERO)
        sells = sum((q.quantity for q in quotes if q.side == 'sell'), ZERO)
        inventory = max(abs(self.positions['ondo'] + buys), abs(self.positions['ondo'] - sells)) * mid
        net = sum(self.positions.values(), ZERO)
        potential_net = max(abs(net + buys), abs(net - sells)) * mid
        if inventory > self.s.max_inventory_usd:
            self.halt('inventory', at)
        if potential_net > self.s.max_net_usd:
            self.halt('net_exposure', at)

    def quote_observation_time(self, start, end):
        intervals = sorted((max(start, q.created_ms), min(end, q.cancel_ms if q.cancel_ms is not None else end))
                           for q in self.quotes if q.quantity > 0)
        total, covered = 0, start
        for left, right in intervals:
            total += max(0, right - max(left, covered))
            covered = max(covered, right)
        return total

    def step(self, sample):
        at = sample.timestamp_ms
        gap = 0 if self.previous_ms is None else at - self.previous_ms
        if self.previous_ms is not None:
            self.gaps.append(gap)
        too_long = gap > self.s.max_stale_ms
        invalid = []
        if sample.errors or sample.ondo is None or sample.lighter is None:
            invalid.append('missing')
        else:
            if abs(sample.ondo.received_ms - sample.lighter.received_ms) > self.s.max_skew_ms:
                invalid.append('skew')
            for book in (sample.ondo, sample.lighter):
                if (at - book.received_ms > self.s.max_stale_ms
                        or (book.source_ms is not None and
                            (at - book.source_ms > self.s.max_stale_ms
                             or book.source_ms > book.received_ms + 1000))):
                    invalid.append('stale')
        if invalid or too_long:
            self.incomplete |= bool(self.quotes or any(self.positions.values()))
            for reason in invalid or ['gap']:
                self.halt(reason, at)
        if invalid:
            self.accrue(at)
            self.valuation_valid = False
            self.previous_ms = at
            return
        observation_cursor = self.previous_ms
        for _, _, book in sorted(((sample.ondo.received_ms, 0, sample.ondo),
                                   (sample.lighter.received_ms, 1, sample.lighter))):
            if observation_cursor is not None and not too_long:
                self.quote_time_ms += self.quote_observation_time(observation_cursor, book.received_ms)
            observation_cursor = book.received_ms
            self.accrue(book.received_ms)
            self.latest[book.venue] = book
            self.budgets[book.venue].update(book)
            if book.venue == 'ondo':
                if not too_long:
                    self.fill_maker(book)
                self.expire(book.received_ms)
            else:
                self.hedge(book)
            self.check_positions(book.received_ms)
        if observation_cursor is not None and not too_long:
            self.quote_time_ms += self.quote_observation_time(observation_cursor, at)
        self.accrue(at)
        self.valuation_valid = True
        basis = abs(sample.ondo.mid - sample.lighter.mid) / sample.lighter.mid * BPS
        if basis > self.s.max_basis_bps:
            self.halt('basis', at)
        if self.previous_mid and abs(sample.ondo.mid - self.previous_mid) / self.previous_mid * BPS > self.s.max_jump_bps:
            self.halt('jump', at)
        self.decide(at, sample.ondo)
        self.previous_mid = sample.ondo.mid
        self.previous_ms = at

    def report(self):
        pnl = self.marked_pnl() if self.valuation_valid else None
        reward = self.s.reward_usdc
        return {
            'model': 'discrete_book_crossing_hypothesis',
            'halted': self.halted, 'risk_reasons': self.reasons,
            'positions': {venue: str(q) for venue, q in self.positions.items()},
            'cash_usdc': {venue: str(q) for venue, q in self.cash.items()},
            'net_quantity': str(sum(self.positions.values(), ZERO)),
            'pending_hedge_quantity': str(sum((p.quantity for p in self.pending), ZERO)),
            'pending_hedges': [{'quantity': str(p.quantity), 'due_ms': p.due_ms,
                                'created_ms': p.created_ms} for p in self.pending],
            'open_quotes': [{'side': q.side, 'price': str(q.price), 'quantity': str(q.quantity),
                             'created_ms': q.created_ms, 'cancel_ms': q.cancel_ms} for q in self.quotes],
            'max_net_exposure_usdc': str(self.max_net),
            'fees_usdc': str(self.fees), 'funding_pnl_usdc': str(self.funding),
            'pnl_before_rewards_usdc': str(pnl) if pnl is not None else None,
            'reward_scenario_usdc': str(reward) if reward is not None else None,
            'scenario_pnl_usdc': str(pnl + reward) if pnl is not None and reward is not None else None,
            'break_even_reward_usdc': str(max(ZERO, -pnl)) if pnl is not None else None,
            'valuation_method': 'bbo_unrealized_excludes_exit_costs',
            'valuation_valid': self.valuation_valid,
            'valuation_received_ms': {venue: b.received_ms for venue, b in self.latest.items()},
            'valuation_source_ms': {venue: b.source_ms for venue, b in self.latest.items()},
            'execution_history_incomplete': self.incomplete,
            'valid_quote_observation_ms': self.quote_time_ms,
            'max_sample_gap_ms': max(self.gaps, default=0),
            'sample_intervals_ms': self.gaps,
            'observed_hedge_wait_ms': self.waits,
            'events': self.events,
            'limitations': [
                '假设成交不是真实成交；无排队或逐笔主动交易，无法重建样本间行情。',
                '盘口数量正增量视为新增流动性；消失再出现的价位视为新增，存在模型误差。',
                'Lighter 盘口仅有接收时间，不能证明源报价新鲜；采样精度不等于配置延迟。',
                '期末按BBO账面估值，未保证退出深度，未扣未来退出费用，未实际平仓。',
                '费用及双边每小时资金费为配置假设；未模拟真实资金费结算时点。',
                '奖励仅为显式输入情景，不估算官方份额、不计算年化。',
                'USD/USDC按1:1记账；未模拟保证金、强平、脱锚或平台故障尾部风险。',
            ]}


def replay(run, samples, override=None):
    if run.get('schema_version') != SCHEMA_VERSION or run.get('engine_version') != __version__:
        raise ValueError('不支持该会话的数据或引擎版本')
    settings = Settings.from_dict(run['settings'])
    if override is not None:
        settings = settings.for_replay(override)
    meta = run['metadata']
    if meta.get('symbol') != settings.symbol or meta.get('ondo_market') != settings.ondo_market:
        raise ValueError('会话标的映射与配置不一致')
    validate_ondo_contract({'success': True, 'result': [meta.get('ondo_contract', {})]}, settings.ondo_market)
    resolve_lighter({'code': 200, 'order_books': [meta.get('lighter_market', {})]}, settings.symbol)
    previous = -1
    simulator = Simulator(settings, meta)
    digest = hashlib.sha256(b'[')
    count = 0
    for sample in samples:
        if (isinstance(sample.timestamp_ms, bool) or not isinstance(sample.timestamp_ms, int)
                or sample.timestamp_ms < previous):
            raise ValueError('样本时间无效或倒退')
        for venue in ('ondo', 'lighter'):
            book = getattr(sample, venue)
            if book and (book.venue != venue or book.symbol != settings.symbol
                         or book.received_ms < previous or book.received_ms > sample.timestamp_ms):
                raise ValueError('来源盘口身份或时间顺序不一致')
        previous = sample.timestamp_ms
        simulator.step(sample)
        if count:
            digest.update(b',')
        digest.update(json.dumps(sample.to_dict(), sort_keys=True, separators=(',', ':')).encode())
        count += 1
    digest.update(b']')
    return {'run_id': run['id'], 'schema_version': SCHEMA_VERSION, 'engine_version': __version__,
            'settings': settings.to_dict(), 'metadata': meta,
            'sample_count': count,
            'input_sha256': digest.hexdigest(),
            **simulator.report()}
