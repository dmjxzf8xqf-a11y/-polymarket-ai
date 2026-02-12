import os
import requests
from flask import Flask

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_test():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": "🔥 서버 정상 작동 테스트 메시지"
    }
    requests.post(url, data=data)

@app.route("/")
def home():
    send_test()
    return "Bot Running"
