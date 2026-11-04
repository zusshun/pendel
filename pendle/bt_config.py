"""历史回测和只读采集参数；奖励及执行假设均须由报告披露。"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType


def _decimal(value, name):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f'{name} 必须是有限数字') from exc
    if not result.is_finite():
        raise ValueError(f'{name} 必须是有限数字')
    if result < 0:
        raise ValueError(f'{name} 不能为负数')
    return result


def _symbol(value, name='symbol'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{0,19}', value):
        raise ValueError(f'{name} 必须是大写标的代码')
    return value


def _sequence(value, name):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f'{name} 必须是非空数组')
    return tuple(value)


def _symbols(value, name):
    result = tuple(_symbol(item, name) for item in _sequence(value, name))
    if len(set(result)) != len(result):
        raise ValueError(f'{name} 不能包含重复标的')
    return result


def _overrides(value):
    if not isinstance(value, Mapping):
        raise ValueError('symbol_overrides 必须是标的配置表')
    result = {}
    for symbol, override in value.items():
        _symbol(symbol)
        if not isinstance(override, Mapping):
            raise ValueError('标的覆盖配置必须是表')
        unknown = set(override) - {'price_tick', 'quantity_step', 'binance_symbol'}
        if unknown:
            raise ValueError('未知标的配置键：' + ', '.join(sorted(map(str, unknown))))
        row = {}
        for name, setting in override.items():
            if name == 'binance_symbol':
                row[name] = _symbol(setting, name)
            else:
                row[name] = _decimal(setting, name)
                if row[name] <= 0:
                    raise ValueError(f'{name} 必须大于零')
        result[symbol] = MappingProxyType(row)
    return MappingProxyType(result)


def _periods(value):
    if not isinstance(value, Mapping):
        raise ValueError('named_periods 必须是命名时段配置表')
    result = {}
    names = {'start_weekday', 'start_hour', 'end_weekday', 'end_hour'}
    for name, period in value.items():
        if not isinstance(name, str) or not name or not isinstance(period, Mapping):
            raise ValueError('命名时段必须有名称和配置表')
        if set(period) != names:
            raise ValueError('命名时段必须包含起止星期和小时，不能有未知键')
        row = {}
        for key, number in period.items():
            maximum = 6 if key.endswith('weekday') else 23
            if type(number) is not int or not 0 <= number <= maximum:
                raise ValueError(f'{key} 必须是 0–{maximum} 的整数')
            row[key] = number
        result[name] = MappingProxyType(row)
    return MappingProxyType(result)


def _serialise(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _serialise(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialise(item) for item in value]
    return value


@dataclass(frozen=True)
class SymbolSettings:
    """一个标的的合约身份和价格、数量精度。"""

    symbol: str
    price_tick: Decimal
    quantity_step: Decimal
    binance_symbol: str

    @property
    def ondo_market(self):
        return self.symbol + '-USD.P'


@dataclass(frozen=True)
class _CommonSettings:
    price_tick: Decimal = Decimal('0.01')
    quantity_step: Decimal = Decimal('0.1')
    band_bps: Decimal = Decimal('25')
    request_interval_ms: int = 250
    http_timeout_s: Decimal = Decimal('5')
    symbol_overrides: Mapping = field(default_factory=dict)

    def __post_init__(self):
        for setting in fields(self):
            value = getattr(self, setting.name)
            if isinstance(setting.default, Decimal):
                object.__setattr__(self, setting.name, _decimal(value, setting.name))
            elif type(setting.default) is int:
                if type(value) is not int or value < 0:
                    raise ValueError(f'{setting.name} 必须是非负整数')
            elif type(setting.default) is bool and type(value) is not bool:
                raise ValueError(f'{setting.name} 必须是布尔值')
        object.__setattr__(self, 'symbol_overrides', _overrides(self.symbol_overrides))
        self._positive('price_tick', 'quantity_step', 'band_bps', 'http_timeout_s')
        if self.band_bps >= 10000:
            raise ValueError('band_bps 必须小于 10000 bps')

    def _positive(self, *names):
        for name in names:
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} 必须大于零')

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, Mapping):
            raise ValueError('配置必须是键值表')
        unknown = set(data) - {setting.name for setting in fields(cls)}
        if unknown:
            raise ValueError('未知配置键：' + ', '.join(sorted(map(str, unknown))))
        return cls(**data)

    @staticmethod
    def read_values(path):
        with Path(path).open('rb') as stream:
            return tomllib.load(stream, parse_float=Decimal)

    @classmethod
    def from_file(cls, path):
        return cls.from_dict(cls.read_values(path))

    def to_dict(self):
        return {setting.name: _serialise(getattr(self, setting.name)) for setting in fields(self)}

    def for_symbol(self, symbol):
        symbol = _symbol(symbol)
        override = self.symbol_overrides.get(symbol, {})
        return SymbolSettings(symbol, override.get('price_tick', self.price_tick),
                              override.get('quantity_step', self.quantity_step),
                              override.get('binance_symbol', symbol + 'USDT'))


@dataclass(frozen=True)
class BacktestSettings(_CommonSettings):
    """确定性历史回测参数；默认值是研究情景而非官方奖励规则。"""

    symbols: tuple = ('INTC', 'DRAM', 'CRCL')
    quote_center: str = 'binance_basis'
    quote_notional: Decimal = Decimal('800')
    quote_offset_bps: Decimal = Decimal('10')
    max_inventory_usd: Decimal = Decimal('1600')
    principal_usd: Decimal = Decimal('1000')
    # 兼容旧配置键；基差有效性由样本数量和最大年龄控制。
    basis_window_s: Decimal = Decimal('300')
    basis_pair_max_age_s: Decimal = Decimal('30')
    basis_samples: int = 20
    max_basis_age_s: Decimal = Decimal('21600')
    max_ref_age_s: Decimal = Decimal('120')
    ref_fallback: str = 'ondo_mid'
    max_ondo_mid_age_s: Decimal = Decimal('1800')
    max_depth_age_s: Decimal = Decimal('120')
    markout_max_delay_s: Decimal = Decimal('1800')
    max_mapping_dev_bps: Decimal = Decimal('500')
    data_latency_ms: int = 100
    place_latency_ms: int = 500
    cancel_latency_ms: int = 500
    requote_bps: Decimal = Decimal('2')
    requote_mode: str = 'cancel_first'
    at_price_fill_ratio: Decimal = Decimal('0.5')
    at_price_fill_ratios: tuple = (Decimal('0'), Decimal('0.5'), Decimal('1'))
    pickoff_enabled: bool = True
    pickoff_edge_bps: Decimal = Decimal('2.5')
    hedge_mode: str = 'none'
    hedge_latency_ms: int = 500
    lighter_basis_bps: Decimal = Decimal('0')
    lighter_half_spread_bps: Decimal = Decimal('1')
    hedge_slippage_bps: Decimal = Decimal('1')
    guard_window_s: Decimal = Decimal('10')
    guard_move_bps: Decimal = Decimal('20')
    guard_latency_ms: int = 100
    guard_cooldown_s: Decimal = Decimal('30')
    ondo_maker_fee_bps: Decimal = Decimal('1')
    ondo_taker_fee_bps: Decimal = Decimal('2.5')
    lighter_taker_fee_bps: Decimal = Decimal('2.5')
    ondo_half_spread_bps: Decimal = Decimal('1')
    ondo_funding_bps_per_hour: Decimal = Decimal('0')
    lighter_funding_bps_per_hour: Decimal = Decimal('0')
    pool_usdc_per_week: Decimal = Decimal('110000')
    reward_markets: int = 87
    reward_weight: str = 'flat'
    reward_two_sided: bool = True
    min_quote_usd: Decimal = Decimal('0')
    reward_mid_error_bps: Decimal = Decimal('0')
    reward_mid_error_scenarios: tuple = (Decimal('0'), Decimal('10'), Decimal('20'))
    comp_source: str = 'scenarios'
    comp_scenarios: tuple = tuple(map(Decimal, ('1000', '3000', '10000', '30000',
        '100000', '300000', '1000000', '4000000')))
    named_periods: Mapping = field(default_factory=lambda: {'周末': {
        'start_weekday': 5, 'start_hour': 9, 'end_weekday': 0, 'end_hour': 5}})

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, 'symbols', _symbols(self.symbols, 'symbols'))
        self._positive('quote_notional', 'max_inventory_usd', 'principal_usd',
                       'basis_window_s', 'basis_pair_max_age_s', 'basis_samples',
                       'max_basis_age_s', 'max_ref_age_s', 'max_ondo_mid_age_s', 'max_depth_age_s',
                       'guard_window_s', 'reward_markets')
        for name in ('quote_offset_bps', 'lighter_half_spread_bps', 'hedge_slippage_bps',
                     'ondo_half_spread_bps', 'pickoff_edge_bps', 'reward_mid_error_bps'):
            if getattr(self, name) >= 10000:
                raise ValueError(f'{name} 必须小于 10000 bps')
        for name in ('at_price_fill_ratios', 'comp_scenarios', 'reward_mid_error_scenarios'):
            values = tuple(_decimal(item, name) for item in _sequence(getattr(self, name), name))
            object.__setattr__(self, name, values)
        if any(item >= 10000 for item in self.reward_mid_error_scenarios):
            raise ValueError('reward_mid_error_scenarios 必须小于 10000 bps')
        if self.at_price_fill_ratio > 1 or any(item > 1 for item in self.at_price_fill_ratios):
            raise ValueError('同价成交比例必须在 0–1 之间')
        for name, choices in {
                'quote_center': ('binance_basis',), 'requote_mode': ('cancel_first',),
                'ref_fallback': ('ondo_mid', 'none'),
                'hedge_mode': ('none', 'lighter_proxy'), 'reward_weight': ('flat', 'linear'),
                'comp_source': ('scenarios', 'observed')}.items():
            if getattr(self, name) not in choices:
                raise ValueError(f'{name} 只支持：' + ', '.join(choices))
        object.__setattr__(self, 'named_periods', _periods(self.named_periods))


@dataclass(frozen=True)
class CollectSettings(_CommonSettings):
    """公开行情采集参数，HTTP、WS、时钟和等待器由采集服务注入。"""

    focus_symbols: tuple = ('INTC', 'DRAM', 'CRCL')
    focus_snapshot_s: Decimal = Decimal('1')
    other_snapshot_s: Decimal = Decimal('60')
    contracts_interval_s: Decimal = Decimal('300')
    lighter_poll_s: Decimal = Decimal('2')
    lighter_meta_refresh_s: Decimal = Decimal('3600')
    silence_timeout_s: Decimal = Decimal('30')
    market_stale_s: Decimal = Decimal('600')
    source_silence_warn_s: Decimal = Decimal('180')
    binance_reconnect_s: Decimal = Decimal('82800')
    ondo_ping_s: Decimal = Decimal('30')
    binance_ws_url: str = 'wss://fstream.binance.com/market/stream?streams='
    ondo_ws_url: str = 'wss://api.ondoperps.xyz/ws'
    rest_scan_interval_s: Decimal = Decimal('60')
    max_disk_gb: Decimal = Decimal('20')
    min_free_gb: Decimal = Decimal('2')
    data_dir: str = 'data/collect'
    log_dir: str = 'log'
    max_http_workers: int = 4
    write_queue_size: int = 1024
    depth: int = 100
    max_tick_depth: int = 10

    @classmethod
    def from_dict(cls, data):
        """接受面向部署的 focus_markets，并兼容既有 focus_symbols。"""
        if isinstance(data, Mapping) and 'focus_markets' in data:
            data = dict(data)
            markets = _symbols(data.pop('focus_markets'), 'focus_markets')
            if 'focus_symbols' in data and _symbols(data['focus_symbols'], 'focus_symbols') != markets:
                raise ValueError('focus_markets 与 focus_symbols 必须一致')
            data['focus_symbols'] = markets
        return super().from_dict(data)

    @property
    def focus_markets(self):
        """焦点标的代码；Ondo 市场名称由 for_symbol 解析。"""
        return self.focus_symbols

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, 'focus_symbols', _symbols(self.focus_symbols, 'focus_symbols'))
        self._positive('focus_snapshot_s', 'other_snapshot_s', 'contracts_interval_s',
                       'lighter_poll_s', 'lighter_meta_refresh_s', 'silence_timeout_s', 'market_stale_s',
                       'source_silence_warn_s', 'binance_reconnect_s',
                       'ondo_ping_s', 'rest_scan_interval_s', 'max_disk_gb',
                       'max_http_workers', 'write_queue_size')
        for name in ('data_dir', 'log_dir'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f'{name} 必须是非空路径')
        for name in ('binance_ws_url', 'ondo_ws_url'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.startswith(('wss://', 'ws://')):
                raise ValueError(f'{name} 必须是 WS 地址')
        if not 1 <= self.depth <= 100:
            raise ValueError('depth 必须在 1–100 之间')
        if not 0 <= self.max_tick_depth <= 10:
            raise ValueError('max_tick_depth 必须在 0–10 之间')
