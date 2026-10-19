from decimal import Decimal
import pytest
import bot
from app import validate_config

class EndRun(BaseException):
    pass

@pytest.mark.parametrize('sell_price,stop_loss', [(102,0),(98,-1)])
def test_signal_buy_and_exit_with_base_asset_fee(monkeypatch, sell_price, stop_loss):
    orders, statuses = [], []
    class Exchange:
        def __init__(self, *args, **kwargs):
            assert kwargs['testnet'] is True
        def get_account(self): return {'balances':[{'asset':'USDT','free':'1000'}]}
        def get_symbol_info(self, symbol):
            return {'baseAsset':'BTC','filters':[{'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001'}]}
        def get_klines(self, **kwargs):
            return [[i*60000,0,0,0,price,vol,0,0,0,0,0,0] for i,price,vol in [(0,100,1),(1,100,1),(2,102,100)]]
        def get_symbol_ticker(self, **kwargs):
            return {'price': str(sell_price if orders else 100)}
        def create_order(self, **kwargs):
            orders.append(kwargs)
            price = 100 if kwargs['side']=='BUY' else sell_price
            qty = kwargs['quantity']
            return dict(executedQty=qty,cummulativeQuoteQty=str(Decimal(qty)*Decimal(price)), fills=[dict(price=str(price),commission='0.001',commissionAsset='BTC')])
    class ImmediateThread:
        def __init__(self, target, args): self.target,self.args=target,args
        def start(self): self.target(*self.args)
    def sleep(seconds):
        if len(orders) >= 2: raise EndRun()
    monkeypatch.setattr(bot,'Client',Exchange)
    monkeypatch.setattr(bot.threading,'Thread',ImmediateThread)
    monkeypatch.setattr(bot.time,'sleep',sleep)
    bot.trade_log.clear()
    config = validate_config(dict(symbols=['BTCUSDT'],interval='1m',binance_api_key='fake',binance_api_secret='fake',stop_loss_threshold=stop_loss))
    with pytest.raises(EndRun): bot.run_bot(config,lambda *args: statuses.append(args))
    assert [o['side'] for o in orders] == ['BUY','SELL']
    assert Decimal(orders[1]['quantity']) == Decimal('0.099')
    assert [r['side'] for r in bot.trade_log] == ['BUY','SELL']
    assert bot.trade_log[0]['price'] == 100
    bot.trade_log.clear()
