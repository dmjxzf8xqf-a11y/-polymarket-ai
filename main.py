import requests
import time
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }
    requests.post(url, data=payload)

def check_market():
    url = "https://gamma-api.polymarket.com/markets"
    response = requests.get(url)
    data = response.json()

    for market in data[:20]:
        if "outcomes" in market and market["outcomes"]:
            try:
                yes_price = float(market["outcomes"][0]["price"])
                if yes_price > 0.80:
                    send_telegram(f"⚠ 과열 감지: {market['question']} \nYES 확률: {yes_price*100:.1f}%")
            except:
                pass

from flask import Flask
import threading
import os

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_bot():
    while True:
        check_market()
        time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
