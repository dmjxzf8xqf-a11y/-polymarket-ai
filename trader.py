import os
import requests
from py_clob_client.client import ClobClient

GAMMA = "https://gamma-api.polymarket.com"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

POLY_HOST = os.getenv("POLY_HOST", "https://clob.polymarket.com")
POLY_CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
POLY_FUNDER = os.getenv("POLY_FUNDER") or None

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "3"))
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "20"))


class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None
        self.last_pick = []
        self.last_action = None

    # -----------------------------
    # 텔레그램 알림
    # -----------------------------
    def notify(self, text: str):
        if not BOT_TOKEN or not CHAT_ID:
            print(text)
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=10
            )
        except Exception as e:
            print("telegram error:", e)

    def public_state(self):
        return {
            "last_pick": self.last_pick,
            "last_action": self.last_action,
            "dry_run": DRY_RUN
        }

    # -----------------------------
    # CLOB 연결
    # -----------------------------
    def _init_client(self):
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("POLY_PRIVATE_KEY missing")

        # 🔍 디버그 정보 출력
        self.notify(
            f"DEBUG host={POLY_HOST} "
            f"chain={POLY_CHAIN_ID} "
            f"sig={POLY_SIGNATURE_TYPE} "
            f"key_len={len(POLY_PRIVATE_KEY)} "
            f"key_0x={POLY_PRIVATE_KEY.startswith('0x')} "
            f"funder_len={len(POLY_FUNDER) if POLY_FUNDER else 0} "
            f"funder_0x={POLY_FUNDER.startswith('0x') if POLY_FUNDER else False}"
        )

        c = ClobClient(
            POLY_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIGNATURE_TYPE,
            funder=POLY_FUNDER,
        )

        # L2 creds 생성
        c.set_api_creds(c.create_or_derive_api_creds())

        self.client = c
        self.notify("✅ Polymarket CLOB 연결 OK")

    # -----------------------------
    # 시장 선택 (Gamma API 안정 파싱)
    # -----------------------------
    def _pick_markets(self):
        r = requests.get(
            f"{GAMMA}/markets?active=true&limit=200",
            timeout=25
        )
        r.raise_for_status()

        data = r.json()

        # 다양한 응답 구조 대응
        if isinstance(data, dict):
            markets = (
                data.get("markets")
                or data.get("data")
                or data.get("results")
                or []
            )
        elif isinstance(data, list):
            markets = data
        else:
            markets = []

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
            except Exception:
                vol = 0.0

            picks.append({
                "slug": slug,
                "question": q,
                "yes": str(token_ids[0]),
                "no": str(token_ids[1]),
                "vol": vol,
            })

        picks.sort(key=lambda x: x["vol"], reverse=True)
        return picks[:MAX_MARKETS]

    # -----------------------------
    # 루프 실행
    # -----------------------------
    def tick(self):
        if self.client is None:
            self._init_client()

        picks = self._pick_markets()

        self.notify(
            f"📡 heartbeat OK | chain={POLY_CHAIN_ID} "
            f"| dry_run={DRY_RUN} | loop={LOOP_SECONDS}"
        )

        if not picks:
            self.notify("DEBUG picks=0 top_slug=none")
            self.last_action = "no picks"
            return

        self.notify(
            f"DEBUG picks={len(picks)} top_slug={picks[0]['slug']}"
        )

        self.last_pick = [
            {"slug": p["slug"], "vol": p["vol"]}
            for p in picks
        ]

        target = picks[0]
        self.last_action = f"picked {target['slug']}"

        if DRY_RUN:
            self.notify(
                "🧪 DRY_RUN: 거래 후보 선정됨 (주문은 안 나감)\n"
                f"{target['slug']}\n"
                f"{target['question']}"
            )
            return
