"""
signal_server.py
=================
Flask API يكشف إشارات التداول (buy/sell/hold) لـ PRO-TRADING-BOT.
مخصص باش يستهلكه فرونت-إند خارجي (مثل Sandoq) عبر polling.

لا ينفذ أي صفقة حقيقية — فقط يحلل ويرجع القرار + الثقة + الأسباب.
"""

import os
import time
import threading
import requests
import pandas as pd
from flask import Flask, jsonify
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


app = Flask(__name__)

# CORS: بدّل origin بدومان Sandoq الحقيقي فـ production بدل "*"
ALLOWED_ORIGIN = os.getenv("SANDOQ_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})

# الأزواج اللي كيدعمها Sandoq (لازم يتطابقو مع COINS فـ Sandoq)
SUPPORTED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
    "DOGEUSDT", "SHIBUSDT", "PEPEUSDT",
]

CONFIDENCE_THRESHOLD = float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "0.65"))
CACHE_TTL_SECONDS = int(os.getenv("SIGNAL_CACHE_TTL", "20"))  # كاش خفيف باش ما نضربوش Binance بزاف

indicators = TechnicalIndicators()
detector = BottomTopDetector()
mentor = TradingMentor()

_cache_lock = threading.Lock()
_signal_cache = {}  # {symbol: {"data": {...}, "ts": float}}

# 🔔 تنبيهات تيليجرام — بوت جديد مخصص لـ PRO-TRADING-BOT (ماشي MyfadherBOT/ABDUGEMINIBOT)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_last_notified = {}  # {symbol: "buy"|"sell"} — باش ما نكرروش نفس الإشارة كل polling


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
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"❌ فشل إرسال تنبيه تيليجرام: {e}")


def _compute_signal(symbol: str) -> dict:
    """يحسب إشارة واحدة لعملة واحدة."""
    try:
        df = get_klines_mexc(symbol, interval="1m", limit=100)
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

        # قرار مركّب: نديرو priority لـ bottom/top detector (rule-based) + AI كتأكيد
        if bottom["is_bottom"] and bottom["confidence"] >= CONFIDENCE_THRESHOLD:
            signal, confidence, reasons = "buy", bottom["confidence"], bottom["reasons"]
        elif top["is_top"] and top["confidence"] >= CONFIDENCE_THRESHOLD:
            signal, confidence, reasons = "sell", top["confidence"], top["reasons"]
        elif ai_pred["confidence"] >= CONFIDENCE_THRESHOLD:
            signal, confidence, reasons = ai_pred["signal"], ai_pred["confidence"], ["تنبؤ نموذج AI"]
        else:
            signal, confidence, reasons = "hold", max(bottom["confidence"], top["confidence"]), []

        # 🔔 تنبيه تيليجرام — غير إذا الإشارة جديدة (تبدلت عن آخر مرة)، ماشي كل polling
        if signal in ("buy", "sell") and _last_notified.get(symbol) != signal:
            send_telegram_alert(symbol, signal, confidence, price, reasons)
            _last_notified[symbol] = signal
        elif signal == "hold":
            _last_notified.pop(symbol, None)  # نفسحو الحالة باش إشارة جديدة تنبه من جديد لاحقا

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


def _get_cached_or_compute(symbol: str) -> dict:
    now = time.time()
    with _cache_lock:
        cached = _signal_cache.get(symbol)
        if cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
            return cached["data"]

    data = _compute_signal(symbol)
    with _cache_lock:
        _signal_cache[symbol] = {"data": data, "ts": now}
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


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
