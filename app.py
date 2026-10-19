"""Local Ypsilon dashboard. Run a single process with python app.py."""
import math
import re
from threading import Thread, Lock
from flask import Flask, request, jsonify, render_template
from bot import run_bot, trade_log, trade_log_lock

app = Flask(__name__, template_folder=".")
app.config["MAX_CONTENT_LENGTH"] = 16384
worker = None
worker_lock = Lock()
state = {"status": "idle", "message": "Bot başlatılmadı."}

def report(status, message):
    with worker_lock:
        state.update(status=status, message=message)

def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError("JSON nesnesi gerekli.")
    c = dict(data)
    symbols = c.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20:
        raise ValueError("1–20 sembol girin.")
    if any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{2,16}USDT", s) for s in symbols):
        raise ValueError("Sembolleri BTCUSDT biçiminde girin.")
    c["symbols"] = list(dict.fromkeys(symbols))
    if c.get("interval") not in {"1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w","1M"}:
        raise ValueError("Geçersiz mum aralığı.")
    for key, low, high, default, integer in [
        ("limit",2,1000,15,True), ("volume_multiplier",0.01,1000,2,False),
        ("price_increase_threshold",0.001,100,1,False),
        ("usdt_amount",0.01,1000000,10,False), ("max_usdt_limit",0.01,1000000,100,False),
        ("max_trades_limit",1,10000,5,True), ("cooldown_hours",0,8760,1,False),
        ("cooldown_candles",0,100000,4,True), ("stop_loss_threshold",-100,100,0,False)]:
        value = c.get(key, default)
        if isinstance(value, bool): raise ValueError(f"Geçersiz değer: {key}")
        try: value = float(value)
        except (TypeError, ValueError): raise ValueError(f"Geçersiz değer: {key}")
        if not math.isfinite(value) or not low <= value <= high or (integer and not value.is_integer()):
            raise ValueError(f"Geçersiz değer: {key}")
        c[key] = int(value) if integer else value
    if c["usdt_amount"] > c["max_usdt_limit"]:
        raise ValueError("Alım tutarı toplam bütçeyi aşamaz.")
    if not isinstance(c.get("testnet", True), bool): raise ValueError("Geçersiz ortam.")
    c["testnet"] = c.get("testnet", True)
    for key in ("binance_api_key", "binance_api_secret", "telegram_token", "telegram_chat_id"):
        if not isinstance(c.get(key, ""), str): raise ValueError(f"Geçersiz değer: {key}")
        c[key] = c.get(key, "").strip()
    if not c["binance_api_key"] or not c["binance_api_secret"]:
        raise ValueError("Seçilen ortama ait Binance API anahtarları gerekli.")
    if bool(c["telegram_token"]) != bool(c["telegram_chat_id"]):
        raise ValueError("Telegram token ve chat ID birlikte girilmeli.")
    return c

def launch(config):
    try:
        run_bot(config, report)
    except Exception:
        report("error", "Bot durdu. Bağlantı ve hesap ayarlarını kontrol edin.")
    finally:
        with worker_lock:
            if state["status"] != "error":
                state.update(status="idle", message="Bot durdu.")

@app.get("/")
def index():
    return render_template("starter-page.html")

@app.post("/start-bot")
def start_bot():
    global worker
    try:
        config = validate_config(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(message=str(exc)), 400
    with worker_lock:
        if worker is not None and worker.is_alive():
            return jsonify(message="Bot zaten çalışıyor."), 409
        state.update(status="starting", message="Binance bağlantısı kontrol ediliyor.")
        worker = Thread(target=launch, args=(config,), daemon=True)
        worker.start()
    return jsonify(message="Bot başlatılıyor."), 202

@app.get("/status")
def status():
    with worker_lock:
        return jsonify(state)

@app.get("/trade-log")
def get_trade_log():
    with trade_log_lock:
        return jsonify(list(trade_log))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
