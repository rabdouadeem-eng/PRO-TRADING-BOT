from core.binance_connector import BinanceConnector
from strategies.bottom_top_detector import BottomTopDetector
from strategies.technical_indicators import TechnicalIndicators
from ai_teacher.trading_mentor import TradingMentor
from config.settings import Settings
from utils.logger import setup_logger
from utils.notifications import send_telegram_notification
import time
import json
import os
import numpy as np  # ⚠️ manquant dans l'original — np.log10 est utilisé plus bas

logger = setup_logger("trading_engine")

POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "..", "active_positions.json")


class TradingEngine:
    def __init__(self):
        self.connector = BinanceConnector()
        self.detector = BottomTopDetector()
        self.indicators = TechnicalIndicators()
        self.mentor = TradingMentor()
        self.active_positions = self._load_positions()  # ✅ يقرا المراكز المحفوظة عند البداية

    # ─── تخزين دائم للمراكز المفتوحة ──────────────────────────
    def _load_positions(self):
        """يقرا المراكز المفتوحة من ملف JSON إذا كان موجود"""
        if os.path.exists(POSITIONS_FILE):
            try:
                with open(POSITIONS_FILE, 'r') as f:
                    positions = json.load(f)
                    if positions:
                        logger.info(f"📂 تم استرجاع {len(positions)} مركز مفتوح من الملف: {list(positions.keys())}")
                    return positions
            except Exception as e:
                logger.error(f"❌ خطأ قراءة ملف المراكز: {e}")
                return {}
        return {}

    def _save_positions(self):
        """يحفظ المراكز الحالية فملف JSON بعد كل تغيير"""
        try:
            with open(POSITIONS_FILE, 'w') as f:
                json.dump(self.active_positions, f, indent=2)
        except Exception as e:
            logger.error(f"❌ خطأ حفظ ملف المراكز: {e}")

    def calculate_quantity(self, symbol, amount_usd):
        """حساب الكمية بناءً على المبلغ بالدولار"""
        price = self.connector.get_symbol_price(symbol)
        if price is None:
            return None

        quantity = amount_usd / price
        lot_size = self._get_lot_size(symbol)
        quantity = round(quantity, -int(np.log10(lot_size)))
        return quantity

    def _get_lot_size(self, symbol):
        """جلب حجم اللوت الأدنى"""
        info = self.connector.client.get_symbol_info(symbol)
        for filter_item in info['filters']:
            if filter_item['filterType'] == 'LOT_SIZE':
                return float(filter_item['stepSize'])
        return 0.00001

    def analyze_market(self, symbol, interval='1h'):
        """تحليل السوق لزوج معين"""
        logger.info(f"🔍 تحليل السوق: {symbol}")

        df = self.connector.get_klines(symbol, interval=interval, limit=200)
        if df is None:
            return None

        df = self.indicators.add_all_indicators(df)
        bottom_signal = self.detector.detect_bottom(df)
        top_signal = self.detector.detect_top(df)
        mentor_prediction = self.mentor.predict(df)

        return {
            'symbol': symbol,
            'df': df,
            'bottom_signal': bottom_signal,
            'top_signal': top_signal,
            'mentor_prediction': mentor_prediction,
            'current_price': df['close'].iloc[-1]
        }

    def execute_strategy(self, symbol, trade_amount, interval='1h'):
        """تنفيذ استراتيجية التداول"""
        analysis = self.analyze_market(symbol, interval)
        if not analysis:
            return None

        current_price = analysis['current_price']
        bottom = analysis['bottom_signal']
        top = analysis['top_signal']
        mentor = analysis['mentor_prediction']

        logger.info(f"💰 السعر الحالي {symbol}: {current_price}")
        logger.info(f"📊 ثقة المعلم: {mentor['confidence']:.2%}")

        # ✅ الحارس الأساسي: لا تشري إذا عندنا ديجا مركز مفتوح لنفس الزوج
        if symbol in self.active_positions:
            logger.info(f"⏭️ [{symbol}] مركز مفتوح ديجا - تخطي فحص الشراء")
        elif bottom['is_bottom'] and bottom['confidence'] >= 0.6:
            if mentor['signal'] == 'buy' or mentor['confidence'] < 0.5:
                logger.info(f"🟢 إشارة شراء قوية - نقاط القاع: {bottom['score']}")
                return self._execute_buy(symbol, trade_amount, current_price, bottom)

        # منطق البيع
        if symbol in self.active_positions:
            position = self.active_positions[symbol]
            profit_percent = (current_price - position['entry_price']) / position['entry_price']

            should_sell = False
            reason = ""

            if profit_percent >= Settings.PROFIT_TARGET:
                should_sell = True
                reason = f"تحقيق ربح {profit_percent*100:.2f}%"
            elif top['is_top'] and top['confidence'] >= 0.6:
                should_sell = True
                reason = f"إشارة ذروة - نقاط: {top['score']}"
            elif profit_percent <= -Settings.STOP_LOSS:
                should_sell = True
                reason = f"وقف خسارة: {profit_percent*100:.2f}%"

            if should_sell:
                return self._execute_sell(symbol, position, current_price, reason)

        return None

    def _execute_buy(self, symbol, amount, price, signal):
        """تنفيذ الشراء"""
        # ✅ حارس إضافي أخير قبل أي إرسال أمر فعلي (double-check)
        if symbol in self.active_positions:
            logger.warning(f"⚠️ [{symbol}] محاولة شراء مكررة تم رفضها")
            return None

        balance = self.connector.get_account_balance('USDT')
        trade_amount = min(amount, balance * 0.95)

        if trade_amount < Settings.MIN_TRADE_AMOUNT:
            logger.warning(f"⚠️ الرصيد غير كافٍ للتداول")
            return None

        quantity = self.calculate_quantity(symbol, trade_amount)
        if quantity is None or quantity <= 0:
            return None

        order = self.connector.place_buy_order(symbol, quantity)
        if order:
            self.active_positions[symbol] = {
                'entry_price': price,
                'quantity': quantity,
                'amount': trade_amount,
                'entry_time': time.time(),
                'signal': signal
            }
            self._save_positions()  # ✅ يحفظ فورا بعد الشراء

            msg = f"✅ شراء {symbol}\nالكمية: {quantity}\nالسعر: {price}\nالثقة: {signal['confidence']:.0%}"
            send_telegram_notification(msg)
            return order

        return None

    def _execute_sell(self, symbol, position, current_price, reason):
        """تنفيذ البيع"""
        quantity = position['quantity']
        profit = (current_price - position['entry_price']) * quantity
        profit_percent = (current_price - position['entry_price']) / position['entry_price'] * 100

        order = self.connector.place_sell_order(symbol, quantity)
        if order:
            df = self.connector.get_klines(symbol, limit=200)
            outcome = 1 if profit > 0 else 0
            self.mentor.teach(df, outcome)

            del self.active_positions[symbol]
            self._save_positions()  # ✅ يحفظ فورا بعد البيع (إزالة المركز)

            msg = f"💰 بيع {symbol}\nالسبب: {reason}\nالربح: {profit:.2f} USDT ({profit_percent:.2f}%)"
            send_telegram_notification(msg)
            return order

        return None

    def run_continuous(self, interval_seconds=300):
        """تشغيل مستمر بفترات زمنية"""
        logger.info("🚀 بدء التشغيل المستمر")

        while True:
            try:
                all_symbols = Settings.SUPPORTED_PAIRS['major'] + Settings.SUPPORTED_PAIRS['meme']

                for symbol in all_symbols:
                    try:
                        self.execute_strategy(
                            symbol,
                            Settings.DEFAULT_TRADE_AMOUNT,
                            interval='1h'
                        )
                    except Exception as e:
                        logger.error(f"❌ خطأ في {symbol}: {e}")

                    time.sleep(5)

                logger.info(f"⏰ في انتظار الدورة التالية ({interval_seconds} ثانية)")
                time.sleep(interval_seconds)

            except KeyboardInterrupt:
                logger.info("🛑 إيقاف البوت")
                break
            except Exception as e:
                logger.error(f"❌ خطأ عام: {e}")
                time.sleep(60)
