# Polymarket BTC 5m Bot 🤖

**Hybrid Strategy** (Trend Following + Mean Reversion) | **Conservative Risk**

## Indicators dùng

|Indicator        |Loại          |Weight   |
|-----------------|--------------|---------|
|EMA 9/21 Cross   |Trend         |+2 điểm  |
|MACD             |Trend         |+1 điểm  |
|RSI              |Mean Reversion|+1~3 điểm|
|Bollinger Bands  |Mean Reversion|+1~2 điểm|
|5-candle Momentum|Trend         |+1 điểm  |

Signal chỉ trade khi **confidence ≥ 65%** (cấu hình được)

-----

## Railway Deploy

### Bước 1: Tạo Telegram Bot

1. Nhắn BotFather: `/newbot`
1. Lưu **TOKEN**
1. Lấy **User ID** của bạn: nhắn @userinfobot

### Bước 2: Lấy Polymarket API Keys

1. Vào <https://polymarket.com> → Connect Wallet
1. Vào Profile → API Keys → Create Key
1. Lưu: API Key, Secret, Passphrase
1. Lưu Proxy Wallet address

### Bước 3: Deploy Railway

1. Push code lên GitHub
1. Vào <https://railway.app> → New Project → Deploy from GitHub
1. Thêm **Environment Variables**:

```
TELEGRAM_TOKEN=xxx
TELEGRAM_USER_ID=123456789
POLY_API_KEY=xxx
POLY_API_SECRET=xxx
POLY_PASSPHRASE=xxx
POLY_PRIVATE_KEY=xxx
PROXY_WALLET=0x...
BET_AMOUNT=1.0
MIN_CONFIDENCE=65
MAX_DAILY_LOSS=10
```

1. Deploy → Done!

-----

## Telegram Commands

- `/start` - Menu chính
- `/signal` - Xem signal hiện tại
- `/stats` - Thống kê trades

## Nút bấm

- 📊 **Signal** - Phân tích BTC ngay
- 💰 **Balance** - Số dư USDC
- 🤖 **Auto ON/OFF** - Bật/tắt tự động trade
- 📈 **Trade thủ công** - Đặt lệnh theo signal
- 📉 **Stats** - Win rate, PnL
- 🔍 **Tìm Market** - Market BTC 5m còn active

-----

## Risk Management

- ✅ Bet cố định (mặc định $1/lệnh)
- ✅ Stop loss ngày ($10 mặc định)
- ✅ Cooldown 3 phút giữa các lệnh
- ✅ Min confidence 65% mới trade
- ✅ Auth: chỉ User ID của bạn được dùng