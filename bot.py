"""
Polymarket BTC 5m UP/DOWN Trading Bot v2
- pUSD collateral (April 2026 upgrade)
- Auto derive API credentials từ private key
- Proxy wallet + gasless relayer
- Hybrid Strategy: Trend + Mean Reversion
- Conservative Risk Management
"""

import asyncio
import aiohttp
import json
import logging
import os
import time
import math
from datetime import datetime, timezone
from collections import deque
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# py-clob-client (Polymarket official SDK)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, Side

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Config từ ENV ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID  = int(os.getenv("TELEGRAM_USER_ID", "0"))

# Chỉ cần 2 thứ này — bot tự lấy phần còn lại
PRIVATE_KEY      = os.getenv("PRIVATE_KEY", "")   # EOA private key (hex, không có 0x)
PROXY_WALLET     = os.getenv("PROXY_WALLET", "")  # Địa chỉ proxy wallet 0x...

# Trading config
BET_AMOUNT       = float(os.getenv("BET_AMOUNT", "1.0"))      # pUSD mỗi lệnh
MIN_CONFIDENCE   = float(os.getenv("MIN_CONFIDENCE", "65"))   # % tối thiểu vào lệnh
MAX_DAILY_LOSS   = float(os.getenv("MAX_DAILY_LOSS", "10"))   # pUSD stop loss ngày

CLOB_HOST        = "https://clob.polymarket.com"
GAMMA_HOST       = "https://gamma-api.polymarket.com"
CHAIN_ID         = 137   # Polygon mainnet

# ── Bot State ─────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.running         = False
        self.auto_trade      = False
        self.daily_pnl       = 0.0
        self.total_trades    = 0
        self.wins            = 0
        self.losses          = 0
        self.active_market   = None
        self.last_signal     = None
        self.last_trade_time = 0
        self.session         = None
        self.clob_client     = None   # py-clob-client instance
        self.api_ready       = False

state = BotState()

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 1: AUTO INIT CLOB CLIENT (tự derive API key)
# ════════════════════════════════════════════════════════════════════════════

def init_clob_client() -> bool:
    """
    Tự động derive API credentials từ private key.
    Dùng signature_type=1 cho proxy wallet (email/Magic style).
    """
    if not PRIVATE_KEY or not PROXY_WALLET:
        log.error("Thiếu PRIVATE_KEY hoặc PROXY_WALLET")
        return False
    try:
        # Tạo client với private key — dùng signature_type=1 cho proxy wallet
        client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=1,   # proxy wallet
            funder=PROXY_WALLET
        )
        # Tự derive hoặc tạo API credentials (chỉ cần gọi 1 lần)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        state.clob_client = client
        state.api_ready = True
        log.info(f"✅ API creds derived: {creds.api_key[:8]}...")
        return True
    except Exception as e:
        log.error(f"❌ init_clob_client error: {e}")
        return False

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 2: MARKET DATA
# ════════════════════════════════════════════════════════════════════════════

async def get_btc_klines(interval="1m", limit=30) -> list:
    try:
        async with state.session.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        log.warning(f"Klines error: {e}")
    return []


async def get_active_btc_market() -> Optional[dict]:
    """Tìm BTC 5m UP/DOWN market đang active, còn 30s-5 phút."""
    try:
        async with state.session.get(
            f"{GAMMA_HOST}/markets",
            params={"active": "true", "tag": "crypto", "limit": 100},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return None
            markets = await r.json()

        now = time.time()
        best = None
        for m in markets:
            text = (m.get("slug", "") + m.get("question", "")).lower()
            if ("bitcoin" not in text and "btc" not in text):
                continue
            if "5" not in text:
                continue
            if "up" not in text and "down" not in text:
                continue

            end_ts = m.get("endDate") or m.get("end_date_iso")
            if not end_ts:
                continue
            try:
                from dateutil import parser as dp
                end_dt = dp.parse(str(end_ts)).timestamp()
                remaining = end_dt - now
                if 30 < remaining < 300:
                    if best is None or remaining < best["_remaining"]:
                        m["_remaining"] = remaining
                        best = m
            except Exception:
                continue
        return best
    except Exception as e:
        log.warning(f"Market fetch error: {e}")
        return None


async def get_pusd_balance() -> float:
    """Lấy số dư pUSD từ CLOB API."""
    try:
        if state.clob_client:
            bal = state.clob_client.get_balance()
            return float(bal) if bal else 0.0
    except Exception as e:
        log.warning(f"Balance error: {e}")
    return 0.0

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 3: HYBRID SIGNAL ENGINE
# ════════════════════════════════════════════════════════════════════════════

def compute_rsi(closes: list, period=14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def compute_ema(closes: list, period: int) -> float:
    if not closes:
        return 0.0
    k = 2 / (period + 1)
    ema = closes[0]
    for p in closes[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def compute_macd(closes: list):
    if len(closes) < 26:
        return 0, 0
    ema12 = compute_ema(closes[-12:], 12)
    ema26 = compute_ema(closes[-26:], 26)
    macd_line = ema12 - ema26
    signal = compute_ema([macd_line] * 9, 9)
    return macd_line, signal


def compute_bollinger(closes: list, period=20):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((x - mid)**2 for x in window) / period)
    return mid - 2*std, mid, mid + 2*std


async def generate_signal() -> dict:
    """
    Hybrid Signal:
    TREND     → EMA cross, MACD, momentum
    MEAN REV  → RSI, Bollinger Bands
    Vote system: cộng điểm, confidence = score_winner / total
    """
    klines = await get_btc_klines(interval="1m", limit=30)
    if not klines:
        return {"direction": None, "confidence": 0, "reason": "Không lấy được data"}

    closes = [float(k[4]) for k in klines]
    current = closes[-1]

    score_up = 0
    score_dn = 0
    reasons  = []

    # ── EMA Cross ───────────────────────────────────────────────────
    ema9  = compute_ema(closes[-9:], 9)
    ema21 = compute_ema(closes, 21)
    if ema9 > ema21 * 1.0002:
        score_up += 2; reasons.append("EMA↑")
    elif ema9 < ema21 * 0.9998:
        score_dn += 2; reasons.append("EMA↓")

    # ── MACD ────────────────────────────────────────────────────────
    macd_l, macd_s = compute_macd(closes)
    if macd_l > macd_s:
        score_up += 1; reasons.append("MACD↑")
    else:
        score_dn += 1; reasons.append("MACD↓")

    # ── RSI ─────────────────────────────────────────────────────────
    rsi = compute_rsi(closes)
    if rsi < 30:
        score_up += 3; reasons.append(f"RSI={rsi:.0f}oversold↑")
    elif rsi > 70:
        score_dn += 3; reasons.append(f"RSI={rsi:.0f}overbought↓")
    elif rsi < 45:
        score_up += 1; reasons.append(f"RSI={rsi:.0f}↑")
    elif rsi > 55:
        score_dn += 1; reasons.append(f"RSI={rsi:.0f}↓")

    # ── Bollinger ────────────────────────────────────────────────────
    bb_l, bb_m, bb_h = compute_bollinger(closes)
    if bb_l and bb_h:
        if current < bb_l:
            score_up += 2; reasons.append("BB_low↑")
        elif current > bb_h:
            score_dn += 2; reasons.append("BB_high↓")
        elif current < bb_m:
            score_up += 1; reasons.append("BB_mid↑")
        else:
            score_dn += 1; reasons.append("BB_mid↓")

    # ── 5-nến Momentum ───────────────────────────────────────────────
    mom = (closes[-1] - closes[-5]) / closes[-5] * 100
    if mom > 0.05:
        score_up += 1; reasons.append(f"Mom+{mom:.2f}%↑")
    elif mom < -0.05:
        score_dn += 1; reasons.append(f"Mom{mom:.2f}%↓")

    total = score_up + score_dn
    if total == 0:
        return {"direction": None, "confidence": 0, "reason": "Neutral"}

    if score_up > score_dn:
        direction  = "UP"
        confidence = round(score_up / total * 100, 1)
    elif score_dn > score_up:
        direction  = "DOWN"
        confidence = round(score_dn / total * 100, 1)
    else:
        direction  = None
        confidence = 50.0

    return {
        "direction":  direction,
        "confidence": confidence,
        "score_up":   score_up,
        "score_dn":   score_dn,
        "rsi":        round(rsi, 1),
        "ema9":       round(ema9, 1),
        "ema21":      round(ema21, 1),
        "price":      current,
        "reason":     " | ".join(reasons),
        "bb_low":     round(bb_l, 0) if bb_l else None,
        "bb_high":    round(bb_h, 0) if bb_h else None,
    }

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 4: ORDER PLACEMENT (dùng py-clob-client + pUSD)
# ════════════════════════════════════════════════════════════════════════════

async def place_order(token_id: str, direction: str, amount_pusd: float) -> dict:
    """
    Đặt market order bằng py-clob-client.
    Polymarket dùng pUSD (USDC-backed) — gasless qua relayer.
    """
    if not state.api_ready or not state.clob_client:
        return {"success": False, "error": "API chưa sẵn sàng"}

    if state.daily_pnl <= -MAX_DAILY_LOSS:
        return {"success": False, "error": f"Đã đạt stop loss ngày: ${MAX_DAILY_LOSS}"}

    try:
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_pusd,   # pUSD amount
        )
        # Chạy trong thread riêng vì py-clob-client dùng sync
        loop = asyncio.get_event_loop()
        signed_order = await loop.run_in_executor(
            None,
            lambda: state.clob_client.create_market_order(order_args)
        )
        resp = await loop.run_in_executor(
            None,
            lambda: state.clob_client.post_order(signed_order, OrderType.FOK)
        )
        order_id = resp.get("orderID") or resp.get("id", "")
        success   = bool(order_id) or resp.get("success", False)
        return {"success": success, "order_id": order_id, "data": resp}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 5: AUTO TRADE LOOP
# ════════════════════════════════════════════════════════════════════════════

async def auto_trade_loop(context: ContextTypes.DEFAULT_TYPE):
    """Mỗi 30s: phân tích → signal → trade nếu đủ điều kiện."""
    if not state.auto_trade or not state.api_ready:
        return

    # Cooldown 3 phút
    if time.time() - state.last_trade_time < 180:
        return

    signal = await generate_signal()
    state.last_signal = signal

    if not signal["direction"] or signal["confidence"] < MIN_CONFIDENCE:
        return

    market = await get_active_btc_market()
    if not market:
        return

    state.active_market = market
    direction = signal["direction"]

    # Tìm token ID cho UP/DOWN
    token_id = None
    for o in market.get("outcomes", []):
        name = (o.get("name") or o.get("title") or "").upper()
        if direction in name:
            token_id = o.get("clobTokenId") or o.get("token_id")
            break

    if not token_id:
        return

    result = await place_order(token_id, direction, BET_AMOUNT)
    state.last_trade_time = time.time()
    state.total_trades += 1

    emoji = "✅" if result["success"] else "❌"
    msg = (
        f"{emoji} *AUTO TRADE*\n"
        f"{'🟢 UP' if direction=='UP' else '🔴 DOWN'} | {signal['confidence']}%\n"
        f"Bet: *${BET_AMOUNT} pUSD*\n"
        f"RSI: {signal.get('rsi')} | Score: ↑{signal['score_up']} ↓{signal['score_dn']}\n"
        f"BTC: ${signal.get('price'):,.0f}\n"
    )
    msg += f"`{result.get('order_id','')}`" if result["success"] else f"Lỗi: `{result.get('error','?')}`"

    try:
        await context.bot.send_message(ALLOWED_USER_ID, msg, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 6: TELEGRAM HANDLERS
# ════════════════════════════════════════════════════════════════════════════

def auth(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Signal", callback_data="signal"),
         InlineKeyboardButton("💰 Balance", callback_data="balance")],
        [InlineKeyboardButton("🤖 Auto ON", callback_data="auto_on"),
         InlineKeyboardButton("⏹ Auto OFF", callback_data="auto_off")],
        [InlineKeyboardButton("📈 Trade thủ công", callback_data="manual"),
         InlineKeyboardButton("📉 Stats", callback_data="stats")],
        [InlineKeyboardButton("🔍 Tìm Market", callback_data="find_market"),
         InlineKeyboardButton("⚙️ Status", callback_data="status")],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    api_status = "✅ API Ready" if state.api_ready else "❌ API chưa kết nối"
    await update.message.reply_text(
        f"🤖 *Polymarket BTC 5m Bot v2*\n"
        f"Hybrid Strategy | pUSD | Gasless\n"
        f"{api_status}\n\n"
        f"Chọn chức năng:",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    msg = await update.message.reply_text("⏳ Đang phân tích BTC...")
    signal = await generate_signal()
    state.last_signal = signal
    d = signal.get("direction")
    c = signal.get("confidence", 0)
    arrow = "🟢 UP" if d == "UP" else ("🔴 DOWN" if d == "DOWN" else "⚪ NEUTRAL")
    verdict = "✅ Đủ điều kiện vào lệnh" if (d and c >= MIN_CONFIDENCE) else f"⛔ Chưa đủ ({c}% < {MIN_CONFIDENCE}%)"
    await msg.edit_text(
        f"📊 *BTC Signal*\n\n"
        f"{arrow} | *{c}%*\n"
        f"Score: ↑{signal.get('score_up',0)} ↓{signal.get('score_dn',0)}\n\n"
        f"RSI: `{signal.get('rsi')}`\n"
        f"EMA9/21: `{signal.get('ema9')} / {signal.get('ema21')}`\n"
        f"BTC: `${signal.get('price'):,.0f}`\n"
        f"BB: `{signal.get('bb_low')} – {signal.get('bb_high')}`\n\n"
        f"`{signal.get('reason','')}`\n\n"
        f"{verdict}",
        parse_mode="Markdown"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ALLOWED_USER_ID:
        return
    data = q.data

    # ── Signal ──────────────────────────────────────────────────────
    if data == "signal":
        sig = await generate_signal()
        state.last_signal = sig
        d = sig.get("direction")
        c = sig.get("confidence", 0)
        arrow = "🟢 UP" if d == "UP" else ("🔴 DOWN" if d == "DOWN" else "⚪ NEUTRAL")
        verdict = "✅ Đủ điều kiện" if (d and c >= MIN_CONFIDENCE) else "⛔ Chưa đủ"
        await q.edit_message_text(
            f"📊 {arrow} | *{c}%*\n"
            f"RSI:{sig.get('rsi')} | ↑{sig.get('score_up')} ↓{sig.get('score_dn')}\n"
            f"BTC: ${sig.get('price'):,.0f}\n"
            f"`{sig.get('reason','')}`\n{verdict}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="signal"),
                InlineKeyboardButton("🏠 Menu", callback_data="menu")
            ]])
        )

    # ── Balance ──────────────────────────────────────────────────────
    elif data == "balance":
        bal = await get_pusd_balance()
        pnl_e = "🟢" if state.daily_pnl >= 0 else "🔴"
        await q.edit_message_text(
            f"💰 *Balance*\n\npUSD: `${bal:.2f}`\n{pnl_e} PnL hôm nay: `${state.daily_pnl:.2f}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    # ── Auto ON/OFF ───────────────────────────────────────────────────
    elif data == "auto_on":
        if not state.api_ready:
            await q.edit_message_text(
                "❌ API chưa sẵn sàng. Kiểm tra PRIVATE_KEY và PROXY_WALLET.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )
            return
        state.auto_trade = True
        await q.edit_message_text(
            f"🤖 *Auto Trade: BẬT*\n\n"
            f"Confidence tối thiểu: {MIN_CONFIDENCE}%\n"
            f"Bet/lệnh: ${BET_AMOUNT} pUSD\n"
            f"Stop loss ngày: ${MAX_DAILY_LOSS}\n"
            f"Cooldown: 3 phút/lệnh",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏹ Tắt", callback_data="auto_off"),
                InlineKeyboardButton("🏠 Menu", callback_data="menu")
            ]])
        )

    elif data == "auto_off":
        state.auto_trade = False
        await q.edit_message_text(
            "⏹ *Auto Trade: TẮT*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    # ── Manual Trade ─────────────────────────────────────────────────
    elif data == "manual":
        if not state.api_ready:
            await q.edit_message_text("❌ API chưa sẵn sàng")
            return
        if not state.last_signal:
            state.last_signal = await generate_signal()
        sig = state.last_signal
        d = sig.get("direction")
        c = sig.get("confidence", 0)
        if not d or c < MIN_CONFIDENCE:
            await q.edit_message_text(
                f"⛔ Signal yếu ({c}% < {MIN_CONFIDENCE}%)\nHãy đợi tín hiệu tốt hơn.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Check lại", callback_data="signal"),
                     InlineKeyboardButton("🏠 Menu", callback_data="menu")]
                ])
            )
            return
        arrow = "🟢 UP" if d == "UP" else "🔴 DOWN"
        await q.edit_message_text(
            f"📈 *Trade thủ công*\n\n{arrow} | {c}%\nBet: ${BET_AMOUNT} pUSD\n\nXác nhận?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ {d}", callback_data=f"trade_{d}"),
                InlineKeyboardButton("❌ Huỷ", callback_data="menu")
            ]])
        )

    # ── Execute Trade ─────────────────────────────────────────────────
    elif data.startswith("trade_"):
        direction = data.split("_")[1]
        await q.edit_message_text("⏳ Đang tìm market và đặt lệnh...")
        market = await get_active_btc_market()
        if not market:
            await q.edit_message_text(
                "❌ Không tìm thấy BTC 5m market đang active",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )
            return
        token_id = None
        for o in market.get("outcomes", []):
            name = (o.get("name") or o.get("title") or "").upper()
            if direction in name:
                token_id = o.get("clobTokenId") or o.get("token_id")
                break
        if not token_id:
            await q.edit_message_text(
                "❌ Không tìm được token ID cho outcome này",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )
            return
        result = await place_order(token_id, direction, BET_AMOUNT)
        state.total_trades += 1
        state.last_trade_time = time.time()
        if result["success"]:
            await q.edit_message_text(
                f"✅ *Lệnh thành công!*\n{'🟢 UP' if direction=='UP' else '🔴 DOWN'} | ${BET_AMOUNT} pUSD\n`{result.get('order_id','')}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )
        else:
            await q.edit_message_text(
                f"❌ *Lỗi*: `{result.get('error','Unknown')}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )

    # ── Stats ─────────────────────────────────────────────────────────
    elif data == "stats":
        wr = (state.wins / state.total_trades * 100) if state.total_trades > 0 else 0
        pnl_e = "🟢" if state.daily_pnl >= 0 else "🔴"
        await q.edit_message_text(
            f"📉 *Stats*\n\n"
            f"Tổng lệnh: {state.total_trades}\n"
            f"Thắng/Thua: {state.wins}/{state.losses}\n"
            f"Win Rate: {wr:.1f}%\n"
            f"{pnl_e} PnL hôm nay: ${state.daily_pnl:.2f}\n"
            f"Auto: {'🟢 ON' if state.auto_trade else '🔴 OFF'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    # ── Find Market ───────────────────────────────────────────────────
    elif data == "find_market":
        await q.edit_message_text("⏳ Đang tìm market...")
        m = await get_active_btc_market()
        if m:
            remaining = int(m.get("_remaining", 0))
            outcomes = m.get("outcomes", [])
            out_text = " | ".join([o.get("name", "") for o in outcomes])
            await q.edit_message_text(
                f"🔍 *Market Found*\n\n"
                f"{m.get('question','N/A')}\n"
                f"Outcomes: {out_text}\n"
                f"Còn: *{remaining}s*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 Signal", callback_data="signal"),
                    InlineKeyboardButton("🏠 Menu", callback_data="menu")
                ]])
            )
        else:
            await q.edit_message_text(
                "❌ Không tìm thấy BTC 5m market nào (còn 30s–5 phút)",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )

    # ── Status ────────────────────────────────────────────────────────
    elif data == "status":
        api_s = "✅ Ready" if state.api_ready else "❌ Chưa kết nối"
        await q.edit_message_text(
            f"⚙️ *System Status*\n\n"
            f"API CLOB: {api_s}\n"
            f"Collateral: pUSD (USDC-backed)\n"
            f"Chain: Polygon (gasless relayer)\n"
            f"Strategy: Hybrid EMA+MACD+RSI+BB\n"
            f"Bet/lệnh: ${BET_AMOUNT} pUSD\n"
            f"Min confidence: {MIN_CONFIDENCE}%\n"
            f"Stop loss/ngày: ${MAX_DAILY_LOSS}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    # ── Menu ──────────────────────────────────────────────────────────
    elif data == "menu":
        api_s = "✅" if state.api_ready else "❌"
        await q.edit_message_text(
            f"🤖 *Polymarket BTC 5m Bot v2*\nAPI: {api_s} | Auto: {'🟢' if state.auto_trade else '🔴'}",
            reply_markup=main_keyboard(),
            parse_mode="Markdown"
        )

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 7: MAIN
# ════════════════════════════════════════════════════════════════════════════

async def post_init(application: Application):
    state.session = aiohttp.ClientSession()
    # Init CLOB client trong thread riêng
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, init_clob_client)
    if success:
        log.info("✅ Polymarket CLOB client ready")
        try:
            await application.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text="🤖 *Bot khởi động thành công!*\n✅ API kết nối OK\n💰 Dùng pUSD (gasless)\n\nGõ /start để bắt đầu",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    else:
        log.error("❌ CLOB client init failed — kiểm tra PRIVATE_KEY và PROXY_WALLET")


async def post_shutdown(application: Application):
    if state.session:
        await state.session.close()


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("❌ Chưa set TELEGRAM_TOKEN")
    if not PRIVATE_KEY:
        raise ValueError("❌ Chưa set PRIVATE_KEY")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Auto trade mỗi 30 giây
    app.job_queue.run_repeating(auto_trade_loop, interval=30, first=15)

    log.info("🚀 Bot đang chạy...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
