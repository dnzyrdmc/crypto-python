# app.py
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from threading import Thread
from bot import run_bot, trade_log, trade_log_lock 

app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    return render_template("starter-page.html")

@app.route("/start-bot", methods=["POST"])
def start_bot():
    config = request.get_json()
    print("[CONFIG ALINDI]", config)
    thread = Thread(target=run_bot, args=(config,))
    thread.daemon = True 
    thread.start()
    return jsonify({"message": "✅ Bot arka planda başlatıldı."})
    

@app.route("/trade-log")
def get_trade_log():
    with trade_log_lock: 
        log_copy = list(trade_log)
    return jsonify(log_copy)

if __name__ == "__main__":
    app.run(debug=True)