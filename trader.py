import os
import time
import math
import requests

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

GAMMA = "https://gamma-api.polymarket.com"

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Polymarket CLOB
POLY_HOST = os.getenv("POLY_HOST", "https://clob.polymarket.com").rstrip("/")
POLY_CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
POLY_FUNDER = os.getenv("POLY_FUNDER") or None

# Mode
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "20"))

# Limits
TRADES_LIMIT = int(os.getenv("TRADES_LIMIT", "100"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "50"))

# 공격모드 필터(거래 더 자주)
MIN_24H_VOL = float(os.getenv("MIN_24H_VOL", "600"))     # 낮춰서 기회↑
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.12"))      # 완화
ACTIVE_MIN = float(os.getenv("ACTIVE_MIN", "0.35"))      # 활발구간
ACTIVE_MAX = float(os.getenv("ACTIVE_MAX", "0.65"))

# Sizing (23달러 기준 추천 2.5~3.0)
TRADE_USDC = float(os.getenv("TRADE_USDC", "3.0"))

# Exit rules (자동매도)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.02"))   # +2%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))       # -2%
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "1800"))   # 30분

# Day stoploss (추정)
DAY_STOPLOSS_PCT = float(os.getenv("DAY_STOPLOSS_PCT", "0.10")) # -10%

# 금칙어(쉼표)
BLACKLIST = [w.strip().lower() for w in os.getenv("BLACKLIST", "").split(",") if w.strip()]

DEBUG = os.getenv("DEBUG", "1") == "1"
HEARTBEAT_EVERY_N_LOOPS = int(os.getenv("HEARTBEAT_EVERY_N_LOOPS", str(max(1, 60 // max(1, LOOP_SECONDS)))))

# ---- helpers ----
def _to_float(x, default=None):
    try:
        return float(x)
    except:
        return default

def _floor_to(x: float, decimals: int) -> float:
    p = 10 ** decimals
    return math.floor(x * p) / p

def _round_price(p: float) -> float:
    # 가격은 보통 4dp면 안전
    return _floor_to(p, 4)

def _round_size(s: float) -> float:
    # size는 2dp로 보수적(클라/틱제약 이슈 예방용)
    return _floor_to(s, 2)

class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None

        self._day_key = None
        self._loop_n = 0

        # 캐시
        self._last_markets_cache = None
        self._last_markets_cache_ts = 0

        # 상태 표시용
        self.last_pick = []
        self.last_action = None

        # 단일 포지션만(안정)
        self.position = None
        # position = {
        #   "slug": str, "question": str, "token_id": str, "label": "YES"/"NO",
        #   "entry_price": float, "size": float, "opened_at": float, "tp_order_id": str|None
        # }

        # 반복 slug 방지
        self._last_slug = None
        self._last_slug_ts = 0

    # ---------- Telegram ----------
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
            "trades_today": self.state.get("trades_today", 0),
            "trades_limit": TRADES_LIMIT,
            "day_pnl_est": round(float(self.state.get("day_pnl", 0.0)), 6),
            "halted": bool(self.state.get("halted", False)),
            "position": self.position,
            "last_pick": self.last_pick,
            "last_action": self.last_action,
        }

    # ---------- day reset ----------
    def _today_key(self):
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _reset_day_if_needed(self):
        day = self._today_key()
        if self._day_key != day:
            self._day_key = day
            self.state["day_start_equity"] = None
            self.state["day_pnl"] = 0.0
            self.state["trades_today"] = 0
            self.state["halted"] = False
            self.notify(f"🗓️ 데이 리셋: {day} | trades_today=0 | stoploss=-{int(DAY_STOPLOSS_PCT*100)}%")

    def _ensure_day_start_equity(self):
        if self.state.get("day_start_equity") is None:
            est = max(TRADE_USDC * 10.0, 10.0)
            self.state["day_start_equity"] = est
            self.notify(f"📌 day_start_equity(추정)={est:.2f} USDC")

    def _check_day_stoploss(self):
        self._ensure_day_start_equity()
        start = float(self.state.get("day_start_equity") or 0)
        pnl = float(self.state.get("day_pnl") or 0.0)
        if start > 0 and pnl <= -DAY_STOPLOSS_PCT * start:
            self.state["halted"] = True
            self.notify(f"🛑 일일 손절 발동(추정): pnl={pnl:.2f} / start={start:.2f}")
            return True
        return False

    # ---------- client ----------
    def _init_client(self):
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("POLY_PRIVATE_KEY missing")

        if DEBUG:
            self.notify(
                f"DEBUG host={POLY_HOST} chain={POLY_CHAIN_ID} sig={POLY_SIGNATURE_TYPE} "
                f"key_len={len(POLY_PRIVATE_KEY)} key_0x={POLY_PRIVATE_KEY.startswith('0x')} "
                f"funder_len={(len(POLY_FUNDER) if POLY_FUNDER else 0)} funder_0x={(POLY_FUNDER or '').startswith('0x')}"
            )

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

    # ---------- gamma ----------
    def _gamma_markets(self):
        now = time.time()
        if self._last_markets_cache and (now - self._last_markets_cache_ts) < max(10, LOOP_SECONDS):
            return self._last_markets_cache

        r = requests.get(f"{GAMMA}/markets", timeout=25)
        r.raise_for_status()
        markets = r.json()

        self._last_markets_cache = markets
        self._last_markets_cache_ts = now
        return markets

    def _is_blacklisted(self, text: str):
        t = (text or "").lower()
        return any(b in t for b in BLACKLIST)

    def _pick_markets(self):
        markets = self._gamma_markets()

        picks = []
        for m in markets[:MAX_MARKETS * 3]:
            slug = m.get("slug")
            q = m.get("question") or m.get("title") or ""
            token_ids = m.get("clobTokenIds") or m.get("clob_token_ids") or m.get("tokenIds") or m.get("token_ids")

            if not slug or not q or not isinstance(token_ids, list) or len(token_ids) < 2:
                continue
            if self._is_blacklisted(q) or self._is_blacklisted(slug):
                continue

            vol = m.get("volume24hr") or m.get("volume_24hr") or m.get("volume24h") or m.get("volume") or 0
            vol = _to_float(vol, 0.0)
            if vol < MIN_24H_VOL:
                continue

            picks.append({
                "slug": slug,
                "question": q.strip(),
                "yes": str(token_ids[0]),
                "no": str(token_ids[1]),
                "vol": vol,
            })

        picks.sort(key=lambda x: x["vol"], reverse=True)
        return picks[:MAX_MARKETS]

    # ---------- orderbook ----------
    def _get_book_mid_and_spread(self, token_id: str):
        book = self.client.get_order_book(token_id)
        bids = book.get("bids") or []
        asks = book.get("asks") or []

        bid = _to_float(bids[0].get("price")) if bids else None
        ask = _to_float(asks[0].get("price")) if asks else None
        if bid is None or ask is None:
            return None, None, None, None

        mid = (bid + ask) / 2.0
        spread = ask - bid
        return bid, ask, mid, spread

    def _choose_side(self, yes_id: str, no_id: str):
        yb, ya, ym, ys = self._get_book_mid_and_spread(yes_id)
        nb, na, nm, ns = self._get_book_mid_and_spread(no_id)
        if ym is None or nm is None:
            return None

        # 공격형: active 구간이면 가산점, 스프레드 좁을수록 가산점
        def score(mid, spread):
            if spread is None:
                return -1e9
            s = -spread
            if ACTIVE_MIN <= mid <= ACTIVE_MAX:
                s += 0.01
            return s

        cands = []
        if ys is not None and ys <= MAX_SPREAD:
            cands.append(("YES", yes_id, yb, ya, ym, ys, score(ym, ys)))
        if ns is not None and ns <= MAX_SPREAD:
            cands.append(("NO", no_id, nb, na, nm, ns, score(nm, ns)))

        if not cands:
            return None
        cands.sort(key=lambda x: x[-1], reverse=True)
        return cands[0]  # label, token_id, bid, ask, mid, spread, score

    # ---------- order submit ----------
    def _post_order(self, token_id: str, side: str, price: float, size: float, order_type: str):
        # side: "BUY" / "SELL"
        if DRY_RUN:
            return {"dry_run": True, "side": side, "price": price, "size": size, "order_type": order_type}

        # OrderArgs + post_order(order, orderType)
        args = OrderArgs(token_id=token_id, price=float(price), size=float(size), side=side)
        signed = self.client.create_order(args)
        resp = self.client.post_order(signed, order_type)
        return resp

    def _extract_order_id(self, resp):
        if isinstance(resp, dict):
            return resp.get("orderID") or resp.get("orderId") or resp.get("id") or resp.get("order_id")
        return None

    def _cancel(self, order_id: str):
        if DRY_RUN or not order_id:
            return
        if hasattr(self.client, "cancel"):
            return self.client.cancel(order_id)
        if hasattr(self.client, "cancel_order"):
            return self.client.cancel_order(order_id)
        return None

    # ---------- position logic ----------
    def _enter(self, m):
        yes_id, no_id = m["yes"], m["no"]
        chosen = self._choose_side(yes_id, no_id)
        if not chosen:
            return False

        label, token_id, bid, ask, mid, spread, _ = chosen

        # entry는 체결확률 올리려고 ask에 "시장성 limit" + FOK
        entry_price = _round_price(float(ask))
        if entry_price <= 0:
            return False

        size = TRADE_USDC / entry_price
        size = _round_size(size)
        if size <= 0:
            return False

        self.notify(
            f"{'🧪' if DRY_RUN else '🟩'} ENTRY\n"
            f"{m['question']}\nslug={m['slug']}\n"
            f"{label} | ask={entry_price:.4f} mid={mid:.4f} spread={spread:.4f} vol24h={m['vol']:.0f}\n"
            f"usdc={TRADE_USDC:.2f} -> size≈{size:.2f}"
        )

        entry_resp = self._post_order(token_id, "BUY", entry_price, size, OrderType.FOK)

        # FOK 실패 시 보통 에러/실패로 돌아옴(형태는 환경마다 다름)
        if isinstance(entry_resp, dict) and entry_resp.get("error"):
            self.notify(f"❌ ENTRY 실패: {entry_resp}")
            return False

        # TP 주문(GTC) 깔기
        tp_price = _round_price(entry_price * (1.0 + TAKE_PROFIT_PCT))
        tp_resp = self._post_order(token_id, "SELL", tp_price, size, OrderType.GTC)
        tp_id = self._extract_order_id(tp_resp)

        self.position = {
            "slug": m["slug"],
            "question": m["question"],
            "token_id": token_id,
            "label": label,
            "entry_price": entry_price,
            "size": float(size),
            "opened_at": time.time(),
            "tp_order_id": str(tp_id) if tp_id else None,
        }

        self.state["trades_today"] = int(self.state.get("trades_today", 0)) + 1
        self.last_action = f"entered {m['slug']} {label}"

        self.notify(f"✅ ENTRY 완료 + TP 세팅 | TP@{tp_price:.4f} | tp_order_id={self.position['tp_order_id']}")
        return True

    def _exit_now(self, reason: str):
        if not self.position:
            return

        token_id = self.position["token_id"]
        entry = float(self.position["entry_price"])
        size = float(self.position["size"])
        tp_id = self.position.get("tp_order_id")

        # TP 취소 후 즉시 청산
        self._cancel(tp_id)

        bid, ask, mid, spread = self._get_book_mid_and_spread(token_id)
        if bid is None:
            self.notify("❌ EXIT 실패: bid 없음")
            return

        exit_price = _round_price(float(bid))

        self.notify(
            f"{'🧪' if DRY_RUN else '🟥'} EXIT({reason})\n"
            f"SELL@bid={exit_price:.4f} size={size:.2f} | entry={entry:.4f}"
        )

        _ = self._post_order(token_id, "SELL", exit_price, size, OrderType.FOK)

        # PnL “추정” 누적(정확 아님)
        est = (exit_price - entry) * size
        self.state["day_pnl"] = float(self.state.get("day_pnl", 0.0)) + float(est)

        self.position = None
        self.last_action = f"exited {reason}"

    # ---------- main tick ----------
    def tick(self):
        self._loop_n += 1
        self._reset_day_if_needed()

        if self.client is None:
            self._init_client()

        if self._loop_n % HEARTBEAT_EVERY_N_LOOPS == 0:
            pos = "Y" if self.position else "N"
            self.notify(
                f"🛰️ heartbeat | day={self._day_key} | pnl_est={float(self.state.get('day_pnl',0.0)):.4f} | "
                f"trades={int(self.state.get('trades_today',0))}/{TRADES_LIMIT} | pos={pos} | DRY_RUN={DRY_RUN}"
            )

        if self.state.get("halted"):
            return
        if int(self.state.get("trades_today", 0)) >= TRADES_LIMIT:
            self.state["halted"] = True
            self.notify(f"🛑 trades_limit 도달: {TRADES_LIMIT}회 -> 오늘 중지")
            return
        if self._check_day_stoploss():
            return

        # 포지션 있으면 SL/TIME만 체크 (TP는 주문이 책에 걸려있음)
        if self.position:
            token_id = self.position["token_id"]
            entry = float(self.position["entry_price"])
            opened = float(self.position["opened_at"])

            bid, ask, mid, spread = self._get_book_mid_and_spread(token_id)
            if mid is None:
                return

            # 손절
            if mid <= entry * (1.0 - STOP_LOSS_PCT):
                self._exit_now("SL")
                return

            # 시간청산
            if (time.time() - opened) >= MAX_HOLD_SECONDS:
                self._exit_now("TIME")
                return

            self.last_action = "holding"
            return

        # 신규 진입 탐색
        picks = self._pick_markets()
        self.last_pick = [{"slug": p["slug"], "vol": p["vol"]} for p in picks]

        if not picks:
            self.last_action = "no picks"
            return

        # 같은 slug 반복 방지(5분)
        for m in picks:
            if self._last_slug == m["slug"] and (time.time() - self._last_slug_ts) < 300:
                continue
            ok = self._enter(m)
            if ok:
                self._last_slug = m["slug"]
                self._last_slug_ts = time.time()
                return

        self.last_action = "no entry signal"
