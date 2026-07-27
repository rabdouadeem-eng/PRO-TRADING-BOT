"""
signal_server.py
=================
Flask API يكشف إشارات التداول (buy/sell/hold) لـ PRO-TRADING-BOT.
مخصص باش يستهلكه فرونت-إند خارجي (مثل Sandoq) عبر polling.

+ Webhook Telegram (/telegram/webhook) باش البوت يرد على /start و أوامر أخرى.

لا ينفذ أي صفقة حقيقية — فقط يحلل ويرجع القرار + الثقة + الأسباب.
"""

import os
import time
import threading
import requests
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

from strategies.technical_indicators import TechnicalIndicators
from strategies.bottom_top_detector import BottomTopDetector
from ai_teacher.trading_mentor import TradingMentor
from utils.logger import setup_logger

logger = setup_logger("signal_server")

# 🔑 Binance.com كيبلوكي IP ديال Render (451 geo-restriction) — نفس المشكل اللي طاح فيه
# Trading-Bot- قبل. نستعملو MEXC (بيانات عامة فقط، قراءة، بلا أوامر) بحال ما درنا هناك.
MEXC_KLINES_URL = "https://api.mexc.com/api/v3/klines"


def get_klines_mexc(symbol: str, interval: str = "1m", limit: int = 100):
    """يجيب شموع من MEXC (بلا geo-block) ويرجعها كـ DataFrame بنفس شكل BinanceConnector.get_klines"""
    try:
        resp = requests.get(
            MEXC_KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            return None

        df = pd.DataFrame(raw, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logger.error(f"❌ خطأ فـ جلب بيانات MEXC لـ {symbol}: {e}")
        return None


# 💱 Forex + Gold/Silver — عبر Twelve Data (مجاني، 800 طلب/يوم)
# المفتاح: https://twelvedata.com/apikey (تسجيل مجاني)
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"

# Symbol داخلي (بلا سلاش) → Symbol لي كيفهمو Twelve Data (بسلاش)
FOREX_SYMBOL_MAP = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "XAUUSD": "XAU/USD",
    "XAGUSD": "XAG/USD",
}
FOREX_SYMBOLS = list(FOREX_SYMBOL_MAP.keys())


def get_klines_forex(symbol: str, interval: str = "5min", limit: int = 100):
    """يجيب شموع فوركس/ذهب/فضة من Twelve Data ويرجعها بنفس شكل get_klines_mexc."""
    if not TWELVEDATA_API_KEY:
        logger.warning("⚠️ TWELVEDATA_API_KEY غير موجود — الفوركس معطل")
        return None
    td_symbol = FOREX_SYMBOL_MAP.get(symbol)
    if not td_symbol:
        return None
    try:
        resp = requests.get(
            TWELVEDATA_URL,
            params={
                "symbol": td_symbol,
                "interval": interval,
                "outputsize": limit,
                "apikey": TWELVEDATA_API_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        values = raw.get("values")
        if not values:
            logger.error(f"❌ رد Twelve Data بلا بيانات لـ {symbol}: {raw}")
            return None

        df = pd.DataFrame(values)
        df = df.rename(columns={"datetime": "timestamp"})
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df["volume"] = df.get("volume", 0)
        # Twelve Data كيرجع الأحدث فالأول — نقلبو الترتيب باش يكون الأقدم فالأول
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"❌ خطأ فـ جلب بيانات Twelve Data لـ {symbol}: {e}")
        return None


app = Flask(__name__)

# CORS: بدّل origin بدومان Sandoq الحقيقي فـ production بدل "*"
ALLOWED_ORIGIN = os.getenv("SANDOQ_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})

# الأزواج اللي كيدعمها Sandoq (لازم يتطابقو مع COINS فـ Sandoq)
SUPPORTED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "DOGEUSDT", "SHIBUSDT", "PEPEUSDT",
]

CONFIDENCE_THRESHOLD = float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "0.80"))
CACHE_TTL_SECONDS = int(os.getenv("SIGNAL_CACHE_TTL", "20"))  # كاش خفيف باش ما نضربوش Binance بزاف

indicators = TechnicalIndicators()
detector = BottomTopDetector()
mentor = TradingMentor()

_cache_lock = threading.Lock()
_signal_cache = {}  # {symbol: {"data": {...}, "ts": float}}

# 🔔 تنبيهات تيليجرام — بوت جديد مخصص لـ PRO-TRADING-BOT (ماشي MyfadherBOT/ABDUGEMINIBOT)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# سر بسيط باش نتأكدو أن الطلب جاي فعلا من Telegram (اختياري لكن مستحسن)
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

_last_notified = {}  # {symbol: "buy"|"sell"} — باش ما نكرروش نفس الإشارة كل polling


def send_telegram_message(chat_id, text: str):
    """إرسال رسالة عامة لأي chat_id (كتستعمل من الأوامر ومن التنبيهات)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"❌ فشل إرسال رسالة تيليجرام: {e}")


def send_telegram_alert(symbol: str, signal: str, confidence: float, price: float, reasons: list):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    emoji = "🟢" if signal == "buy" else "🔴"
    label = "شراء" if signal == "buy" else "بيع"
    reasons_txt = "\n".join(f"• {r}" for r in reasons) if reasons else "—"
    text = (
        f"{emoji} <b>إشارة {label} — {symbol}</b>\n"
        f"السعر: {price}\n"
        f"الثقة: {confidence*100:.0f}%\n"
        f"الأسباب:\n{reasons_txt}"
    )
    send_telegram_message(TELEGRAM_CHAT_ID, text)


def _compute_signal(symbol: str, market: str = "crypto") -> dict:
    """يحسب إشارة واحدة لعملة/زوج واحد. market: 'crypto' (MEXC) أو 'forex' (Twelve Data)."""
    try:
        if market == "forex":
            df = get_klines_forex(symbol, interval="5min", limit=100)
        else:
            df = get_klines_mexc(symbol, interval="5m", limit=100)

        if df is None or len(df) < 30:
            return {
                "symbol": symbol,
                "signal": "hold",
                "confidence": 0.0,
                "reasons": ["بيانات غير كافية"],
                "price": None,
            }

        df = indicators.add_all_indicators(df)
        bottom = detector.detect_bottom(df)
        top = detector.detect_top(df)
        ai_pred = mentor.predict(df)

        price = float(df["close"].iloc[-1])

        if bottom["is_bottom"] and bottom["confidence"] >= CONFIDENCE_THRESHOLD:
            signal, confidence, reasons = "buy", bottom["confidence"], bottom["reasons"]
        elif top["is_top"] and top["confidence"] >= CONFIDENCE_THRESHOLD:
            signal, confidence, reasons = "sell", top["confidence"], top["reasons"]
        elif ai_pred["confidence"] >= CONFIDENCE_THRESHOLD:
            signal, confidence, reasons = ai_pred["signal"], ai_pred["confidence"], ["تنبؤ نموذج AI"]
        else:
            signal, confidence, reasons = "hold", max(bottom["confidence"], top["confidence"]), []

        if signal in ("buy", "sell") and _last_notified.get(symbol) != signal:
            send_telegram_alert(symbol, signal, confidence, price, reasons)
            _last_notified[symbol] = signal
        elif signal == "hold":
            _last_notified.pop(symbol, None)

        return {
            "symbol": symbol,
            "signal": signal,
            "confidence": round(float(confidence), 4),
            "reasons": reasons,
            "price": price,
        }
    except Exception as e:
        logger.error(f"❌ خطأ فـ حساب الإشارة لـ {symbol}: {e}")
        return {
            "symbol": symbol,
            "signal": "hold",
            "confidence": 0.0,
            "reasons": [f"error: {e}"],
            "price": None,
        }


def _get_cached_or_compute(symbol: str, market: str = "crypto") -> dict:
    cache_key = f"{market}:{symbol}"
    now = time.time()
    with _cache_lock:
        cached = _signal_cache.get(cache_key)
        if cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"]

    data = _compute_signal(symbol, market=market)
    with _cache_lock:
        _signal_cache[cache_key] = {"data": data, "ts": now}
    return data


@app.route("/api/signal/<symbol>", methods=["GET"])
def get_signal(symbol):
    symbol = symbol.upper()
    if symbol not in SUPPORTED_SYMBOLS:
        return jsonify({"error": f"symbol {symbol} not supported"}), 400
    return jsonify(_get_cached_or_compute(symbol))


@app.route("/api/signals", methods=["GET"])
def get_all_signals():
    """يرجع إشارات كل العملات المدعومة فـ نداء واحد — هذا اللي يستعملو Sandoq."""
    results = [_get_cached_or_compute(s) for s in SUPPORTED_SYMBOLS]
    return jsonify({"threshold": CONFIDENCE_THRESHOLD, "signals": results, "ts": time.time()})


@app.route("/api/forex-signal/<symbol>", methods=["GET"])
def get_forex_signal(symbol):
    symbol = symbol.upper()
    if symbol not in FOREX_SYMBOLS:
        return jsonify({"error": f"symbol {symbol} not supported"}), 400
    return jsonify(_get_cached_or_compute(symbol, market="forex"))


@app.route("/api/forex-signals", methods=["GET"])
def get_all_forex_signals():
    results = [_get_cached_or_compute(s, market="forex") for s in FOREX_SYMBOLS]
    return jsonify({"threshold": CONFIDENCE_THRESHOLD, "signals": results, "ts": time.time()})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────────────────
# 🤖 Telegram Webhook — كيرد على الأوامر (/start, /status, /signals)
# ─────────────────────────────────────────────────────────

def _handle_telegram_command(text: str, chat_id):
    cmd = text.strip().split()[0].lower() if text.strip() else ""

    if cmd == "/start":
        send_telegram_message(
            chat_id,
            "🤖 <b>PRO TRADING BOT</b> — مرحبا بيك!\n\n"
            "هذا البوت كيبعت تنبيهات آلية بلا تدخلك (buy/sell) على العملات لي تحت المراقبة.\n\n"
            "الأوامر المتاحة:\n"
            "/status — حالة السيرفر\n"
            "/signals — آخر إشارات كل العملات (كريبتو)\n"
            "/forex — آخر إشارات الفوركس + ذهب/فضة",
        )
    elif cmd == "/status":
        send_telegram_message(chat_id, f"✅ السيرفر شغال. حد الثقة الحالي: {CONFIDENCE_THRESHOLD*100:.0f}%")
    elif cmd == "/signals":
        results = [_get_cached_or_compute(s) for s in SUPPORTED_SYMBOLS]
        lines = [f"{r['symbol']}: {r['signal']} ({r['confidence']*100:.0f}%)" for r in results]
        send_telegram_message(chat_id, "📊 <b>آخر الإشارات</b>\n" + "\n".join(lines))
    elif cmd == "/forex":
        results = [_get_cached_or_compute(s, market="forex") for s in FOREX_SYMBOLS]
        lines = [f"{r['symbol']}: {r['signal']} ({r['confidence']*100:.0f}%)" for r in results]
        send_telegram_message(chat_id, "💱 <b>فوركس + ذهب/فضة</b>\n" + "\n".join(lines))
    else:
        send_telegram_message(chat_id, "❓ أمر غير معروف. جرب /start")


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    # حماية بسيطة: Telegram كيبعت هاد الهيدر إيلا كنت غادي بـ secret_token فـ setWebhook
    if TELEGRAM_WEBHOOK_SECRET:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_secret != TELEGRAM_WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if message:
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")
        if chat_id and text.startswith("/"):
            try:
                _handle_telegram_command(text, chat_id)
            except Exception as e:
                logger.error(f"❌ خطأ فـ معالجة أمر تيليجرام: {e}")

    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────
# ⚡ فيتامين سي — Keep-Alive باش الـ free instance ما ينعسش
# Render كيرقد السيرفر بعد ~15 دقيقة بلا طلبات خارجية.
# هاد الـ thread كيضرب URL العمومي ديال السيرفر (ماشي localhost)
# كل 10 دقايق باش يبقى live.
# ─────────────────────────────────────────────────────────

KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL_SECONDS", str(10 * 60)))
# Render كيزيد هاد الـ env var أوتوماتيكيا (اسم السيرفيس + .onrender.com)
SELF_URL = os.getenv("RENDER_EXTERNAL_URL", "")


def _keep_alive_loop():
    if not SELF_URL:
        logger.warning("⚠️ RENDER_EXTERNAL_URL غير موجود — keep-alive معطل")
        return
    ping_url = f"{SELF_URL.rstrip('/')}/api/health"
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            requests.get(ping_url, timeout=10)
            logger.info("💊 keep-alive ping ✅")
        except Exception as e:
            logger.warning(f"⚠️ keep-alive ping فشل: {e}")


threading.Thread(target=_keep_alive_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
