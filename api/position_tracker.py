"""
position_tracker.py
====================
يحل مشكل الاعتماد على localStorage فالمتصفح لتتبع صفقات Paper Trading.
كل شي هنا خدام فالسيرفر (Flask) — يخدم حتى لو الصفحة مغلقة.

الاستخدام (فـ signal_server.py):
    from position_tracker import PositionTracker
    tracker = PositionTracker(get_signal_func=_get_cached_or_compute)
    tracker.start_background_checker()   # يبدا الفحص الدوري فالخلفية

    # فـ route ديال فتح صفقة (POST /api/trade/open):
    tracker.open_trade(symbol, direction, price, sl, tp, reason="...")

    # فـ route ديال قراءة الصفقات (GET /api/trades):
    tracker.get_all_trades()

    # فـ route ديال الإحصائيات (GET /api/stats):
    tracker.get_stats()
"""

import json
import os
import time
import threading
from utils.logger import setup_logger

logger = setup_logger("position_tracker")

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades_data.json")
CHECK_INTERVAL_SECONDS = 60  # كل دقيقة يفحص الصفقات المفتوحة


class PositionTracker:
    def __init__(self, get_signal_func):
        """
        get_signal_func: دالة تاخد (symbol, market) وترجع dict فيه 'price'
                          (نفس _get_cached_or_compute اللي عندك فـ signal_server.py)
        """
        self._get_signal = get_signal_func
        self._lock = threading.Lock()
        self._trades = self._load_trades()

    # ─── تخزين دائم ──────────────────────────
    def _load_trades(self):
        if os.path.exists(TRADES_FILE):
            try:
                with open(TRADES_FILE, "r") as f:
                    trades = json.load(f)
                    logger.info(f"📂 تم استرجاع {len(trades)} صفقة من الملف")
                    return trades
            except Exception as e:
                logger.error(f"❌ خطأ قراءة ملف الصفقات: {e}")
        return []

    def _save_trades(self):
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump(self._trades, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"❌ خطأ حفظ ملف الصفقات: {e}")

    # ─── فتح صفقة ──────────────────────────
    def open_trade(self, symbol, direction, price, sl, tp, market="forex", reason=""):
        with self._lock:
            # ✅ حارس ضد التكرار: نرفض فتح صفقة جديدة إذا عندنا ديجا صفقة مفتوحة لنفس الزوج
            existing = [t for t in self._trades if t["symbol"] == symbol and t["status"] == "open"]
            if existing:
                logger.warning(f"⏭️ [{symbol}] صفقة مفتوحة ديجا - تم رفض الفتح المكرر")
                return {"error": "duplicate_position", "existing_trade_id": existing[0]["id"]}

            trade = {
                "id": int(time.time() * 1000),
                "symbol": symbol,
                "market": market,
                "direction": direction,
                "entry": price,
                "sl": sl,
                "tp": tp,
                "status": "open",
                "result": None,
                "reason": reason,
                "opened_at": time.time(),
                "closed_at": None,
            }
            self._trades.append(trade)
            self._save_trades()
            logger.info(f"✅ [{symbol}] صفقة {direction} جديدة مفتوحة عند {price}")
            return trade

    def close_trade(self, trade_id, result=None):
        with self._lock:
            for t in self._trades:
                if t["id"] == trade_id and t["status"] == "open":
                    t["status"] = "closed"
                    t["closed_at"] = time.time()
                    if result:
                        t["result"] = result
                    self._save_trades()
                    return t
            return None

    def get_all_trades(self):
        with self._lock:
            return list(reversed(self._trades))

    def get_stats(self):
        with self._lock:
            trades = self._trades
        today_start = time.time() - (time.time() % 86400)
        closed_today = [t for t in trades if t["status"] == "closed" and t["closed_at"] and t["closed_at"] >= today_start]
        wins = [t for t in closed_today if t["result"] == "win"]
        losses = [t for t in closed_today if t["result"] == "loss"]
        open_count = len([t for t in trades if t["status"] == "open"])
        win_rate = round(len(wins) / len(closed_today) * 100) if closed_today else None
        return {
            "win_rate": win_rate,
            "open_count": open_count,
            "wins_today": len(wins),
            "losses_today": len(losses),
        }

    # ─── الفحص الدوري (الجزء المهم) ──────────────────────────
    def _check_open_trades_once(self):
        with self._lock:
            open_trades = [t for t in self._trades if t["status"] == "open"]

        changed = False
        for t in open_trades:
            try:
                sig = self._get_signal(t["symbol"], market=t.get("market", "forex"))
                price = sig.get("price")
                if price is None:
                    continue

                hit_tp = (t["direction"] == "buy" and price >= t["tp"]) or \
                         (t["direction"] == "sell" and price <= t["tp"])
                hit_sl = (t["direction"] == "buy" and price <= t["sl"]) or \
                         (t["direction"] == "sell" and price >= t["sl"])

                if hit_tp or hit_sl:
                    result = "win" if hit_tp else "loss"
                    with self._lock:
                        t["status"] = "closed"
                        t["result"] = result
                        t["closed_at"] = time.time()
                    logger.info(f"🔔 [{t['symbol']}] صفقة أُغلقت تلقائيا: {result} عند {price}")
                    changed = True
            except Exception as e:
                logger.error(f"❌ خطأ فحص صفقة {t['symbol']}: {e}")

        if changed:
            with self._lock:
                self._save_trades()

    def start_background_checker(self):
        def loop():
            logger.info(f"🚀 بدء مراقبة الصفقات المفتوحة كل {CHECK_INTERVAL_SECONDS} ثانية (فالخلفية)")
            while True:
                try:
                    self._check_open_trades_once()
                except Exception as e:
                    logger.error(f"❌ خطأ عام فحلقة الفحص: {e}")
                time.sleep(CHECK_INTERVAL_SECONDS)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
