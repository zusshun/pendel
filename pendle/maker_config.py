"""实盘做市配置；示例参数未标定时禁止进入正式交易。"""

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .maker_risk import RiskLimits


def _number(value, name, *, decimal=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value)) if decimal else float(value)
        if (not result.is_finite() if decimal else not math.isfinite(result)) or result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f'{name} 必须是大于零的有限数字') from exc


def _url(value, name, scheme):
    if not isinstance(value, str):
        raise ValueError(f'{name} 必须是安全地址')
    parts = urlsplit(value)
    if (parts.scheme != scheme or not parts.hostname or parts.username or parts.password
            or parts.fragment):
        raise ValueError(f'{name} 必须使用 {scheme}，不得包含用户名、密码或片段')
    return parts


@dataclass(frozen=True)
class MakerSettings:
    """首版只支持单市场；所有安全门槛都在构造时校验。"""

    market: str = 'IBM-USD.P'
    mode: str = 'dry-run'
    account: str = 'default'
    data_dir: Path = Path('data/maker')
    secret_file: Path | None = None
    telegram_secret_file: Path | None = None
    healthcheck_url: str = ''
    healthcheck_waived: bool = False
    quote_notional: Decimal = Decimal('400')
    soft_cap_usd: Decimal = Decimal('300')
    hard_cap_usd: Decimal = Decimal('600')
    max_position_usd: Decimal = Decimal('1200')
    max_account_exposure_usd: Decimal = Decimal('1200')
    max_margin_ratio: Decimal = Decimal('.3')
    max_window_loss_usd: Decimal = Decimal('25')
    emergency_flatten_loss_usd: Decimal = Decimal('50')
    offset_bps: Decimal = Decimal('12')
    skew_bps: Decimal = Decimal('4')
    min_offset_bps: Decimal = Decimal('8')
    band_bps: Decimal = Decimal('25')
    band_margin_bps: Decimal = Decimal('3')
    requote_bps: Decimal = Decimal('3')
    min_requote_interval_s: float = 10.0
    anchor_max_age_s: float = 600.0
    anchor_ema_tau_s: float = 120.0
    # 仅保留给外部参考 basis 的平滑；短周期中心 EMA 改用时间常数。
    anchor_ema_alpha: Decimal = Decimal('.01')
    max_anchor_drift_bps: Decimal = Decimal('15')
    slow_anchor_tau_s: float = 3600.0
    # 2026-11-04 实测 IBM 1 小时真实移动约 110bps。
    max_slow_drift_bps: Decimal = Decimal('150')
    # 只防灾难性累计漂移；自我引用的主要防线是 M14 的对称报价。
    max_mark_phase_drift_bps: Decimal = Decimal('300')
    max_ext_drift_bps: Decimal = Decimal('20')
    max_ext_age_s: float = 5.0
    # 标记价格按价变推送；接收静默、REST 确认和源时间合理性分别判断。
    mark_quiet_max_s: float = 60.0
    mark_rest_confirm_s: float = 15.0
    mark_rest_poll_after_s: float = 10.0
    mark_rest_poll_interval_s: float = 10.0
    mark_source_max_age_s: float = 3600.0
    discovery_quiet_max_s: float = 300.0
    max_book_age_s: float = 3.0
    narrow_spread_bps: Decimal = Decimal('20')
    source_min_dwell_s: float = 30.0
    max_mid_mark_bps: Decimal = Decimal('8')
    max_spread_bps: Decimal = Decimal('300')
    jump_window_s: float = 5.0
    jump_bps: Decimal = Decimal('15')
    gate_interval_s: float = 300.0
    gate_comp_max_usd: Decimal = Decimal('3500')
    gate_weight: str = 'flat'
    cooldown_s: float = 60.0
    stable_s: float = 30.0
    reconcile_interval_s: float = 30.0
    quiet_s: float = 3.0
    request_timeout_s: float = 5.0
    cancel_confirm_timeout_s: float = 10.0
    rate_per_second: float = 1.0
    public_ping_s: float = 30.0
    deadman_timeout_s: int = 30
    deadman_ack_timeout_s: float = 15.0
    deadman_ack_match: Mapping = field(default_factory=lambda: {
        'type': 'subscribed', 'channel': 'cancelAllOrdersAfterPerps', 'data.timeout_seconds': 30})
    mark_channel: str = 'markPricesPerps'
    binance_symbol: str = ''
    binance_ws_url: str = 'wss://fstream.binance.com/market/stream?streams='
    prod_rest_url: str = 'https://api.ondoperps.xyz'
    prod_ws_url: str = 'wss://api.ondoperps.xyz/ws'
    sandbox_rest_url: str = 'https://api.ondoperps-sandbox.xyz'
    sandbox_ws_url: str = 'wss://api.ondoperps-sandbox.xyz/ws'
    guards_calibrated: bool = False
    sandbox_verified: bool = False
    reduce_only_gtc_verified: bool = False
    window_start_day: int = 5
    window_start_time: str = '09:00'
    window_end_day: int = 0
    window_end_time: str = '05:00'
    timezone: str = 'Asia/Shanghai'
    max_flatten_slippage_bps: Decimal = Decimal('60')
    flatten_attempts: int = 3
    flatten_timeout_s: float = 60.0
    unknown_query_attempts: int = 3
    unknown_query_interval_s: float = 3.0
    unknown_timeout_s: float = 60.0
    strategy_interval_s: float = 1.0
    health_log_interval_s: float = 60.0
    log_level: str = 'INFO'
    _for_emergency: InitVar[bool] = False

    def __post_init__(self, _for_emergency):
        if type(_for_emergency) is not bool:
            raise ValueError('应急加载标识必须是布尔值')
        for setting in fields(self):
            value = getattr(self, setting.name)
            if isinstance(setting.default, Decimal):
                object.__setattr__(self, setting.name,
                                   _number(value, setting.name, decimal=True))
            elif type(setting.default) is float:
                object.__setattr__(self, setting.name, _number(value, setting.name))
            elif type(setting.default) is int and type(value) is not int:
                raise ValueError(f'{setting.name} 必须是整数')
            elif type(setting.default) is bool and type(value) is not bool:
                raise ValueError(f'{setting.name} 必须是布尔值')
        if self.mode not in ('dry-run', 'sandbox', 'live'):
            raise ValueError('mode 必须是 dry-run、sandbox 或 live')
        if (not isinstance(self.log_level, str)
                or self.log_level.upper() not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')):
            raise ValueError('log_level 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL')
        object.__setattr__(self, 'log_level', self.log_level.upper())
        if self.gate_weight not in ('flat', 'linear'):
            raise ValueError('gate_weight 必须是 flat 或 linear')
        if not isinstance(self.market, str) or not re.fullmatch(r'[A-Z0-9]+-USD\.P', self.market):
            raise ValueError('market 必须是单个 Ondo USD 永续市场')
        if not isinstance(self.account, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.account):
            raise ValueError('account 必须是安全的账户标识')
        if self.data_dir is None:
            raise ValueError('data_dir 必须是有效目录路径')
        for name in ('data_dir', 'secret_file', 'telegram_secret_file'):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, (str, Path)) or not str(value):
                    raise ValueError(f'{name} 必须是有效文件路径')
                object.__setattr__(self, name, Path(value))
        self.risk_limits
        self._validate_relations()
        self._validate_addresses()
        self._validate_window()
        match = self.deadman_ack_match
        if (isinstance(match, Mapping)
                and set(match) == {'type', 'channel', 'data.timeout_seconds'}
                and match['type'] == 'subscribed'
                and match['channel'] == 'cancelAllOrdersAfterPerps'
                and type(match['data.timeout_seconds']) is int):
            # 默认规则始终随超时配置变化，重建配置时也不遗留旧值。
            match = {**match, 'data.timeout_seconds': self.deadman_timeout_s}
        if (not isinstance(match, Mapping) or not match
                or any(not isinstance(key, str) or not key
                       or type(value) not in (str, bool, int)
                       for key, value in match.items())):
            raise ValueError('deadman_ack_match 必须是非空字段匹配表，值为字符串、布尔或整数')
        object.__setattr__(self, 'deadman_ack_match', MappingProxyType(dict(match)))
        for name in ('mark_channel', 'binance_symbol'):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f'{name} 必须是字符串，可留空禁用')
        if self.mode == 'live' and not self.mark_channel.strip():
            raise ValueError('live 必须启用标记价格频道')
        if self.mode == 'live' and not _for_emergency:
            if not self.guards_calibrated or not self.sandbox_verified:
                raise ValueError('live 要求守卫已标定，且已完成沙盒验收')
            if not self.healthcheck_url and not self.healthcheck_waived:
                raise ValueError('live 必须配置外部心跳 healthcheck_url 或明确设置 healthcheck_waived=true')

    def _validate_relations(self):
        if not self.soft_cap_usd < self.hard_cap_usd:
            raise ValueError('soft_cap_usd 必须小于 hard_cap_usd')
        if self.emergency_flatten_loss_usd < self.max_window_loss_usd:
            raise ValueError('紧急减仓亏损阈值不能低于窗口熔断阈值')
        if (self.band_bps >= 10000 or self.max_flatten_slippage_bps >= 10000
                or self.min_offset_bps > self.offset_bps - self.skew_bps
                or self.offset_bps + self.skew_bps > self.band_bps - self.band_margin_bps):
            raise ValueError('偏移及库存偏移必须落在最小偏移与 band 安全边界内')
        if self.anchor_ema_alpha > 1:
            raise ValueError('anchor_ema_alpha 不能大于一')
        if self.narrow_spread_bps <= 5:
            raise ValueError('narrow_spread_bps 必须大于 5，以保留正的来源切回门槛')
        if self.narrow_spread_bps > self.max_spread_bps:
            raise ValueError('narrow_spread_bps 不能大于 max_spread_bps')
        if (not 0 < self.deadman_timeout_s <= 30 or not 0 < self.flatten_attempts <= 3
                or not 0 < self.unknown_query_attempts <= 3
                or self.flatten_timeout_s > 60 or self.unknown_timeout_s > 60):
            raise ValueError('死人开关、消歧或减仓重试预算超过安全上限')

    def _validate_addresses(self):
        prod = _url(self.prod_rest_url, 'prod_rest_url', 'https')
        prod_ws = _url(self.prod_ws_url, 'prod_ws_url', 'wss')
        sandbox = _url(self.sandbox_rest_url, 'sandbox_rest_url', 'https')
        sandbox_ws = _url(self.sandbox_ws_url, 'sandbox_ws_url', 'wss')
        production_hosts = {prod.hostname.rstrip('.'), prod_ws.hostname.rstrip('.'),
                            'api.ondoperps.xyz'}
        if (sandbox.hostname.rstrip('.') in production_hosts
                or sandbox_ws.hostname.rstrip('.') in production_hosts):
            raise ValueError('沙盒地址不得指向正式环境')
        _url(self.binance_ws_url, 'binance_ws_url', 'wss')
        if self.healthcheck_url:
            _url(self.healthcheck_url, 'healthcheck_url', 'https')
        elif not isinstance(self.healthcheck_url, str):
            raise ValueError('healthcheck_url 必须是 HTTPS 地址或空字符串')

    def _validate_window(self):
        for name in ('window_start_day', 'window_end_day'):
            if not 0 <= getattr(self, name) <= 6:
                raise ValueError(f'{name} 必须是 0–6，周一为零')
        for name in ('window_start_time', 'window_end_time'):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
                raise ValueError(f'{name} 必须是 HH:MM 格式')
        if (self.window_start_day, self.window_start_time) == (self.window_end_day,
                                                             self.window_end_time):
            raise ValueError('窗口起止不能相同')
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError('timezone 必须是有效 IANA 时区') from exc

    @property
    def risk_limits(self):
        return RiskLimits(quote_notional=self.quote_notional,
                          max_position_usd=self.max_position_usd,
                          max_account_exposure_usd=self.max_account_exposure_usd,
                          max_margin_ratio=self.max_margin_ratio,
                          hard_cap_usd=self.hard_cap_usd)

    @property
    def rest_url(self):
        return self.sandbox_rest_url if self.mode == 'sandbox' else self.prod_rest_url

    @property
    def ws_url(self):
        return self.sandbox_ws_url if self.mode == 'sandbox' else self.prod_ws_url

    @property
    def instance_lock_path(self):
        return self.data_dir / f'{self.account}.lock'

    @property
    def halt_path(self):
        return self.data_dir / f'{self.account}.halt'

    @property
    def loss_path(self):
        return self.data_dir / f'{self.account}.loss.json'

    @classmethod
    def from_dict(cls, values, *, for_emergency=False):
        if not isinstance(values, Mapping):
            raise ValueError('maker 配置必须是键值表')
        unknown = set(values) - {setting.name for setting in fields(cls)}
        if unknown:
            raise ValueError('未知 maker 配置键：' + ', '.join(sorted(map(str, unknown))))
        return cls(**values, _for_emergency=for_emergency)

    @classmethod
    def from_file(cls, path, *, for_emergency=False):
        with Path(path).open('rb') as stream:
            values = tomllib.load(stream, parse_float=Decimal)
        if set(values) != {'maker'}:
            raise ValueError('配置必须只包含 [maker] 表')
        return cls.from_dict(values['maker'], for_emergency=for_emergency)
