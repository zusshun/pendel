"""An unofficial parameterized reward model using Decimal for amounts and weights."""

from decimal import Decimal

from .market import number


WEEK_MINUTES = Decimal('10080')
ZERO = Decimal('0')
ONE = Decimal('1')


def _amount(value, name, positive=False, signed=False):
    try:
        result = number(value)
    except ValueError as exc:
        raise ValueError(f'{name} 必须是有限数字') from exc
    if positive and result <= 0:
        raise ValueError(f'{name} 必须大于零')
    if not signed and result < 0:
        raise ValueError(f'{name} 不能为负数')
    return result


def distance_bps(price, reward_mid, reward_mid_error_bps=0):
    """Compute absolute distance from the midpoint and add the historical midpoint error scenario."""
    price = _amount(price, 'price', positive=True)
    reward_mid = _amount(reward_mid, 'reward_mid', positive=True)
    error = _amount(reward_mid_error_bps, 'reward_mid_error_bps')
    return abs(price - reward_mid) / reward_mid * 10000 + error


def distance_weight(distance_bps, band_bps=25, weight_function='flat',
                    reward_mid_error_bps=0):
    """Return the distance weight; flat includes the boundary, while linear is zero at the boundary."""
    distance = _amount(distance_bps, 'distance_bps')
    band = _amount(band_bps, 'band_bps', positive=True)
    distance += _amount(reward_mid_error_bps, 'reward_mid_error_bps')
    if weight_function not in ('flat', 'linear'):
        raise ValueError('weight_function 必须是 flat 或 linear')
    if distance > band:
        return ZERO
    return ONE if weight_function == 'flat' else ONE - distance / band


def quote_weight(price, reward_mid, band_bps=25, weight_function='flat',
                 reward_mid_error_bps=0):
    """Return one quote's distance weight; the caller computes quantity and notional value."""
    return distance_weight(distance_bps(price, reward_mid), band_bps,
                           weight_function, reward_mid_error_bps)


def eligible_weight(bids, asks, reward_mid, *, band_bps=25, weight_function='flat',
                    reward_mid_error_bps=0, reward_two_sided=True, min_quote_usd=0):
    """Compute eligible weight from each order's minimum notional and distance, then check two-sided eligibility."""
    reward_mid = _amount(reward_mid, 'reward_mid', positive=True)
    minimum = _amount(min_quote_usd, 'min_quote_usd')
    distance_weight(0, band_bps, weight_function, reward_mid_error_bps)
    if not isinstance(reward_two_sided, bool):
        raise ValueError('reward_two_sided 必须是布尔值')
    sides = []
    for rows in (bids, asks):
        total = ZERO
        try:
            for raw_price, raw_quantity in rows:
                price = _amount(raw_price, 'price', positive=True)
                quantity = _amount(raw_quantity, 'quantity')
                notional = price * quantity
                if notional >= minimum:
                    total += notional * quote_weight(price, reward_mid, band_bps,
                                                      weight_function, reward_mid_error_bps)
        except (TypeError, ValueError) as exc:
            raise ValueError('挂单价格或数量无效：' + str(exc)) from exc
        sides.append(total)
    if reward_two_sided and any(value == 0 for value in sides):
        return ZERO
    return sum(sides, ZERO)


def minute_share(my_weight, comp_weight):
    """Share is zero without eligible orders and one when eligible orders face no competition."""
    mine = _amount(my_weight, 'my_weight')
    comp = _amount(comp_weight, 'comp_weight')
    return mine / (mine + comp) if mine else ZERO


def _minute_pool(pool_usdc_per_week, reward_markets):
    pool = _amount(pool_usdc_per_week, 'pool_usdc_per_week')
    if isinstance(reward_markets, bool) or not isinstance(reward_markets, int) or reward_markets <= 0:
        raise ValueError('reward_markets 必须是正整数')
    return pool / reward_markets / WEEK_MINUTES


def minute_reward(my_weight, comp_weight, pool_usdc_per_week, reward_markets=1):
    """Allocate the weekly pool across markets and minutes per week, then multiply by our share."""
    return _minute_pool(pool_usdc_per_week, reward_markets) * minute_share(my_weight, comp_weight)


def _weights(my_weights):
    return tuple(_amount(value, 'my_weight') for value in my_weights)


def total_reward(my_weights, comp_weight, pool_usdc_per_week, reward_markets=1):
    """Sum minute rewards under constant competition; zero-weight minutes receive no reward."""
    weights = _weights(my_weights)
    comp = _amount(comp_weight, 'comp_weight')
    pool = _minute_pool(pool_usdc_per_week, reward_markets)
    return pool * sum((minute_share(value, comp) for value in weights), ZERO)


def break_even_comp(my_weights, pnl_without_reward, pool_usdc_per_week, reward_markets=1):
    """Solve for constant competition C*; return a status and Decimal value, or None when no solution exists."""
    weights = _weights(my_weights)
    pnl = _amount(pnl_without_reward, 'pnl_without_reward', signed=True)
    pool = _minute_pool(pool_usdc_per_week, reward_markets)
    if pnl >= 0:
        return {'status': 'no_rewards_needed', 'comp': None}
    eligible = tuple(value for value in weights if value > 0)
    if not eligible:
        return {'status': 'never_eligible', 'comp': None}
    loss = -pnl
    maximum = pool * len(eligible)
    if maximum < loss:
        return {'status': 'unprofitable_even_at_zero_comp', 'comp': None}
    if maximum == loss:
        return {'status': 'break_even', 'comp': ZERO}
    low = ZERO
    high = pool * sum(eligible, ZERO) / loss
    while True:
        middle = (low + high) / 2
        if middle in (low, high):
            break
        large = tuple(value for value in eligible if value >= middle)
        small = tuple(value for value in eligible if value < middle)
        # Express large-weight shares as one minus the shortfall to avoid rounding away tiny shares added to one.
        surplus = pool * len(large) - loss
        surplus += pool * sum((value / (value + middle) for value in small), ZERO)
        surplus -= pool * sum((middle / (value + middle) for value in large), ZERO)
        if surplus == 0:
            return {'status': 'break_even', 'comp': middle}
        if surplus > 0:
            low = middle
        else:
            high = middle
    return {'status': 'break_even', 'comp': (low + high) / 2}


def required_pool_per_market(my_weights, pnl_without_reward, comp_weight):
    """Return the weekly pool per market needed to break even, or None if never eligible."""
    weights = _weights(my_weights)
    pnl = _amount(pnl_without_reward, 'pnl_without_reward', signed=True)
    comp = _amount(comp_weight, 'comp_weight')
    if pnl >= 0:
        return ZERO
    shares = sum((minute_share(value, comp) for value in weights), ZERO)
    return -pnl * WEEK_MINUTES / shares if shares else None
