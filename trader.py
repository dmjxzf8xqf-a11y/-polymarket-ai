import os
import time
import requests
from py_clob_client.client import ClobClient

GAMMA = "https://gamma-api.polymarket.com"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

POLY_HOST = os.getenv("POLY_HOST", "https://clob.polymarket.com").rstrip("/")
POLY_CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
POLY_FUNDER = os.getenv("POLY_FUNDER") or None

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "20"))

_last_sent = {"text": None, "ts": 0}


class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None

    def notify(self, text: str):
        now = time.time()
        if _last_sent["text"] == text and (now - _last_sent["ts"]) < 15:
            return
        _last_sent["text"] = text
        _last_sent["ts"] = now

        if not BOT_TOKEN or not CHAT_ID:
            print(text)
            return

        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )

    def _init_client(self):
        c = ClobClient(
            POLY_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIGNATURE_TYPE,
            funder=POLY_FUNDER,
        )
        c.set_api_creds(c.create_or_derive_api_creds())
        self.client = c
        self.notify("✅ Polymarket CLOB 연결 OK")

    def _fetch_markets(self):
        r = requests.get(f"{GAMMA}/markets?limit=50", timeout=20)
        r.raise_for_status()
        data = r.json()

        # 🔎 구조 디버깅
        if isinstance(data, dict):
            keys = list(data.keys())
            self.notify(f"DEBUG gamma_keys={keys}")
            markets = data.get("data") or data.get("markets") or data.get("results")
        else:
            markets = data

        return markets or []

    def tick(self):
        if self.client is None:
            self._init_client()

        try:
            markets = self._fetch_markets()
        except Exception as e:
            self.notify(f"❌ gamma error: {e}")
            return

        self.notify(f"DEBUG markets_count={len(markets)}")

        if not markets:
            return

        top = markets[0]

        slug = top.get("slug")
        question = top.get("question") or top.get("title")

        self.notify(
            "🧪 DRY_RUN\n"
            f"slug={slug}\n"
            f"{question}"
        )
