#bot.py

import requests
import pandas as pd
import time
import threading
from binance.client import Client
from binance.enums import *
from decimal import Decimal, getcontext
from datetime import datetime, timezone

# --- Ön uca gönderilecek işlem günlüğü ---
trade_log = []
trade_log_lock = threading.Lock()


def run_bot(config, on_status=None):
    on_status = on_status or (lambda status, message: None)
    # --- Paylaşılan durum (repeat-buy engeli + aynı mum engeli) ---
    active_positions = {}
    last_trade_candle = {}
    last_buy_time = {}

    # Global Sayaçlar ve Kilit
    total_usdt_spent = 0.0
    total_trades = 0
    state_lock = threading.Lock()

    """
    Ana bot çalıştırma fonksiyonu.
    Yapılandırmayı alır, Binance'e bağlanır ve alım-satım döngüsünü başlatır.
    """
    try:
        # --- 1. Yapılandırma Ayarlarını Yükle ---
        symbols = config["symbols"]
        interval = config["interval"]
        limit = int(config["limit"])
        volume_multiplier = float(config["volume_multiplier"])
        price_increase = float(config["price_increase_threshold"]) / 100.0

        max_usdt_limit = float(config.get("max_usdt_limit", 100.0))
        max_trades_limit = int(config.get("max_trades_limit", 5))

        cooldown_hours = float(config.get("cooldown_hours", 1.0))
        cooldown_candles = int(config.get("cooldown_candles", 4))

        # Zero preserves the original disabled stop-loss default.
        raw_sl = float(config.get("stop_loss_threshold", 0))
        stop_loss_enabled = abs(raw_sl) > 0
        stop_loss = abs(raw_sl) / 100.0

        usdt_amount = float(config["usdt_amount"])
        telegram_token = config["telegram_token"]
        telegram_chat_id = config["telegram_chat_id"]
        api_key = config["binance_api_key"]
        api_secret = config["binance_api_secret"]

        client = Client(api_key, api_secret, testnet=config.get("testnet", True), requests_params={"timeout": 10})

        def send_telegram(msg):
            """Basit Telegram mesaj gönderme fonksiyonu."""
            if not telegram_token or not telegram_chat_id:
                return
            try:
                url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                response = requests.post(url, data={"chat_id": telegram_chat_id, "text": msg}, timeout=10)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                print("Telegram bildirimi gönderilemedi.")
            except Exception as e:
                print(f"BEKLENMEYEN TELEGRAM HATASI: {e}")

        # --- 2. Başlangıç Kontrolleri ---
        try:
            account = client.get_account()
            balances = account["balances"]
            usdt_balance = next((b for b in balances if b["asset"] == "USDT"), None)
            if usdt_balance:
                msg = f"✅ Binance API bağlantısı başarılı.\n💼 USDT Bakiyesi: {float(usdt_balance['free']):.2f} USDT"
            else:
                msg = "✅ Binance API bağlantısı başarılı. Ancak USDT bakiyesi bulunamadı."
            send_telegram(msg)
        except Exception as e:
            error_msg = f"❌ Binance API bağlantı hatası veya bakiye kontrolü başarısız: {str(e)}"
            send_telegram(error_msg)
            print(error_msg)
            on_status("error", "Binance bağlantısı başarısız. API anahtarlarını ve ortamı kontrol edin.")
            return

        def get_klines(symbol):
            """Binance API'sinden mum verilerini çeker ve DataFrame'e dönüştürür."""
            try:
                data = client.get_klines(symbol=symbol, interval=interval, limit=limit)
                df = pd.DataFrame(data, columns=[
                    'open_time', 'open', 'high', 'low', 'close', 'volume',
                    'close_time', 'qav', 'trades', 'tb_base_vol', 'tb_quote_vol', 'ignore'
                ])
                if not df.empty:
                    df['volume'] = pd.to_numeric(df['volume'])
                    df['close'] = pd.to_numeric(df['close'])
                return df
            except requests.exceptions.RequestException as e:
                print(f"Klines Veri Çekme Hatası ({symbol}): {e}")
                return pd.DataFrame()
            except Exception as e:
                print(f"Klines İşleme Hatası ({symbol}): {e}")
                return pd.DataFrame()

        def get_lot_size_info(symbol):
            """Bir sembol için lot boyutu (stepSize) ve minimum miktar (minQty) bilgilerini alır."""
            try:
                info = client.get_symbol_info(symbol)
                if not info:
                    raise ValueError(f"Sembol bilgisi bulunamadı: {symbol}")
                for f in info['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['stepSize']), float(f['minQty'])
                raise ValueError(f"'LOT_SIZE' filtresi bulunamadı: {symbol}")
            except Exception as e:
                send_telegram(f"❌ Lot Size bilgisi alınamadı ({symbol}): {e}")
                print(f"Lot Size Hatası ({symbol}): {e}")
                return None, None

        def calculate_quantity(symbol):
            """USDT miktarına ve sembol kurallarına göre alınacak miktarı hesaplar."""
            try:
                price = float(client.get_symbol_ticker(symbol=symbol)["price"])
                step_size, min_qty = get_lot_size_info(symbol)

                if step_size is None or min_qty is None:
                    return None

                if price * min_qty > usdt_amount:
                    print(f"ℹ️ {symbol}: Belirlenen USDT tutarı ({usdt_amount}), minimum işlem tutarını karşılamıyor. İşlem atlandı.")
                    return None

                getcontext().prec = 12
                raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
                decimal_step_size = Decimal(str(step_size))

                qty_decimal = (raw_qty // decimal_step_size) * decimal_step_size
                qty = float(qty_decimal)

                if qty < min_qty:
                    send_telegram(f"ℹ️ {symbol}: Hesaplanan miktar ({qty:.8f}) minimum alım miktarının ({min_qty}) altında. İşlem atlandı.")
                    return None

                decimals = abs(decimal_step_size.as_tuple().exponent)
                final_qty_str = f"%.{decimals}f" % qty
                return final_qty_str

            except Exception as e:
                send_telegram(f"❌ Miktar hesaplama hatası ({symbol}): {e}")
                print(f"Miktar Hesaplama Hatası ({symbol}): {e}")
                return None

        def execute_trade(symbol, side):
            """Hesaplanan miktarla market emri gönderir."""
            qty = calculate_quantity(symbol)
            if qty is None:
                raise ValueError("Alım/Satım için miktar hesaplanamadı veya yetersiz.")

            print(f"DEBUG: {symbol} - {side} için {qty} miktarıyla emir gönderiliyor.")
            try:
                order = client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY if side == "BUY" else SIDE_SELL,
                    type=ORDER_TYPE_MARKET,
                    quantity=qty
                )
                return order
            except Exception as e:
                send_telegram(f"❌ Emir gönderme hatası ({symbol}, {side}): {str(e)}")
                print(f"Emir Gönderme Hatası ({symbol}, {side}): {e}")
                raise

        def analyze_and_trade(symbol):
            """Piyasa verisini analiz eder, sinyal arar ve işlem yapar."""
            # --- HATA DÜZELTMESİ BURADA YAPILDI ---
            nonlocal total_usdt_spent, total_trades

            df = get_klines(symbol)
            if df.empty or len(df) < 2:
                print(f"WARN: {symbol}: Analiz için yeterli veri bulunamadı ({len(df)} mum).")
                return

            current_candle_time = df.iloc[-1]['open_time']

            with state_lock:
                if active_positions.get(symbol, False):
                    print(f"SKIP: {symbol} zaten açık pozisyonda, yeni alım yapılmayacak.")
                    return
                if last_trade_candle.get(symbol) == current_candle_time:
                    print(f"SKIP: {symbol} aynı mumda zaten işlem yapıldı.")
                    return

                # SAAT FİLTRESİ
                if symbol in last_buy_time:
                    elapsed_time = datetime.now() - last_buy_time[symbol]
                    if elapsed_time.total_seconds() < cooldown_hours * 3600:
                        print(f"SKIP: {symbol} için {cooldown_hours} saatlik bekleme süresi dolmadı. Kalan süre: {cooldown_hours * 3600 - elapsed_time.total_seconds():.0f} saniye.")
                        return

                # MUM FİLTRESİ
                if symbol in last_trade_candle and len(df) > 2:
                    last_trade_open_time_ms = last_trade_candle[symbol]
                    current_open_time_ms = df.iloc[-1]['open_time']
                    candle_duration_ms = current_open_time_ms - df.iloc[-2]['open_time']
                    candles_passed = (current_open_time_ms - last_trade_open_time_ms) / candle_duration_ms
                    if candles_passed < cooldown_candles:
                        print(f"SKIP: {symbol} için {cooldown_candles} mum bekleme süresi dolmadı. Geçen mum sayısı: {candles_passed:.0f}.")
                        return

                # MAKSİMUM İŞLEM VE PARA FİLTRESİ
                if total_trades >= max_trades_limit:
                    print("SKIP: Maksimum işlem limitine ulaşıldı, alım yapılmayacak.")
                    return
                if total_usdt_spent + usdt_amount > max_usdt_limit:
                    print("SKIP: Maksimum harcama limitine ulaşıldı, alım yapılmayacak.")
                    return

            print(f"\n--- {symbol} için analiz başlatılıyor ---")

            # --- Analiz Mantığı ---
            volume_avg = df['volume'].iloc[:-1].mean()
            last_candle = df.iloc[-1]
            previous_candle = df.iloc[-2]

            last_volume = last_candle['volume']
            last_price = last_candle['close']
            previous_price = previous_candle['close']

            volume_condition_met = last_volume > volume_avg * volume_multiplier
            price_condition_met = (last_price - previous_price) / previous_price > price_increase

            print(f"DEBUG: [{symbol}] Son Hacim: {last_volume:.2f}, Ort. Hacim: {volume_avg:.2f} (Koşul: {volume_condition_met})")
            price_change_perc = ((last_price - previous_price) / previous_price) * 100
            print(f"DEBUG: [{symbol}] Son Fiyat: {last_price:.4f}, Önceki Fiyat: {previous_price:.4f}, Değişim: %{price_change_perc:.2f} (Koşul: {price_condition_met})")

            if volume_condition_met and price_condition_met:
                msg = (f"📈 ALIM SİNYALİ: {symbol}\n"
                       f"Hacim Koşulu: Sağlandı ({last_volume:.2f} > {volume_avg:.2f} * {volume_multiplier})\n"
                       f"Fiyat Koşulu: Sağlandı (%{price_change_perc:.2f} > %{price_increase*100:.2f})")
                send_telegram(msg)
                print(f"INFO: {symbol} - Alım sinyali oluştu.")

                try:
                    buy_order = execute_trade(symbol, "BUY")
                    if buy_order and 'fills' in buy_order and buy_order['fills']:
                        entry_price = float(buy_order['cummulativeQuoteQty']) / float(buy_order['executedQty'])
                        qty_bought = buy_order['executedQty']
                        send_telegram(f"🟢 ALIM BAŞARILI: {symbol}\nMiktar: {qty_bought}\nOrtalama Fiyat: {entry_price:.4f}")
                        print(f"INFO: {symbol} - Alım başarılı. Giriş fiyatı: {entry_price}")

                        with trade_log_lock:
                            trade_log.append({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "symbol": symbol,
                                "side": "BUY",
                                "qty": float(qty_bought),
                                "price": float(entry_price),
                                "amount_usdt": round(float(qty_bought) * float(entry_price), 2)
                            })


                        with state_lock:
                            active_positions[symbol] = True
                            last_trade_candle[symbol] = current_candle_time
                            last_buy_time[symbol] = datetime.now()
                            total_usdt_spent += float(qty_bought) * entry_price
                            total_trades += 1

                        base_asset = client.get_symbol_info(symbol)['baseAsset']
                        fee = sum(Decimal(f['commission']) for f in buy_order['fills'] if f['commissionAsset'] == base_asset)
                        step, _ = get_lot_size_info(symbol)
                        sell_qty = (Decimal(qty_bought) - fee) // Decimal(str(step)) * Decimal(str(step))
                        t = threading.Thread(target=monitor_position, args=(symbol, entry_price, format(sell_qty, 'f')))
                        t.daemon = True
                        t.start()

                    else:
                        with state_lock:
                            active_positions[symbol] = True
                        on_status("error", f"{symbol}: Emir yanıtı eksik; hesabı manuel kontrol edin.")
                        send_telegram(f"❌ ALIM EMRİ BAŞARISIZ: {symbol}\nBinance'ten geçerli bir sipariş yanıtı alınamadı.")
                        print(f"ERROR: {symbol} - Alım emri yanıtı eksik veya hatalı.")

                except Exception as e:
                    # A timeout may hide a filled order: never automatically buy again.
                    with state_lock:
                        active_positions[symbol] = True
                    on_status("error", f"{symbol}: Emir durumu belirsiz; hesabı manuel kontrol edin.")
                    print(f"ERROR: {symbol} - Alım işlemi sırasında hata: {e}")
            else:
                print(f"--- {symbol} için alım koşulları sağlanmadı ---")

        def monitor_position(symbol, entry_price, original_qty):
            """Alım sonrası pozisyonu kâr hedefi ve (varsa) stop-loss için izler."""
            print(f"INFO: {symbol} - {entry_price} fiyatından alım sonrası izleme başladı.")
            monitor_iterations = 0

            while True:
                time.sleep(10)
                monitor_iterations += 1

                try:
                    current_price = float(client.get_symbol_ticker(symbol=symbol)['price'])
                except Exception as e:
                    print(f"WARN: [{symbol}] İzleme sırasında fiyat alınamadı: {e}")
                    continue

                change = (current_price - entry_price) / entry_price
                print(f"DEBUG: [{symbol}] İzleme - Güncel Fiyat: {current_price:.4f}, Değişim: %{change*100:.2f}")

                # 1) Kâr al koşulu
                if change >= price_increase:
                    send_telegram(f"✅ KÂR HEDEFİNE ULAŞILDI: {symbol}\nKâr Oranı: %{change*100:.2f}\nSatış yapılıyor...")
                    try:
                        sell_order = client.create_order(
                            symbol=symbol,
                            side=SIDE_SELL,
                            type=ORDER_TYPE_MARKET,
                            quantity=original_qty
                        )
                        sell_price = float(sell_order['cummulativeQuoteQty']) / float(sell_order['executedQty'])
                        send_telegram(f"✅ KÂR SATIŞI BAŞARILI: {symbol}\nMiktar: {sell_order['executedQty']}\nOrtalama Satış Fiyatı: {sell_price:.4f}")
                        print(f"INFO: {symbol} - Kar satışı başarılı.")

                        with trade_log_lock:
                            trade_log.append({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "symbol": symbol,
                                "side": "SELL",
                                "qty": float(sell_order['executedQty']),
                                "price": float(sell_price),
                                "amount_usdt": round(float(sell_order['executedQty']) * float(sell_price), 2)
                            })

                        with state_lock:
                            active_positions[symbol] = False
                    except Exception as sell_err:
                        on_status("error", f"{symbol}: Satış doğrulanamadı; hesabı manuel kontrol edin.")
                        send_telegram(f"❌ KÂR SATIŞI HATASI ({symbol}): {sell_err}\nLÜTFEN MANUEL KONTROL EDİN!")
                        print(f"ERROR: {symbol} - Kar satışı sırasında hata: {sell_err}")
                    finally:
                        break

                # 2) Stop-loss koşulu
                if stop_loss_enabled and change <= -stop_loss:
                    send_telegram(f"🛑 STOP-LOSS TETİKLENDİ: {symbol}\nZarar Oranı: %{abs(change)*100:.2f}\nSatış yapılıyor...")
                    try:
                        sell_order = client.create_order(
                            symbol=symbol,
                            side=SIDE_SELL,
                            type=ORDER_TYPE_MARKET,
                            quantity=original_qty
                        )
                        sell_price = float(sell_order['cummulativeQuoteQty']) / float(sell_order['executedQty'])
                        send_telegram(f"🛑 STOP-LOSS SATIŞI BAŞARILI: {symbol}\nMiktar: {sell_order['executedQty']}\nOrtalama Satış Fiyatı: {sell_price:.4f}")
                        print(f"INFO: {symbol} - Stop-loss satışı başarılı.")

                        with trade_log_lock:
                            trade_log.append({
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "symbol": symbol,
                                "side": "SELL",
                                "qty": float(sell_order['executedQty']),
                                "price": float(sell_price),
                                "amount_usdt": round(float(sell_order['executedQty']) * float(sell_price), 2)
                            })

                        with state_lock:
                            active_positions[symbol] = False
                    except Exception as sell_err:
                        on_status("error", f"{symbol}: Satış doğrulanamadı; hesabı manuel kontrol edin.")
                        send_telegram(f"❌ STOP-LOSS SATIŞ HATASI ({symbol}): {sell_err}\nLÜTFEN MANUEL KONTROL EDİN!")
                        print(f"ERROR: {symbol} - Stop-loss satışı sırasında hata: {sell_err}")
                    finally:
                        break

                if monitor_iterations > 60480: # yaklaşık 7 gün
                    on_status("error", f"{symbol}: İzleme süresi doldu; açık pozisyonu manuel kontrol edin.")
                    send_telegram(f"ℹ️ {symbol}: İzleme zaman aşımına uğradı. Hedefler oluşmadı. İzleme durduruldu. Lütfen manuel kontrol edin.")
                    print(f"INFO: {symbol}: İzleme zaman aşımına uğradı.")
                    break

        # --- Ana Döngü ---
        sl_text = f" | SL: {'Açık ' + str(abs(raw_sl)) + '%' if stop_loss_enabled else 'Kapalı'}"
        send_telegram(f"🤖 Bot Başladı! Mod: Çoklu Coin{sl_text}")
        print(f"Bot Başladı! Mod: Çoklu Coin{sl_text}")

        on_status("running", "Bot çalışıyor. " + ("Testnet" if config.get("testnet", True) else "Canlı hesap"))
        while True:
            print("\n" + "="*50)
            print(f"Döngü başlangıcı: {time.ctime()}")
            print("="*50)
            for sym in symbols:
                try:
                    analyze_and_trade(sym)
                except Exception as e:
                    error_msg = f"⚠ Ana döngüde beklenmeyen hata ({sym}): {str(e)}"
                    send_telegram(error_msg)
                    print(f"ERROR: {error_msg}")

            wait_time = 10
            print(f"\nINFO: Tüm semboller kontrol edildi, {wait_time} saniye bekleniyor...")
            time.sleep(wait_time)

    except Exception as e:
        critical_error_msg = f"🔥 KRİTİK GENEL HATA: Bot durduruldu! Hata: {str(e)}"
        try:
            send_telegram(critical_error_msg)
        except:
            pass
        on_status("error", "Bot durdu. Bağlantı ve hesap ayarlarını kontrol edin.")
        print(f"[KRİTİK HATA]: {e}")
