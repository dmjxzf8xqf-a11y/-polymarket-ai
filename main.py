import os
import time
import threading
from flask import Flask, jsonify
from trader import Trader

app = Flask(__name__)

state = {
    "running": False,
    "halted": False,
    "day_start_equity": None,
    "day_pnl": 0.0,
    "trades_today": 0,
    "last_heartbeat": None,
    "last_event": None,
    "last_error": None,
}

trader = Trader(state)

@app.route("/")
def home():
    return "Bot Running"

@app.route("/health")
def health():
    return jsonify({**state, **trader.public_state()})

def loop():
    state["running"] = True
    trader.notify("🤖 봇 시작됨 (DRY_RUN 모드면 주문은 안 나감)")
    loop_n = 0

    while True:
        try:
            loop_n += 1
            state["last_heartbeat"] = time.strftime("%Y-%m-%d %H:%M:%S")
            trader.tick()
            state["last_error"] = None
        except Exception as e:
            state["last_error"] = str(e)
            trader.notify(f"❌ 루프 에러: {e}")

        time.sleep(int(os.getenv("LOOP_SECONDS", "20")))

if __name__ == "__main__":
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
