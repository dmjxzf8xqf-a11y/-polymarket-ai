import os
import time
import random
import requests
from datetime import datetime, timedelta, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, OpenOrderParams
from py_clob_client.order_builder.constants import BUY, SELL

GAMMA = "https://gamma-api.polymarket.com"
KST = timezone(timedelta(hours=9))

# -----------------------------
# ENV (Telegram)
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# -----------------------------
# ENV (Polymarket)
# -----------------------------
POLY_HOST = os.getenv("POLY_HOST", "https://clob.polymarket.com").rstrip("/")
POLY_CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))
POLY_PRIVATE_KEY = (os.getenv("POLY_PRIVATE_KEY") or "").strip()
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
POLY_FUNDER = (os.getenv("POLY_FUNDER") or "").strip() or None

# -----------------------------
# ENV (Mode / risk)
# -----------------------------
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

START_EQUITY_USDC = float(os.getenv("START_EQUITY_USDC", "23"))  # 너는 23 추천
DAILY_STOP_LOSS_PCT = float(os.getenv("DAILY_STOP_LOSS_PCT", "0.10"))  # -10%

ORDER_USDC = float(os.getenv("ORDER_USDC", "1.0"))  # 1회 베팅액(USDC)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.02"))  # +2%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))      # -2%
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "120"))   # 2시간

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "10"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "1"))

# -----------------------------
# ENV (market selection)
# -----------------------------
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "50"))       # gamma에서 볼 시장 수
TOPN_EVAL = int(os.getenv("TOPN_EVAL", "15"))           # 오더북까지 평가할 상위 N개
ROTATE_TOP_N = int(os.getenv("ROTATE_TOP_N", "5"))      # 그 중 최종 선택 후보
RANDOMIZE = os.getenv("RANDOMIZE", "1") == "1"          # 1: 랜덤, 0: 로테이션

MIN_VOL_24H = float(os.getenv("MIN_VOL_24H", "0"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.08"))     # 너무 벌어진 시장은 제외

# -----------------------------
# ENV (notify)
# -----------------------------
NOTIFY_COOLDOWN_SECONDS = int(os.getenv("NOTIFY_COOLDOWN_SECONDS", "300"))
HEARTBEAT_EVERY = int(os.getenv("HEARTBEAT_EVERY", "6"))  # tick 몇 번마다 하트비트

DEBUG = os.getenv("DEBUG", "1") == "1"


class Trader:
    def __init__(self, state: dict):
        self.state = state
        self.client = None

        # daily
        self.day = self._today_kst()
        self.realized_pnl = 0.0
        self.stopped_today = False
        self.trades_today = 0

        # position (in-memory)
        self.pos = None  # dict with token_id, side, entry_price, size, tp_order_id, opened_ts

        # selection rotation
        self._tick = 0
        self._rotate_idx = 0

        # notify
        self._last_notify_ts = 0
        self._last_notify_key = None

    # -----------------------------
    # utilities
    # -----------------------------
    def _today_kst(self):
        return datetime.now(KST).strftime("%Y-%m-%d")

    def _now_ts(self):
        return int(time.time())

    def notify(self, text: str, key: str = None, cooldown: int = None):
        if cooldown is None:
            cooldown = NOTIFY_COOLDOWN_SECONDS

        now = self._now_ts()
        if key is not None:
            if self._last_notify_key == key and (now - self._last_notify_ts) < cooldown:
                return
            self._last_notify_key = key
            self._last_notify_ts = now

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

    def _debug(self, msg: str):
        if DEBUG:
            self.notify(f"DEBUG {msg}", key=None, cooldown=0)

    def public_state(self):
        return {
            "day": self.day,
            "realized_pnl": round(self.realized_pnl, 4),
            "stopped_today": self.stopped_today,
            "trades_today": self.trades_today,
            "pos": self.pos,
            "dry_run": DRY_RUN,
        }

    # -----------------------------
    # daily stop loss
    # -----------------------------
    def _reset_day_if_needed(self):
        today = self._today_kst()
        if today != self.day:
            self.day = today
            self.realized_pnl = 0.0
            self.stopped_today = False
            self.trades_today = 0
            self.notify(f"🗓️ 일자 변경: {self.day} (손익/횟수 리셋)")

    def _daily_stop_hit(self):
        limit = -abs(START_EQUITY_USDC * DAILY_STOP_LOSS_PCT)
        if self.realized_pnl <= limit:
            if not self.stopped_today:
                self.stopped_today = True
                self.notify(
                    f"🛑 일 손절 발동: PnL={self.realized_pnl:.2f} USDC "
                    f"(기준 {START_EQUITY_USDC:.2f}의 -{DAILY_STOP_LOSS_PCT*100:.0f}%)"
                )
            return True
        return self.stopped_today

    # -----------------------------
    # client
    # -----------------------------
    def _init_client(self):
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("POLY_PRIVATE_KEY missing")

        self._debug(
            f"host={POLY_HOST} chain={POLY_CHAIN_ID} sig={POLY_SIGNATURE_TYPE} "
            f"key_len={len(POLY_PRIVATE_KEY)} key_0x={POLY_PRIVATE_KEY.startswith('0x')} "
            f"funder_len={(len(POLY_FUNDER) if POLY_FUNDER else 0)} funder_0x={(POLY_FUNDER.startswith('0x') if POLY_FUNDER else False)}"
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

    # -----------------------------
    # market data
    # -----------------------------
    def _fetch_markets(self):
        r = requests.get(f"{GAMMA}/markets", timeout=25)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def _best_levels(self, token_id: str):
        """
        returns (bid, bid_size, ask, ask_size, mid, spread, imbalance) or None
        imbalance: -1~+1 (bid size 우위면 +)
        """
        try:
            ob = self.client.get_order_book(token_id)
        except Exception:
            return None

        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return None

        try:
            bid = float(bids[0].get("price"))
            bid_size = float(bids[0].get("size") or bids[0].get("quantity") or 0)
            ask = float(asks[0].get("price"))
            ask_size = float(asks[0].get("size") or asks[0].get("quantity") or 0)
        except Exception:
            return None

        spread = max(0.0, ask - bid)
        mid = (ask + bid) / 2.0
        denom = (bid_size + ask_size)
        imbalance = (bid_size - ask_size) / denom if denom > 0 else 0.0

        return bid, bid_size, ask, ask_size, mid, spread, imbalance

    # -----------------------------
    # selection + YES/NO decision
    # -----------------------------
    def _candidates(self):
        markets = self._fetch_markets()

        cands = []
        for m in markets[:MAX_MARKETS]:
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

            if vol < MIN_VOL_24H:
                continue

            cands.append({
                "slug": slug,
                "question": q,
                "yes": str(token_ids[0]),
                "no": str(token_ids[1]),
                "vol": vol,
            })

        cands.sort(key=lambda x: x["vol"], reverse=True)
        return cands

    def _score_and_pick(self, cands):
        # 오더북까지 볼 상위 N개만
        eval_list = cands[:max(1, min(len(cands), TOPN_EVAL))]

        scored = []
        for m in eval_list:
            y = self._best_levels(m["yes"])
            n = self._best_levels(m["no"])
            if not y or not n:
                continue

            y_bid, y_bsz, y_ask, y_asz, y_mid, y_spread, y_imb = y
            n_bid, n_bsz, n_ask, n_asz, n_mid, n_spread, n_imb = n

            # 스프레드 필터 (둘 다 너무 벌어지면 제외)
            if y_spread > MAX_SPREAD and n_spread > MAX_SPREAD:
                continue

            # YES/NO 결정: imbalance 더 큰 쪽(매수 호가가 더 두꺼운 쪽) 선택
            if y_imb >= n_imb:
                side = "YES"
                token_id = m["yes"]
                bid, ask, mid, spread, imb = y_bid, y_ask, y_mid, y_spread, y_imb
            else:
                side = "NO"
                token_id = m["no"]
                bid, ask, mid, spread, imb = n_bid, n_ask, n_mid, n_spread, n_imb

            # 점수: 거래량 / (spread+eps) * (1+|imb|)
            score = (m["vol"] / max(spread, 1e-6)) * (1.0 + abs(imb))

            scored.append({
                **m,
                "score": score,
                "pick_side": side,
                "pick_token_id": token_id,
                "pick_bid": bid,
                "pick_ask": ask,
                "pick_mid": mid,
                "pick_spread": spread,
                "pick_imb": imb,
            })

        if not scored:
            return None

        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:max(1, min(len(scored), ROTATE_TOP_N))]

        if RANDOMIZE:
            return random.choice(top)

        self._rotate_idx = (self._rotate_idx + 1) % len(top)
        return top[self._rotate_idx]

    # -----------------------------
    # trading helpers
    # -----------------------------
    def _place_buy(self, token_id: str, ask_price: float):
        """
        BUY size(shares) 계산: ORDER_USDC / ask_price
        -> 지정가(ask)로 FOK (즉시 전량 체결 아니면 취소)
        """
        size = max(0.01, ORDER_USDC / max(ask_price, 1e-6))
        order = OrderArgs(token_id=token_id, price=float(ask_price), size=float(size), side=BUY)
        signed = self.client.create_order(order)
        resp = self.client.post_order(signed, OrderType.FOK)  # 시장가처럼 즉시 체결 목적  [oai_citation:1‡PyPI](https://pypi.org/project/py-clob-client/)
        return resp, size

    def _place_tp_sell(self, token_id: str, tp_price: float, size: float):
        """
        익절 매도: 지정가(tp_price)로 GTC 걸어두기
        """
        order = OrderArgs(token_id=token_id, price=float(tp_price), size=float(size), side=SELL)
        signed = self.client.create_order(order)
        resp = self.client.post_order(signed, OrderType.GTC)  # 오래 대기  [oai_citation:2‡PyPI](https://pypi.org/project/py-clob-client/)
        return resp

    def _cancel_order(self, order_id: str):
        try:
            self.client.cancel(order_id)
        except Exception:
            pass

    def _market_exit_sell(self, token_id: str, bid_price: float, size: float):
        """
        손절/시간초과 청산: 현재 bid(팔리는 가격)에 FOK로 즉시 청산 시도
        """
        # NOTE: 일부 케이스에서 "전량 매도"가 실패하는 이슈가 보고된 적이 있어
        #       안전하게 99%만 먼저 시도 (필요하면 나중에 남은 분량 재청산)  [oai_citation:3‡GitHub](https://github.com/Polymarket/py-clob-client/issues/265?utm_source=chatgpt.com)
        size_to_sell = max(0.01, size * 0.99)

        order = OrderArgs(token_id=token_id, price=float(bid_price), size=float(size_to_sell), side=SELL)
        signed = self.client.create_order(order)
        resp = self.client.post_order(signed, OrderType.FOK)
        return resp, size_to_sell

    # -----------------------------
    # position lifecycle
    # -----------------------------
    def _open_position(self, pick):
        token_id = pick["pick_token_id"]
        slug = pick["slug"]
        side = pick["pick_side"]
        ask = pick["pick_ask"]
        bid = pick["pick_bid"]
        mid = pick["pick_mid"]

        entry_price = float(ask)  # 보수적으로 ask를 엔트리로 잡음(실체결은 더 좋을 수도)
        tp_price = min(0.99, entry_price * (1.0 + TAKE_PROFIT_PCT))
        sl_price = max(0.01, entry_price * (1.0 - STOP_LOSS_PCT))

        if DRY_RUN:
            self.notify(
                "🧪 DRY_RUN: 진입 시뮬레이션\n"
                f"- slug: {slug}\n"
                f"- side: {side}\n"
                f"- entry(ask): {entry_price:.3f}\n"
                f"- TP: {tp_price:.3f} (+{TAKE_PROFIT_PCT*100:.1f}%)\n"
                f"- SL: {sl_price:.3f} (-{STOP_LOSS_PCT*100:.1f}%)\n"
                f"- hold_max: {MAX_HOLD_MINUTES}m\n"
                f"- order_usdc: {ORDER_USDC:.2f}\n"
            )
            # DRY_RUN에서도 포지션 상태를 만들어서 exit 로직 테스트 가능하게 함
            size = max(0.01, ORDER_USDC / max(entry_price, 1e-6))
            self.pos = {
                "slug": slug,
                "token_id": token_id,
                "side": side,
                "entry_price": entry_price,
                "size": size,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "tp_order_id": None,
                "opened_ts": self._now_ts(),
            }
            self.trades_today += 1
            return

        # LIVE
        buy_resp, size = self._place_buy(token_id, ask_price=ask)

        # TP 주문
        tp_resp = self._place_tp_sell(token_id, tp_price=tp_price, size=size)
        tp_order_id = tp_resp.get("orderID") or tp_resp.get("id") or tp_resp.get("order_id")

        self.pos = {
            "slug": slug,
            "token_id": token_id,
            "side": side,
            "entry_price": entry_price,
            "size": size,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_order_id": tp_order_id,
            "opened_ts": self._now_ts(),
        }
        self.trades_today += 1

        self.notify(
            "✅ 진입 완료\n"
            f"- slug: {slug}\n"
            f"- side: {side}\n"
            f"- entry(ask): {entry_price:.3f}\n"
            f"- size(shares): {size:.4f}\n"
            f"- TP 주문: {tp_price:.3f} (id={tp_order_id})\n"
            f"- SL 트리거: {sl_price:.3f}\n"
            f"- hold_max: {MAX_HOLD_MINUTES}m\n"
        )

    def _check_exit(self):
        if not self.pos:
            return

        token_id = self.pos["token_id"]
        entry = float(self.pos["entry_price"])
        size = float(self.pos["size"])
        tp_price = float(self.pos["tp_price"])
        sl_price = float(self.pos["sl_price"])
        opened_ts = int(self.pos["opened_ts"])
        tp_order_id = self.pos.get("tp_order_id")

        lv = self._best_levels(token_id)
        if not lv:
            return

        bid, bid_size, ask, ask_size, mid, spread, imb = lv

        # (A) 익절이 “이미 체결”되었는지 완벽히 확인하려면 체결/포지션 API를 더 붙여야 함.
        #     여기서는 단순화: mid가 tp 이상이면 TP 주문이 체결될 가능성이 크므로,
        #     LIVE에서는 TP 주문을 그대로 두고, 손절/시간초과만 적극 청산한다.
        #     (원하면 다음 단계에서 get_trades()/포지션 조회로 완전 자동화 가능)

        # (B) 손절 트리거
        stop_hit = (mid <= sl_price)

        # (C) 시간 초과 트리거
        time_hit = (self._now_ts() - opened_ts) >= (MAX_HOLD_MINUTES * 60)

        if not stop_hit and not time_hit:
            return

        reason = "STOP_LOSS" if stop_hit else "TIME_EXIT"
        self.notify(
            f"⚠️ 청산 트리거: {reason}\n"
            f"- mid={mid:.3f} (entry={entry:.3f})\n"
            f"- bid/ask={bid:.3f}/{ask:.3f} spread={spread:.3f}\n"
        )

        if DRY_RUN:
            # DRY_RUN에서 실현손익 가정(보수적으로 bid에 청산)
            exit_price = float(bid)
            pnl = (exit_price - entry) * size
            self.realized_pnl += pnl
            self.notify(f"🧪 DRY_RUN 청산 가정: exit@bid={exit_price:.3f} pnl≈{pnl:.3f} USDC")
            self.pos = None
            return

        # LIVE: TP 주문 취소 후 즉시 청산 시도
        if tp_order_id:
            self._cancel_order(tp_order_id)

        exit_resp, sold_size = self._market_exit_sell(token_id, bid_price=bid, size=size)

        # 보수적 PnL 추정(실체결은 더 좋을 수도/나쁠 수도)
        exit_price = float(bid)
        pnl = (exit_price - entry) * sold_size
        self.realized_pnl += pnl

        self.notify(
            "✅ 청산 시도 완료(FOK)\n"
            f"- exit@bid: {exit_price:.3f}\n"
            f"- sold_size: {sold_size:.4f}\n"
            f"- pnl≈ {pnl:.3f} USDC\n"
            f"- day_pnl≈ {self.realized_pnl:.3f} USDC\n"
        )

        # 포지션 종료(남은 잔량 처리까지 하려면 추가 로직 필요)
        self.pos = None

    # -----------------------------
    # main tick
    # -----------------------------
    def tick(self):
        self._tick += 1
        self._reset_day_if_needed()

        if self.client is None:
            self._init_client()

        # 하트비트
        if HEARTBEAT_EVERY > 0 and (self._tick % HEARTBEAT_EVERY == 0):
            self.notify(
                f"📡 heartbeat | day={self.day} pnl={self.realized_pnl:.2f} "
                f"| trades={self.trades_today}/{MAX_TRADES_PER_DAY} | pos={'Y' if self.pos else 'N'} | DRY_RUN={DRY_RUN}"
            )

        # 일손절
        if self._daily_stop_hit():
            return

        # 포지션 있으면 청산 조건만 체크
        if self.pos:
            self._check_exit()
            return

        # 포지션이 없으면 신규 진입 가능 여부 체크
        if self.trades_today >= MAX_TRADES_PER_DAY:
            return

        # 후보 선정
        cands = self._candidates()
        pick = self._score_and_pick(cands)

        if not pick:
            return

        # 신규 진입
        self._open_position(pick)
