import websocket, json, time, sys

URL = "wss://ws-testnet.gate.com/v4/ws/futures/usdt"
SYMBOL = "BTC_USDT"
results = {"connected": False, "subscribed": False, "updates": 0, "errors": [], "first_update": None, "raw": []}

def on_open(ws):
    results["connected"] = True
    msg = {"time": int(time.time()), "channel": "futures.kline", "event": "subscribe", "payload": ["1m", SYMBOL]}
    ws.send(json.dumps(msg))
    results["subscribed"] = True

def on_message(ws, message):
    results["updates"] += 1
    if results["first_update"] is None:
        results["first_update"] = time.time()
    try:
        obj = json.loads(message)
        if isinstance(obj, dict) and "error" in obj:
            results["errors"].append(str(obj)[:300])
        if results["updates"] <= 5:
            results["raw"].append(message[:300])
    except Exception:
        results["raw"].append(message[:300])

def on_error(ws, error):
    results["errors"].append(repr(error)[:300])

def on_close(ws, code, msg):
    results["close"] = (code, str(msg)[:100])

ws = websocket.WebSocketApp(URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
t0 = time.time()
def run():
    ws.run_forever(ping_interval=10, ping_timeout=5, reconnect=2)
import threading
th = threading.Thread(target=run, daemon=True)
th.start()
time.sleep(15)
ws.close()
time.sleep(1)
print("connected:", results["connected"], "subscribed:", results["subscribed"], "updates:", results["updates"])
print("first_update_after_connect_s:", round(results["first_update"] - t0, 2) if results["first_update"] else None)
print("errors:", results["errors"][:5])
print("raw sample:")
for r in results["raw"][:5]:
    print("   ", r)