"""Record public order books for a bounded duration, storing errors per sample."""

import math
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from .market import (HttpClient, MarketError, RateLimited, parse_lighter, parse_ondo,
                     resolve_lighter, validate_ondo_contract)
from .storage import Sample, Store

ONDO = 'https://api.ondoperps.xyz'
LIGHTER = 'https://mainnet.zklighter.elliot.ai'


class PublicSources:
    def __init__(self, settings, client=None, now=time.time):
        self.settings = settings
        self.client = client or HttpClient(settings.http_timeout_s)
        self.now = now
        self.market_id = None

    def setup(self):
        contract = validate_ondo_contract(
            self.client.get_json(ONDO + '/v1/perps/contracts'), self.settings.ondo_market)
        market = resolve_lighter(self.client.get_json(LIGHTER + '/api/v1/orderBooks'), self.settings.symbol)
        self.market_id = market['market_id']
        return {'symbol': self.settings.symbol, 'ondo_market': self.settings.ondo_market,
                'ondo_contract': contract, 'lighter_market': market}

    def fetch(self, venue):
        if venue == 'ondo':
            url = ONDO + '/v1/perps/depth?' + urlencode({
                'market': self.settings.ondo_market, 'depth': self.settings.depth})
        else:
            if self.market_id is None:
                raise MarketError('尚未初始化 Lighter 市场映射')
            url = LIGHTER + '/api/v1/orderBookOrders?' + urlencode({
                'market_id': self.market_id, 'limit': self.settings.depth})
        raw = self.client.get_json(url)
        received_ms = int(self.now() * 1000)
        try:
            parsed = (parse_ondo(raw, self.settings.ondo_market, received_ms) if venue == 'ondo'
                      else parse_lighter(raw, self.settings.symbol, received_ms))
        except MarketError as exc:
            exc.raw = raw
            raise
        return parsed, raw


class Recorder:
    def __init__(self, settings, sources=None, now=time.time, sleep=time.sleep):
        self.settings = settings
        self.sources = sources or PublicSources(settings, now=now)
        self.now, self.sleep = now, sleep

    def record(self, db, seconds):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('录制时长必须是正的有限秒数')
        count = failed = 0
        status = 'completed'
        with Store(db) as store:
            run_id = store.create_run(self.settings, {}, 'record')
            try:
                store.set_metadata(run_id, self.sources.setup())
            except MarketError as exc:
                store.append(run_id, Sample(int(self.now() * 1000), None, None, {'setup': str(exc)}))
                raise
            deadline = self.now() + seconds
            with ThreadPoolExecutor(max_workers=2) as pool:
                try:
                    while self.now() < deadline:
                        started = self.now()
                        futures = {venue: pool.submit(self.sources.fetch, venue)
                                   for venue in ('ondo', 'lighter')}
                        books, errors, raw = {}, {}, {}
                        delay = 0
                        for venue, future in futures.items():
                            try:
                                books[venue], raw[venue] = future.result()
                            except MarketError as exc:
                                errors[venue] = str(exc)
                                if hasattr(exc, 'raw'):
                                    raw[venue] = exc.raw
                                if isinstance(exc, RateLimited):
                                    delay = max(delay, exc.retry_after_s)
                        store.append(run_id, Sample(int(self.now() * 1000), books.get('ondo'),
                                                     books.get('lighter'), errors, raw))
                        count += 1
                        failed += bool(errors)
                        wait = max(0, delay, self.settings.poll_interval_s - (self.now() - started))
                        remaining = deadline - self.now()
                        if remaining > 0 and wait > 0:
                            self.sleep(min(wait, remaining))
                except KeyboardInterrupt:
                    status = 'interrupted'
        return {'run_id': run_id, 'samples': count, 'failed_samples': failed, 'status': status}
