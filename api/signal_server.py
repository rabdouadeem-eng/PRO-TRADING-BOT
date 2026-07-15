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
from flask import Flask, jsonify
from flask_cors import CORS

from config.settings import Settings
from core.binance_connector import BinanceConnector
from strategies.technical_indicators import TechnicalIndicators
from strategies.bottom_top_detector import BottomTopDetector
from ai_teacher.trading_mentor import TradingMentor
from utils.logger import setup_logger

logger = setup_logger("signal_server")

# 🔑 signal_server يقرا فقط (bilā أوامر حقيقية) — نجبروه يخدم على mainnet
# بلا ما نمسو Config/Settings.py الأصلي (اللي خاصو يبقى TESTNET=True لأي شي آخر)
Settings.TESTNET = False

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

connector = BinanceConnector()
indicators = TechnicalIndicators()
detector = BottomTopDetector()
mentor = TradingMentor()

_cache_lock = threading.Lock()
_signal_cache = {}  # {symbol: {"data": {...}, "ts": float}}


def _compute_signal(symbol: str) -> dict:
    """يحسب إشارة واحدة لعملة واحدة."""
    try:
        df = connector.get_klines(symbol, interval="1m", limit=100)
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
