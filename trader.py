import os
import time
import math
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
TRADES_LIMIT = int(os.getenv("TRADES_LIMIT", "100"))            # ✅ 너 요청: 100번
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "50"))               # 감시할 마켓 수

# Filters (✅ YES/NO 결정 기준)
MIN_24H_VOL = float(os.getenv("MIN_24H_VOL", "1000"))           # 24h 거래량 필터 (너무 낮은 건 제외)
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.08"))             # (ask-bid) 스프레드 상한
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.05"))               # 너무 싼 구간 제외
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.95"))               # 너무 비싼 구간 제외

# Sizing / risk
TRADE_USDC = float(os.getenv("TRADE_USDC", "1.0"))              # 1회 진입 금액(USDC)
DAY_STOPLOSS_PCT = float(os.getenv("DAY_STOPLOSS_PCT", "0.10")) # ✅ 하루 손절 -10%

# 금칙어(쉼표로 구분)
BLACKLIST = [w.strip().lower() for w in os.getenv("BLACKLIST", "").split(",") if w.strip()]

# 알림 스팸 방지
HEARTBEAT_EVERY_N_LOOPS = int(os.getenv("HEARTBEAT_EVERY_N_LOOPS", str(max(1, 60 // max(1, LOOP_SECONDS)))))
DEBUG = os.getenv("DEBUG", "1") == "1"


class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None
        self.last_pick = []
        self.last_action = None
        self._day_key = None  # YYYY-MM-DD
        self._last_markets_cache = None
        self._last_markets_cache_ts = 0

    # ----------------- Utils -----------------
    def notify(self, text: str):
        # 텔레그램 세팅 안돼있으면 콘솔로만 출력
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
            "max_markets": MAX_MARKETS,
            "min_24h_vol": MIN_24H_VOL,
            "max_spread": MAX_SPREAD,
            "trade_usdc": TRADE_USDC,
            "day_stoploss_pct": DAY_STOPLOSS_PCT,
            "blacklist": BLACKLIST,
            "last_pick": self.last_pick,
            "last_action": self.last_action,
        }

    def _today_key(self):
        # Render는 UTC일 수 있어서 로컬 날짜가 필요하면 env로 조정 가능하지만,
        # 여기선 단순히 UTC 기준으로 하루 리셋.
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

    # ----------------- Polymarket -----------------
    def _init_client(self):
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("POLY_PRIVATE_KEY missing")

        # 간단 검증 로그(키 값 자체는 절대 출력 X)
        key_len = len(POLY_PRIVATE_KEY)
        funder_len = len(POLY_FUNDER) if POLY_FUNDER else 0
        if DEBUG:
            self.notify(
                f"DEBUG host={POLY_HOST} chain={POLY_CHAIN_ID} sig={POLY_SIGNATURE_TYPE} "
                f"key_len={key_len} key_0x={POLY_PRIVATE_KEY.startswith('0x')} "
                f"funder_len={funder_len} funder_0x={(POLY_FUNDER or '').startswith('0x')}"
            )

        c = ClobClient(
            POLY_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIGNATURE_TYPE,
            funder=POLY_FUNDER,
        )

        # L2 API creds (주문/조회에 필요)
        c.set_api_creds(c.create_or_derive_api_creds())

        self.client = c
        self.notify("✅ Polymarket CLOB 연결 OK")

    def _gamma_markets(self):
        # Gamma가 가끔 느려서 캐시(20초~) 사용
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
        for m in markets[:MAX_MARKETS * 3]:  # 넉넉히 훑고 필터
            slug = m.get("slug")
            q = m.get("question") or m.get("title") or ""
            token_ids = m.get("clobTokenIds") or m.get("clob_token_ids") or m.get("tokenIds") or m.get("token_ids")

            if not slug or not q or not isinstance(token_ids, list) or len(token_ids) < 2:
                continue
            if self._is_blacklisted(q) or self._is_blacklisted(slug):
                continue

            # 24h volume
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

        # 거래량 높은 순
        picks.sort(key=lambda x: x["vol"], reverse=True)
        return picks[:MAX_MARKETS]

    def _get_book_mid_and_spread(self, token_id: str):
        """
        returns (bid, ask, mid, spread)
        - spread = ask - bid
        """
        # py_clob_client 메서드 이름이 환경에 따라 다를 수 있어서 방어적으로 호출
        book = None
        if hasattr(self.client, "get_order_book"):
            book = self.client.get_order_book(token_id)
        elif hasattr(self.client, "get_orderbook"):
            book = self.client.get_orderbook(token_id)
        else:
            raise RuntimeError("CLOB client missing orderbook method")

        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def top_price(levels):
            # levels: [{price:'0.51', size:'123'}] 같은 형태를 기대
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

    def _decide_side(self, yes_mid, yes_spread, no_mid, no_spread):
        """
        ✅ YES/NO 결정 기준(가격/스프레드/오즈 기반)
        - 거래 가능한 가격 구간: [MIN_PRICE, MAX_PRICE]
        - 스프레드가 MAX_SPREAD 이하
        - YES/NO 중 '스프레드가 더 타이트'한 쪽 우선
        - 오즈(=mid)가 극단(0/1)에 가까우면 제외
        return ("YES" or "NO") or None
        """
        candidates = []

        if yes_mid is not None and MIN_PRICE <= yes_mid <= MAX_PRICE and yes_spread is not None and yes_spread <= MAX_SPREAD:
            # 오즈 기반 가중치: 0.5 근처(불확실) + 스프레드 타이트 선호
            score = (1.0 - abs(yes_mid - 0.5)) / (yes_spread + 1e-6)
            candidates.append(("YES", score))

        if no_mid is not None and MIN_PRICE <= no_mid <= MAX_PRICE and no_spread is not None and no_spread <= MAX_SPREAD:
            score = (1.0 - abs(no_mid - 0.5)) / (no_spread + 1e-6)
            candidates.append(("NO", score))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def _place_order(self, token_id: str, side: str, price: float, size: float):
        """
        side: "BUY" 만 사용(단순)
        price: 0~1
        size: 수량(share) = USDC / price 로 계산
        """
        # DRY_RUN이면 주문 안 나감
        if DRY_RUN:
            return {"dry_run": True}

        # 메서드 이름 방어적으로
        if hasattr(self.client, "create_order"):
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
            # create_order가 내부 전송까지 하는 케이스
            return order

        raise RuntimeError("CLOB client missing create_order")

    # ----------------- Risk / pnl -----------------
    def _ensure_day_start_equity(self):
        # 정확한 계정 equity를 API로 뽑아오는 건 환경마다 달라서,
        # 여기선 기본값(없으면 0)으로 두고, 손절은 "state의 day_pnl" 기준으로만 적용.
        if self.state["day_start_equity"] is None:
            # 최소 기준을 TRADE_USDC * 10 정도로 잡아둠 (너 돈 23 USDC라서 과격하지 않게)
            # 실제로는 추후 balance API 붙이면 더 정확해짐.
            est = max(TRADE_USDC * 10.0, 10.0)
            self.state["day_start_equity"] = est
            self.notify(f"📌 day_start_equity(추정)={est:.2f} USDC")

    def _check_stoploss(self):
        self._ensure_day_start_equity()
        start = float(self.state["day_start_equity"] or 0)
        pnl = float(self.state["day_pnl"] or 0.0)
        if start <= 0:
            return

        if pnl <= -DAY_STOPLOSS_PCT * start:
            self.state["halted"] = True
            self.notify(f"🛑 일일 손절 발동: pnl={pnl:.2f} / start={start:.2f} (<= -{int(DAY_STOPLOSS_PCT*100)}%)")

    # ----------------- Main tick -----------------
    def tick(self, loop_n: int = 0):
        self._reset_day_if_needed()

        # halt면 아무것도 안 함(하트비트만)
        if self.state.get("halted"):
            if loop_n % HEARTBEAT_EVERY_N_LOOPS == 0:
                self.notify(f"🛰️ heartbeat | day={self._day_key} | pnl={self.state['day_pnl']:.2f} | trades={self.state['trades_today']}/{TRADES_LIMIT} | HALTED=True")
            return

        if self.client is None:
            self._init_client()

        # 하트비트(너무 자주 안 오게)
        if loop_n % HEARTBEAT_EVERY_N_LOOPS == 0:
            self.notify(f"🛰️ heartbeat | day={self._day_key} pnl={self.state['day_pnl']:.2f} | trades={self.state['trades_today']}/{TRADES_LIMIT} | DRY_RUN={DRY_RUN}")

        # 거래횟수 제한
        if self.state["trades_today"] >= TRADES_LIMIT:
            self.state["halted"] = True
            self.notify(f"🛑 trades_limit 도달: {TRADES_LIMIT}회 -> 오늘은 중지")
            return

        self._check_stoploss()
        if self.state.get("halted"):
            return

        picks = self._pick_markets()
        self.last_pick = [{"slug": p["slug"], "vol": p["vol"]} for p in picks]
        if not picks:
            self.last_action = "no picks"
            return

        # 가장 거래량 높은 후보부터 하나씩 검사해서 "조건 맞는 것" 찾기
        chosen = None
        chosen_detail = None

        for m in picks[:MAX_MARKETS]:
            yes_id = m["yes"]
            no_id = m["no"]

            yb, ya, ym, ys = self._get_book_mid_and_spread(yes_id)
            nb, na, nm, ns = self._get_book_mid_and_spread(no_id)

            # 조건 기반 방향 결정
            side_pick = self._decide_side(ym, ys, nm, ns)
            if not side_pick:
                continue

            chosen = m
            chosen_detail = {
                "yes_mid": ym, "yes_spread": ys,
                "no_mid": nm, "no_spread": ns,
                "pick": side_pick
            }
            break

        if not chosen:
            self.last_action = "no market passed filters"
            return

        slug = chosen["slug"]
        question = chosen["question"]
        pick = chosen_detail["pick"]

        # 주문 파라미터 계산 (BUY only)
        if pick == "YES":
            token_id = chosen["yes"]
            mid = chosen_detail["yes_mid"]
            bid, ask, _, spread = self._get_book_mid_and_spread(token_id)
        else:
            token_id = chosen["no"]
            mid = chosen_detail["no_mid"]
            bid, ask, _, spread = self._get_book_mid_and_spread(token_id)

        if bid is None or ask is None:
            self.last_action = "missing book"
            return

        # “가격/스프레드” 기준: 보수적으로 bid 쪽에 maker로 걸기
        price = float(bid)

        # size(share) = USDC / price
        usdc = float(TRADE_USDC)
        if price <= 0:
            return
        size = usdc / price

        msg = (
            f"🧪 DRY_RUN={DRY_RUN}\n"
            f"slug={slug}\n"
            f"{question}\n"
            f"pick={pick} | price={price:.4f} | spread={spread:.4f} | vol24h={chosen['vol']:.0f}\n"
            f"usdc={usdc:.2f} -> size≈{size:.4f} shares"
        )
        self.notify(msg)
        self.last_action = f"picked {slug} {pick} @ {price:.4f}"

        # DRY_RUN이면 여기서 끝
        if DRY_RUN:
            return

        # 실주문
        res = self._place_order(token_id=token_id, side="BUY", price=price, size=size)
        self.state["trades_today"] += 1

        # PnL은 여기선 체결/포지션 평가가 없어서 0 유지(추후 체결/청산 로직 붙이면 업데이트)
        self.notify(f"✅ 주문 제출됨 | trades_today={self.state['trades_today']}/{TRADES_LIMIT}\n{res}")
