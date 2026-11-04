"""Chinese historical backtest reports; retrospective markouts do not affect trading or reward decisions."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil, log, sqrt
from statistics import median

from . import reward
from .backtest import gap_duration_ms

ZERO = Decimal('0')
BEIJING = timezone(timedelta(hours=8))
ONLINE_REASONS = ('online', 'fallback_online', 'no_reference', 'no_basis', 'guard', 'post_only',
                  'requote', 'inventory_limit', 'data_gap')
ONLINE_LABELS = {'online': '在线（至少一侧有效）', 'fallback_online': '回退在线', 'no_reference': '无参考价',
                 'no_basis': '无基差', 'guard': '守卫', 'post_only': 'post-only 拒单',
                 'requote': '撤单或重报空档', 'inventory_limit': '库存上限', 'data_gap': '数据断档'}
ASSUMPTIONS = (
    '奖励规则、奖励池、市场数、band、权重、双边门槛均为参数化假设，未经官方确认。',
    '历史奖励中间价近似为币安可见价加分侧滚动基差；币安过期时可回退到 Ondo 分侧成交中位数中点，reward_mid_error_bps 只用于条件性奖励情景。',
    '报价中心是近似值，Ondo 与币安基差会漂移；post-only 使用最近可见主动成交估计买一卖一，估计可能过时。',
    '穿价成交忽略我方订单对真实价格路径的影响；同价成交比例是假设，需比较 0、0.5、1。',
    '竞争量来自本周扫描实测或恒定情景，并非历史周末真实盘口；observed 仅匹配北京时间小时及工作日或周末，不跨桶补齐。',
    '盘口未完整覆盖 band 时竞争量仅为下界，估算奖励仅为上界；缺失样本保持缺失。',
    'Lighter 对冲使用币安价加代理基差、半价差及滑点，不能替代 Lighter 历史真实成交和可用流动性。',
    '期末分别按两个平台实际持仓计价，mark 为账面估值，liquidate 另外计 taker 费和半价差；待发送对冲在期末取消。',
    '成交发生后，策略须等待 data_latency_ms 才能观察回报并决策；对冲在回报可见后再等待 hedge_latency_ms 执行。',
    '未模拟保证金、杠杆、资金划转和强平；名义盈利不能证明给定本金可执行，各平台最大名义值与本金之比单列。',
    '仅一个周末样本，不能外推稳定收益；数据覆盖不足或持仓跨采集断档时，损益不可判定。',
    'markout 用成交后 10s、60s、300s 的可见 quote_center，已做基差调整；目标中心缺失时，取配置延后上限内首个有效中心并单列延后笔数，超出窗口或延后上限仍缺失。原始币安价格仅作辅助列。',
    '价差收益与 300s 逆向选择按成交归属时段；库存漂移为账面损益的剩余项。费用为 maker 费、对冲费和资金费；edge 另减对冲价差滑点。',
    '任一成交缺少成交时中心或 300s 中心时，完整 edge、库存漂移及 edge C* 不可判定；已观测收益、缺失数及覆盖比例单列，缺失不视为零。',
    '工作日非美股时段竞争量取北京时间 05:00（含）至 21:30（不含）的同权重实测中位数：工作日代理，非周末实测。',
    '在线表示至少一侧真实已生效订单且报价中心有效、无守卫或数据断档；单边在线不等于奖励双边资格。在线率原因互斥且合计覆盖总时长，回退在线单列。',
    '基差按可见主动买卖成交分别配对当时有效币安价，分别取未过期样本中位数后求中点；两侧成交无需同时发生。',
    'Ondo 回退中心仅使用当时已可见且未过期的双侧成交。回退期间守卫不可用；pickoff 仅在币安参考价有效时判断。回退成交时 edge 为成交时中心边际，回退 300s edge 不含共享对冲和资金费。',
    '实现波动为币安相邻逐笔对数收益平方和的平方根，单位 bps，不年化；P90 使用最近秩法。',
    '分时段在线与暂停时间优先使用累计毫秒差额；边界损益使用该时刻事件处理前的累计估值，旧格式快照只能近似分桶。',
    '价格和数量精度默认取 2026-11-04 markets 快照，并非回测历史窗口当时快照；配置显式 overrides 优先。',
    '上线字段只提供周末 C* 判据及最后历史盘口的情景比较；离线报告不能判断当前行情，必须实时复核。',
    '盘口观测的 band_bps 必须与回测一致；该标的任一观测 band 缺失、无效或不一致时，observed 竞争量、完整度及其奖励情景不可用。',
    '回退期另列以成交后 300s 起第一个币安推算 center 为基准的 markout，受相同延后上限与窗口限制，只作辅助披露，不参与 edge。',
)


def _d(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _at(row):
    if 'time_ms' in row:
        return int(row['time_ms'])
    if 'transact_time' in row:
        return int(row['transact_time'])
    return int(row['time_ns']) // 1000000


def _at_ns(row):
    return int(row['time_ns']) if 'time_ns' in row else _at(row) * 1000000


def beijing_bucket(time_ms):
    """Return weekday/weekend status and the Beijing-time hour; weekday numbering starts at zero for Monday."""
    dt = datetime.fromtimestamp(time_ms / 1000, BEIJING)
    return ('weekend' if dt.weekday() >= 5 else 'weekday', dt.hour)


def in_named_period(time_ms, period):
    """Treat named periods as weekly cycles with inclusive starts and exclusive ends, including week wraparound."""
    dt = datetime.fromtimestamp(time_ms / 1000, BEIJING)
    current = dt.weekday() * 86400 + dt.hour * 3600 + dt.minute * 60 + dt.second
    start = period['start_weekday'] * 86400 + period['start_hour'] * 3600
    end = period['end_weekday'] * 86400 + period['end_hour'] * 3600
    return start <= current < end if start < end else current >= start or current < end


def _stats(values):
    values = sorted(_d(value) for value in values if value is not None)
    if not values:
        return {'median': None, 'p90': None}
    return {'median': median(values), 'p90': values[max(0, ceil(len(values) * .9) - 1)]}


def _top_depth(row):
    total = ZERO
    for side, price in (('bids', row['bid']), ('asks', row['ask'])):
        levels = row.get('tick_depth', {}).get(side, ())
        value = next((_d(level.get('usd', 0)) for level in levels
                      if _d(level['price']) == _d(price)), None)
        if value is None:
            return None
        total += value
    return total


def _band_unavailable_reason(observations, settings):
    """Validate each asset's full observation set without mixing competition across different bands."""
    for row in observations:
        payload = row.get('payload')
        band = row.get('band_bps', payload.get('band_bps') if isinstance(payload, dict) else None)
        if band is None:
            return '盘口观测 band_bps 缺失，无法确认与回测一致；该标的 observed 竞争量和完整度不可用。'
        try:
            band = _d(band)
            if not band.is_finite() or band <= ZERO:
                raise ValueError
        except (ArithmeticError, TypeError, ValueError):
            return '盘口观测 band_bps 无效；该标的 observed 竞争量和完整度不可用。'
        if band != settings.band_bps:
            return ('盘口观测 band_bps ' + str(band) + ' 与回测 band_bps '
                    + str(settings.band_bps) + ' 不一致；该标的 observed 竞争量和完整度不可用。')
    return None


def competition_report(observations, settings):
    """Rank by market, Beijing-time hour, and day type; explicitly mark buckets without observations as missing."""
    observations = list(observations)
    groups = defaultdict(list)
    for row in observations:
        groups[(row['market'], *beijing_bucket(row['received_ms']))].append(row)
    rows = []
    distance_weight = reward.distance_weight(settings.quote_offset_bps, settings.band_bps,
                                              settings.reward_weight, settings.reward_mid_error_bps)
    my_weight = settings.quote_notional * 2 * distance_weight
    if settings.quote_notional < settings.min_quote_usd:
        my_weight = ZERO
    for market in sorted({row['market'] for row in observations}):
        reason = _band_unavailable_reason(
            [row for row in observations if row['market'] == market], settings)
        for day_type in ('weekday', 'weekend'):
            for hour in range(24):
                samples = groups.get((market, day_type, hour), [])
                usable = samples if reason is None else []
                row = {'market': market, 'day_type': day_type, 'hour': hour,
                       'sample_count': len(samples), 'missing': not usable,
                       'available': bool(usable), 'unavailable_reason': reason,
                       'uncertain': not usable or any(not item['book_band_complete'] for item in usable),
                       'status': '不可用：' + reason if reason else '缺失' if not samples else ('不确定' if any(
                           not item['book_band_complete'] for item in samples) else '完整'),
                       'coverage_start_ms': min((item['received_ms'] for item in samples), default=None),
                       'coverage_end_ms': max((item['received_ms'] for item in samples), default=None)}
                for weight in ('flat', 'linear'):
                    stats = _stats(_d(item[weight + '_bid_usd']) + _d(item[weight + '_ask_usd'])
                                   for item in usable)
                    row[weight + '_comp_median'] = stats['median']
                    row[weight + '_comp_p90'] = stats['p90']
                row['comp_median'] = row[settings.reward_weight + '_comp_median']
                row['comp_p90'] = row[settings.reward_weight + '_comp_p90']
                row['top_depth_sample_count'] = sum(_top_depth(item) is not None for item in samples)
                for prefix, values in (
                        ('spread_bps', (item['spread_bps'] for item in samples)),
                        ('top_depth', (_top_depth(item) for item in samples)),
                        ('completeness', (int(item['book_band_complete']) for item in usable))):
                    row.update({prefix + '_' + key: value for key, value in _stats(values).items()})
                row['estimated_reward_per_hour'] = (
                    reward.minute_reward(my_weight, row['comp_median'], settings.pool_usdc_per_week,
                                         settings.reward_markets) * 60 if usable else None)
                rows.append(row)
    rows.sort(key=lambda row: (row['missing'], -(row['estimated_reward_per_hour'] or ZERO),
                               row['market'], row['day_type'], row['hour']))
    return {'timezone': 'Asia/Shanghai (UTC+8)', 'rows': rows,
            'weight': settings.reward_weight, 'quote_notional': settings.quote_notional,
            'coverage_start_ms': min((row['received_ms'] for row in observations), default=None),
            'coverage_end_ms': max((row['received_ms'] for row in observations), default=None),
            'assumptions': list(ASSUMPTIONS)}


def _markout_stats(fills, samples):
    values = [row for row in samples if row.get('bps') is not None]
    raw = [row for row in samples if row.get('binance_bps') is not None]
    return {'sample_count': len(values), 'missing_count': len(fills) - len(values),
            'delayed_count': sum(bool(row.get('delayed')) for row in values),
            'mean_bps': sum((row['bps'] for row in values), ZERO) / len(values) if values else None,
            'total_usd': sum((row['usd'] for row in values), ZERO),
            'reference': 'quote_center', 'binance_sample_count': len(raw),
            'binance_missing_count': len(fills) - len(raw),
            'binance_mean_bps': sum((row['binance_bps'] for row in raw), ZERO) / len(raw) if raw else None,
            'binance_total_usd': sum((row['binance_usd'] for row in raw), ZERO)}


def _markouts(fills, prices, end_ms, max_ref_age_s, markout_max_delay_s=1800):
    """If the center price is missing, use the first later valid value within the delay limit, solely for retrospective analysis."""
    times = [_at_ns(row) for row in prices]
    valid = [(at, row) for at, row in zip(times, prices) if row.get('price') is not None]
    valid_times = [at for at, _ in valid]
    max_delay_ns = _d(markout_max_delay_s) * 1000000000
    output, samples = {}, defaultdict(list)
    for horizon in (10, 60, 300):
        for fill_index, fill in enumerate(fills):
            target = _at_ns(fill) + horizon * 1000000000
            i = bisect_right(times, target) - 1
            if target >= end_ms * 1000000:
                continue
            row = prices[i] if i >= 0 else {}
            # Legacy formats lack explicit invalidation events, so retain their reference-age threshold.
            stale = (i >= 0 and 'time_ns' not in row and
                     target - times[i] > max_ref_age_s * 1000000000)
            center_row, center_at = ({} if stale else row), (times[i] if i >= 0 else None)
            delayed = False
            if center_row.get('price') is None:
                after = bisect_right(valid_times, target)
                if after < len(valid):
                    candidate_at, candidate = valid[after]
                    if candidate_at < end_ms * 1000000 and candidate_at - target <= max_delay_ns:
                        center_row, center_at, delayed = candidate, candidate_at, True
            sign = 1 if fill['side'] == 'buy' else -1
            price, quantity = _d(fill['price']), _d(fill['quantity'])
            sample = {'time_ms': _at(fill), 'time_ns': _at_ns(fill), 'bps': None,
                      'usd': None, 'binance_bps': None, 'binance_usd': None,
                      'adverse_selection': None, 'fill_index': fill_index,
                      'ref_fallback': bool(fill.get('ref_fallback')),
                      'target_time_ns': target, 'center_time_ns': center_at,
                      'delayed': delayed,
                      'delay_s': _d(center_at - target) / 1000000000 if delayed else ZERO}
            if center_row.get('price') is not None:
                center = _d(center_row['price'])
                difference = (center - price) * sign
                sample.update(bps=difference / price * 10000, usd=difference * quantity)
                if fill.get('center') is not None:
                    sample['adverse_selection'] = sign * (center - _d(fill['center'])) * quantity
            if not stale and row.get('binance_price') is not None:
                difference = (_d(row['binance_price']) - price) * sign
                sample.update(binance_bps=difference / price * 10000, binance_usd=difference * quantity)
            samples[horizon].append(sample)
        output[str(horizon) + 's'] = _markout_stats(fills, samples[horizon])
    return output, samples


def _fallback_binance_markouts(fills, prices, end_ms, markout_max_delay_s):
    """The auxiliary metric uses only the first explicit Binance-derived center price at or after the target time."""
    fallback_fills = [fill for fill in fills if fill.get('ref_fallback')]
    candidates = [row for row in prices if row.get('center_source') == 'binance_basis'
                  and row.get('price') is not None and not row.get('ref_fallback')]
    times = [_at_ns(row) for row in candidates]
    samples = []
    for fill in fallback_fills:
        target = _at_ns(fill) + 300 * 1000000000
        i = bisect_left(times, target)
        if target >= end_ms * 1000000 or i == len(candidates):
            continue
        center_at = times[i]
        if (center_at >= end_ms * 1000000 or
                center_at - target > _d(markout_max_delay_s) * 1000000000):
            continue
        price, quantity = _d(fill['price']), _d(fill['quantity'])
        difference = (_d(candidates[i]['price']) - price) * (1 if fill['side'] == 'buy' else -1)
        samples.append({'time_ms': _at(fill), 'time_ns': _at_ns(fill),
                        'target_time_ns': target, 'center_time_ns': center_at,
                        'bps': difference / price * 10000, 'usd': difference * quantity,
                        'delayed': center_at > target,
                        'delay_s': _d(center_at - target) / 1000000000})
    return _fallback_binance_stats(fallback_fills, samples), samples


def _fallback_binance_stats(fills, samples):
    """Report auxiliary markouts separately from per-fill adverse selection in the primary metric."""
    stats = {key: value for key, value in _markout_stats(fills, samples).items()
             if not key.startswith('binance_')}
    return {**stats, 'reference': 'binance_basis',
            'description': '以之后第一个币安推算 center 为基准的 300s markout；目标为成交后 300s，延后受配置上限约束，不参与 edge'}


def _decomposition(fills, samples, mark, costs):
    """Keep missing values indeterminate; observed components cannot stand in for the complete edge."""
    spread = sum(((1 if fill['side'] == 'buy' else -1) *
                  (_d(fill['center']) - _d(fill['price'])) * _d(fill['quantity'])
                  for fill in fills if fill.get('center') is not None), ZERO)
    valid = [row for row in samples if row.get('adverse_selection') is not None]
    adverse = sum((row['adverse_selection'] for row in valid), ZERO)
    missing = len(fills) - len(valid)
    spread_missing = sum(fill.get('center') is None for fill in fills)
    complete = not missing and mark is not None
    fees = sum((_d(costs.get(key, 0)) for key in ('maker_fees', 'hedge_fees', 'funding_cost')), ZERO)
    hedge_cost = _d(costs.get('hedge_cost', 0))
    drift = mark - spread - adverse + fees if complete else None
    return {'spread_capture': None if spread_missing else spread,
            'adverse_selection': None if missing else adverse,
            'inventory_drift': drift, 'fees': fees, 'hedge_cost': hedge_cost,
            'edge_pnl': spread + adverse - fees - hedge_cost if complete else None,
            'mark_pnl': mark, 'reconciled_mark_pnl': spread + adverse + drift - fees if complete else None,
            'observed_spread_capture': spread, 'observed_adverse_selection': adverse,
            'fill_count': len(fills), 'sample_count': len(valid), 'missing_count': missing,
            'delayed_count': sum(bool(row.get('delayed')) for row in valid),
            'coverage_ratio': _d(len(valid)) / len(fills) if fills else Decimal('1'),
            'spread_missing_count': spread_missing, 'complete': complete}


def _period_valuation(weights, mark, liquidate, settings, edge=None):
    values = {}
    for mode, pnl in (('edge', edge), ('mark', mark), ('liquidate', liquidate)):
        values[mode] = {'pnl': pnl, 'c_star': reward.break_even_comp(
            weights, pnl, settings.pool_usdc_per_week, settings.reward_markets) if pnl is not None
            else {'status': 'unavailable', 'comp': None}}
    return values


def _scenarios(minutes, valuation, settings, observed):
    output = []
    comps = (None,) if settings.comp_source == 'observed' else settings.comp_scenarios
    for comp in comps:
        shares, missing, missing_weights, uncertain = ZERO, 0, 0, False
        for minute in minutes:
            if minute.get('reward_weight_missing'):
                missing_weights += 1
                continue
            weight = _d(minute.get('my_weight', 0))
            if weight == 0:
                continue
            item = observed.get(beijing_bucket(_at(minute))) if comp is None else None
            if comp is None and item is None:
                missing += 1
                continue
            shares += reward.minute_share(weight, item['comp_median'] if item else comp)
            uncertain |= bool(item and item['uncertain'])
        amount = settings.pool_usdc_per_week / settings.reward_markets / reward.WEEK_MINUTES * shares
        partial = bool(missing or missing_weights)
        row = {'comp_source': settings.comp_source, 'comp': comp,
               'reward': None if partial else amount, 'known_reward': amount,
               'missing_comp_minutes': missing, 'missing_weight_minutes': missing_weights,
               'partial': partial,
               'reward_is_upper_bound': uncertain}
        for mode in ('edge', 'mark', 'liquidate'):
            pnl = valuation[mode]['pnl']
            row['net_pnl_' + mode] = None if pnl is None or partial else pnl + amount
            row['required_pool_per_market_' + mode] = (
                None if pnl is None or partial or (pnl < 0 and shares == 0) else
                max(ZERO, -pnl) * reward.WEEK_MINUTES / shares if shares else ZERO)
        output.append(row)
    return output


def _fallback_metrics(fills, samples, totals, duration, determinate):
    """Report fallback duration and fills separately; do not allocate shared hedging costs to fill subsets."""
    fallback_fills = [fill for fill in fills if fill.get('ref_fallback')]
    fallback_samples = [sample for sample in samples if sample.get('ref_fallback')]
    fees = sum((_d(fill.get('fee', 0)) for fill in fallback_fills), ZERO)
    parts = _decomposition(fallback_fills, fallback_samples,
                           ZERO if determinate else None, {'maker_fees': fees})
    elapsed = _d(totals.get('fallback_ms', 0))
    edge = _d(totals.get('fallback_edge', parts['observed_spread_capture']))
    return {'fallback_ms': elapsed,
            'fallback_rate': elapsed / duration if duration else None,
            'fallback_fill_count': totals.get('fallback_fill_count', len(fallback_fills)),
            'fallback_edge': edge,
            'fallback_edge_net': _d(totals.get('fallback_edge_net', edge - fees)),
            'fallback_pnl_decomposition': parts}


def _error_minutes(minutes, error, settings):
    """Use replay-recorded order weights for error scenarios; leave unavailable legacy results unknown."""
    output = []
    for minute in minutes:
        weights = {_d(key): _d(value) for key, value in
                   minute.get('reward_weights_by_error', {}).items()}
        if error in weights:
            output.append({**minute, 'my_weight': weights[error]})
        elif error == settings.reward_mid_error_bps:
            output.append(minute)
        else:
            output.append({**minute, 'my_weight': ZERO, 'reward_weight_missing': True})
    return output


def _reward_error_scenarios(minutes, valuation, settings, observed):
    return [{'reward_mid_error_bps': error,
             'scenarios': _scenarios(_error_minutes(minutes, error, settings),
                                     valuation, settings, observed)}
            for error in settings.reward_mid_error_scenarios]


def _collections(result, prices, settings, markout_samples):
    """Distribute to hourly buckets and named periods in one pass rather than rescanning trade history per minute."""
    groups = defaultdict(lambda: defaultdict(list))

    def keys(at):
        yield ('hour', *beijing_bucket(at))
        for name, period in settings.named_periods.items():
            if in_named_period(at, period):
                yield ('named', name)

    sources = (('minutes', result.get('minutes', ())), ('fills', result.get('fills', ())),
               ('ondo_trades', result.get('ondo_trades', ())), ('prices', prices))
    for kind, rows in sources:
        for row in rows:
            for key in keys(_at(row)):
                groups[key][kind].append(row)
    at = result['start_ms']
    while at < result['end_ms']:
        end = min(result['end_ms'], (at // 3600000 + 1) * 3600000)
        for key in keys(at):
            groups[key]['duration_ms'].append(end - at)
        at = end
    previous_price = None
    for row in prices:
        current = _d(row['price'])
        if previous_price is not None:
            value = log(float(current / previous_price)) ** 2
            for key in keys(_at(row)):
                groups[key]['squared_returns'].append(value)
        previous_price = current
    for horizon, samples in markout_samples.items():
        for sample in samples:
            for key in keys(sample['time_ms']):
                groups[key]['markout_' + str(horizon)].append(sample)
    cumulative_keys = ('pnl_mark', 'pnl_liquidate', 'maker_fees', 'hedge_fees',
                       'hedge_cost', 'basis_pnl', 'funding_cost', 'online_ms',
                       'guard_pause_ms', 'guard_triggers', 'guard_pause_segments',
                       'fallback_ms', 'fallback_fill_count', 'fallback_edge', 'fallback_edge_net',
                       *('online_breakdown_' + name for name in ONLINE_REASONS))

    def counters(row):
        return {**row, **{'online_breakdown_' + name: value for name, value in
                          row.get('online_breakdown_ms', {}).items()}}

    previous = dict.fromkeys(cumulative_keys, ZERO)
    available = {name for minute in result.get('minutes', ()) for name in cumulative_keys
                 if name in counters(minute.get('before_events', minute))}
    if 'maker_fees' not in available:
        for fill in result.get('fills', ()):
            fee = _d(fill.get('fee', 0))
            previous['maker_fees'] += fee
            for key in keys(_at(fill)):
                groups[key]['maker_fees'].append(fee)
    if 'guard_triggers' not in available:
        for event in result.get('events', ()):
            if event['type'] == 'guard_trigger':
                previous['guard_triggers'] += 1
                for key in keys(_at(event)):
                    groups[key]['guard_triggers'].append(1)
    for minute in sorted(result.get('minutes', ()), key=_at):
        at = max(result['start_ms'], _at(minute) - 1)
        values = counters(minute.get('before_events', minute))
        for name in cumulative_keys:
            if name not in values:
                continue
            value = _d(values[name])
            for key in keys(at):
                groups[key][name].append(value - previous[name])
            previous[name] = value
    terminal = counters({**result.get('totals', {}), **{
        'pnl_' + mode: row['pnl'] for mode, row in result.get('valuation', {}).items()}})
    for name in cumulative_keys:
        if name in ('online_ms', 'guard_pause_ms') and name not in available:
            continue
        if name in terminal:
            for key in keys(result['end_ms'] - 1):
                groups[key][name].append(_d(terminal[name]) - previous[name])
    for name in settings.named_periods:
        groups[('named', name)]
    return groups


def _aggregate(group, result, settings, observed):
    minutes, fills = group.get('minutes', ()), group.get('fills', ())
    interval_ms = sum(group.get('duration_ms', ()))
    online_ms = sum(max(0, min(result['end_ms'], _at(row) + 60000) - _at(row)) for row in minutes
                    if row.get('online', False))
    paused_ms = sum(max(0, min(result['end_ms'], _at(row) + 60000) - _at(row)) for row in minutes
                    if row.get('guard_paused', False))
    if 'online_ms' in group:
        online_ms = sum(group['online_ms'], ZERO)
    if 'guard_pause_ms' in group:
        paused_ms = sum(group['guard_pause_ms'], ZERO)
    active = group.get('ondo_trades', ())
    output = {'minute_count': len(minutes), 'eligible_minutes': sum(
        _d(row.get('my_weight', 0)) > 0 for row in minutes),
        'online_ms': online_ms, 'online_rate': _d(online_ms) / interval_ms if interval_ms else None,
        'guard_pause_ms': paused_ms, 'guard_triggers': sum(group.get('guard_triggers', ()), ZERO),
        'guard_pause_segments': sum(group.get('guard_pause_segments', ()), ZERO),
        'online_breakdown_ms': {name: sum(group.get('online_breakdown_' + name, ()), ZERO)
                                for name in ONLINE_REASONS},
        'fill_count': len(fills),
        'pickoff_count': sum(row.get('fill_type') == 'pickoff' for row in fills),
        'fill_notional': sum((_d(row['notional']) for row in fills), ZERO),
        'active_notional': sum((_d(row.get('cost', _d(row['price']) * _d(row['size'])))
                                for row in active), ZERO),
        'active_buy_notional': sum((_d(row.get('cost', _d(row['price']) * _d(row['size'])))
                                    for row in active if row['aggressor_side'] == 'buy'), ZERO),
        'active_sell_notional': sum((_d(row.get('cost', _d(row['price']) * _d(row['size'])))
                                     for row in active if row['aggressor_side'] == 'sell'), ZERO),
        'realized_volatility_bps': _d(sqrt(sum(group.get('squared_returns', ())))) * 10000
            if group.get('prices') else None,
        'my_weight_sum': sum((_d(row.get('my_weight', 0)) for row in minutes), ZERO),
        'inventory_minutes': sum(bool(row.get('has_inventory')) for row in minutes)}
    for name in ('maker_fees', 'hedge_fees', 'hedge_cost', 'basis_pnl', 'funding_cost'):
        output[name] = sum(group.get(name, ()), ZERO)
    values = {}
    for mode in ('mark', 'liquidate'):
        name = 'pnl_' + mode
        values[mode] = sum(group[name], ZERO) if name in group else (ZERO if not minutes and not fills else None)
        if result.get('coverage', {}).get('pnl_determinate') is False:
            values[mode] = None
    weights = [_d(row.get('my_weight', 0)) for row in minutes]
    output['pnl_decomposition'] = _decomposition(fills, group.get('markout_300', ()),
                                                values['mark'], output)
    output.update(_fallback_metrics(fills, group.get('markout_300', ()),
        {name: sum(group[name], ZERO) for name in (
            'fallback_ms', 'fallback_fill_count', 'fallback_edge', 'fallback_edge_net') if name in group},
        interval_ms, result.get('coverage', {}).get('pnl_determinate') is not False))
    output['valuation'] = _period_valuation(weights, values['mark'], values['liquidate'], settings,
                                           output['pnl_decomposition']['edge_pnl'])
    output['inventory_pnl'] = (values['mark'] + output['maker_fees'] + output['hedge_fees']
        + output['hedge_cost'] + output['funding_cost'] - output['basis_pnl']) if values['mark'] is not None else None
    output['scenarios'] = _scenarios(minutes, output['valuation'], settings, observed)
    output['reward_mid_error_scenarios'] = _reward_error_scenarios(
        minutes, output['valuation'], settings, observed)
    output['markout'] = {}
    for horizon in (10, 60, 300):
        output['markout'][str(horizon) + 's'] = _markout_stats(fills, group.get('markout_' + str(horizon), ()))
    output['fallback_binance_300s_markout'] = _fallback_binance_stats(
        [fill for fill in fills if fill.get('ref_fallback')],
        group.get('markout_fallback_binance_300', ()))
    return output


def _compare_comp(comp, c_star):
    if comp is None:
        return 'unavailable'
    status = c_star.get('status', 'unavailable')
    if status == 'break_even':
        return ('below_c_star' if comp < c_star['comp'] else
                'at_c_star' if comp == c_star['comp'] else 'above_c_star')
    return status


def _weekday_proxy(observations, settings, minutes, weekend):
    """Use observed weekday competition from 05:00 to 21:30 with the same weighting for constant weekend scenarios."""
    reason = _band_unavailable_reason(observations, settings)
    samples = []
    for row in observations:
        at = datetime.fromtimestamp(row['received_ms'] / 1000, BEIJING)
        if at.weekday() < 5 and 300 <= at.hour * 60 + at.minute < 1290:
            samples.append(row)
    comp = median(_d(row[settings.reward_weight + '_bid_usd']) +
                  _d(row[settings.reward_weight + '_ask_usd']) for row in samples) if samples and not reason else None
    weights = [_d(row.get('my_weight', 0)) for row in minutes]
    missing_weights = sum(bool(row.get('reward_weight_missing')) for row in minutes)
    amount = reward.total_reward(weights, comp, settings.pool_usdc_per_week,
                                 settings.reward_markets) if comp is not None and not missing_weights else None
    valuation = weekend.get('valuation', {})
    uncertain = bool(reason) or not samples or any(not row['book_band_complete'] for row in samples)
    scenario = {'reward': amount, 'reward_is_upper_bound': uncertain,
                'missing_weight_minutes': missing_weights,
                'c_star_comparison': {}}
    for mode in ('edge', 'mark', 'liquidate'):
        value = valuation.get(mode, {})
        pnl = value.get('pnl')
        scenario['net_pnl_' + mode] = pnl + amount if pnl is not None and amount is not None else None
        c_star = (reward.break_even_comp(weights, pnl, settings.pool_usdc_per_week,
                                        settings.reward_markets)
                  if pnl is not None and not missing_weights
                  else {'status': 'unavailable', 'comp': None})
        scenario['c_star_comparison'][mode] = _compare_comp(comp, c_star)
    return {'label': '工作日非美股时段（北京时间 05:00–21:30）实测竞争量中位数；工作日代理，非周末实测',
            'weight': settings.reward_weight, 'sample_count': len(samples), 'comp_median': comp,
            'available': comp is not None, 'unavailable_reason': reason,
            'uncertain': uncertain, 'weekend': scenario,
            'coverage_start_ms': min((row['received_ms'] for row in samples), default=None),
            'coverage_end_ms': max((row['received_ms'] for row in samples), default=None)}


def _sensitivity(result, alternatives, settings):
    runs = {_d(settings.at_price_fill_ratio): result}
    for row in alternatives:
        if 'result' in row:
            runs[_d(row['ratio'])] = row['result']
        else:
            runs[_d(row.get('at_price_fill_ratio', settings.at_price_fill_ratio))] = row
    output = []
    for ratio in settings.at_price_fill_ratios:
        row = {'at_price_fill_ratio': ratio}
        if ratio not in runs:
            row['status'] = 'not_run'
        else:
            data = runs[ratio]
            determinate = data.get('coverage', {}).get('pnl_determinate') is not False
            centers = sorted(data.get('center_prices', ()), key=_at_ns)
            _, samples = _markouts(data.get('fills', ()), centers, data['end_ms'],
                                  settings.max_ref_age_s, settings.markout_max_delay_s)
            decomposition = _decomposition(data.get('fills', ()), samples[300],
                data['valuation']['mark']['pnl'] if determinate else None, data['totals'])
            row.update(status='ok', fill_count=data['totals']['fill_count'],
                       fill_notional=data['totals'].get('fill_notional', ZERO),
                       pnl_edge=decomposition['edge_pnl'],
                       edge_missing_count=decomposition['missing_count'],
                       edge_delayed_count=decomposition['delayed_count'],
                       pnl_mark=data['valuation']['mark']['pnl'] if determinate else None,
                       pnl_liquidate=data['valuation']['liquidate']['pnl'] if determinate else None)
            weights = [_d(minute.get('my_weight', 0)) for minute in data.get('minutes', ())]
            row['c_star'] = {mode: reward.break_even_comp(
                weights, row['pnl_' + mode], settings.pool_usdc_per_week,
                settings.reward_markets) if row['pnl_' + mode] is not None
                else {'status': 'unavailable', 'comp': None}
                for mode in ('edge', 'mark', 'liquidate')}
        output.append(row)
    return output


def build_report(result, settings, *, depth_observations=(), binance_trades=(), sensitivity=()):
    """Summarize one asset's replay, retaining Decimal amounts for caller-side serialization."""
    market = result['symbol'] + '-USD.P'
    observations = [row for row in depth_observations if row['market'] == market]
    band_reason = _band_unavailable_reason(observations, settings)
    competition = competition_report(observations, settings)
    observed = {(row['day_type'], row['hour']): row for row in competition['rows'] if row['available']}
    prices = sorted(result.get('binance_prices') or list(binance_trades), key=_at)
    prices = [row for row in prices if result['start_ms'] <= _at(row) < result['end_ms']]
    centers = sorted(result.get('center_prices', ()), key=_at_ns)
    centers = [row for row in centers if result['start_ms'] * 1000000 <= _at_ns(row)
               < result['end_ms'] * 1000000]
    fills = result.get('fills', ())
    markout, markout_samples = _markouts(fills, centers, result['end_ms'],
                                        settings.max_ref_age_s, settings.markout_max_delay_s)
    fallback_binance_markout, fallback_binance_samples = _fallback_binance_markouts(
        fills, centers, result['end_ms'], settings.markout_max_delay_s)
    markout_samples['fallback_binance_300'] = fallback_binance_samples
    groups = _collections(result, prices, settings, markout_samples)
    hourly, named = [], {}
    for key, group in groups.items():
        summary = _aggregate(group, result, settings, observed)
        if key[0] == 'hour':
            summary.update(day_type=key[1], hour=key[2])
            hourly.append(summary)
        else:
            named[key[1]] = summary
    weights = [_d(row.get('my_weight', 0)) for row in result.get('minutes', ())]
    determinate = result.get('coverage', {}).get('pnl_determinate') is not False
    decomposition = _decomposition(fills, markout_samples[300],
                                   result['valuation']['mark']['pnl'] if determinate else None,
                                   result['totals'])
    valuation = _period_valuation(weights, result['valuation']['mark']['pnl'] if determinate else None,
                                  result['valuation']['liquidate']['pnl'] if determinate else None,
                                  settings, decomposition['edge_pnl'])
    for mode in ('mark', 'liquidate'):
        valuation[mode] = {**result['valuation'][mode], **valuation[mode]}
    latest = max(observations, key=lambda row: row['received_ms'], default=None)
    weekend = named.get('周末', {})
    weekend_valuation = weekend.get('valuation', {})
    cstar = weekend_valuation.get('edge', {}).get('c_star', {'status': 'unavailable', 'comp': None})
    latest_comp = (_d(latest[settings.reward_weight + '_bid_usd'])
                   + _d(latest[settings.reward_weight + '_ask_usd'])) if latest and not band_reason else None
    eligible = bool(not band_reason and weekend.get('eligible_minutes') and latest and latest['book_band_complete'] and
                    ((cstar['status'] == 'break_even' and latest_comp < cstar['comp'])
                     or cstar['status'] == 'no_rewards_needed'))
    total = result['totals']
    pickoff_indices = {i for i, fill in enumerate(fills) if fill.get('fill_type') == 'pickoff'}
    pickoff_fills = [fill for i, fill in enumerate(fills) if i in pickoff_indices]
    pickoff_samples = [row for row in markout_samples[300] if row['fill_index'] in pickoff_indices]
    pickoff_parts = _decomposition(pickoff_fills, pickoff_samples, ZERO if determinate else None,
                                  {'maker_fees': sum((_d(fill.get('fee', 0))
                                                     for fill in pickoff_fills), ZERO)})
    duration = result['end_ms'] - result['start_ms']
    volatility = sum(log(float(_d(after['price']) / _d(before['price']))) ** 2
                     for before, after in zip(prices, prices[1:]))
    output = {**result, 'status': 'ok', 'pnl_determinate': determinate,
              'hedge_mode': settings.hedge_mode,
              'ref_fallback': result.get('ref_fallback', getattr(settings, 'ref_fallback', 'ondo_mid')),
              'timezone': 'Asia/Shanghai (UTC+8)',
              'hourly': sorted(hourly, key=lambda row: (row['day_type'], row['hour'])),
              'named_periods': named, 'valuation': valuation, 'markout': markout,
              'fallback_binance_300s_markout': fallback_binance_markout,
              'pnl_decomposition': decomposition,
              'pickoff': {'count': total.get('pickoff_count', len(pickoff_fills)),
                          'pnl': total.get('pickoff_pnl') if determinate else None,
                          'hold_to_end_pnl': total.get('pickoff_hold_to_end_pnl') if determinate else None,
                          'edge_pnl': pickoff_parts['edge_pnl'],
                          'delayed_count': pickoff_parts['delayed_count'],
                          'missing_count': pickoff_parts['missing_count'],
                          'pnl_description': '成交时中心价逐笔归因减 maker 费，不含共享对冲和资金费',
                          'hold_to_end_pnl_description': '期末中心价逐笔归因减 maker 费，不含共享对冲和资金费',
                          'edge_description': '300s markout 减该类成交 maker 费，不含共享对冲和资金费'},
              'weekday_proxy': _weekday_proxy(observations, settings,
                  groups[('named', '周末')].get('minutes', ()), weekend),
              'scenarios': _scenarios(result.get('minutes', ()), valuation, settings, observed),
              'sensitivity': _sensitivity(result, sensitivity, settings),
              'realized_volatility_bps': _d(sqrt(volatility)) * 10000 if prices else None,
              'online_rate': _d(total.get('online_ms', 0)) / duration if duration else None,
              'online_breakdown_ms': dict(total.get('online_breakdown_ms', {})),
              'guard_pause_segments': total.get('guard_pause_segments', 0),
              'competition': {'uncertain': any(row['uncertain'] for row in observed.values()) or not observed,
                              'available': bool(observed), 'unavailable_reason': band_reason,
                              'coverage_start_ms': competition['coverage_start_ms'],
                              'coverage_end_ms': competition['coverage_end_ms'],
                              'rows': [row for row in competition['rows'] if row['sample_count']]},
              'max_notional_to_principal': {
                  'ondo': _d(total.get('max_ondo_notional', 0)) / settings.principal_usd,
                  'lighter': _d(total.get('max_lighter_notional', 0)) / settings.principal_usd},
              'launch_criterion': {'description': '周六北京时间 09:00 检查：实时 comp < 周末 edge C*，且盘口完整覆盖 band；账面与强平 C* 同时列作辅助。edge 缺样时不提供上线结论。最后历史盘口仅作情景比较，当前是否上线必须实时复核。',
                                   'weekend_c_star': {mode: value['c_star'] for mode, value in weekend_valuation.items()},
                                   'eligible_now': False, 'requires_live_check': True,
                                   'requires_realtime_verification': True,
                                   'sample_comparison_eligible': eligible,
                                   'eligible_at_observation': eligible, 'observed_comp': latest_comp,
                                   'unavailable_reason': band_reason,
                                   'observed_ms': latest['received_ms'] if latest else None,
                                   'book_band_complete': None if band_reason else bool(latest and latest['book_band_complete'])},
              'settings': settings.to_dict(), 'assumptions': list(dict.fromkeys(
                  (*ASSUMPTIONS, *result.get('assumptions', ()))))}
    output.update(_fallback_metrics(fills, markout_samples[300], total, duration, determinate))
    output['reward_mid_error_scenarios'] = _reward_error_scenarios(
        result.get('minutes', ()), valuation, settings, observed)
    for scenario in output['reward_mid_error_scenarios']:
        error = scenario['reward_mid_error_bps']
        scenario['named_periods'] = {
            name: next(row for row in summary['reward_mid_error_scenarios']
                       if row['reward_mid_error_bps'] == error)
            for name, summary in named.items()}
        scenario['weekday_proxy'] = _weekday_proxy(observations, settings,
            _error_minutes(groups[('named', '周末')].get('minutes', ()), error, settings), weekend)
    return output


def _fmt(value, places=4):
    if value is None:
        return '缺失'
    if isinstance(value, (Decimal, float, int)):
        return format(value, '.' + str(places) + 'f').rstrip('0').rstrip('.') or '0'
    return str(value).replace('|', '\\|').replace('\n', ' ')


def _cstar(value):
    names = {'no_rewards_needed': '无需奖励', 'unprofitable_even_at_zero_comp': '零竞争仍亏损',
             'never_eligible': '从未合格', 'unavailable': '缺失'}
    return _fmt(value.get('comp')) if value.get('status') == 'break_even' else names.get(
        value.get('status'), '缺失')


def _iso(value):
    return datetime.fromtimestamp(value / 1000, BEIJING).isoformat() if value is not None else '缺失'


def _percent(value):
    return _fmt(_d(value) * 100, 2) + '%' if value is not None else '缺失'


def _comparison(value):
    return {'below_c_star': '低于 C*', 'at_c_star': '等于 C*', 'above_c_star': '高于 C*',
            'no_rewards_needed': '无需奖励', 'unprofitable_even_at_zero_comp': '零竞争仍亏损',
            'never_eligible': '从未合格', 'unavailable': '缺失'}.get(value, '缺失')


def markdown_summary(reports):
    """Compare fallback and hedge modes in the first table; detailed sections cover only the default fallback metric."""
    if isinstance(reports, dict):
        reports = reports.get('reports', [reports])
    lines = ['# 历史奖励回测摘要', '',
             '金额单位为 USD/USDC；C* 为合格竞争名义额。时间使用北京时间（UTC+8）。', '',
             '首表并列 ondo_mid 与 none 两种参考价回退口径，使用命名时段“周末”的 C*；edge 为主决策口径，账面与强平结果为辅助。奖励和 edge 净收益采用代理竞争量、中间价误差 0bps。在线率和回退时长占比为全窗口，至少一侧有效即在线，奖励资格另按双边规则判断。', '',
             '| 标的 | 回退口径 | 对冲模式 | 状态 | 周末 edge C* | 周末账面 C* | 周末强平 C* | 在线率 | 回退时长占比 | 周末奖励（0bps） | 周末 edge 净收益（0bps） | pickoff 次数 | pickoff 即时损益 | pickoff 持有至期末损益 | 周末成交数 | 周末 300s 有效/缺样 | 周末延后估计笔数 |',
             '|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for report in reports:
        fallback = report.get('ref_fallback', 'ondo_mid')
        if report.get('status') != 'ok':
            columns = [report.get('symbol', ''), fallback, report.get('hedge_mode', '缺失'),
                       '拒绝：' + report.get('reason', '数据不足'), *(['—'] * 13)]
            lines.append('| ' + ' | '.join(_fmt(value) for value in columns) + ' |')
            continue
        weekend = report['named_periods'].get('周末', {})
        values = weekend.get('valuation', {})
        parts = weekend.get('pnl_decomposition', {})
        zero_error = next((row['weekday_proxy']['weekend']
                           for row in report.get('reward_mid_error_scenarios', ())
                           if _d(row['reward_mid_error_bps']) == ZERO), None)
        proxy = zero_error or report['weekday_proxy']['weekend']
        pickoff = report.get('pickoff', {})
        columns = [report['symbol'], fallback, report['hedge_mode'], '完成',
                   *[_cstar(values.get(mode, {}).get('c_star', {}))
                     for mode in ('edge', 'mark', 'liquidate')], _percent(report.get('online_rate')),
                   _percent(report.get('fallback_rate')), proxy['reward'], proxy['net_pnl_edge'],
                   pickoff.get('count', 0), pickoff.get('pnl'), pickoff.get('hold_to_end_pnl'),
                   weekend.get('fill_count', 0),
                   str(parts.get('sample_count', 0)) + '/' + str(parts.get('missing_count', 0)),
                   parts.get('delayed_count', 0)]
        lines.append('| ' + ' | '.join(_fmt(value) for value in columns) + ' |')
    default_reports = [report for report in reports
                       if report.get('ref_fallback', 'ondo_mid') == 'ondo_mid']
    if not default_reports:
        default_reports = reports
    lines += ['', '300s 中心缺失时，使用配置延后上限内首个有效中心；延后估计参与 edge 和 edge C*，笔数单列。超过延后上限、目标超出窗口或成交时中心缺失，仍使该时段完整 edge、库存漂移和 edge C* 不可用；缺样不当作零逆向选择。', '',
              '| 标的 | 对冲模式 | 工作日代理竞争量 | 样本数 | 周末奖励 | 周末净收益 edge | 周末净收益账面 | 周末净收益强平 | 与周末 edge C* 比较 |',
              '|---|---|---:|---:|---:|---:|---:|---:|---|']
    for report in default_reports:
        if report.get('status') != 'ok':
            continue
        proxy = report['weekday_proxy']
        scenario = proxy['weekend']
        lines.append('| ' + ' | '.join(_fmt(value) for value in (
            report['symbol'], report['hedge_mode'], proxy['comp_median'], proxy['sample_count'],
            scenario['reward'], scenario['net_pnl_edge'], scenario['net_pnl_mark'],
            scenario['net_pnl_liquidate'], _comparison(scenario['c_star_comparison']['edge']))) + ' |')
    lines += ['', '工作日非美股时段（北京时间 05:00–21:30）实测竞争量中位数，同配置权重函数；**工作日代理，非周末实测**。盘口未覆盖完整 band 时，竞争量为下界、奖励为上界；详细状态见各标的。']
    lines += ['', '| 标的 | 对冲模式 | 中间价误差 bps | 代理竞争量下周末奖励 | 代理竞争量下周末 edge 净收益 |',
              '|---|---|---:|---:|---:|']
    for report in default_reports:
        if report.get('status') != 'ok':
            continue
        for row in report.get('reward_mid_error_scenarios', ()):
            scenario = row['weekday_proxy']['weekend']
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                report['symbol'], report['hedge_mode'], row['reward_mid_error_bps'],
                scenario['reward'], scenario['net_pnl_edge'])) + ' |')
    for report in default_reports:
        if report.get('status') != 'ok':
            continue
        total, parts = report['totals'], report['pnl_decomposition']
        liquidation = report['valuation']['liquidate']
        spread_source = {'depth_observations_median': '实测价差中位数/2',
                         'config': '配置回退'}.get(liquidation.get('ondo_half_spread_source'), '缺失')
        pickoff = report.get('pickoff', {})
        lines += ['', '## ' + report['symbol'] + ' / ' + report['hedge_mode']
                  + ' / ' + report.get('ref_fallback', 'ondo_mid'), '',
                  '窗口：' + _iso(report['start_ms']) + ' 至 ' + _iso(report['end_ms']) + '。', '',
                  '全窗口 edge 损益 ' + _fmt(report['valuation']['edge']['pnl'])
                  + '；账面损益 ' + _fmt(report['valuation']['mark']['pnl']) + '；强平损益 '
                  + _fmt(report['valuation']['liquidate']['pnl']) + '；成交名义额 '
                  + _fmt(total.get('fill_notional')) + '；在线率 ' + _percent(report.get('online_rate')) + '。', '',
                  '回退时长占比 ' + _percent(report.get('fallback_rate'))
                  + '；回退成交数 ' + str(report.get('fallback_fill_count', 0))
                  + '；回退成交时 edge ' + _fmt(report.get('fallback_edge'))
                  + '，扣 maker 费后 ' + _fmt(report.get('fallback_edge_net'))
                  + '；回退 300s edge ' + _fmt(report.get('fallback_pnl_decomposition', {}).get('edge_pnl')) + '。', '',
                  '全窗口 C*：edge ' + _cstar(report['valuation']['edge']['c_star'])
                  + '；账面 ' + _cstar(report['valuation']['mark']['c_star'])
                  + '；强平 ' + _cstar(report['valuation']['liquidate']['c_star']) + '。', '',
                  'Ondo 强平半价差 ' + _fmt(liquidation.get('ondo_half_spread_bps'))
                  + ' bps，来源：' + spread_source + '，样本 '
                  + str(liquidation.get('ondo_half_spread_sample_count', 0)) + '。', '',
                  'pickoff 次数 ' + str(pickoff.get('count', 0)) + '；pickoff 即时损益 '
                  + _fmt(pickoff.get('pnl')) + '（' + pickoff.get('pnl_description', '')
                  + '）；pickoff 持有至期末损益 ' + _fmt(pickoff.get('hold_to_end_pnl'))
                  + '（' + pickoff.get('hold_to_end_pnl_description', '')
                  + '）；300s edge 损益 ' + _fmt(pickoff.get('edge_pnl'))
                  + '（' + pickoff.get('edge_description', '') + '）。', '',
                  '300s edge 含延后估计 ' + str(parts.get('delayed_count', 0)) + ' 笔，最大延后 '
                  + _fmt(report.get('settings', {}).get('markout_max_delay_s')) + ' 秒。', '',
                  '最大名义值/本金：Ondo ' + _fmt(report['max_notional_to_principal']['ondo'])
                  + '，Lighter ' + _fmt(report['max_notional_to_principal']['lighter']) + '。', '',
                  '成本：maker 费 ' + _fmt(total.get('maker_fees'))
                  + '；对冲费 ' + _fmt(total.get('hedge_fees'))
                  + '；对冲价差滑点 ' + _fmt(total.get('hedge_cost'))
                  + '；资金费 ' + _fmt(total.get('funding_cost'))
                  + '；基差盈亏 ' + _fmt(total.get('basis_pnl')) + '。', '',
                  '守卫原始触发 ' + str(total.get('guard_triggers', 0))
                  + ' 次；暂停段数 ' + str(report['guard_pause_segments'])
                  + '；暂停 ' + _fmt(_d(total.get('guard_pause_ms', 0)) / 1000) + ' 秒。', '',
                  '| 盈亏拆解时段 | 价差收益 | 逆向选择 | 库存漂移 | 费用 | 对冲价差滑点 | edge 损益 | 300s 有效/缺样 | 延后估计笔数 | 已观测逆向选择 |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for name, item in [('全窗口', report), *report['named_periods'].items()]:
            part = item['pnl_decomposition']
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                name, part['spread_capture'], part['adverse_selection'], part['inventory_drift'],
                part['fees'], part['hedge_cost'], part['edge_pnl'],
                str(part['sample_count']) + '/' + str(part['missing_count']),
                part.get('delayed_count', 0),
                part['observed_adverse_selection'])) + ' |')
        if not parts['complete']:
            lines += ['', '全窗口完整 edge 不可判定：300s 缺样 ' + str(parts['missing_count'])
                      + '/' + str(parts['fill_count']) + '，覆盖率 ' + _percent(parts['coverage_ratio'])
                      + '；已观测价差收益 ' + _fmt(parts['observed_spread_capture'])
                      + '、已观测逆向选择 ' + _fmt(parts['observed_adverse_selection'])
                      + ' 不能代替完整 edge 决策。']
        lines += ['', '| 在线率互斥原因 | 时长秒 | 占总时长 |', '|---|---:|---:|']
        duration = report['end_ms'] - report['start_ms']
        for name in ONLINE_REASONS:
            amount = report['online_breakdown_ms'].get(name)
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                ONLINE_LABELS[name], _d(amount) / 1000 if amount is not None else None,
                _percent(_d(amount) / duration) if amount is not None and duration else None)) + ' |')
        lines.append('| 合计 | ' + _fmt(sum(report['online_breakdown_ms'].values(), ZERO) / 1000)
                     + ' | ' + _percent(sum(report['online_breakdown_ms'].values(), ZERO) / duration if duration else None) + ' |')
        lines += ['', report['launch_criterion']['description'], '',
                  '| 命名时段 | 分钟数 | 成交次数 | edge C* | 账面 C* | 强平 C* |',
                  '|---|---:|---:|---:|---:|---:|']
        for name, row in report['named_periods'].items():
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                name, row['minute_count'], row['fill_count'],
                *[_cstar(row['valuation'][mode]['c_star']) for mode in ('edge', 'mark', 'liquidate')])) + ' |')
        proxy = report['weekday_proxy']
        scenario = proxy['weekend']
        if report['competition'].get('unavailable_reason'):
            lines += ['', report['competition']['unavailable_reason']]
        lines += ['', proxy['label'] + '：' + _fmt(proxy['comp_median'])
                  + '，样本 ' + str(proxy['sample_count']) + '，权重 ' + proxy['weight']
                  + '，' + ('不可用' if proxy.get('unavailable_reason') else
                           '盘口完整' if not proxy['uncertain'] else '缺失或盘口不完整（奖励上界）') + '。',
                  '代理采集时段：' + _iso(proxy['coverage_start_ms']) + ' 至 ' + _iso(proxy['coverage_end_ms']) + '。',
                  '与周末 C* 比较：' + '；'.join(mode + ' ' + _comparison(scenario['c_star_comparison'][mode])
                                                   for mode in ('edge', 'mark', 'liquidate')) + '。', '',
                  '| 全窗口竞争量情景 | 奖励 | 净收益 edge | 净收益账面 | 净收益强平 | 所需每市场周池 edge | 所需每市场周池账面 | 所需每市场周池强平 |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|']
        for row in report['scenarios']:
            label = '实测同小时' if row['comp_source'] == 'observed' else _fmt(row['comp'])
            if row.get('partial'):
                label += '（缺失 ' + str(row['missing_comp_minutes']) + ' 分钟）'
            if row.get('reward_is_upper_bound'):
                label += '（奖励上界）'
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                label, row['reward'], *[row['net_pnl_' + mode] for mode in ('edge', 'mark', 'liquidate')],
                *[row['required_pool_per_market_' + mode] for mode in ('edge', 'mark', 'liquidate')])) + ' |')
        lines += ['', '| 全窗口 markout | center 均值 bps | center 总额 | 有效/缺样 | 延后估计笔数 | 原始币安辅助均值 bps |',
                  '|---|---:|---:|---:|---:|---:|']
        for horizon, row in report['markout'].items():
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                horizon, row['mean_bps'], row['total_usd'],
                str(row['sample_count']) + '/' + str(row['missing_count']),
                row.get('delayed_count', 0), row['binance_mean_bps'])) + ' |')
        lines += ['', '| 回退期辅助 markout | 均值 bps | 总额 | 有效/缺样 | 延后估计笔数 |',
                  '|---|---:|---:|---:|---:|']
        for name, item in [('全窗口', report), *report['named_periods'].items()]:
            row = item['fallback_binance_300s_markout']
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                name + '：以之后第一个币安推算 center 为基准的 300s markout',
                row['mean_bps'], row['total_usd'],
                str(row['sample_count']) + '/' + str(row['missing_count']),
                row['delayed_count'])) + ' |')
        lines += ['', '目标为回退成交后 300s，取目标时刻起第一个有效币安推算 center；受配置延后上限与报告窗口限制，仅作辅助披露，不参与 edge 主口径。']
        lines += ['', '| 同价成交比例 | 成交次数 | edge 损益 | edge C* | 账面损益 | 强平损益 | edge 缺样数 |',
                  '|---|---:|---:|---:|---:|---:|---:|']
        for row in report['sensitivity']:
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                row['at_price_fill_ratio'], row.get('fill_count'), row.get('pnl_edge'),
                _cstar(row.get('c_star', {}).get('edge', {})), row.get('pnl_mark'),
                row.get('pnl_liquidate'), row.get('edge_missing_count'))) + ' |')
        lines += ['', '| 日类型/北京时间小时 | 主动成交额 | 实现波动 bps | 在线率 | 成交次数 | 成交名义额 | 暂停秒 |',
                  '|---|---:|---:|---:|---:|---:|---:|']
        for row in report['hourly']:
            label = ('周末' if row['day_type'] == 'weekend' else '工作日') + ' ' + str(row['hour'])
            lines.append('| ' + ' | '.join(_fmt(value) for value in (
                label, row['active_notional'], row['realized_volatility_bps'], _percent(row['online_rate']),
                row['fill_count'], row['fill_notional'], _d(row['guard_pause_ms']) / 1000)) + ' |')
        coverage = report.get('coverage', {})
        lines += ['', '数据覆盖：' + ('完整' if coverage.get('complete') else '不足或有断档')
                  + '。竞争量采集时段：' + _iso(report['competition']['coverage_start_ms'])
                  + ' 至 ' + _iso(report['competition']['coverage_end_ms']) + '。']
        duration = coverage.get('gap_duration_ms', gap_duration_ms(
            coverage.get('gaps', ()), report['start_ms'], report['end_ms']))
        lines += ['', '断档总时长：' + _fmt(_d(duration) / 1000)
                  + ' 秒（按回测窗口裁剪，跨来源重叠区间只计一次）。']
        if not report.get('pnl_determinate', True):
            reason = ('持仓或挂单跨数据断档，或参考价不足' if coverage.get('gaps')
                      else '对冲执行时参考价失效，无法确认对冲')
            lines += ['', '损益不可判定：' + reason + '；不提供净收益、C* 和所需奖励池。']
        for gap in coverage.get('gaps', ()):
            lines.append('断档：' + _fmt(gap.get('reason', '未注明原因')) + '，'
                         + _iso(gap.get('start_ms')) + ' 至 ' + _iso(gap.get('end_ms')) + '。')
    lines += ['', '## 假设与限制', '']
    assumptions = dict.fromkeys((*ASSUMPTIONS, *(text for report in reports
                                for text in report.get('assumptions', ()))))
    lines += ['- ' + text for text in assumptions]
    return '\n'.join(lines) + '\n'


def competition_markdown(report):
    """Output the full ranking by estimated hourly rewards, including hours without observations."""
    lines = ['# 盘口竞争排名', '',
             '按估算每小时奖励从高到低排序，工作日与周末分别分桶；P90 使用最近秩法。', '',
             '采集时段：' + _iso(report.get('coverage_start_ms')) + ' 至 '
             + _iso(report.get('coverage_end_ms')) + '。', '',
             '| 市场 | 日类型 | 北京时间小时 | 样本数 | 竞争中位数 | 竞争 P90 | 价差中位数 bps | 价差 P90 | 一档深度中位数 | 一档深度 P90 | 完整度中位数 | 完整度 P90 | 估算小时奖励 | 状态 |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for row in report['rows']:
        values = (row['market'], '周末' if row['day_type'] == 'weekend' else '工作日', row['hour'],
                  row['sample_count'], row['comp_median'], row['comp_p90'], row['spread_bps_median'],
                  row['spread_bps_p90'], row['top_depth_median'], row['top_depth_p90'],
                  row['completeness_median'], row['completeness_p90'],
                  row['estimated_reward_per_hour'], row['status'])
        lines.append('| ' + ' | '.join(_fmt(value) for value in values) + ' |')
    lines += ['', '不确定：盘口未完整覆盖 band，竞争量为下界、奖励为上界。缺失小时不使用其他时段补齐。', '',
              '估算使用配置中的报价名义额、距离、权重和未经官方确认的奖励池；排名不代表可执行收益。']
    return '\n'.join(lines) + '\n'
