"""
signal_server.py
=================
Flask API يكشف إشارات التداول (buy/sell/hold) لـ PRO-TRADING-BOT.

🔧 نسخة مصلّحة (14 نوفمبر 2026):
   - التركيز على الذهب والفوركس فقط (الكريبتو معطل افتراضياً، يبقى في Sandoq)
   - منع الإشارات وقت ما السوق مسكّر (من الجمعة 22:00 UTC حتى الأحد 22:00 UTC)
   - منع التكرار (cooldown) ومنع إشارة عكسية خلال فترة التبريد
   - إلا كان شراء وبيع مؤهلين فنفس الوقت → HOLD (تناقض)
   - ستوب وهدف مبنيين على ATR وكيبانو فالإشارة وفتيليغرام
"""

import os
import time
import threading
from datetime import datetime, timezone

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
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
}
FOREX_SYMBOLS = list(FOREX_SYMBOL_MAP.keys())

# ساعات فتح وغلق السوق (UTC). فالشتاء: 22:00. فالصيف: 21:00 → بدّلهم بالـ env
FOREX_CLOSE_HOUR_UTC = int(os.getenv("FOREX_CLOSE_HOUR_UTC", "22"))
FOREX_OPEN_HOUR_UTC = int(os.getenv("FOREX_OPEN_HOUR_UTC", "22"))


def is_forex_market_open(now=None) -> bool:
    """السوق مسكّر: السبت كامل + الجمعة بعد ساعة الغلق + الأحد قبل ساعة الفتح (UTC)."""
    now = now or datetime.now(timezone.utc)
    wd, h = now.weekday(), now.hour  # Monday=0 ... Sunday=6
    if wd == 5:
        return False
    if wd == 4 and h >= FOREX_CLOSE_HOUR_UTC:
        return False
    if wd == 6 and h < FOREX_OPEN_HOUR_UTC:
        return False
    return True


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


def get_all_forex_klines_batch(interval: str = "5min", limit: int = 100) -> dict:
    """يرجع dict: {symbol_internal: DataFrame} لكل الأزواج، بطلب واحد فقط."""
    result = {}
    if not TWELVEDATA_API_KEY:
        logger.warning("⚠️ TWELVEDATA_API_KEY غير موجود — الفوركس معطل")
        return result

    td_symbols_str = ",".join(FOREX_SYMBOL_MAP.values())
    reverse_map = {v: k for k, v in FOREX_SYMBOL_MAP.items()}

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

        if "values" in raw:
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

# الكريبتو معطل افتراضياً (يخدم فـ Sandoq). باش تفعّلو: CRYPTO_ENABLED=true
CRYPTO_ENABLED = os.getenv("CRYPTO_ENABLED", "false").lower() == "true"
SUPPORTED_SYMBOLS = (
    ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "SHIBUSDT", "PEPEUSDT"]
    if CRYPTO_ENABLED else []
)

CONFIDENCE_THRESHOLD = float(os.getenv("SIGNAL_CONFIDENCE_THRESHOLD", "0.80"))
CACHE_TTL_SECONDS = int(os.getenv("SIGNAL_CACHE_TTL", "300"))

# منع التكرار: مدة التبريد بين إشارتين لنفس الرمز (بالدقائق)
SIGNAL_COOLDOWN_SECONDS = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "60")) * 60

# ستوب/هدف على أساس ATR
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "2.0"))
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "3.0"))

# التداول التلقائي (Paper Trading فقط، ما ينفذش على حساب حقيقي)
AUTO_TRADE_ENABLED = os.getenv("AUTO_TRADE_ENABLED", "true").lower() == "true"
AUTO_TRADE_SL_PCT = float(os.getenv("AUTO_TRADE_SL_PCT", "1.5")) / 100
AUTO_TRADE_TP_PCT = float(os.getenv("AUTO_TRADE_TP_PCT", "3.0")) / 100
AUTO_TRADE_INTERVAL_SECONDS = int(os.getenv("AUTO_TRADE_INTERVAL_SECONDS", "60"))
AUTO_TRADE_SYMBOLS = SUPPORTED_SYMBOLS + FOREX_SYMBOLS

INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1"))

indicators = TechnicalIndicators()
detector = BottomTopDetector()
mentor = TradingMentor()

_cache_lock = threading.Lock()
_signal_cache = {}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# آخر إشارة تبعثات لكل رمز: {symbol: {"signal": "buy"/"sell", "ts": float}}
_last_signal = {}
_signal_lock = threading.Lock()


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


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    return f"{v:.5f}" if abs(v) < 10 else f"{v:.2f}"


def send_telegram_alert(symbol: str, signal: str, confidence: float, price: float,
                        reasons: list, sl=None, tp=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    emoji = "🟢" if signal == "buy" else "🔴"
    label = "شراء" if signal == "buy" else "بيع"
    reasons_txt = "\n".join(f"• {r}" for r in reasons) if reasons else "—"
    text = (
        f"{emoji} <b>إشارة {label} — {symbol}</b>\n"
        f"السعر: {_fmt_price(price)}\n"
        f"ستوب: {_fmt_price(sl)} | هدف: {_fmt_price(tp)}\n"
        f"الثقة: {confidence*100:.0f}%\n"
        f"الأسباب:\n{reasons_txt}\n"
        f"⚠️ للمراقبة فقط — القرار والتنفيذ يدوي"
    )
    send_telegram_message(TELEGRAM_CHAT_ID, text)


def _market_for_symbol(symbol: str) -> str:
    return "forex" if symbol in FOREX_SYMBOLS else "crypto"


def _atr(df, period: int = 14):
    try:
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        val = tr.rolling(period).mean().iloc[-1]
        return float(val) if pd.notna(val) else None
    except Exception:
        return None


def _hold(symbol: str, reasons: list, confidence: float = 0.0, price=None) -> dict:
    return {
        "symbol": symbol, "signal": "hold", "confidence": round(float(confidence), 4),
        "reasons": reasons, "price": price, "atr": None, "sl": None, "tp": None,
    }


def _compute_signal(symbol: str, market: str = "crypto", df=None) -> dict:
    try:
        # 🔧 السوق مسكّر → ما نحسبوش ولا نبعثو أي إشارة
        if market == "forex" and not is_forex_market_open():
            return _hold(symbol, ["السوق مسكّر"])

        if df is None:
            if market == "forex":
                df = get_klines_forex(symbol, interval="5min", limit=100)
            else:
                df = get_klines_mexc(symbol, interval="5m", limit=100)

        if df is None or len(df) < 30:
            return _hold(symbol, ["بيانات غير كافية"])

        df = indicators.add_all_indicators(df)
        bottom = detector.detect_bottom(df)
        top = detector.detect_top(df)
        ai_pred = mentor.predict(df)

        price = float(df["close"].iloc[-1])
        atr = _atr(df, ATR_PERIOD)

        bottom_ok = bottom["is_bottom"] and bottom["confidence"] >= CONFIDENCE_THRESHOLD
        top_ok = top["is_top"] and top["confidence"] >= CONFIDENCE_THRESHOLD

        if bottom_ok and top_ok:
            # 🔧 تناقض: قاع وقمة فنفس الوقت → ما نعطيوش إشارة
            signal, confidence, reasons = "hold", 0.0, ["تناقض بين إشارة الشراء والبيع"]
        elif bottom_ok:
            signal, confidence, reasons = "buy", bottom["confidence"], bottom["reasons"]
        elif top_ok:
            signal, confidence, reasons = "sell", top["confidence"], top["reasons"]
        elif ai_pred["confidence"] >= CONFIDENCE_THRESHOLD:
            signal, confidence, reasons = ai_pred["signal"], ai_pred["confidence"], ["تنبؤ نموذج AI"]
        else:
            signal, confidence, reasons = "hold", max(bottom["confidence"], top["confidence"]), []

        notify = False
        if signal in ("buy", "sell"):
            now = time.time()
            with _signal_lock:
                last = _last_signal.get(symbol)
                if last and (now - last["ts"]) < SIGNAL_COOLDOWN_SECONDS:
                    if last["signal"] != signal:
                        # 🔧 إشارة عكسية خلال فترة التبريد → نتجاهلوها
                        signal = "hold"
                        reasons = ["إشارة عكسية خلال فترة التبريد — متجاهلة"]
                    # نفس الاتجاه خلال التبريد → نخليها بلا تنبيه جديد
                else:
                    _last_signal[symbol] = {"signal": signal, "ts": now}
                    notify = True

        sl = tp = None
        if signal in ("buy", "sell"):
            if atr and atr > 0:
                if signal == "buy":
                    sl, tp = price - ATR_SL_MULT * atr, price + ATR_TP_MULT * atr
                else:
                    sl, tp = price + ATR_SL_MULT * atr, price - ATR_TP_MULT * atr
            else:
                if signal == "buy":
                    sl, tp = price * (1 - AUTO_TRADE_SL_PCT), price * (1 + AUTO_TRADE_TP_PCT)
                else:
                    sl, tp = price * (1 + AUTO_TRADE_SL_PCT), price * (1 - AUTO_TRADE_TP_PCT)

        if notify:
            send_telegram_alert(symbol, signal, confidence, price, reasons, sl=sl, tp=tp)

        return {
            "symbol": symbol, "signal": signal,
            "confidence": round(float(confidence), 4),
            "reasons": reasons, "price": price,
            "atr": atr, "sl": sl, "tp": tp,
        }
    except Exception as e:
        logger.error(f"❌ خطأ فـ حساب الإشارة لـ {symbol}: {e}")
        return _hold(symbol, [f"error: {e}"])


def _get_cached_or_compute(symbol: str, market: str = None) -> dict:
    market = market or _market_for_symbol(symbol)
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


def _get_or_compute_all_forex_batch() -> list:
    """يجيب كل الأزواج بطلب HTTP واحد (batch). السوق مسكّر → ما كاين حتى طلب."""
    now = time.time()
    cached_results = {}
    missing = []

    with _cache_lock:
        for s in FOREX_SYMBOLS:
            cached = _signal_cache.get(f"forex:{s}")
            if cached and (now - cached["ts"]) < CACHE_TTL_SECONDS:
                cached_results[s] = cached["data"]
            else:
                missing.append(s)

    if missing:
        if is_forex_market_open():
            batch_data = get_all_forex_klines_batch(interval="5min", limit=100)
        else:
            batch_data = {}
        for s in missing:
            data = _compute_signal(s, market="forex", df=batch_data.get(s))
            with _cache_lock:
                _signal_cache[f"forex:{s}"] = {"data": data, "ts": time.time()}
            cached_results[s] = data

    return [cached_results[s] for s in FOREX_SYMBOLS]


tracker = PositionTracker(get_signal_func=_get_cached_or_compute)
tracker.start_background_checker()


def _auto_trade_loop():
    logger.info(
        f"🤖 الفتح التلقائي (Paper) — عتبة الثقة: {CONFIDENCE_THRESHOLD} "
        f"| ATR SL x{ATR_SL_MULT} / TP x{ATR_TP_MULT}"
    )
    while True:
        try:
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
                if sig["price"] is None or sig.get("sl") is None or sig.get("tp") is None:
                    continue

                price = sig["price"]
                direction = sig["signal"]
                sl, tp = sig["sl"], sig["tp"]
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
                    continue
                logger.info(
                    f"🟢 صفقة Paper {direction} {symbol} @ {price} "
                    f"(ثقة {sig['confidence']}) SL:{round(sl, 4)} TP:{round(tp, 4)}"
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
    return jsonify(_get_cached_or_compute(symbol, market="crypto"))


@app.route("/api/signals", methods=["GET"])
def get_all_signals():
    results = [_get_cached_or_compute(s, market="crypto") for s in SUPPORTED_SYMBOLS]
    return jsonify({"threshold": CONFIDENCE_THRESHOLD, "signals": results, "ts": time.time()})


@app.route("/api/forex-signal/<symbol>", methods=["GET"])
def get_forex_signal(symbol):
    symbol = symbol.upper()
    if symbol not in FOREX_SYMBOLS:
        return jsonify({"error": f"symbol {symbol} not supported"}), 400
    return jsonify(_get_cached_or_compute(symbol, market="forex"))


@app.route("/api/forex-signals", methods=["GET"])
def get_all_forex_signals():
    results = _get_or_compute_all_forex_batch()
    return jsonify({
        "threshold": CONFIDENCE_THRESHOLD,
        "market_open": is_forex_market_open(),
        "signals": results,
        "ts": time.time(),
    })


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
    return jsonify({
        "status": "ok",
        "auto_trade": AUTO_TRADE_ENABLED,
        "crypto_enabled": CRYPTO_ENABLED,
        "market_open": is_forex_market_open(),
    })


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({
        "capital": INITIAL_CAPITAL,
        "risk_pct": RISK_PER_TRADE_PCT,
        "sl_pct": round(AUTO_TRADE_SL_PCT * 100, 2),
        "tp_pct": round(AUTO_TRADE_TP_PCT * 100, 2),
        "atr_sl_mult": ATR_SL_MULT,
        "atr_tp_mult": ATR_TP_MULT,
        "cooldown_minutes": SIGNAL_COOLDOWN_SECONDS // 60,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
    })


FOREX_DASHBOARD_HTML = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>صندوق الفوركس — PRO TRADING BOT</title>
<style>
  body { background:#0d0d0d; color:#eee; font-family:sans-serif; margin:0; padding:12px; }
  h2 { font-size:16px; margin:14px 0 8px; }
  .card { background:#161616; border:1px solid #2a2a2a; border-radius:10px; padding:12px; margin-bottom:14px; }
  .sig-row { padding:10px 0; border-bottom:1px solid #222; }
  .sig-row:last-child { border-bottom:none; }
  .sig-top { display:flex; justify-content:space-between; align-items:center; }
  .sig-reason { font-size:11px; color:#888; margin-top:4px; }
  .buy { color:#2ecc71; } .sell { color:#e74c3c; } .hold { color:#e6b800; }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .stat-box { background:#0d0d0d; border:1px solid #2a2a2a; border-radius:8px; padding:8px; text-align:center; }
  .stat-box .val { font-size:16px; font-weight:bold; }
  .stat-box .lbl { font-size:11px; color:#999; }
  table { width:100%; border-collapse:collapse; font-size:11px; }
  th, td { text-align:center; padding:6px 3px; border-bottom:1px solid #222; }
  .auto-badge { display:inline-block; background:#1e9e50; color:#fff; font-size:11px; padding:3px 8px; border-radius:6px; margin-bottom:10px; }
  .closed-badge { display:inline-block; background:#b03a2e; color:#fff; font-size:11px; padding:3px 8px; border-radius:6px; margin-bottom:10px; }
  #refresh-note { color:#8B949E; font-size:12px; text-align:center; margin-top:10px; }
</style>
</head>
<body>
  <div class="auto-badge">🤖 Paper Trading — ذهب وفوركس</div>
  <span id="market-badge"></span>

  <h2>💱 الإعدادات</h2>
  <div class="card stats" id="settings"></div>

  <h2>📊 الإشارات الحية</h2>
  <div class="card" id="signals">جاري التحميل...</div>

  <h2>📈 الإحصائيات</h2>
  <div class="card stats" id="stats"></div>

  <h2>🧾 سجل الصفقات (Paper)</h2>
  <div class="card">
    <table>
      <thead><tr><th>الزوج</th><th>الاتجاه</th><th>دخول</th><th>SL</th><th>TP</th><th>الحالة</th></tr></thead>
      <tbody id="trades"><tr><td colspan="6">جاري التحميل...</td></tr></tbody>
    </table>
  </div>

  <div id="refresh-note">يتحدث تلقائياً كل 30 ثانية</div>

<script>
async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}
function fmt(v) {
  if (typeof v === "number") return Math.abs(v) < 10 ? v.toFixed(5) : v.toFixed(2);
  return (v === null || v === undefined) ? "—" : String(v);
}
function signalClass(sig) {
  if (sig === "buy") return "buy";
  if (sig === "sell") return "sell";
  return "hold";
}
function signalLabel(sig) {
  if (sig === "buy") return "🟢 BUY";
  if (sig === "sell") return "🔴 SELL";
  return "🟡 HOLD";
}
function boxes(obj) {
  return Object.entries(obj)
    .filter(function (e) { return e[1] === null || typeof e[1] !== "object"; })
    .map(function (e) {
      return '<div class="stat-box"><div class="val">' + fmt(e[1]) + '</div><div class="lbl">' + e[0] + '</div></div>';
    }).join("");
}

async function loadSignals() {
  try {
    const d = await getJSON("/api/forex-signals");
    document.getElementById("market-badge").innerHTML = d.market_open
      ? ""
      : '<div class="closed-badge">⛔ السوق مسكّر</div>';
    document.getElementById("signals").innerHTML = d.signals.map(function (s) {
      const extra = (s.sl && s.tp)
        ? ' | SL ' + fmt(s.sl) + ' | TP ' + fmt(s.tp)
        : "";
      return '<div class="sig-row"><div class="sig-top"><b>' + s.symbol + '</b>' +
        '<span class="' + signalClass(s.signal) + '">' + signalLabel(s.signal) +
        ' (' + Math.round(s.confidence * 100) + '%)</span></div>' +
        '<div class="sig-reason">السعر: ' + fmt(s.price) + extra + '</div>' +
        '<div class="sig-reason">' + (s.reasons || []).join(" • ") + '</div></div>';
    }).join("");
  } catch (e) {
    document.getElementById("signals").textContent = "خطأ فـ التحميل";
  }
}

async function loadSettings() {
  try {
    document.getElementById("settings").innerHTML = boxes(await getJSON("/api/settings"));
  } catch (e) {}
}

async function loadStats() {
  try {
    document.getElementById("stats").innerHTML = boxes(await getJSON("/api/trade-stats"));
  } catch (e) {}
}

async function loadTrades() {
  try {
    let data = await getJSON("/api/trades");
    let list = Array.isArray(data) ? data : [].concat.apply([], Object.values(data).filter(Array.isArray));
    const body = document.getElementById("trades");
    if (!list.length) {
      body.innerHTML = '<tr><td colspan="6">ما كاين حتى صفقة</td></tr>';
      return;
    }
    body.innerHTML = list.slice(-30).reverse().map(function (t) {
      const dir = t.direction || t.side || "";
      const entry = t.entry_price !== undefined ? t.entry_price : t.price;
      return '<tr><td>' + fmt(t.symbol) + '</td><td class="' + signalClass(dir) + '">' + dir +
        '</td><td>' + fmt(entry) + '</td><td>' + fmt(t.sl) + '</td><td>' + fmt(t.tp) +
        '</td><td>' + fmt(t.status) + '</td></tr>';
    }).join("");
  } catch (e) {}
}

function refreshAll() {
  loadSettings();
  loadSignals();
  loadStats();
  loadTrades();
}
refreshAll();
setInterval(refreshAll, 30000);
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def forex_dashboard():
    return FOREX_DASHBOARD_HTML


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
