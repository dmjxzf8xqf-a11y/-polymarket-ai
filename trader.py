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
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "3"))
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "20"))

# 스팸 방지용(같은 메시지 반복 전송 막기)
_last_sent = {"text": None, "ts": 0}


class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None
        self.last_pick = []
        self.last_action = None

    def notify(self, text: str):
        # 같은 메시지가 너무 자주 반복되면 스킵
        now = time.time()
        if _last_sent["text"] == text and (now - _last_sent["ts"]) < 30:
            return
        _last_sent["text"] = text
        _last_sent["ts"] = now

        if not BOT_TOKEN or not CHAT_ID:
            print(text)
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=10,
            )
        except Exception as e:
            print("telegram error:", e)

    def public_state(self):
        return {
            "last_pick": self.last_pick,
            "last_action": self.last_action,
            "dry_run": DRY_RUN,
            "loop_seconds": LOOP_SECONDS,
        }

    def _debug(self):
        k = POLY_PRIVATE_KEY or ""
        f = POLY_FUNDER or ""
        self.notify(
            f"DEBUG host={POLY_HOST} | chain={POLY_CHAIN_ID} | sig={POLY_SIGNATURE_TYPE} | "
            f"key_len={len(k)} key_0x={k.startswith('0x')} | funder_len={len(f)} funder_0x={f.startswith('0x')}"
        )

    def _init_client(self):
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("POLY_PRIVATE_KEY missing")

        self._debug()

        c = ClobClient(
            POLY_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIGNATURE_TYPE,
            funder=POLY_FUNDER,
        )

        # L2 creds 세팅
        c.set_api_creds(c.create_or_derive_api_creds())
        self.client = c
        self.notify("✅ Polymarket CLOB 연결 OK")

    def _fetch_markets(self):
        # Gamma 응답이 list일 때도, dict(data/results/markets)일 때도 처리
        r = requests.get(f"{GAMMA}/markets", timeout=25)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return (
                data.get("markets")
                or data.get("data")
                or data.get("results")
                or []
            )
        return []

    def _pick_markets(self):
        markets = self._fetch_markets()

        picks = []
        for m in markets:
            slug = m.get("slug")
            q = m.get("question") or m.get("title") or m.get("name")

            token_ids = (
                m.get("clobTokenIds")
                or m.get("clob_token_ids")
                or m.get("tokenIds")
                or m.get("token_ids")
                or []
            )

            active = m.get("active", True)
            if not active:
                continue

            if not slug or not q or not isinstance(token_ids, list) or len(token_ids) < 2:
                continue

            vol = (
                m.get("volume24hr")
                or m.get("volume_24hr")
                or m.get("volume24h")
                or m.get("volume")
                or 0
            )
            try:
                vol = float(vol)
            except:
                vol = 0.0

            picks.append(
                {
                    "slug": slug,
                    "question": q,
                    "yes": str(token_ids[0]),
                    "no": str(token_ids[1]),
                    "vol": vol,
                }
            )

        picks.sort(key=lambda x: x["vol"], reverse=True)
        return picks[:MAX_MARKETS]

    def tick(self):
        if self.client is None:
            self._init_client()

        try:
            picks = self._pick_markets()
        except Exception as e:
            self.last_action = "market fetch failed"
            self.notify(f"❌ market fetch error: {e}")
            return

        self.last_pick = [{"slug": p["slug"], "vol": p["vol"]} for p in picks]

        if not picks:
            self.last_action = "no picks"
            self.notify("DEBUG picks=0 top_slug=none")
            return

        target = picks[0]
        self.last_action = f"picked {target['slug']}"
        self.notify(f"DEBUG picks={len(picks)} top_slug={target['slug']}")

        # DRY_RUN이면 주문 안 냄 (알림만)
        if DRY_RUN:
            self.notify(
                "🧪 DRY_RUN: 거래 후보 선정됨(주문은 안 나감)\n"
                f"{target['slug']}\n{target['question']}\n"
                f"vol={target['vol']}"
            )
            return
