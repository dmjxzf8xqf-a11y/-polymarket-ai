import os
import time
import requests
from py_clob_client.client import ClobClient

GAMMA = "https://gamma-api.polymarket.com"

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Polymarket CLOB
POLY_HOST = os.getenv("POLY_HOST", "https://clob.polymarket.com").rstrip("/")
POLY_CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))          # Polygon mainnet = 137
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")            # 0x + 64 hex
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
POLY_FUNDER = os.getenv("POLY_FUNDER") or None                  # 보통 지갑주소(0x..)

# Mode / limits
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "20"))
TRADES_LIMIT = int(os.getenv("TRADES_LIMIT", "100"))            # ✅ 하루 거래횟수 100
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "50"))

# Filters (YES/NO 결정 기준)
MIN_24H_VOL = float(os.getenv("MIN_24H_VOL", "1000"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.08"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.95"))

# Sizing / risk
TRADE_USDC = float(os.getenv("TRADE_USDC", "1.0"))
DAY_STOPLOSS_PCT = float(os.getenv("DAY_STOPLOSS_PCT", "0.10"))  # ✅ 하루 손절 -10%

# Exit rules (✅ 다음단계 핵심)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.03"))     # +3%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))         # -2%
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "1800"))     # 30분

# Entry behavior
ENTRY_TIMEOUT_SECONDS = int(os.getenv("ENTRY_TIMEOUT_SECONDS", "120"))  # 2분 안 채워지면 포기
ONE_POSITION = os.getenv("ONE_POSITION", "1") == "1"                    # ✅ 한 포지션만 (안정)

# 금칙어(쉼표로 구분)
BLACKLIST = [w.strip().lower() for w in os.getenv("BLACKLIST", "").split(",") if w.strip()]

# 알림/디버그
DEBUG = os.getenv("DEBUG", "1") == "1"
HEARTBEAT_EVERY_N_LOOPS = int(os.getenv("HEARTBEAT_EVERY_N_LOOPS", str(max(1, 60 // max(1, LOOP_SECONDS)))))


class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None
        self.last_pick = []
        self.last_action = None
        self._day_key = None

        # 단일 포지션 상태
        self.position = None
        # position = {
        #   "slug": str,
        #   "question": str,
        #   "token_id": str,
        #   "pick": "YES"/"NO",
        #   "entry_price": float,
        #   "size": float,
        #   "opened_at": epoch,
        #   "entry_order_id": str|None,
        #   "exit_order_id": str|None
        # }

        self._last_markets_cache = None
        self._last_markets_cache_ts = 0

    # ----------------- Telegram -----------------
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
            "trades_limit": TRADES_LIMIT,
            "trades_today": self.state.get("trades_today", 0),
            "day_pnl": round(float(self.state.get("day_pnl", 0.0)), 6),
            "halted": bool(self.state.get("halted", False)),
            "position": self.position,
            "last_pick": self.last_pick,
            "last_action": self.last_action,
            "tp_pct": TAKE_PROFIT_PCT,
            "sl_pct": STOP_LOSS_PCT,
            "max_hold_s": MAX_HOLD_SECONDS,
        }

    # ----------------- Day reset / risk -----------------
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
            self.notify(f"🛑 일일 손절 발동: pnl={pnl:.2f} / start={start:.2f} (<= -{int(DAY_STOPLOSS_PCT*100)}%)")

    # ----------------- Polymarket client -----------------
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

    # ----------------- Gamma markets -----------------
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
        if not BLACKLIST:
            return False
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
            try:
                vol = float(vol)
            except:
                vol = 0.0
            if vol < MIN_24H_VOL:
                continue

            picks.append({
                "slug": slug,
                "question": q,
                "yes": str(token_ids[0]),
                "no": str(token_ids[1]),
                "vol": vol,
            })

        picks.sort(key=lambda x: x["vol"], reverse=True)
        return picks[:MAX_MARKETS]

    # ----------------- Order book helpers -----------------
    def _get_order_book(self, token_id: str):
        if hasattr(self.client, "get_order_book"):
            return self.client.get_order_book(token_id)
        if hasattr(self.client, "get_orderbook"):
            return self.client.get_orderbook(token_id)
        raise RuntimeError("CLOB client missing orderbook method")

    def _get_book_mid_and_spread(self, token_id: str):
        book = self._get_order_book(token_id)
        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def top_price(levels):
            if not levels:
                return None
            p = levels[0].get("price")
            try:
                return float(p)
            except:
                return None

        bid = top_price(bids)
        ask = top_price(asks)
        if bid is None or ask is None:
            return None, None, None, None
        mid = (bid + ask) / 2.0
        spread = ask - bid
        return bid, ask, mid, spread

    # ----------------- Decision logic -----------------
    def _decide_side(self, yes_mid, yes_spread, no_mid, no_spread):
        candidates = []

        if yes_mid is not None and MIN_PRICE <= yes_mid <= MAX_PRICE and yes_spread is not None and yes_spread <= MAX_SPREAD:
            score = (1.0 - abs(yes_mid - 0.5)) / (yes_spread + 1e-6)
            candidates.append(("YES", score))

        if no_mid is not None and MIN_PRICE <= no_mid <= MAX_PRICE and no_spread is not None and no_spread <= MAX_SPREAD:
            score = (1.0 - abs(no_mid - 0.5)) / (no_spread + 1e-6)
            candidates.append(("NO", score))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    # ----------------- Orders (defensive wrappers) -----------------
    def _create_and_post_order(self, token_id: str, side: str, price: float, size: float):
        """
        side: "BUY" / "SELL"
        price: 0~1
        size: shares
        return: dict (maybe includes order id)
        """
        if DRY_RUN:
            return {"dry_run": True, "token_id": token_id, "side": side, "price": price, "size": size}

        if not hasattr(self.client, "create_order"):
            raise RuntimeError("CLOB client missing create_order")

        order = self.client.create_order(
            token_id=token_id,
            side=side,
            price=str(round(price, 4)),
            size=str(round(size, 4)),
        )

        if hasattr(self.client, "post_order"):
            return self.client.post_order(order)
        if hasattr(self.client, "submit_order"):
            return self.client.submit_order(order)

        return order

    def _extract_order_id(self, res):
        # 다양한 응답 형태 대응
        if not res:
            return None
        if isinstance(res, dict):
            for k in ["orderID", "orderId", "id", "order_id"]:
                if k in res and res[k]:
                    return str(res[k])
        return None

    def _cancel_order(self, order_id: str):
        if DRY_RUN:
            return {"dry_run": True, "cancel": order_id}
        if hasattr(self.client, "cancel_order"):
            return self.client.cancel_order(order_id)
        if hasattr(self.client, "cancel"):
            return self.client.cancel(order_id)
        raise RuntimeError("CLOB client missing cancel method")

    def _get_order_status(self, order_id: str):
        """
        filled 판단을 위해 최대한 넓게 대응:
        - get_order(order_id)
        - get_orders(order_ids=[...])
        - get_open_orders() 에서 없는지 확인(보수적)
        return: dict or None
        """
        if DRY_RUN:
            return {"dry_run": True, "status": "filled"}

        if hasattr(self.client, "get_order"):
            return self.client.get_order(order_id)

        if hasattr(self.client, "get_orders"):
            res = self.client.get_orders(order_ids=[order_id])
            # res가 list/dict일 수 있음
            if isinstance(res, list) and res:
                return res[0]
            if isinstance(res, dict):
                return res

        # 최후수단: open_orders에 없으면 filled/closed로 추정
        if hasattr(self.client, "get_open_orders"):
            opens = self.client.get_open_orders()
            if isinstance(opens, list):
                for o in opens:
                    oid = o.get("orderID") or o.get("orderId") or o.get("id") or o.get("order_id")
                    if str(oid) == str(order_id):
                        return o
                return {"status": "closed_or_filled"}
        return None

    def _is_filled(self, order_obj: dict):
        if not order_obj:
            return False
        s = str(order_obj.get("status", "")).lower()
        if "filled" in s:
            return True
        # 일부 응답: remaining/filledSize 등
        filled_sz = order_obj.get("filledSize") or order_obj.get("filled_size")
        remaining = order_obj.get("remainingSize") or order_obj.get("remaining_size")
        try:
            if remaining is not None and float(remaining) == 0.0:
                return True
        except:
            pass
        try:
            if filled_sz
