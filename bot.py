"""
Polymarket BTC 5m UP/DOWN Trading Bot
Strategy: Hybrid (Trend + Mean Reversion)
Risk: Conservative fixed bet
Deploy: Telegram + Railway
"""

import asyncio
import aiohttp
import hmac
import hashlib
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

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Config từ ENV ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID  = int(os.getenv("TELEGRAM_USER_ID", "0"))   # ID của bạn

# Polymarket CLOB API
POLY_API_KEY    = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")  # hex, no 0x

# Wallet
PROXY_WALLET   = os.getenv("PROXY_WALLET", "")        # địa chỉ ví proxy

# Trading config
BET_AMOUNT     = float(os.getenv("BET_AMOUNT", "1.0"))    # USDC mỗi lệnh
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "65"))  # % tối thiểu để vào lệnh
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "10"))  # USDC stop lỗ ngày
CLOB_HOST      = "https://clob.polymarket.com"
GAMMA_HOST     = "https://gamma-api.polymarket.com"

# ── BTC Price Cache ──────────────────────────────────────────────────────────
price_history: deque = deque(maxlen=50)   # giá BTC 1 phút gần nhất

# ── Bot State ────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.running      = False
        self.auto_trade   = False
        self.daily_pnl    = 0.0
        self.total_trades = 0
        self.wins         = 0
        self.losses       = 0
        self.active_market: Optional[dict] = None
        self.last_signal:   Optional[dict] = None
        self.last_trade_time = 0
        self.session: Optional[aiohttp.ClientSession] = None

state = BotState()

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 1: MARKET DATA
# ════════════════════════════════════════════════════════════════════════════

async def get_btc_price() -> Optional[float]:
    """Lấy giá BTC realtime từ Binance."""
    try:
        async with state.session.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status == 200:
                data = await r.json()
                return float(data["price"])
    except Exception as e:
        log.warning(f"Binance price error: {e}")
    return None


async def get_btc_klines(interval="1m", limit=20) -> list:
    """Lấy nến BTC từ Binance."""
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
    """Tìm market BTC 5m UP/DOWN đang active trên Polymarket."""
    try:
        async with state.session.get(
            f"{GAMMA_HOST}/markets",
            params={
                "active": "true",
                "tag": "crypto",
                "limit": 50
            },
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return None
            markets = await r.json()

        now = time.time()
        best = None
        for m in markets:
            slug = (m.get("slug", "") + m.get("question", "")).lower()
            if "bitcoin" not in slug and "btc" not in slug:
                continue
            if "5" not in slug or ("up" not in slug and "down" not in slug):
                continue
            # Ưu tiên market sắp hết hạn trong 2-5 phút
            end_ts = m.get("endDate") or m.get("end_date_iso")
            if not end_ts:
                continue
            try:
                from dateutil import parser as dparser
                end_dt = dparser.parse(str(end_ts)).timestamp()
                remaining = end_dt - now
                if 30 < remaining < 300:   # còn 30s - 5 phút
                    if best is None or remaining < (best["_remaining"]):
                        m["_remaining"] = remaining
                        best = m
            except Exception:
                continue
        return best
    except Exception as e:
        log.warning(f"Market fetch error: {e}")
        return None

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 2: HYBRID STRATEGY SIGNALS
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
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


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
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    macd_line = ema12 - ema26
    signal = compute_ema(closes[-9:], 9) if len(closes) >= 9 else macd_line
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
    - TREND: EMA9 vs EMA21, MACD
    - MEAN REVERSION: RSI + Bollinger Bands
    - Vote system: cộng điểm từng indicator
    """
    klines = await get_btc_klines(interval="1m", limit=30)
    if not klines:
        return {"direction": None, "confidence": 0, "reason": "Không lấy được dữ liệu"}

    closes = [float(k[4]) for k in klines]   # close price
    current = closes[-1]

    score_up = 0
    score_down = 0
    reasons = []

    # ── Trend: EMA Cross ─────────────────────────────────────────────
    ema9  = compute_ema(closes[-9:],  9)
    ema21 = compute_ema(closes[-21:], 21)
    if ema9 > ema21 * 1.0002:
        score_up += 2
        reasons.append("EMA9>EMA21 ↑")
    elif ema9 < ema21 * 0.9998:
        score_down += 2
        reasons.append("EMA9<EMA21 ↓")
    else:
        reasons.append("EMA cross: neutral")

    # ── Trend: MACD ──────────────────────────────────────────────────
    macd_line, macd_signal = compute_macd(closes)
    if macd_line > macd_signal:
        score_up += 1
        reasons.append("MACD bullish ↑")
    else:
        score_down += 1
        reasons.append("MACD bearish ↓")

    # ── Mean Reversion: RSI ──────────────────────────────────────────
    rsi = compute_rsi(closes)
    if rsi < 30:
        score_up += 3     # oversold → kỳ vọng tăng
        reasons.append(f"RSI={rsi:.1f} oversold ↑")
    elif rsi > 70:
        score_down += 3   # overbought → kỳ vọng giảm
        reasons.append(f"RSI={rsi:.1f} overbought ↓")
    elif rsi < 45:
        score_up += 1
        reasons.append(f"RSI={rsi:.1f} lean ↑")
    elif rsi > 55:
        score_down += 1
        reasons.append(f"RSI={rsi:.1f} lean ↓")

    # ── Mean Reversion: Bollinger ────────────────────────────────────
    bb_low, bb_mid, bb_high = compute_bollinger(closes)
    if bb_low and bb_high:
        if current < bb_low:
            score_up += 2
            reasons.append("Dưới BB lower ↑")
        elif current > bb_high:
            score_down += 2
            reasons.append("Trên BB upper ↓")
        elif current < bb_mid:
            score_up += 1
            reasons.append("Dưới BB mid ↑")
        else:
            score_down += 1
            reasons.append("Trên BB mid ↓")

    # ── Momentum: 5 nến gần nhất ─────────────────────────────────────
    recent_5 = closes[-5:]
    momentum = (recent_5[-1] - recent_5[0]) / recent_5[0] * 100
    if momentum > 0.05:
        score_up += 1
        reasons.append(f"Momentum +{momentum:.3f}% ↑")
    elif momentum < -0.05:
        score_down += 1
        reasons.append(f"Momentum {momentum:.3f}% ↓")

    # ── Tổng hợp ─────────────────────────────────────────────────────
    total = score_up + score_down
    if total == 0:
        return {"direction": None, "confidence": 0, "reason": "Không có tín hiệu"}

    if score_up > score_down:
        direction = "UP"
        confidence = (score_up / total) * 100
    elif score_down > score_up:
        direction = "DOWN"
        confidence = (score_down / total) * 100
    else:
        direction = None
        confidence = 50.0

    return {
        "direction": direction,
        "confidence": round(confidence, 1),
        "score_up": score_up,
        "score_down": score_down,
        "rsi": round(rsi, 1),
        "ema9": round(ema9, 1),
        "ema21": round(ema21, 1),
        "price": current,
        "reason": " | ".join(reasons),
        "bb_low": round(bb_low, 1) if bb_low else None,
        "bb_high": round(bb_high, 1) if bb_high else None,
    }

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 3: POLYMARKET ORDER PLACEMENT
# ════════════════════════════════════════════════════════════════════════════

def poly_sign_l1(timestamp: str, method: str, path: str, body: str = "") -> str:
    """Tạo chữ ký L1 cho CLOB API."""
    msg = timestamp + method.upper() + path + body
    return hmac.new(
        POLY_API_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()


def poly_headers(method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    sig = poly_sign_l1(ts, method, path, body)
    return {
        "Content-Type": "application/json",
        "POLY-API-KEY": POLY_API_KEY,
        "POLY-SIGNATURE": sig,
        "POLY-TIMESTAMP": ts,
        "POLY-PASSPHRASE": POLY_PASSPHRASE,
    }


async def get_market_orderbook(token_id: str) -> Optional[dict]:
    """Lấy orderbook để tính giá tốt nhất."""
    try:
        path = f"/book?token_id={token_id}"
        async with state.session.get(
            CLOB_HOST + path,
            headers=poly_headers("GET", path),
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        log.warning(f"Orderbook error: {e}")
    return None


async def place_market_order(token_id: str, side: str, amount_usdc: float) -> dict:
    """
    Đặt lệnh market order trên CLOB.
    side: 'BUY' hoặc 'SELL'
    """
    if not all([POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE, PROXY_WALLET]):
        return {"success": False, "error": "Chưa cấu hình API keys"}

    # Conservative: chỉ đặt lệnh nếu còn trong ngưỡng thua lỗ
    if state.daily_pnl <= -MAX_DAILY_LOSS:
        return {"success": False, "error": f"Đã đạt giới hạn thua lỗ ngày: ${MAX_DAILY_LOSS}"}

    try:
        body_dict = {
            "order": {
                "salt": int(time.time() * 1000),
                "maker": PROXY_WALLET,
                "signer": PROXY_WALLET,
                "taker": "0x0000000000000000000000000000000000000000",
                "tokenId": token_id,
                "makerAmount": str(int(amount_usdc * 1_000_000)),   # USDC 6 decimals
                "takerAmount": str(int(amount_usdc * 0.95 * 1_000_000)),  # min fill 95%
                "expiration": str(int(time.time()) + 300),           # 5 phút
                "nonce": "0",
                "feeRateBps": "0",
                "side": side,
                "signatureType": 0,
                "signature": "0x"   # TODO: thêm EIP-712 signing nếu cần
            },
            "owner": PROXY_WALLET,
            "orderType": "MKT"
        }
        body = json.dumps(body_dict)
        path = "/order"

        async with state.session.post(
            CLOB_HOST + path,
            headers=poly_headers("POST", path, body),
            data=body,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            resp = await r.json()
            if r.status == 200 and resp.get("success"):
                return {"success": True, "order_id": resp.get("orderID", ""), "data": resp}
            else:
                return {"success": False, "error": str(resp), "status": r.status}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def get_balance() -> float:
    """Lấy số dư USDC từ Polymarket."""
    try:
        path = f"/balance?owner={PROXY_WALLET}"
        async with state.session.get(
            CLOB_HOST + path,
            headers=poly_headers("GET", path),
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status == 200:
                data = await r.json()
                return float(data.get("balance", 0))
    except Exception as e:
        log.warning(f"Balance error: {e}")
    return 0.0

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 4: AUTO TRADING LOOP
# ════════════════════════════════════════════════════════════════════════════

async def auto_trade_loop(context: ContextTypes.DEFAULT_TYPE):
    """Chạy mỗi 30 giây: phân tích → signal → trade nếu đủ điều kiện."""
    if not state.auto_trade:
        return

    # Cooldown 3 phút giữa các lệnh
    if time.time() - state.last_trade_time < 180:
        return

    signal = await generate_signal()
    state.last_signal = signal

    if not signal["direction"] or signal["confidence"] < MIN_CONFIDENCE:
        return

    # Tìm market phù hợp
    market = await get_active_btc_market()
    if not market:
        return

    state.active_market = market
    direction = signal["direction"]

    # Lấy token ID của outcome UP hoặc DOWN
    outcomes = market.get("outcomes", [])
    token_id = None
    for o in outcomes:
        name = (o.get("name") or o.get("title") or "").upper()
        if direction in name:
            token_id = o.get("clobTokenId") or o.get("token_id")
            break

    if not token_id:
        log.warning("Không tìm được token_id")
        return

    result = await place_market_order(token_id, "BUY", BET_AMOUNT)
    state.last_trade_time = time.time()
    state.total_trades += 1

    emoji = "✅" if result["success"] else "❌"
    msg = (
        f"{emoji} *AUTO TRADE*\n"
        f"Signal: *{direction}* ({signal['confidence']}%)\n"
        f"Bet: ${BET_AMOUNT} USDC\n"
        f"RSI: {signal.get('rsi', 'N/A')} | Score: ↑{signal['score_up']} ↓{signal['score_down']}\n"
    )
    if result["success"]:
        msg += f"Order ID: `{result.get('order_id', 'N/A')}`"
    else:
        msg += f"Lỗi: {result.get('error', 'Unknown')}"

    try:
        await context.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=msg,
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ════════════════════════════════════════════════════════════════════════════
#  SECTION 5: TELEGRAM COMMANDS
# ════════════════════════════════════════════════════════════════════════════

def auth(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    kb = [
        [InlineKeyboardButton("📊 Signal", callback_data="signal"),
         InlineKeyboardButton("💰 Balance", callback_data="balance")],
        [InlineKeyboardButton("🤖 Auto ON", callback_data="auto_on"),
         InlineKeyboardButton("⏹ Auto OFF", callback_data="auto_off")],
        [InlineKeyboardButton("📈 Trade thủ công", callback_data="manual"),
         InlineKeyboardButton("📉 Stats", callback_data="stats")],
        [InlineKeyboardButton("🔍 Tìm Market", callback_data="find_market"),
         InlineKeyboardButton("⚙️ Config", callback_data="config")],
    ]
    await update.message.reply_text(
        "🤖 *Polymarket BTC 5m Bot*\n"
        "Hybrid Strategy | Conservative Risk\n\n"
        "Chọn chức năng:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    msg = await update.message.reply_text("⏳ Đang phân tích...")
    signal = await generate_signal()
    state.last_signal = signal

    d = signal.get("direction")
    c = signal.get("confidence", 0)
    arrow = "🟢 UP" if d == "UP" else ("🔴 DOWN" if d == "DOWN" else "⚪ NEUTRAL")
    verdict = "✅ ĐỦ ĐIỀU KIỆN VÀO LỆNH" if (d and c >= MIN_CONFIDENCE) else "⛔ Chưa đủ điều kiện"

    text = (
        f"📊 *BTC Signal*\n\n"
        f"Hướng: {arrow}\n"
        f"Confidence: *{c}%*\n"
        f"Score: ↑{signal.get('score_up',0)} | ↓{signal.get('score_down',0)}\n\n"
        f"RSI: `{signal.get('rsi','N/A')}`\n"
        f"EMA9: `{signal.get('ema9','N/A')}` | EMA21: `{signal.get('ema21','N/A')}`\n"
        f"BTC: `${signal.get('price','N/A'):,.0f}`\n\n"
        f"Indicators:\n`{signal.get('reason','N/A')}`\n\n"
        f"{verdict}"
    )
    await msg.edit_text(text, parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    wr = (state.wins / state.total_trades * 100) if state.total_trades > 0 else 0
    pnl_emoji = "🟢" if state.daily_pnl >= 0 else "🔴"
    await update.message.reply_text(
        f"📈 *Trading Stats*\n\n"
        f"Tổng lệnh: {state.total_trades}\n"
        f"Thắng: {state.wins} | Thua: {state.losses}\n"
        f"Win Rate: {wr:.1f}%\n"
        f"{pnl_emoji} PnL hôm nay: ${state.daily_pnl:.2f}\n"
        f"Auto Trade: {'🟢 ON' if state.auto_trade else '🔴 OFF'}\n"
        f"Bet/lệnh: ${BET_AMOUNT}",
        parse_mode="Markdown"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID:
        return

    data = query.data

    if data == "signal":
        signal = await generate_signal()
        state.last_signal = signal
        d = signal.get("direction")
        c = signal.get("confidence", 0)
        arrow = "🟢 UP" if d == "UP" else ("🔴 DOWN" if d == "DOWN" else "⚪ NEUTRAL")
        verdict = "✅ Đủ điều kiện" if (d and c >= MIN_CONFIDENCE) else "⛔ Chưa đủ"
        await query.edit_message_text(
            f"📊 *Signal*: {arrow} | {c}%\n"
            f"RSI: {signal.get('rsi')} | Score ↑{signal.get('score_up')} ↓{signal.get('score_down')}\n"
            f"BTC: ${signal.get('price'):,.0f}\n"
            f"{verdict}\n\n`{signal.get('reason','')}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="signal"),
                InlineKeyboardButton("🏠 Menu", callback_data="menu")
            ]])
        )

    elif data == "balance":
        bal = await get_balance()
        await query.edit_message_text(
            f"💰 *Balance*\n\nUSDC: ${bal:.2f}\nPnL hôm nay: ${state.daily_pnl:.2f}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    elif data == "auto_on":
        state.auto_trade = True
        await query.edit_message_text(
            f"🤖 *Auto Trade: BẬT*\n\nBot sẽ tự trade khi confidence ≥ {MIN_CONFIDENCE}%\n"
            f"Bet mỗi lệnh: ${BET_AMOUNT}\nMax thua ngày: ${MAX_DAILY_LOSS}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏹ Tắt Auto", callback_data="auto_off"),
                InlineKeyboardButton("🏠 Menu", callback_data="menu")
            ]])
        )

    elif data == "auto_off":
        state.auto_trade = False
        await query.edit_message_text(
            "⏹ *Auto Trade: TẮT*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    elif data == "manual":
        if not state.last_signal:
            signal = await generate_signal()
            state.last_signal = signal
        sig = state.last_signal
        d = sig.get("direction")
        c = sig.get("confidence", 0)
        if not d or c < MIN_CONFIDENCE:
            await query.edit_message_text(
                f"⛔ Signal hiện tại chưa đủ mạnh ({c}% < {MIN_CONFIDENCE}%)\nHãy chờ tín hiệu tốt hơn.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )
            return
        await query.edit_message_text(
            f"📈 *Trade thủ công*\n\nSignal: {'🟢 UP' if d=='UP' else '🔴 DOWN'} | {c}%\nBet: ${BET_AMOUNT}\n\nXác nhận đặt lệnh?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ {'UP' if d=='UP' else 'DOWN'}", callback_data=f"trade_{d}"),
                 InlineKeyboardButton("❌ Huỷ", callback_data="menu")]
            ])
        )

    elif data.startswith("trade_"):
        direction = data.split("_")[1]
        market = await get_active_btc_market()
        if not market:
            await query.edit_message_text("❌ Không tìm thấy market đang active")
            return
        outcomes = market.get("outcomes", [])
        token_id = None
        for o in outcomes:
            name = (o.get("name") or o.get("title") or "").upper()
            if direction in name:
                token_id = o.get("clobTokenId") or o.get("token_id")
                break
        if not token_id:
            await query.edit_message_text("❌ Không tìm được token ID")
            return
        result = await place_market_order(token_id, "BUY", BET_AMOUNT)
        state.total_trades += 1
        state.last_trade_time = time.time()
        if result["success"]:
            await query.edit_message_text(
                f"✅ *Lệnh đặt thành công!*\n{direction} ${BET_AMOUNT}\nOrder: `{result.get('order_id')}`",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"❌ *Lỗi đặt lệnh*\n`{result.get('error', 'Unknown error')}`",
                parse_mode="Markdown"
            )

    elif data == "stats":
        wr = (state.wins / state.total_trades * 100) if state.total_trades > 0 else 0
        await query.edit_message_text(
            f"📈 Lệnh: {state.total_trades} | W:{state.wins} L:{state.losses} | WR:{wr:.1f}%\n"
            f"PnL: ${state.daily_pnl:.2f} | Auto: {'ON' if state.auto_trade else 'OFF'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    elif data == "find_market":
        m = await get_active_btc_market()
        if m:
            state.active_market = m
            remaining = int(m.get("_remaining", 0))
            await query.edit_message_text(
                f"🔍 *Market Found*\n\n{m.get('question','N/A')}\nCòn: {remaining}s",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )
        else:
            await query.edit_message_text(
                "❌ Không tìm thấy BTC 5m market nào đang active",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )

    elif data == "config":
        await query.edit_message_text(
            f"⚙️ *Config hiện tại*\n\n"
            f"Bet/lệnh: ${BET_AMOUNT}\n"
            f"Min confidence: {MIN_CONFIDENCE}%\n"
            f"Max thua ngày: ${MAX_DAILY_LOSS}\n"
            f"Strategy: Hybrid (Trend + MR)\n"
            f"Indicators: EMA9/21, MACD, RSI, BB",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
        )

    elif data == "menu":
        kb = [
            [InlineKeyboardButton("📊 Signal", callback_data="signal"),
             InlineKeyboardButton("💰 Balance", callback_data="balance")],
            [InlineKeyboardButton("🤖 Auto ON", callback_data="auto_on"),
             InlineKeyboardButton("⏹ Auto OFF", callback_data="auto_off")],
            [InlineKeyboardButton("📈 Trade thủ công", callback_data="manual"),
             InlineKeyboardButton("📉 Stats", callback_data="stats")],
            [InlineKeyboardButton("🔍 Tìm Market", callback_data="find_market"),
             InlineKeyboardButton("⚙️ Config", callback_data="config")],
        ]
        await query.edit_message_text(
            "🤖 *Polymarket BTC 5m Bot*\nHybrid Strategy | Conservative Risk",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )


# ════════════════════════════════════════════════════════════════════════════
#  SECTION 6: MAIN
# ════════════════════════════════════════════════════════════════════════════

async def post_init(application: Application):
    """Khởi tạo aiohttp session."""
    state.session = aiohttp.ClientSession()
    log.info("Bot khởi động xong ✅")


async def post_shutdown(application: Application):
    if state.session:
        await state.session.close()


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("Chưa set TELEGRAM_TOKEN")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Auto trade job mỗi 30 giây
    app.job_queue.run_repeating(auto_trade_loop, interval=30, first=10)

    log.info("🚀 Bot đang chạy...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
