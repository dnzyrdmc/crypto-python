# Binance Trading Bot

This bot trades on Binance Spot using volume and price momentum.
It opens market BUY orders when volume and price increase conditions are met,
then monitors positions in separate threads and closes them with market SELL
orders on profit target or stop-loss.

## Features
- Volume and price momentum entry
- Market buy/sell orders
- Take profit and optional stop-loss
- Max active trades and max USDT limit
- Candle and time-based cooldowns
- One-shot or continuous mode
- Telegram notifications
- Trade logging

## Requirements
- Python 3.9+
- Binance API key & secret
- Telegram bot token & chat ID

## Usage
```python
from bot import run_bot
run_bot(config)

Related articles:
- Binance API’si ile Otomatik Ticaret Botu Oluşturmaya Giriş - https://medium.com/@denizyardimci/binance-apisi-ile-otomatik-    ticaret-botu-olu%C5%9Fturmaya-giri%C5%9F-6d9970bed9bb

- Binance API ile Otomatik Ticaret Botu Oluşturmaya Giriş (Bölüm 2) - https://medium.com/@denizyardimci/binance-api-ile-otomatik-ticaret-botu-olu%C5%9Fturmaya-giri%C5%9F-b%C3%B6l%C3%BCm-2-c8e710e9c7c7

Other Articles: 
- Deniz Yardımcı - https://medium.com/@denizyardimci
