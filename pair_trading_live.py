import hmac
import hashlib
import base64
import time
import json
import requests
import pandas as pd
from datetime import datetime, timezone
import schedule
import ssl
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from dotenv import load_dotenv
import os

# ========= PARAMÈTRES =========

# Fichier .env pour les clés API et le webhook Discord à créer dans le même répertoire
load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
API_PASSPHRASE = os.getenv("API_PASSPHRASE")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
BASE_URL = "https://api.bitget.com"
weight = 0.75
leverage = 2

# ========= UTILS =========
def get_timestamp():
    return str(int(time.time() * 1000))

def send_discord_message(content):
    if not DISCORD_WEBHOOK:
        print("No Discord webhook configured.")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content})
        if r.status_code not in (200, 204):
            print(f"Discord webhook error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Exception sending Discord message: {e}")

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_version'] = ssl.PROTOCOL_TLSv1_2
        return super().init_poolmanager(*args, **kwargs)

session = requests.Session()
session.mount('https://', TLSAdapter())

# ========= SIGNATURE =========
def sign_request(timestamp, method, path, body):
    pre_hash = timestamp + method.upper() + path + body
    sign = hmac.new(API_SECRET.encode(), pre_hash.encode(), hashlib.sha256).digest()
    return base64.b64encode(sign).decode()

# ========= PRIX =========
def get_binance_daily_close(symbol, limit=1000):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1d", "limit": limit}
    r = requests.get(url, params=params)

    if r.status_code != 200:
        print(f"Erreur API Binance : {r.status_code} | {r.text}")
        return pd.Series(dtype=float)

    try:
        data = r.json()
    except Exception as e:
        print(f"Erreur parsing JSON : {e}")
        return pd.Series(dtype=float)

    df = pd.DataFrame(data, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('datetime', inplace=True)
    df['close'] = df['close'].astype(float)
    return df['close']

def get_live_price(symbol):
    url = "https://api.binance.com/api/v3/ticker/price"
    r = requests.get(url, params={"symbol": symbol})
    return float(r.json()['price'])

# ========= SIGNAL =========
def get_live_signal(window=6):
    btc_hist = get_binance_daily_close("BTCUSDT")
    avax_hist = get_binance_daily_close("AVAXUSDT")
    ratio_hist = btc_hist / avax_hist
    ema = ratio_hist.ewm(span=window, adjust=False).mean()
    last_ema = ema.iloc[-1]

    btc_live = get_live_price("BTCUSDT")
    avax_live = get_live_price("AVAXUSDT")
    live_ratio = btc_live / avax_live

    if live_ratio > last_ema:
        signal = "Long BTC / Short AVAX"
        btc_weight = weight
        avax_weight = -weight
    else:
        signal = "Short BTC / Long AVAX"
        btc_weight = -weight
        avax_weight = weight

    info = (f"Signal: {signal} | BTC={btc_live:.2f} | AVAX={avax_live:.2f} "
            f"| Ratio={live_ratio:.4f} | EMA{window}d={last_ema:.4f}")
    print(info)
    send_discord_message(info)

    return {
        "signal": signal,
        "btc_price": btc_live,
        "avax_price": avax_live,
        "btc_weight": btc_weight,
        "avax_weight": avax_weight
    }

# ========= BALANCE =========
def get_balance_usdt():
    timestamp = get_timestamp()
    path = "/api/mix/v1/account/account"
    query = "?symbol=BTCUSDT_UMCBL&marginCoin=USDT"
    sign = sign_request(timestamp, "GET", path + query, "")
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-SIGN": sign,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    r = session.get(BASE_URL + path + query, headers=headers)
    try:
        bal = float(r.json()["data"]["available"])
        print(f"Balance USDT: {bal}")
        send_discord_message(f"Balance USDT: {bal}")
        return bal
    except Exception as e:
        err = f"Erreur balance: {e}"
        print(err)
        send_discord_message(err)
        return None

# ========= POSITIONS ACTUELLES =========
def get_current_positions():
    timestamp = get_timestamp()
    path = "/api/mix/v1/position/allPosition"
    query = "?productType=umcbl"
    sign = sign_request(timestamp, "GET", path + query, "")
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-SIGN": sign,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    r = session.get(BASE_URL + path + query, headers=headers)
    try:
        data = r.json()["data"]
        pos = {p["symbol"]: p["holdSide"] for p in data if float(p["total"]) > 0}
        print(f"Positions actuelles: {pos}")
        send_discord_message(f"Positions actuelles: {pos}")
        return pos
    except Exception as e:
        err = f"Erreur positions: {e}"
        print(err)
        send_discord_message(err)
        return {}

# ========= ORDRES =========
def place_order(symbol, marginCoin, size, side, leverage=2):
    price = get_live_price(symbol.replace('_UMCBL',''))
    timestamp = get_timestamp()
    path = "/api/mix/v1/order/placeOrder"
    body = {
        "symbol": symbol,
        "marginCoin": marginCoin,
        "size": str(size),
        "side": side,
        "orderType": "market",
        "leverage": leverage
    }
    body_json = json.dumps(body)
    sign = sign_request(timestamp, "POST", path, body_json)
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-SIGN": sign,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    r = session.post(BASE_URL + path, headers=headers, data=body_json)
    res = r.json()
    if res.get("code") == "00000":
        msg = f"{side} {symbol}: size={size}, price={price}"
    else:
        msg = f"Erreur order {symbol}: {res}"
    print(msg)
    send_discord_message(msg)

def close_position(symbol, side, size, leverage=2):
    close_side = "close_long" if side == "long" else "close_short"
    place_order(symbol, "USDT", size, close_side, leverage)

# ========= STRATÉGIE PRINCIPALE =========
def run_strategy():
    try:
        sig = get_live_signal(window=6)
    except Exception as e:
        msg = f"Erreur lors du signal : {e}. Prochain essai demain."
        print(msg)
        send_discord_message(msg)
        return
    
    pos = get_current_positions()
    capital = get_balance_usdt()

    if capital is None:
        return

    btc_expo = abs(sig['btc_weight']) * capital
    avax_expo = abs(sig['avax_weight']) * capital

    btc_size = round(btc_expo / sig['btc_price'], 4)
    avax_size = round(avax_expo / sig['avax_price'], 2)

    desired = {
        'BTCUSDT_UMCBL': 'long' if sig['btc_weight'] > 0 else 'short',
        'AVAXUSDT_UMCBL': 'long' if sig['avax_weight'] > 0 else 'short'
    }

    if pos.get('BTCUSDT_UMCBL') == desired['BTCUSDT_UMCBL'] and pos.get('AVAXUSDT_UMCBL') == desired['AVAXUSDT_UMCBL']:
        msg = "Signal inchangé. Positions déjà alignées. Aucun ordre à passer."
        print(msg)
        send_discord_message(msg)
        return

    for sym in ['BTCUSDT_UMCBL', 'AVAXUSDT_UMCBL']:
        cur = pos.get(sym)
        if cur:
            print(f"Fermeture {sym} {cur}")
            send_discord_message(f"Fermeture {sym} {cur}")
            size = btc_size if 'BTC' in sym else avax_size
            close_position(sym, cur, size, leverage)

    time.sleep(1)

    send_discord_message(
        f"BTC size: {btc_size} (expo: {btc_expo:.2f} USDT) | "
        f"AVAX size: {avax_size} (expo: {avax_expo:.2f} USDT)"
    )

    print(f"Ouverture sizes BTC={btc_size}, AVAX={avax_size}")
    send_discord_message(f"Ouverture sizes BTC={btc_size}, AVAX={avax_size}")

    place_order(
        'BTCUSDT_UMCBL', 'USDT', btc_size,
        'open_long' if desired['BTCUSDT_UMCBL'] == 'long' else 'open_short',
        leverage
    )
    place_order(
        'AVAXUSDT_UMCBL', 'USDT', avax_size,
        'open_long' if desired['AVAXUSDT_UMCBL'] == 'long' else 'open_short',
        leverage
    )
    

# ========= LANCEMENT =========
run_strategy()
schedule.every().day.at("00:00").do(run_strategy)

while True:
    schedule.run_pending()
    time.sleep(1)

