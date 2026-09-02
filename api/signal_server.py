"""
signal_server.py
=================
Flask API يكشف إشارات التداول (buy/sell/hold) لـ PRO-TRADING-BOT.
مخصص باش يستهلكه فرونت-إند خارجي (مثل Sandoq) عبر polling.

✅ يفتح الصفقات تلقائياً عند بلوغ عتبة الثقة العالية، ويغلقها تلقائياً عند SL/TP
   (الإغلاق التلقائي كان موجوداً أصلاً عبر tracker.start_background_checker()).
تتبع الصفقات (Paper Trading) توا فالسيرفر (position_tracker.py) بدل localStorage.

🔧 [تعديل] حل مشكل 429 Too Many Requests من Twelve Data:
   - إضافة تأخير بسيط بين طلبات الفوركس فـ get_all_forex_signals
   - زيادة مدة الكاش الافتراضية لـ 120 ثانية (بدل 60) لتقليل عدد الطلبات
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
from api.position_tracker import PositionTracker

logger = setup_logger("signal_server")

MEXC_KLINES_URL = "https://api.mexc.com/api/v3/klines"


def get_klines_mexc(symbol: str, interval: str = "1m", limit: int = 100):
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


TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"

FOREX_SYMBOL_MAP = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "XAUUSD": "XAU/USD",
    "XAGUSD": "XAG/USD",
}
FOREX_SYMBOLS = list(FOREX_SYMBOL_MAP.keys())

# 🔧 [تعديل] تأخير بسيط بين كل طلب فوركس متتالي (بالثواني) — قابل للضبط عبر env var
FOREX_REQUEST_DELAY_SECONDS = float(os.getenv("FOREX_REQUEST_DELAY_SECONDS", "1.5"))


def get_klines_forex(symbol: str, interval: str = "5min", limit: int = 100):
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
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"❌ خطأ فـ جلب بيانات Twelve Data لـ {symbol}: {e}")
        return None


# 🔧 [تعديل جديد] جلب كل أزواج الفوركس بطلب HTTP واحد فقط بدل 5 طلبات منفصلة.
# Twelve Data كيدعم عدة symbols مفصولين بفاصلة فنفس endpoint (/time_series?symbol=EUR/USD,GBP/USD,...)
# ورد الـAPI كيرجع dict مفتاحو الرمز (مثال: {"EUR/USD": {...}, "GBP/USD": {...}})
def get_all_forex_klines_batch(interval: str = "5min", limit: int = 100) -> dict:
    """يرجع dict: {symbol_internal: DataFrame} لكل أزواج الفوركس، بطلب واحد فقط."""
    result = {}
    if not TWELVEDATA_API_KEY:
        logger.warning("⚠️ TWELVEDATA_API_KEY غير موجود — الفوركس معطل")
        return result

    td_symbols_str = ",".join(FOREX_SYMBOL_MAP.values())  # "EUR/USD,GBP/USD,USD/JPY,XAU/USD,XAG/USD"
    reverse_map = {v: k for k, v in FOREX_SYMBOL_MAP.items()}  # "EUR/USD" -> "EURUSD"

    try:
        resp = requests.get(
            TWELVEDATA_URL,
            params={
                "symbol": td_symbols_str,
                "interval": interval,
                "outputsize": limit,
                "apikey": TWELVEDATA_API_KEY,
            },
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()

        # لما تكون رمز واحد فقط، الرد كيكون flat (فيه "values" مباشرة)
        # لما تكون عدة رموز، الرد كيكون dict مفتاحو كل رمز
        if "values" in raw:
            # حالة نادرة: رمز واحد غير
            only_symbol = list(FOREX_SYMBOL_MAP.values())[0]
            items = {only_symbol: raw}
        else:
            items = raw

        for td_symbol, payload in items.items():
            internal_symbol = reverse_map.get(td_symbol)
            if not internal_symbol:
                continue
            values = payload.get("values") if isinstance(payload, dict) else None
            if not values:
                logger.error(f"❌ رد Twelve Data (batch) بلا بيانات لـ {td_symbol}: {payload}")
                continue
            df = pd.DataFrame(values)
            df = df.rename(columns={"datetime": "timestamp"})
            for col in ["open", "high", "low", "close"]:
                df[col] = df[col].astype(float)
            df["volume"] = df.get("volume", 0)
            df = df.iloc[::-1].reset_index(drop=True)
            result[internal_symbol] = df

    except Exception as e:
        logger.error(f"❌ خطأ فـ جلب بيانات Twelve Data (batch): {e}")

    return result


app = Flask(__name__)

ALLOWED_ORIGIN = os.getenv("SANDOQ_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})

SUPPORTED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "DOGEUSDT", "SHIBUSDT", "PEPEUSDT",
]

CONFIDENCE_THRESHOLD = float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "0.80"))
# 🔧 [تعديل] رفع مدة الكاش الافتراضية من 60 لـ 120 ثانية لتقليل الضغط على Twelve Data
CACHE_TTL_SECONDS = int(os.getenv("SIGNAL_CACHE_TTL", "120"))

# ✅ إعدادات التداول التلقائي (نفس الافتراضيات ديال لوحة التحكم: SL 1.5% / TP 3%)
AUTO_TRADE_ENABLED = os.getenv("AUTO_TRADE_ENABLED", "true").lower() == "true"
AUTO_TRADE_SL_PCT = float(os.getenv("AUTO_TRADE_SL_PCT", "1.5")) / 100
AUTO_TRADE_TP_PCT = float(os.getenv("AUTO_TRADE_TP_PCT", "3.0")) / 100
AUTO_TRADE_INTERVAL_SECONDS = int(os.getenv("AUTO_TRADE_INTERVAL_SECONDS", "60"))
AUTO_TRADE_SYMBOLS = SUPPORTED_SYMBOLS + FOREX_SYMBOLS

indicators = TechnicalIndicators()
detector = BottomTopDetector()
mentor = TradingMentor()

_cache_lock = threading.Lock()
_signal_cache = {}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

_last_notified = {}


def send_telegram_message(chat_id, text: str):
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


def _market_for_symbol(symbol: str) -> str:
    return "forex" if symbol in FOREX_SYMBOLS else "crypto"


def _compute_signal(symbol: str, market: str = "crypto", df=None) -> dict:
    try:
        if df is None:
            if market == "forex":
                df = get_klines_forex(symbol, interval="5min", limit=100)
            else:
                df = get_klines_mexc(symbol, interval="5m", limit=100)

        if df is None or len(df) < 30:
            return {
                "symbol": symbol, "signal": "hold", "confidence": 0.0,
                "reasons": ["بيانات غير كافية"], "price": None,
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
            "symbol": symbol, "signal": signal,
            "confidence": round(float(confidence), 4),
            "reasons": reasons, "price": price,
        }
    except Exception as e:
        logger.error(f"❌ خطأ فـ حساب الإشارة لـ {symbol}: {e}")
        return {
            "symbol": symbol, "signal": "hold", "confidence": 0.0,
            "reasons": [f"error: {e}"], "price": None,
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


# ✅ تتبع الصفقات فالسيرفر (بدل localStorage) — يفحص TP/SL كل دقيقة فالخلفية (إغلاق تلقائي موجود أصلاً)
tracker = PositionTracker(get_signal_func=_get_cached_or_compute)
tracker.start_background_checker()


# ============================================================
# ✅ التداول التلقائي: فتح الصفقة وحدها عند بلوغ عتبة الثقة العالية
# ============================================================
def _auto_trade_loop():
    logger.info(
        f"🤖 تم تشغيل الفتح التلقائي للصفقات — عتبة الثقة: {CONFIDENCE_THRESHOLD} "
        f"| SL: {AUTO_TRADE_SL_PCT*100}% | TP: {AUTO_TRADE_TP_PCT*100}%"
    )
    while True:
        try:
            # 🔧 [تعديل] نجيبو كل إشارات الفوركس بطلب batch واحد أولا (يستعمل الكاش إلا كان صالح)
            forex_signals = {s["symbol"]: s for s in _get_or_compute_all_forex_batch()}

            for symbol in AUTO_TRADE_SYMBOLS:
                market = _market_for_symbol(symbol)
                if market == "forex":
                    sig = forex_signals.get(symbol)
                    if sig is None:
                        continue
                else:
                    sig = _get_cached_or_compute(symbol, market=market)

                if sig["signal"] not in ("buy", "sell"):
                    continue
                if sig["confidence"] < CONFIDENCE_THRESHOLD:
                    continue
                if sig["price"] is None:
                    continue

                price = sig["price"]
                direction = sig["signal"]
                sl = price * (1 - AUTO_TRADE_SL_PCT) if direction == "buy" else price * (1 + AUTO_TRADE_SL_PCT)
                tp = price * (1 + AUTO_TRADE_TP_PCT) if direction == "buy" else price * (1 - AUTO_TRADE_TP_PCT)
                reason = " + ".join(sig.get("reasons", [])) or "تداول تلقائي"

                result = tracker.open_trade(
                    symbol=symbol,
                    direction=direction,
                    price=price,
                    sl=sl,
                    tp=tp,
                    market=market,
                    reason=reason,
                )
                if result.get("error") == "duplicate_position":
                    continue  # عندو ديجا صفقة مفتوحة لهاد الزوج، تخطى
                logger.info(
                    f"🟢 صفقة تلقائية {direction} {symbol} @ {price} "
                    f"(ثقة {sig['confidence']}) SL:{round(sl,4)} TP:{round(tp,4)}"
                )
        except Exception as e:
            logger.error(f"❌ خطأ فـ الفتح التلقائي: {e}")
        time.sleep(AUTO_TRADE_INTERVAL_SECONDS)


if AUTO_TRADE_ENABLED:
    threading.Thread(target=_auto_trade_loop, daemon=True).start()
else:
    logger.info("⏸️ الفتح التلقائي معطل (AUTO_TRADE_ENABLED=false)")


@app.route("/api/signal/<symbol>", methods=["GET"])
def get_signal(symbol):
    symbol = symbol.upper()
    if symbol not in SUPPORTED_SYMBOLS:
        return jsonify({"error": f"symbol {symbol} not supported"}), 400
    return jsonify(_get_cached_or_compute(symbol))


@app.route("/api/signals", methods=["GET"])
def get_all_signals():
    results = [_get_cached_or_compute(s) for s in SUPPORTED_SYMBOLS]
    return jsonify({"threshold": CONFIDENCE_THRESHOLD, "signals": results, "ts": time.time()})


@app.route("/api/forex-signal/<symbol>", methods=["GET"])
def get_forex_signal(symbol):
    symbol = symbol.upper()
    if symbol not in FOREX_SYMBOLS:
        return jsonify({"error": f"symbol {symbol} not supported"}), 400
    return jsonify(_get_cached_or_compute(symbol, market="forex"))


def _get_or_compute_all_forex_batch() -> list:
    """يجيب كل أزواج الفوركس بطلب HTTP واحد فقط (batch)، ثم يحسب الإشارة لكل واحد.
    إلا كان الكاش صالح لكل الأزواج، يرجع من الكاش بلا أي طلب جديد."""
    now = time.time()
    cached_results = {}
    missing = []

    with _cache_lock:
        for s in FOREX_SYMBOLS:
            cache_key = f"forex:{s}"
            cached = _signal_cache.get(cache_key)
            if cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
                cached_results[s] = cached["data"]
            else:
                missing.append(s)

    if missing:
        # 🔧 [تعديل] طلب واحد فقط لكل الأزواج الناقصة بدل طلب لكل واحد
        batch_data = get_all_forex_klines_batch(interval="5min", limit=100)
        for s in missing:
            df = batch_data.get(s)
            data = _compute_signal(s, market="forex", df=df)
            with _cache_lock:
                _signal_cache[f"forex:{s}"] = {"data": data, "ts": time.time()}
            cached_results[s] = data

    return [cached_results[s] for s in FOREX_SYMBOLS]


@app.route("/api/forex-signals", methods=["GET"])
def get_all_forex_signals():
    # 🔧 [تعديل] طلب batch واحد فقط لكل الأزواج بدل 5 طلبات منفصلة — يحل مشكل 429
    results = _get_or_compute_all_forex_batch()
    return jsonify({"threshold": CONFIDENCE_THRESHOLD, "signals": results, "ts": time.time()})


# ─── Routes جداد: فتح/غلق/قراءة الصفقات (فالسيرفر، دائم) ───
@app.route("/api/trade/open", methods=["POST"])
def open_trade():
    data = request.get_json(force=True)
    result = tracker.open_trade(
        symbol=data["symbol"],
        direction=data["direction"],
        price=float(data["price"]),
        sl=float(data["sl"]),
        tp=float(data["tp"]),
        market=data.get("market", "forex"),
        reason=data.get("reason", ""),
    )
    return jsonify(result)


@app.route("/api/trade/close/<int:trade_id>", methods=["POST"])
def close_trade(trade_id):
    result = tracker.close_trade(trade_id)
    if result is None:
        return jsonify({"error": "trade not found or already closed"}), 404
    return jsonify(result)


@app.route("/api/trades", methods=["GET"])
def get_trades():
    return jsonify(tracker.get_all_trades())


@app.route("/api/trade-stats", methods=["GET"])
def get_trade_stats():
    return jsonify(tracker.get_stats())


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "auto_trade": AUTO_TRADE_ENABLED})
