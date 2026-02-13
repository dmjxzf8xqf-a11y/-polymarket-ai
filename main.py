import os
import time
import threading
from flask import Flask, jsonify
from trader import Trader

app = Flask(__name__)

state = {
    "running": False,
    "last_heartbeat": None,
    "last_error": None,
}

trader = Trader(state)

@app.get("/")
def home():
    return "Bot Running"

@app.get("/health")
def health():
    return jsonify({**state, **trader.public_state()})

def loop():
    state["running"] = True
    trader.notify("🤖 봇 시작됨 (DRY_RUN 모드: 주문은 안 나감)")
    while True:
        try:
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
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
