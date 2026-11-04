"""Read and validate research settings, using decimal fixed-point values for all amounts."""

import math
import re
import tomllib
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    symbol: str = 'INTC'
    ondo_market: str = 'INTC-USD.P'
    poll_interval_s: float = 2.0
    http_timeout_s: float = 5.0
    depth: int = 50
    quote_notional: Decimal = Decimal('100')
    quote_offset_bps: Decimal = Decimal('10')
    price_tick: Decimal = Decimal('0.01')
    quantity_step: Decimal = Decimal('0.1')
    cancel_latency_ms: int = 500
    hedge_latency_ms: int = 500
    max_stale_ms: int = 6000
    max_skew_ms: int = 2000
    max_basis_bps: Decimal = Decimal('50')
    max_jump_bps: Decimal = Decimal('100')
    max_inventory_usd: Decimal = Decimal('300')
    max_net_usd: Decimal = Decimal('150')
    max_loss_usd: Decimal = Decimal('10')
    max_hedge_slippage_bps: Decimal = Decimal('20')
    extra_slippage_bps: Decimal = Decimal('1')
    ondo_maker_fee_bps: Decimal = Decimal('1')
    lighter_taker_fee_bps: Decimal = Decimal('0')
    ondo_funding_bps_per_hour: Decimal = Decimal('0')
    lighter_funding_bps_per_hour: Decimal = Decimal('0')
    reward_usdc: Decimal | None = None

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(field.default, Decimal) or field.name == 'reward_usdc':
                if field.name == 'reward_usdc' and value is None:
                    continue
                try:
                    if isinstance(value, bool):
                        raise ValueError
                    value = Decimal(str(value))
                except (InvalidOperation, ValueError, TypeError) as exc:
                    raise ValueError(f'{field.name} 必须是有限数字') from exc
                if not value.is_finite():
                    raise ValueError(f'{field.name} 必须是有限数字')
                if 'funding' not in field.name and value < 0:
                    raise ValueError(f'{field.name} 不能为负数')
                object.__setattr__(self, field.name, value)
            elif isinstance(field.default, (int, float)):
                if isinstance(field.default, float) and isinstance(value, Decimal):
                    value = float(value)
                    object.__setattr__(self, field.name, value)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f'{field.name} 必须是有限数字')
                if isinstance(field.default, int) and not isinstance(value, int):
                    raise ValueError(f'{field.name} 必须是整数')
                if value < 0:
                    raise ValueError(f'{field.name} 不能为负数')
        if not isinstance(self.symbol, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{0,19}', self.symbol):
            raise ValueError('symbol 必须是大写标的代码')
        if self.ondo_market != self.symbol + '-USD.P':
            raise ValueError('Ondo 市场与 symbol 不一致')
        for name in ('quote_notional', 'quote_offset_bps', 'price_tick', 'quantity_step',
                     'max_inventory_usd', 'max_net_usd', 'max_loss_usd', 'max_stale_ms',
                     'poll_interval_s', 'http_timeout_s'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} 必须大于零')
        if not 1 <= self.depth <= 100:
            raise ValueError('depth 必须在 1–100 之间')
        if self.quote_offset_bps >= 10000 or self.extra_slippage_bps >= 10000:
            raise ValueError('报价距离及额外滑点必须小于 10000 bps')

    @classmethod
    def from_dict(cls, data):
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError('未知配置键：' + ', '.join(sorted(unknown)))
        return cls(**data)

    @classmethod
    def from_file(cls, path: str | Path):
        return cls.from_dict(cls.read_values(path))

    @staticmethod
    def read_values(path: str | Path):
        with Path(path).open('rb') as stream:
            return tomllib.load(stream, parse_float=Decimal)

    def to_dict(self):
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}

    def for_replay(self, override):
        if isinstance(override, dict):
            override = Settings.from_dict({**self.to_dict(), **override})
        fixed = ('symbol', 'ondo_market', 'poll_interval_s', 'http_timeout_s', 'depth',
                 'price_tick', 'quantity_step')
        if any(getattr(self, name) != getattr(override, name) for name in fixed):
            raise ValueError('回放不得修改录制身份、精度或采样配置')
        return override
