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


FOREX_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>صندوق الفوركس — PRO TRADING BOT</title>
<style>
  body { background:#0d0d0d; color:#eee; font-family:sans-serif; margin:0; padding:12px; }
  h2 { font-size:16px; margin:14px 0 8px; }
  .card { background:#161616; border:1px solid #2a2a2a; border-radius:10px; padding:12px; margin-bottom:14px; }
  .row { display:flex; gap:8px; margin-bottom:8px; }
  .row > div { flex:1; }
  label { font-size:12px; color:#999; display:block; margin-bottom:3px; }
  input { width:100%; background:#0d0d0d; border:1px solid #333; color:#eee; padding:8px; border-radius:6px; box-sizing:border-box; }
  button { border:none; border-radius:6px; padding:9px 14px; font-weight:bold; cursor:pointer; }
  .btn-save { background:#333; color:#eee; width:100%; }
  .btn-buy { background:#1e9e50; color:#fff; flex:1; }
  .btn-sell { background:#c0392b; color:#fff; flex:1; }
  .btn-close { background:#444; color:#eee; font-size:11px; padding:4px 8px; }
  .sig-row { padding:10px 0; border-bottom:1px solid #222; }
  .sig-row:last-child { border-bottom:none; }
  .sig-top { display:flex; justify-content:space-between; align-items:center; }
  .sig-reason { font-size:11px; color:#888; margin-top:4px; }
  .buy { color:#2ecc71; } .sell { color:#e74c3c; } .hold { color:#e6b800; } .watch { color:#f39c12; }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .stat-box { background:#0d0d0d; border:1px solid #2a2a2a; border-radius:8px; padding:8px; text-align:center; }
  .stat-box .val { font-size:16px; font-weight:bold; }
  .stat-box .lbl { font-size:11px; color:#999; }
  table { width:100%; border-collapse:collapse; font-size:11px; }
  th, td { text-align:center; padding:6px 3px; border-bottom:1px solid #222; }
  .green { color:#2ecc71; } .red { color:#e74c3c; }
  .badge-open { color:#3498db; } .badge-closed { color:#777; }
</style>
</head>
<body>
  <h2>📈 لوحة الإحصائيات</h2>
  <div class="card stats" id="stats"></div>

  <h2>💱 رأس المال والمخاطرة</h2>
  <div class="card">
    <div class="row">
      <div><label>رأس المال $</label><input id="capital" type="number" value="1000"></div>
      <div><label>مخاطرة %</label><input id="risk" type="number" value="1"></div>
    </div>
    <div class="row">
      <div><label>وقف الخسارة %</label><input id="sl" type="number" value="1.5"></div>
      <div><label>هدف الربح %</label><input id="tp" type="number" value="3"></div>
    </div>
    <button class="btn-save" onclick="saveSettings()">💾 حفظ الإعدادات</button>
  </div>

  <h2>📊 الإشارات الحية</h2>
  <div class="card" id="signals">جاري التحميل...</div>

  <h2>🧾 سجل الصفقات (Paper Trading)</h2>
  <div class="card">
    <table>
      <thead><tr><th>الزوج</th><th>الاتجاه</th><th>دخول</th><th>SL</th><th>TP</th><th>ربح $</th><th>الحالة</th><th>المدة</th><th></th></tr></thead>
      <tbody id="trades"></tbody>
    </table>
  </div>

<script>
const SETTINGS_KEY = "forex_dashboard_settings";
const TRADES_KEY = "forex_dashboard_trades";
let latestSignals = {};

function loadSettings() {
  const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  if (s.capital) document.getElementById("capital").value = s.capital;
  if (s.risk) document.getElementById("risk").value = s.risk;
  if (s.sl) document.getElementById("sl").value = s.sl;
  if (s.tp) document.getElementById("tp").value = s.tp;
}
function saveSettings() {
  const s = {
    capital: parseFloat(document.getElementById("capital").value),
    risk: parseFloat(document.getElementById("risk").value),
    sl: parseFloat(document.getElementById("sl").value),
    tp: parseFloat(document.getElementById("tp").value),
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  alert("تم الحفظ ✅");
}

function loadTrades() { return JSON.parse(localStorage.getItem(TRADES_KEY) || "[]"); }
function saveTrades(trades) { localStorage.setItem(TRADES_KEY, JSON.stringify(trades)); }

function signalClass(sig, conf) {
  if (sig === "buy") return "buy";
  if (sig === "sell") return "sell";
  if (conf >= 0.60) return "watch";
  return "hold";
}
function signalLabel(sig, conf) {
  if (sig === "buy") return "🟢 BUY";
  if (sig === "sell") return "🔴 SELL";
  if (conf >= 0.60) return "🟡 مراقبة";
  return "🟡 HOLD";
}

function openTrade(symbol, direction, price) {
  const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  const capital = s.capital || 1000;
  const riskPct = (s.risk || 1) / 100;
  const slPct = (s.sl || 1.5) / 100;
  const tpPct = (s.tp || 3) / 100;

  const sl = direction === "buy" ? price * (1 - slPct) : price * (1 + slPct);
  const tp = direction === "buy" ? price * (1 + tpPct) : price * (1 - tpPct);
  const potentialProfit = capital * riskPct * (tpPct / slPct);
  const sigInfo = latestSignals[symbol] || {};
  const reason = (sigInfo.reasons || []).join(" + ") || "-";

  const trades = loadTrades();
  trades.push({
    id: Date.now(),
    symbol, direction, entry: price,
    sl: sl.toFixed(5), tp: tp.toFixed(5),
    profit: potentialProfit.toFixed(2),
    status: "open", result: null,
    reason,
    openedAt: Date.now(),
    closedAt: null
  });
  saveTrades(trades);
  renderTrades();
  renderStats();
}

function closeTrade(id, result) {
  const trades = loadTrades();
  const t = trades.find(t => t.id === id);
  if (t) { t.status = "closed"; t.closedAt = Date.now(); if (result) t.result = result; }
  saveTrades(trades);
  renderTrades();
  renderStats();
}

function formatDuration(ms) {
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return mins + " د";
  const hrs = Math.floor(mins / 60);
  return hrs + " س " + (mins % 60) + " د";
}

// يتحقق من الصفقات المفتوحة عند كل تحديث أسعار: TP/SL تحققو أوتوماتيك
function checkOpenTrades() {
  const trades = loadTrades();
  let changed = false;
  trades.forEach(t => {
    if (t.status !== "open") return;
    const cur = latestSignals[t.symbol];
    if (!cur || !cur.price) return;
    const price = cur.price;
    if (t.direction === "buy") {
      if (price >= parseFloat(t.tp)) { t.status = "closed"; t.result = "win"; t.closedAt = Date.now(); changed = true; }
      else if (price <= parseFloat(t.sl)) { t.status = "closed"; t.result = "loss"; t.closedAt = Date.now(); changed = true; }
    } else {
      if (price <= parseFloat(t.tp)) { t.status = "closed"; t.result = "win"; t.closedAt = Date.now(); changed = true; }
      else if (price >= parseFloat(t.sl)) { t.status = "closed"; t.result = "loss"; t.closedAt = Date.now(); changed = true; }
    }
  });
  if (changed) saveTrades(trades);
}

function renderTrades() {
  const trades = loadTrades().slice().reverse();
  const tbody = document.getElementById("trades");
  if (trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="color:#666;">لا توجد صفقات بعد</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const dur = formatDuration((t.closedAt || Date.now()) - t.openedAt);
    const statusTxt = t.status === "open" ? '<span class="badge-open">مفتوحة</span>' :
      (t.result === "win" ? '<span class="green">ربح ✅</span>' : t.result === "loss" ? '<span class="red">خسارة ❌</span>' : '<span class="badge-closed">مغلقة</span>');
    return `
    <tr title="${t.reason}">
      <td>${t.symbol}</td>
      <td class="${t.direction === 'buy' ? 'green' : 'red'}">${t.direction === 'buy' ? 'شراء' : 'بيع'}</td>
      <td>${t.entry}</td>
      <td>${t.sl}</td>
      <td>${t.tp}</td>
      <td class="green">${t.profit}</td>
      <td>${statusTxt}</td>
      <td>${dur}</td>
      <td>${t.status === "open" ? `<button class="btn-close" onclick="closeTrade(${t.id})">إغلاق</button>` : ""}</td>
    </tr>
  `; }).join("");
}

function renderStats() {
  const trades = loadTrades();
  const closedToday = trades.filter(t => t.status === "closed" && t.closedAt && new Date(t.closedAt).toDateString() === new Date().toDateString());
  const wins = closedToday.filter(t => t.result === "win");
  const losses = closedToday.filter(t => t.result === "loss");
  const winRate = closedToday.length ? ((wins.length / closedToday.length) * 100).toFixed(0) : "—";
  const profitToday = wins.reduce((sum, t) => sum + parseFloat(t.profit), 0).toFixed(2);
  const lossToday = losses.reduce((sum, t) => sum + parseFloat(t.profit), 0).toFixed(2);
  const openCount = trades.filter(t => t.status === "open").length;
  const bestPairMap = {};
  wins.forEach(t => { bestPairMap[t.symbol] = (bestPairMap[t.symbol] || 0) + parseFloat(t.profit); });
  const bestPair = Object.keys(bestPairMap).sort((a,b) => bestPairMap[b]-bestPairMap[a])[0] || "—";

  document.getElementById("stats").innerHTML = `
    <div class="stat-box"><div class="val">${winRate}%</div><div class="lbl">📈 نسبة النجاح</div></div>
    <div class="stat-box"><div class="val green">$${profitToday}</div><div class="lbl">💰 ربح اليوم</div></div>
    <div class="stat-box"><div class="val red">$${lossToday}</div><div class="lbl">📉 خسارة اليوم</div></div>
    <div class="stat-box"><div class="val">${openCount}</div><div class="lbl">🔥 صفقات مفتوحة</div></div>
    <div class="stat-box" style="grid-column:span 2;"><div class="val">${bestPair}</div><div class="lbl">🏆 أفضل زوج اليوم</div></div>
  `;
}

async function loadSignals() {
  try {
    const res = await fetch("/api/forex-signals");
    const data = await res.json();
    latestSignals = {};
    data.signals.forEach(s => latestSignals[s.symbol] = s);

    checkOpenTrades();

    const div = document.getElementById("signals");
    div.innerHTML = data.signals.map(s => {
      const cls = signalClass(s.signal, s.confidence);
      const label = signalLabel(s.signal, s.confidence);
      const reasonTxt = (s.reasons && s.reasons.length) ? s.reasons.join(" + ") : "لا توجد بيانات كافية";
      return `
      <div class="sig-row">
        <div class="sig-top">
          <div><b>${s.symbol}</b> — ${s.price ?? "—"}</div>
          <div class="${cls}">${label} (${(s.confidence*100).toFixed(0)}%)</div>
          <div>
            <button class="btn-buy" onclick="openTrade('${s.symbol}','buy',${s.price})" ${!s.price ? "disabled" : ""}>شراء</button>
            <button class="btn-sell" onclick="openTrade('${s.symbol}','sell',${s.price})" ${!s.price ? "disabled" : ""}>بيع</button>
          </div>
        </div>
        <div class="sig-reason">${reasonTxt} = ${label} (${(s.confidence*100).toFixed(0)}%)</div>
      </div>
    `; }).join("");

    renderTrades();
    renderStats();
  } catch (e) {
    document.getElementById("signals").innerHTML = "❌ خطأ فـ جلب الإشارات";
  }
}

loadSettings();
renderTrades();
renderStats();
loadSignals();
setInterval(loadSignals, 20000);
</script>
</body>
</html>
"""


@app.route("/forex-dashboard", methods=["GET"])
def forex_dashboard():
    return FOREX_DASHBOARD_HTML


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
