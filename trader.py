import os
import time
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

# ---- 전략 파라미터(환경변수로 조절 가능) ----
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "20"))           # 평가할 후보 마켓 수(거래량 상위 N개)
PICK_TOPK = int(os.getenv("PICK_TOPK", "3"))                # 최종 후보 TOP K개 요약 알림

MIN_VOL_24H = float(os.getenv("MIN_VOL_24H", "10000"))      # 24h 거래량 최소
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.10"))           # YES 중간가 최소(너무 0에 붙은거 제외)
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.90"))           # YES 중간가 최대(너무 1에 붙은거 제외)
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.08"))         # YES 스프레드(ask-bid) 최대
CENTER_BONUS = float(os.getenv("CENTER_BONUS", "0.5"))      # 0.5 근처 선호 강도(0~1, 클수록 0.5 선호)

NOTIFY_COOLDOWN_SECONDS = int(os.getenv("NOTIFY_COOLDOWN_SECONDS", "120"))  # 알림 최소 간격(스팸 방지)

# ------------------------------------------------------------

class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None
        self.last_pick = []
        self.last_action = None

        self._last_notified_slug = None
        self._last_notified_ts = 0

    def notify(self, text: str):
        # 텔레그램 세팅 안되면 콘솔로만
        if not BOT_TOKEN or not CHAT_ID:
            print(text)
            return

        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text},
                timeout=10
            )
            if r.status_code != 200:
                print("telegram send failed:", r.status_code, r.text)
        except Exception as e:
            print("telegram error:", e)

    def public_state(self):
        return {
            "last_pick": self.last_pick,
            "last_action": self.last_action,
            "dry_run": DRY_RUN,
            "chain_id": POLY_CHAIN_ID,
            "host": POLY_HOST
        }

    def _init_client(self):
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("POLY_PRIVATE_KEY missing")

        c = ClobClient(
            POLY_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIGNATURE_TYPE,
            funder=POLY_FUNDER,
        )

        # L2 creds (주문용) - DRY_RUN이어도 여기서 오류나면 미리 잡히게 유지
        c.set_api_creds(c.create_or_derive_api_creds())
        self.client = c

        self.notify("✅ Polymarket CLOB 연결 OK")

    # 1) Gamma에서 마켓 후보 수집 (거래량 큰 것부터)
    def _pick_candidates_from_gamma(self):
        r = requests.get(f"{GAMMA}/markets", timeout=25)
        r.raise_for_status()
        markets = r.json()

        candidates = []
        for m in markets:
            slug = m.get("slug")
            q = m.get("question") or m.get("title")

            token_ids = (
                m.get("clobTokenIds")
                or m.get("clob_token_ids")
                or m.get("tokenIds")
                or m.get("token_ids")
            )

            if not slug or not q or not isinstance(token_ids, list) or len(token_ids) < 2:
                continue

            vol = m.get("volume24hr") or m.get("volume_24hr") or m.get("volume24h") or m.get("volume") or 0
            try:
                vol = float(vol)
            except:
                vol = 0.0

            candidates.append({
                "slug": slug,
                "question": q,
                "yes": str(token_ids[0]),
                "no": str(token_ids[1]),
                "vol": vol,
            })

        candidates.sort(key=lambda x: x["vol"], reverse=True)
        return candidates[:MAX_MARKETS]

    # 2) CLOB에서 YES 오더북을 보고 bid/ask → mid/spread 계산
    def _get_yes_quote(self, yes_token_id: str):
        # py_clob_client 버전에 따라 메소드명이 다를 수 있어서 2단계로 시도
        bid = None
        ask = None

        # (A) client 메소드 시도
        try:
            # 보통: get_order_book(token_id) 형태
            ob = self.client.get_order_book(yes_token_id)
            # ob 구조가 다양한데, 일반적으로 bids/asks 리스트를 가정
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if bids:
                bid = float(bids[0].get("price"))
            if asks:
                ask = float(asks[0].get("price"))
        except Exception:
            pass

        # (B) REST fallback 시도 (호스트에 따라 경로가 다를 수 있음)
        if bid is None or ask is None:
            try:
                # 흔한 케이스 중 하나: /book?token_id=
                rr = requests.get(f"{POLY_HOST.rstrip('/')}/book", params={"token_id": yes_token_id}, timeout=15)
                rr.raise_for_status()
                ob = rr.json()
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                if bids and bid is None:
                    bid = float(bids[0].get("price"))
                if asks and ask is None:
                    ask = float(asks[0].get("price"))
            except Exception:
                pass

        if bid is None or ask is None:
            return None  # 호가 못 가져옴

        mid = (bid + ask) / 2.0
        spread = max(0.0, ask - bid)
        return {"bid": bid, "ask": ask, "mid": mid, "spread": spread}

    # 3) 필터 + 점수화
    def _rank(self, candidates):
        ranked = []
        for c in candidates:
            if c["vol"] < MIN_VOL_24H:
                continue

            q = self._get_yes_quote(c["yes"])
            if not q:
                continue

            mid = q["mid"]
            spread = q["spread"]

            # 필터
            if not (MIN_PRICE <= mid <= MAX_PRICE):
                continue
            if spread > MAX_SPREAD:
                continue

            # 점수: 거래량(클수록) / (스프레드+작은값) * (0.5 근접 보너스)
            center = 1.0 - min(1.0, abs(mid - 0.5) / 0.5)  # 0~1, 0.5면 1
            center_weight = (1.0 - CENTER_BONUS) + (CENTER_BONUS * center)  # CENTER_BONUS가 클수록 0.5 선호

            score = (c["vol"] / (spread + 1e-6)) * center_weight

            ranked.append({
                **c,
                "bid": q["bid"],
                "ask": q["ask"],
                "mid": mid,
                "spread": spread,
                "score": score,
            })

        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    def _should_notify(self, top_slug: str):
        now = time.time()
        if top_slug != self._last_notified_slug:
            if now - self._last_notified_ts >= NOTIFY_COOLDOWN_SECONDS:
                return True
        # slug가 같아도 너무 오래됐으면 한 번쯤은 보내고 싶으면 여기 조건 추가 가능
        return False

    def tick(self):
        if self.client is None:
            self._init_client()

        # 후보 수집
        candidates = self._pick_candidates_from_gamma()

        # 점수화
        ranked = self._rank(candidates)

        self.last_pick = [{"slug": x["slug"], "vol": x["vol"], "mid": round(x["mid"], 4), "spread": round(x["spread"], 4)} for x in ranked[:PICK_TOPK]]

        if not ranked:
            self.last_action = "no ranked markets (filters too strict)"
            # 너무 조용하면 상태만 가끔 보내고 싶으면 여기서 notify 넣어도 됨
            return

        top = ranked[0]
        self.last_action = f"picked {top['slug']} mid={top['mid']:.3f} spread={top['spread']:.3f} vol={top['vol']:.0f}"

        # DRY_RUN에서는 주문은 안 내고, “바뀔 때만” 알림
        if DRY_RUN:
            if self._should_notify(top["slug"]):
                msg_lines = [
                    "🧪 DRY_RUN: 전략(가격/스프레드 필터) 후보 TOP",
                    f"1) {top['slug']}",
                    f"- Q: {top['question']}",
                    f"- vol24h: {top['vol']:.0f}",
                    f"- YES bid/ask: {top['bid']:.3f}/{top['ask']:.3f}",
                    f"- mid: {top['mid']:.3f}  spread: {top['spread']:.3f}",
                    "",
                    f"(필터) vol>={MIN_VOL_24H} | mid {MIN_PRICE}-{MAX_PRICE} | spread<={MAX_SPREAD}",
                ]
                # TOPK 요약도 같이
                if PICK_TOPK > 1:
                    msg_lines.append("")
                    msg_lines.append("📌 TOP 요약:")
                    for i, x in enumerate(ranked[:PICK_TOPK], start=1):
                        msg_lines.append(
                            f"{i}) {x['slug']} | mid={x['mid']:.3f} spread={x['spread']:.3f} vol={x['vol']:.0f}"
                        )

                self.notify("\n".join(msg_lines))
                self._last_notified_slug = top["slug"]
                self._last_notified_ts = time.time()

            return

        # ✅ 실전 주문 로직은 여기 아래에 붙이면 됨 (지금은 요청이 2번이라 여기까지만)
        # 예: top['yes'] 토큰에 LIMIT 주문 등
        # ---------------------------------------------------
