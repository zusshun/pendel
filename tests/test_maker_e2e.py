"""End-to-end acceptance tests with an in-memory exchange, without sockets or real file writes."""

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import OrderStatus, OrdersBlocked
from pendle.ondo_api import SendGate, ApiError
from test_maker import book, MARKET


def live_deadman_ack(timeout=30):
    """Simulate a real private-channel nested dead man's switch subscription acknowledgment."""
    return {'type': 'subscribed', 'channel': 'cancelAllOrdersAfterPerps',
            'data': {'timeout_seconds': timeout}}


class Clock:
    def __init__(self):
        self.now = 100.0
        self.epoch = datetime(2026,11,7,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    def __call__(self):
        return self.now

    def wall(self):
        return self.epoch + self.now-100

    async def sleep(self, delay):
        self.now += delay
        await asyncio.sleep(0)


class Files:
    def __init__(self):
        self.rows, self.events = {}, []
    def exists(self, path):
        return str(path) in self.rows
    def read_text(self, path):
        return json.dumps(self.rows[str(path)])
    def write_json(self, path, row):
        self.rows[str(path)] = row
    def create_halt(self, path, row):
        self.events.append('fsync锁')
        self.rows[str(path)] = row
    def acquire_instance(self, path):
        return 'lock'
    def release_instance(self, handle):
        pass


class Alerts:
    def __init__(self, ok=True):
        self.ok, self.messages = ok, []
    async def self_test(self):
        return self.ok
    async def alert(self, text, **kwargs):
        self.messages.append(text)
        return True
    async def observe(self, **kwargs):
        pass


def publish_exchange_book(bot, exchange, clock):
    """Simulate a full book: preserve current third-party levels and replace the previously published remaining own quantity."""
    if bot.book is None:
        return
    sides=[dict(bot.book.bids),dict(bot.book.asks)]
    for side,price,quantity in getattr(exchange,'published_own',[]):
        if sides[side].get(price,D(0))>=quantity:
            sides[side][price]-=quantity
    published=[]
    for row in exchange.rows.values():
        if row['status'] in ('OPEN','PARTIAL'):
            side=0 if row['side']=='buy' else 1
            price,quantity=D(row['price']),D(row['size'])-D(row['filledSize'])
            sides[side][price]=sides[side].get(price,D(0))+quantity
            published.append((side,price,quantity))
    exchange.published_own=published
    bot.book=book(bids=list(sides[0].items()),asks=list(sides[1].items()),
                  at=int(clock.wall()*1000))
    bot.book_seen=clock()
    bot.engine.mark=(bot.book.mid,clock())
    bot.engine.in_price_discovery = False
    bot.engine.price_discovery_seen = clock()


class Exchange:
    """Writes may time out and IOC orders may fill partially; events use the same account state for accounting."""
    def __init__(self, clock, files):
        self.clock, self.files, self.gate = clock, files, SendGate()
        self.rows, self.calls, self.fills = {}, [], []
        self.position, self.ioc_sizes = D(0), []
        self.place_error = None
        self.on_quote = None
    async def sync_time(self):
        return 0
    def ws_login(self):
        return {'op':'login','args':{'key':'隐藏','time':1,'sign':'隐藏'}}
    async def place_order(self, body, before_send=None, emergency=False):
        self.gate.check()
        if before_send:
            before_send()
        self.calls.append(('place',body))
        if self.place_error:
            raise self.place_error
        cid = body['clientOrderId']
        row = {**body, 'orderId':cid, 'status':'OPEN','filledSize':'0',
               'time_ms':int(self.clock.wall()*1000)}
        if body['timeInForce'] == 'IOC':
            size = min(D(body['size']), self.ioc_sizes.pop(0) if self.ioc_sizes else D(0))
            row.update(status='FILLED' if size == D(body['size']) else 'CANCELED',
                       filledSize=str(size))
            if size:
                self.position += size if body['side']=='buy' else -size
                self.fills.append(dict(fill_id='f'+cid,market=MARKET,side=body['side'],quantity=size,
                    time_ms=row['time_ms'],client_order_id=cid,realized_pnl=D(0),fee=D(0)))
        self.rows[cid] = row
        if self.on_quote is not None:
            self.on_quote()
        return row
    async def get_order(self, cid):
        return self.rows.get(cid)
    async def cancel_all(self, market, stop=False):
        self.calls.append(('cancel_all',))
        self.files.events.append('撤单')
        for row in self.rows.values():
            if row['status']=='OPEN':
                row['status']='CANCELED'
        if self.on_quote is not None:
            self.on_quote()
        return {}
    async def cancel_order(self,cid):
        self.calls.append(('cancel',cid))
        return {}
    async def get_open_orders(self, market, stop=False):
        return [dict(r) for r in self.rows.values() if r['status']=='OPEN']
    async def get_positions(self):
        return [dict(market=MARKET,quantity=self.position,unrealized_pnl=D(0),mark_price=D(100))]
    async def get_balances(self):
        return dict(equity='10000',available='9000',margin_ratio='.01',used_margin='100',
                    under_liquidation=False,time_ms=int(self.clock.wall()*1000))
    async def get_fills(self, market, start_time=None,end_time=None):
        return list(self.fills)


class MakerE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files = Clock(), Files()
        self.exchange = Exchange(self.clock,self.files)
        self.alerts = Alerts()
        self.settings = replace(MakerSettings(),mode='sandbox',quiet_s=.01)
        self.bot = Maker(self.settings,api=self.exchange,alerter=self.alerts,
            rules=MarketRules(D('.01'),D('.001')), files=self.files,
            monotonic=self.clock,clock=self.clock.wall,sleep=self.clock.sleep)
        self.exchange.on_quote = lambda:publish_exchange_book(self.bot,self.exchange,self.clock)
        await self.bot.startup()
        self.bot.state.deadman_ack()
        await self.bot.reconcile_once()
        self.refresh()

    def refresh(self):
        now = int(self.clock.wall()*1000)
        bids,asks=[('99.9','10'),('99','10')],[('100.1','10'),('101','10')]
        for row in self.exchange.rows.values():
            if row['status'] in ('OPEN','PARTIAL'):
                side=bids if row['side']=='buy' else asks
                side.append((row['price'],str(D(row['size'])-D(row['filledSize']))))
        self.bot.book = book(bids=bids,asks=asks,at=now)
        self.exchange.published_own=[(0 if row['side']=='buy' else 1,D(row['price']),
            D(row['size'])-D(row['filledSize'])) for row in self.exchange.rows.values()
            if row['status'] in ('OPEN','PARTIAL')]
        self.bot.book_seen = self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()

    async def test_dry_run_never_writes_even_stop_or_flatten(self):
        self.bot.settings = replace(self.settings,mode='dry-run')
        await self.bot.tick()
        await self.bot.shutdown()
        self.assertFalse(self.exchange.calls)

    async def test_quotes_then_failed_gate_cancels_both(self):
        await self.bot.tick()
        self.assertEqual(len([c for c in self.exchange.calls if c[0]=='place']),2)
        self.bot.engine.settings = replace(self.settings,gate_comp_max_usd=D('1'))
        self.refresh()
        await self.bot.tick()
        self.assertEqual(self.exchange.calls[-1][0],'cancel_all')
        self.assertFalse(self.bot.state.active_orders())

    async def test_unknown_blocks_second_order_on_both_sides(self):
        self.exchange.place_error = TimeoutError()
        await self.bot.tick()
        self.refresh()
        await self.bot.tick()
        self.assertEqual(len([c for c in self.exchange.calls if c[0]=='place']),1)
        self.assertEqual(self.bot.state.market_mode(MARKET),'STOPPING')
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        self.assertFalse(self.bot.state.active_orders())
        self.assertTrue(any(c[0] == 'cancel_all' for c in self.exchange.calls))

    async def test_no_replacement_before_cancel_confirmation(self):
        await self.bot.tick()
        order = self.bot.state.active_orders()[0]
        await self.bot.manager.cancel(order)
        self.refresh()
        await self.bot.tick()
        self.assertEqual(len([c for c in self.exchange.calls if c[0]=='place']),2)

    async def test_halt_fsync_precedes_cancel(self):
        await self.bot.halt.trigger('测试熔断')
        self.assertLess(self.files.events.index('fsync锁'),self.files.events.index('撤单'))
        self.assertTrue(self.exchange.gate.trading_halted)

    async def test_watchdog_seals_all_sends_and_stops_renewal(self):
        self.bot.watchdog.last_seen = {}
        self.assertFalse(await self.bot.watchdog.check_once())
        self.assertFalse(self.exchange.gate.is_open)
        self.assertFalse(await self.bot.watchdog.check_once())
        with self.assertRaises(RuntimeError):
            await self.exchange.gate.send_ws(lambda msg: None,'ping')

    async def test_ioc_zero_fills_halts_after_three_attempts(self):
        self.bot.state.positions[MARKET] = self.exchange.position = D(7)
        await self.bot.tick()
        iocs = [c[1] for c in self.exchange.calls if c[0]=='place']
        self.assertEqual(len(iocs),3)
        self.assertTrue(all(r['reduceOnly'] and r['timeInForce']=='IOC' for r in iocs))
        self.assertEqual(self.bot.halt.exit_code,76)

    async def test_ioc_partial_fills_retry_actual_remainder_below_soft_cap(self):
        self.bot.state.positions[MARKET] = self.exchange.position = D(7)
        self.exchange.ioc_sizes = [D(1),D(2),D(2)]
        await self.bot.tick()
        iocs = [c[1] for c in self.exchange.calls if c[0]=='place']
        self.assertEqual(len(iocs),3)
        self.assertGreater(D(iocs[0]['size']),D(iocs[-1]['size']))
        self.assertLess(self.exchange.position*100,300)
        self.assertIsNone(self.bot.halt.exit_code)

    async def test_window_last_hour_only_reduces(self):
        self.assertFalse(self.bot.settings.reduce_only_gtc_verified)
        await self.assert_window_last_hour_only_reduces(reduce_only=False)

    async def test_window_last_hour_only_reduces_with_verified_reduce_only_gtc(self):
        self.bot.settings = replace(self.settings,reduce_only_gtc_verified=True)
        self.bot.engine.settings = self.bot.settings
        await self.assert_window_last_hour_only_reduces(reduce_only=True)

    async def assert_window_last_hour_only_reduces(self, *, reduce_only):
        self.clock.epoch = datetime(2026,11,9,4,15,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.bot.state.positions[MARKET] = self.exchange.position = D(1)
        self.refresh()
        await self.bot.tick()
        places = [c[1] for c in self.exchange.calls if c[0]=='place']
        self.assertEqual(len(places),1)
        self.assertEqual(places[0]['side'],'sell')
        self.assertTrue(places[0]['postOnly'])
        self.assertEqual(places[0]['timeInForce'],'GTC')
        self.assertEqual(places[0]['reduceOnly'],reduce_only)
        self.assertLessEqual(D(places[0]['size']),abs(self.exchange.position))
        active = self.bot.state.active_orders(MARKET)
        self.assertEqual(len(active),1)
        self.assertEqual(active[0].side,'sell')
        self.assertTrue(active[0].post_only)
        self.assertEqual(active[0].time_in_force,'GTC')
        self.assertEqual(active[0].reduce_only,reduce_only)
        self.assertLessEqual(active[0].remaining,abs(self.exchange.position))
        self.assertFalse(self.bot.qualified)
        self.assertIsNone(self.bot.halt.exit_code)

    async def test_last_hour_plain_post_only_must_reduce_and_fit_position(self):
        from pendle.maker_orders import OrdersBlocked
        self.clock.epoch = datetime(2026,11,9,4,15,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.bot.state.positions[MARKET] = self.exchange.position = D(1)
        self.refresh()
        now = self.bot.monotonic()
        for side, quantity in (('buy', D('.5')), ('sell', D('1.5'))):
            with self.subTest(side=side, quantity=quantity):
                order = self.bot.manager.create_intent(MARKET, side, quantity, D(100), reduce_only=False)
                try:
                    with self.assertRaisesRegex(OrdersBlocked, '窗口最后一小时禁止发送非 reduceOnly 挂单'):
                        self.bot.validate_resting_or_pending(order, self.bot.state, now=now)
                finally:
                    self.bot.state.orders.pop(order.client_order_id, None)

    async def test_invalid_tick_never_places_single_side(self):
        self.bot.rules = MarketRules(D('1'),D('.001'))
        await self.bot.tick()
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_sigterm_runs_stop_and_disables_deadman_only_after_zero(self):
        bot = self.bot
        class WS:
            def __init__(self):
                self.sent=[]
            async def send(self,message):
                self.sent.append(json.loads(message))
                if self.sent[-1].get('timeout_seconds') == 0:
                    await bot.on_private(live_deadman_ack(0))
        self.bot.private_ws=WS()
        await self.bot.tick()
        self.bot.request_stop(15)
        await self.bot.shutdown()
        self.assertTrue(self.bot.stop_event.is_set())
        self.assertFalse(await self.exchange.get_open_orders(MARKET))
        self.assertEqual(self.bot.private_ws.sent[-1]['timeout_seconds'],0)

    async def test_live_rejected_before_writes_when_alert_self_test_fails(self):
        self.bot.settings = replace(self.settings,mode='live',guards_calibrated=True,
            sandbox_verified=True,healthcheck_url='https://example.invalid/health')
        self.alerts.ok=False
        self.bot.instance.release()
        with self.assertRaisesRegex(ValueError,'Telegram'):
            await self.bot.startup(live=True)
        self.assertFalse(self.exchange.calls)

    async def test_deadman_ack_exact_configurable_match_is_required(self):
        self.bot.state.deadman_acked=False
        await self.bot.on_private({'channel':'cancelAllOrdersAfterPerps','status':'wrong'})
        self.assertFalse(self.bot.state.deadman_acked)
        await self.bot.on_private(live_deadman_ack(self.settings.deadman_timeout_s))
        self.assertFalse(self.bot.state.deadman_acked)
        class WS:
            async def send(self,message):
                pass
        self.bot.private_ws=WS()
        await self.bot.on_private({'op':'loggedIn'})
        await self.bot.on_private(live_deadman_ack(self.settings.deadman_timeout_s))
        self.assertTrue(self.bot.state.deadman_acked)

    async def test_public_filters_other_markets_and_uses_every_snapshot(self):
        self.bot.book=None
        def msg(market):
            return {'channel':'depthBooksPerps','data':[{'market':market,'time':
                datetime.fromtimestamp(self.clock.wall(),ZoneInfo('UTC')).isoformat(),
                'bids':[['99.9','10'],['99','10']],'asks':[['100.1','10'],['101','10']]}]}
        await self.bot.on_public(msg('AAPL-USD.P'))
        self.assertIsNone(self.bot.book)
        await self.bot.on_public(msg(MARKET))
        self.assertIsNotNone(self.bot.book)

    async def test_supervisor_sigterm_stops_tasks_and_orders(self):
        self.bot.started=False
        self.bot.instance.release()
        events=[]
        async def feed(private=False):
            events.append('私有' if private else '公开')
            if private:
                self.bot.state.deadman_ack()
                self.bot.watchdog.pong()
                await self.bot.reconcile_once()
            await asyncio.Event().wait()
        async def strategy():
            await asyncio.sleep(0)
            self.bot.request_stop(15)
        self.bot.feed=feed
        self.bot.strategy_loop=strategy
        self.bot.connect=lambda *a,**kw: None
        code=await self.bot.run()
        self.assertEqual(code,0)
        self.assertIn('公开',events)
        self.assertTrue(self.bot._shutdown)

    async def test_exchange_margin_ratio_is_authoritative(self):
        self.bot.state.balances['margin_ratio']=D('.31')
        await self.bot.tick()
        self.assertEqual(self.bot.halt.exit_code,76)
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_stop_signal_cannot_be_cleared_by_resume(self):
        self.bot.request_stop(15)
        self.refresh()
        await self.bot.tick()
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_window_outside_still_monitors_margin(self):
        self.clock.epoch=datetime(2026,11,9,6,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.bot.state.balances['margin_ratio']=D('.4')
        await self.bot.tick()
        self.assertEqual(self.bot.halt.exit_code,76)

    async def test_private_fill_without_market_syncs_then_rest_recovers_it(self):
        at = int(self.clock.wall()*1000)
        await self.bot.on_private({'channel':'fillsPerps','data':[{
            'id':'f1','orderId':'foreign','clientOrderId':'foreign','price':'100','size':'.1',
            'side':'buy','fee':'.01','pnl':'0','time':at,'isMaker':True}]})
        self.assertEqual(self.bot.state.positions[MARKET], D(0))
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(self.bot.reconcile_event.is_set())
        self.assertTrue(any('待沙盒样本' in text for text in self.alerts.messages))
        # Do not infer the market for updates missing it; use market-specific REST reconciliation to backfill fills and fees.
        self.exchange.position = D('.1')
        self.exchange.fills.append(dict(fill_id='f1', market=MARKET, side='buy', quantity=D('.1'),
            time_ms=at, client_order_id='foreign', realized_pnl=D(0), fee=D('.01')))
        await self.bot.reconcile_once()
        self.assertEqual(self.bot.state.positions[MARKET],D('.1'))
        self.assertEqual(self.bot.ledger.loss_usd,D('.01'))

    async def test_halted_position_monitoring_and_emergency_ioc_budget(self):
        self.bot.state.positions[MARKET]=self.exchange.position=D(1)
        self.bot.engine.evaluate(self.bot.book,self.bot.state,self.bot.rules,now=self.clock())
        self.bot.ledger.record('loss',realized_pnl='-60')
        await self.bot.trip_halt('亏损熔断')
        self.assertTrue(self.bot.gate.is_open)
        self.assertTrue(await self.bot.reconcile_once())
        for name in ('Strategy','OrderManager','Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        await self.bot.tick()
        count=len([c for c in self.exchange.calls if c[0]=='place'])
        self.assertEqual(count,3)
        self.assertTrue(all(c[1]['reduceOnly'] for c in self.exchange.calls if c[0]=='place'))
        await self.bot.tick()
        self.assertEqual(len([c for c in self.exchange.calls if c[0]=='place']),count)

    async def test_halted_position_held_past_max_hold_flattens_without_loss_trigger(self):
        """Production 2026-11-06: a halt left 1.63 IBM long unattended for ten hours."""
        self.bot.settings = replace(self.settings, max_hold_s=600.0)
        self.bot.engine.settings = self.bot.settings
        self.bot.state.positions[MARKET]=self.exchange.position=D(1)
        self.bot.engine.evaluate(self.bot.book,self.bot.state,self.bot.rules,now=self.clock())
        self.bot.track_position()
        await self.bot.trip_halt('持仓离场前对账失败')
        self.assertTrue(await self.bot.reconcile_once())
        for name in ('Strategy','OrderManager','Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        await self.bot.tick()
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])
        self.clock.now += 600
        self.refresh()
        self.assertTrue(await self.bot.reconcile_once())
        for name in ('Strategy','OrderManager','Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        await self.bot.tick()
        rows=[c[1] for c in self.exchange.calls if c[0]=='place']
        self.assertTrue(rows)
        self.assertTrue(all(r['reduceOnly'] and r['timeInForce']=='IOC' for r in rows))

    async def test_halted_position_without_max_hold_keeps_loss_only_trigger(self):
        self.bot.state.positions[MARKET]=self.exchange.position=D(1)
        self.bot.engine.evaluate(self.bot.book,self.bot.state,self.bot.rules,now=self.clock())
        self.bot.track_position()
        await self.bot.trip_halt('亏损熔断')
        self.clock.now += 3600
        self.refresh()
        self.assertTrue(await self.bot.reconcile_once())
        for name in ('Strategy','OrderManager','Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        await self.bot.tick()
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_halt_monitor_does_not_bypass_watchdog(self):
        self.bot.state.positions[MARKET]=self.exchange.position=D(1)
        self.bot.ledger.record('loss',realized_pnl='-60')
        await self.bot.trip_halt('亏损熔断')
        await self.bot.watchdog.check_once()
        await self.bot.tick()
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_window_rolls_pnl_only_at_next_actual_window(self):
        old=self.bot.ledger.window_id
        self.bot.ledger.record('loss',fee='1')
        self.clock.epoch+=7*86400
        self.refresh()
        await self.bot.tick()
        self.assertNotEqual(self.bot.ledger.window_id,old)
        self.assertEqual(self.bot.ledger.loss_usd,0)

    async def test_emergency_ioc_uses_fresh_center_after_halt(self):
        self.bot.state.positions[MARKET]=self.exchange.position=D(1)
        self.bot.engine.evaluate(self.bot.book,self.bot.state,self.bot.rules,now=self.clock())
        self.bot.ledger.record('loss',realized_pnl='-60')
        await self.bot.trip_halt('亏损熔断')
        self.assertTrue(await self.bot.reconcile_once())
        for name in ('Strategy','OrderManager','Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        self.bot.book=book([('104.9','10'),('104','10')],[('105.1','10'),('106','10')],
                           at=int(self.clock.wall()*1000))
        self.bot.book_seen=self.clock()
        self.bot.engine.mark=(D(105),self.clock())
        await self.bot.tick()
        rows=[c[1] for c in self.exchange.calls if c[0]=='place']
        self.assertTrue(rows)
        self.assertGreaterEqual(D(rows[0]['price']),D('104.37'))

    async def test_stale_anchor_without_fresh_mark_never_authorizes_hard_cap_ioc(self):
        self.bot.engine.anchor,self.bot.engine.anchor_at=D(100),self.clock()-601
        # A fresh M13 mark can establish a new center; when both sources are unavailable, the old anchor must not authorize IOC orders.
        self.bot.engine.mark=(D(100),self.clock()-self.bot.settings.mark_quiet_max_s-.001)
        self.bot.state.positions[MARKET]=self.exchange.position=D(6)
        self.bot.state.orders={}
        self.bot.book=book(at=int(self.clock.wall()*1000))
        def unavailable(*args,**kwargs):
            return (),()
        self.bot.engine.third_party=unavailable
        await self.bot.tick()
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_reduce_only_invalid_gtc_uses_bounded_passive_fallback(self):
        self.bot.settings=replace(self.settings,reduce_only_gtc_verified=True)
        self.bot.engine.settings=self.bot.settings
        self.bot.state.positions[MARKET]=self.exchange.position=D(3)
        original=self.exchange.place_order
        async def place(body,**kwargs):
            if body.get('reduceOnly'):
                kwargs['before_send']()
                raise ApiError(400,'reduce_only_invalid_tif')
            return await original(body,**kwargs)
        self.exchange.place_order=place
        await self.bot.tick()
        self.assertFalse(self.bot.engine.settings.reduce_only_gtc_verified)
        self.assertFalse(self.bot.state.active_orders())

    async def test_failed_competition_gate_waits_configured_interval(self):
        self.bot.engine.settings=replace(self.settings,gate_comp_max_usd=D(1))
        await self.bot.tick()
        self.bot.engine.settings=self.settings
        self.bot.guard.cooldown_s=self.bot.guard.stable_s=0
        self.clock.now+=1
        self.refresh()
        await self.bot.tick()
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_unavailable_ratio_falls_back_to_account_used_margin(self):
        self.bot.state.balances.update(margin_ratio=None,used_margin='4000')
        await self.bot.tick()
        self.assertEqual(self.bot.halt.exit_code,76)
        self.assertFalse([c for c in self.exchange.calls if c[0]=='place'])

    async def test_unchanged_quotes_do_not_oscillate_from_own_reservations(self):
        await self.bot.tick()
        count=len(self.exchange.calls)
        self.clock.now+=11
        publish_exchange_book(self.bot,self.exchange,self.clock)
        await self.bot.tick()
        self.assertEqual(len(self.exchange.calls),count)
        self.assertEqual(len(self.bot.state.active_orders()),2)
