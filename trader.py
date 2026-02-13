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

# ✅ DRY_RUN=1이면 절대 주문 안 나감 (0으로 바꾸면 실매매 모드가 될 수 있음)
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

# 거래 횟수 (요청대로 기본 100)
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "100"))

# 하루 손절 -10% (실매매 구현 전 “가드” 용. 현재는 체크만/표시만)
DAILY_STOP_LOSS_PCT = float(os.getenv("DAILY_STOP_LOSS_PCT", "-0.10"))

# 후보 시장 몇 개 볼지
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "50"))
TOP_N = int(os.getenv("TOP_N", "3"))  # 2~3만 원하면 여기만 바꾸면 됨

# 금칙어(쉼표로 구분). 예: "biden,coronavirus,election"
BANNED_KEYWORDS = [x.strip().lower() for x in os.getenv("BANNED_KEYWORDS", "biden,coronavirus,election").split(",") if x.strip()]

# YES/NO 결정 기준 기본값
MIN_24H_VOL = float(os.getenv("MIN_24H_VOL", "1000"))    # 24h 볼륨 최소
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.08"))      # 스프레드 상한(0~1)
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))          # 기대우위(간단 휴리스틱)

def _to_float(x, default=0.0):
    try:
        return float(x)
    except:
        return default

def _contains_banned(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in BANNED_KEYWORDS)

class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None

        self.last_pick = []
        self.last_action = None

        self.day = time.strftime("%Y-%m-%d")
        self.trades_today = 0
        self.pnl_today = 0.0  # 현재는 표시용 (실매매/체결 연동 전)

        self._last_notify_ts = 0

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
            "dry_run": DRY_RUN,
            "day": self.day,
            "trades_today": f"{self.trades_today}/{MAX_TRADES_PER_DAY}",
            "pnl_today": round(self.pnl_today, 6),
            "last_pick": self.last_pick,
            "last_action": self.last_action,
        }

    def _init_client(self):
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("POLY_PRIVATE_KEY missing")

        # DEBUG (키 내용 노출 X)
        self.notify(
            f"DEBUG host={POLY_HOST} | chain={POLY_CHAIN_ID} | sig={POLY_SIGNATURE_TYPE} | "
            f"key_len={len(POLY_PRIVATE_KEY)} | key_0x={POLY_PRIVATE_KEY.startswith('0x')} | "
            f"funder_len={len(POLY_FUNDER) if POLY_FUNDER else 0} | funder_0x={(POLY_FUNDER or '').startswith('0x')}"
        )

        c = ClobClient(
            POLY_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIGNATURE_TYPE,
            funder=POLY_FUNDER,
        )

        # ✅ L2 creds 세팅
        c.set_api_creds(c.create_or_derive_api_creds())
        self.client = c
        self.notify("✅ Polymarket CLOB 연결 OK")

    def _reset_day_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.trades_today = 0
            self.pnl_today = 0.0

    def _get_markets(self):
        r = requests.get(f"{GAMMA}/markets", timeout=25)
        r.raise_for_status()
        markets = r.json()
        return markets[:MAX_MARKETS]

    def _score_market(self, m: dict):
        """
        YES/NO 결정 기준(간단 휴리스틱):
        - 24h 거래량 MIN_24H_VOL 이상
        - (가능하면) YES/NO 가격이 존재할 때 스프레드가 MAX_SPREAD 이하
        - YES/NO 중 “더 싸게 살 수 있는 쪽”을 후보로 잡되,
          너무 극단(0.02 이하, 0.98 이상)은 제외(유동성/체결 문제)
        """
        q = m.get("question") or m.get("title") or ""
        if _contains_banned(q):
            return None

        vol = _to_float(m.get("volume24hr") or m.get("volume_24hr") or m.get("volume24h") or m.get("volume") or 0)
        if vol < MIN_24H_VOL:
            return None

        token_ids = m.get("clobTokenIds") or m.get("clob_token_ids") or m.get("tokenIds") or m.get("token_ids")
        if not isinstance(token_ids, list) or len(token_ids) < 2:
            return None

        # Gamma에 가격 배열이 있는 경우 활용
        # 보통 outcomePrices=["0.43","0.57"] 같은 형태가 들어오기도 함
        prices = m.get("outcomePrices") or m.get("outcome_prices")
        yes_p = _to_float(prices[0], 0.0) if isinstance(prices, list) and len(prices) >= 2 else None
        no_p  = _to_float(prices[1], 0.0) if isinstance(prices, list) and len(prices) >= 2 else None

        # 스프레드 추정(대충): |(yes+no)-1|
        spread_est = None
        if yes_p is not None and no_p is not None and yes_p > 0 and no_p > 0:
            spread_est = abs((yes_p + no_p) - 1.0)

        if spread_est is not None and spread_est > MAX_SPREAD:
            return None

        # 너무 극단 가격 제외
        if yes_p is not None and (yes_p < 0.02 or yes_p > 0.98):
            yes_p = None
        if no_p is not None and (no_p < 0.02 or no_p > 0.98):
            no_p = None

        # 방향 선택 (더 싸게 살 수 있는 쪽을 우선 후보로)
        side = None
        price = None
        if yes_p is not None and no_p is not None:
            if yes_p <= no_p:
                side, price = "YES", yes_p
            else:
                side, price = "NO", no_p
        elif yes_p is not None:
            side, price = "YES", yes_p
        elif no_p is not None:
            side, price = "NO", no_p
        else:
            # 가격 정보가 아예 없으면 후보 제외
            return None

        # 간단 edge (값이 낮을수록 “상승 여지” 있다고 보는 매우 러프한 기준)
        edge = max(0.0, (0.50 - price)) if side == "YES" else max(0.0, (0.50 - price))
        if edge < MIN_EDGE:
            # 너무 애매하면 제외
            return None

        return {
            "slug": m.get("slug"),
            "question": q,
            "yes": str(token_ids[0]),
            "no": str(token_ids[1]),
            "vol": vol,
            "side": side,
            "price": price,
            "spread_est": spread_est if spread_est is not None else -1,
            "edge": edge,
        }

    def _pick_markets(self):
        markets = self._get_markets()
        scored = []
        for m in markets:
            s = self._score_market(m)
            if s:
                scored.append(s)

        # 우선순위: 거래량 -> edge -> 스프레드(작을수록)
        scored.sort(key=lambda x: (x["vol"], x["edge"], -x["spread_est"]), reverse=True)

        self.notify(f"DEBUG markets_count={len(markets)} | candidates={len(scored)}")
        return scored[:TOP_N]

    def _maybe_notify_heartbeat(self):
        # 60초에 1번만
        now = time.time()
        if now - self._last_notify_ts < 60:
            return
        self._last_notify_ts = now
        self.notify(
            f"📡 heartbeat | day={self.day} pnl={self.pnl_today:.4f} | "
            f"trades={self.trades_today}/{MAX_TRADES_PER_DAY} | pos=N | DRY_RUN={DRY_RUN}"
        )

    def tick(self):
        self._reset_day_if_needed()
        if self.client is None:
            self._init_client()

        # 하루 손절 -10% 룰(현재는 “체크/가드”만)
        if self.pnl_today <= DAILY_STOP_LOSS_PCT:
            self.last_action = "stopped_by_daily_stop"
            self.notify(f"🛑 하루 손절 룰 발동: pnl={self.pnl_today:.4f} <= {DAILY_STOP_LOSS_PCT}")
            self._maybe_notify_heartbeat()
            return

        if self.trades_today >= MAX_TRADES_PER_DAY:
            self.last_action = "trade_limit_reached"
            self.notify("🛑 오늘 거래 횟수 제한 도달")
            self._maybe_notify_heartbeat()
            return

        picks = self._pick_markets()
        self.last_pick = [{"slug": p["slug"], "vol": p["vol"], "side": p["side"], "price": round(p["price"], 4)} for p in picks]

        if not picks:
            self.last_action = "no picks"
            self._maybe_notify_heartbeat()
            return

        top = picks[0]
        self.last_action = f"picked {top['slug']} {top['side']} @~{top['price']:.4f}"

        # ✅ DRY_RUN에서는 메시지만
        if DRY_RUN:
            self.notify(
                "🧪 DRY_RUN\n"
                f"slug={top['slug']}\n"
                f"side={top['side']} price~{top['price']:.4f} vol={top['vol']:.0f}\n"
                f"{top['question']}"
            )
            self._maybe_notify_heartbeat()
            return

        # ⚠️ 실매매: 여기서부터는 주문 로직이 들어가야 함.
        # 지금은 “안전하게” 막아둠(실수로 DRY_RUN=0 해도 주문 안 나가게).
        raise RuntimeError("실매매 모드( DRY_RUN=0 ) 주문 로직은 아직 비활성화 상태입니다. 먼저 주문/체결/매도 로직을 확정해야 합니다.")
