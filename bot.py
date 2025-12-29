# bot.py

import requests
import pandas as pd
import time
import threading
from binance.client import Client
from binance.enums import *
from decimal import Decimal, getcontext
from datetime import datetime, timezone


trade_log = []
trade_log_lock = threading.Lock()


def run_bot(config):
    active_positions = {}
    last_trade_candle = {}
    last_sell_time = {}

    total_usdt_spent = 0.0
    total_trades = 0
    state_lock = threading.Lock()

    try:
        symbols = config["symbols"]
        interval = config["interval"]
        limit = int(config["limit"])
        volume_multiplier = float(config["volume_multiplier"])
        price_increase = float(config["price_increase_threshold"]) / 100.0

        max_usdt_limit = float(config.get("max_usdt_limit", 100.0))
        max_trades_limit = int(config.get("max_trades_limit", 5))

        one_shot_mode = config.get("one_shot_mode", False)

        cooldown_hours = float(config.get("cooldown_hours", 1.0))
        cooldown_candles = int(config.get("cooldown_candles", 4))

        raw_sl = float(config.get("stop_loss_threshold", 0.0))
        stop_loss_enabled = abs(raw_sl) > 0.0
        stop_loss = abs(raw_sl) / 100.0

        usdt_amount = float(config["usdt_amount"])
        telegram_token = config["telegram_token"]
        telegram_chat_id = config["telegram_chat_id"]
        api_key = config["binance_api_key"]
        api_secret = config["binance_api_secret"]

        client = Client(api_key, api_secret)

        def send_telegram(msg):
            try:
                url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                response = requests.post(
                    url,
                    data={"chat_id": telegram_chat_id, "text": msg},
                    timeout=10
                )
                response.raise_for_status()
            except Exception as e:
                print(f"Telegram hata: {e}")

        try:
            account = client.get_account()
            balances = account["balances"]
            usdt_balance = next((b for b in balances if b["asset"] == "USDT"), None)
            if usdt_balance:
                msg = f"Binance API baglandi. USDT bakiye: {float(usdt_balance['free']):.2f}"
            else:
                msg = "Binance API baglandi fakat USDT bakiyesi bulunamadi."
            send_telegram(msg)
        except Exception as e:
            send_telegram(f"Binance API hata: {e}")
            return

        def get_klines(symbol):
            try:
                url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
                data = requests.get(url, timeout=10).json()
                df = pd.DataFrame(data, columns=[
                    'open_time', 'open', 'high', 'low', 'close', 'volume',
                    'close_time', 'qav', 'trades', 'tb_base_vol',
                    'tb_quote_vol', 'ignore'
                ])
                if not df.empty:
                    df['volume'] = pd.to_numeric(df['volume'])
                    df['close'] = pd.to_numeric(df['close'])
                return df
            except Exception:
                return pd.DataFrame()

        def get_lot_size_info(symbol):
            try:
                info = client.get_symbol_info(symbol)
                for f in info['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['stepSize']), float(f['minQty'])
                return None, None
            except Exception:
                return None, None

        def calculate_quantity(symbol):
            try:
                price = float(client.get_symbol_ticker(symbol=symbol)["price"])
                step_size, min_qty = get_lot_size_info(symbol)
                if step_size is None:
                    return None

                if price * min_qty > usdt_amount:
                    return None

                getcontext().prec = 12
                raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
                decimal_step = Decimal(str(step_size))
                qty = (raw_qty // decimal_step) * decimal_step

                if float(qty) < min_qty:
                    return None

                decimals = abs(decimal_step.as_tuple().exponent)
                return f"%.{decimals}f" % float(qty)
            except Exception:
                return None

        def execute_trade(symbol, side):
            qty = calculate_quantity(symbol)
            if qty is None:
                raise ValueError("Miktar hesaplanamadi")

            return client.create_order(
                symbol=symbol,
                side=SIDE_BUY if side == "BUY" else SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=qty
            )

        def analyze_and_trade(symbol):
            nonlocal total_usdt_spent, total_trades

            df = get_klines(symbol)
            if df.empty or len(df) < 2:
                return

            current_candle_time = df.iloc[-1]['open_time']

            with state_lock:
                if active_positions.get(symbol, False):
                    return

                if last_trade_candle.get(symbol) == current_candle_time:
                    return

                if symbol in last_sell_time:
                    elapsed = datetime.now() - last_sell_time[symbol]
                    if elapsed.total_seconds() < cooldown_hours * 3600:
                        return

                if symbol in last_trade_candle and len(df) > 2:
                    candle_ms = df.iloc[-1]['open_time'] - df.iloc[-2]['open_time']
                    passed = (df.iloc[-1]['open_time'] - last_trade_candle[symbol]) / candle_ms
                    if passed < cooldown_candles:
                        return

                active_count = sum(1 for v in active_positions.values() if v)

                if one_shot_mode:
                    if total_trades >= max_trades_limit:
                        return
                else:
                    if active_count >= max_trades_limit:
                        return

                if total_usdt_spent + usdt_amount > max_usdt_limit:
                    return

            volume_avg = df['volume'].iloc[:-1].mean()
            last = df.iloc[-1]
            prev = df.iloc[-2]

            if last['volume'] > volume_avg * volume_multiplier:
                price_change = (last['close'] - prev['close']) / prev['close']
                if price_change > price_increase:
                    try:
                        buy = execute_trade(symbol, "BUY")
                        price = float(buy['fills'][0]['price'])
                        qty = buy['executedQty']

                        with trade_log_lock:
                            trade_log.append({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "symbol": symbol,
                                "side": "BUY",
                                "qty": float(qty),
                                "price": price,
                                "amount_usdt": float(qty) * price
                            })

                        with state_lock:
                            active_positions[symbol] = True
                            last_trade_candle[symbol] = current_candle_time
                            total_usdt_spent += float(qty) * price
                            total_trades += 1

                        t = threading.Thread(
                            target=monitor_position,
                            args=(symbol, price, qty),
                            daemon=True
                        )
                        t.start()
                    except Exception:
                        pass

        def monitor_position(symbol, entry_price, qty):
            while True:
                time.sleep(10)
                try:
                    current_price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                except Exception:
                    continue

                change = (current_price - entry_price) / entry_price

                if change >= price_increase or (stop_loss_enabled and change <= -stop_loss):
                    try:
                        sell = client.create_order(
                            symbol=symbol,
                            side=SIDE_SELL,
                            type=ORDER_TYPE_MARKET,
                            quantity=qty
                        )
                        price = float(sell['fills'][0]['price'])

                        with trade_log_lock:
                            trade_log.append({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "symbol": symbol,
                                "side": "SELL",
                                "qty": float(sell['executedQty']),
                                "price": price,
                                "amount_usdt": float(sell['executedQty']) * price
                            })

                        with state_lock:
                            active_positions[symbol] = False
                            last_sell_time[symbol] = datetime.now()
                    except Exception:
                        pass
                    break

        send_telegram("Bot basladi")

        while True:
            for sym in symbols:
                analyze_and_trade(sym)
            time.sleep(10)

    except Exception as e:
        try:
            send_telegram(f"Bot durdu: {e}")
        except Exception:
            pass
