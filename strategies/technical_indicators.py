import pandas as pd
import numpy as np
import ta
from ta.trend import SMAIndicator, EMAIndicator, MACD
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import VolumeWeightedAveragePrice

class TechnicalIndicators:
    @staticmethod
    def add_all_indicators(df):
        """إضافة جميع المؤشرات الفنية للبيانات"""
        df = df.copy()

        # المتوسطات المتحركة
        df['sma_20'] = SMAIndicator(close=df['close'], window=20).sma_indicator()
        df['sma_50'] = SMAIndicator(close=df['close'], window=50).sma_indicator()
        df['ema_12'] = EMAIndicator(close=df['close'], window=12).ema_indicator()
        df['ema_26'] = EMAIndicator(close=df['close'], window=26).ema_indicator()

        # 📈 EMA 20/50/200 — تحديد الترند (استراتيجية السكالب)
        df['ema_20'] = EMAIndicator(close=df['close'], window=20).ema_indicator()
        df['ema_50'] = EMAIndicator(close=df['close'], window=50).ema_indicator()
        # EMA 200 محتاجة 200 شمعة على الأقل؛ إيلا ماكانوش كافيين، كيرجع NaN وكنتعاملو معاها فـ trend_direction
        df['ema_200'] = EMAIndicator(close=df['close'], window=200).ema_indicator()

        # RSI
        df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()

        # MACD
        macd = MACD(close=df['close'])
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        df['macd_diff'] = macd.macd_diff()

        # Bollinger Bands
        bollinger = BollingerBands(close=df['close'], window=20, window_dev=2)
        df['bb_upper'] = bollinger.bollinger_hband()
        df['bb_lower'] = bollinger.bollinger_lband()
        df['bb_middle'] = bollinger.bollinger_mavg()

        # Stochastic
        stoch = StochasticOscillator(
            high=df['high'], low=df['low'], close=df['close']
        )
        df['stoch_k'] = stoch.stoch()
        df['stoch_d'] = stoch.stoch_signal()

        # ATR
        df['atr'] = AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close']
        ).average_true_range()

        return df

    @staticmethod
    def trend_direction(df) -> dict:
        """
        يحدد اتجاه الترند بناءً على ترتيب EMA 20/50/200 (استراتيجية السكالب).
        صاعد: close > ema200 > ema50 و ema20 فوقهم كاملين.
        هابط: العكس.
        إيلا EMA200 مازال NaN (بيانات قليلة)، كنستعملو غير EMA20/50 كـ fallback أضعف.
        """
        latest = df.iloc[-1]
        ema20, ema50, ema200 = latest.get('ema_20'), latest.get('ema_50'), latest.get('ema_200')
        close = latest['close']

        if pd.notna(ema200):
            if close > ema200 and ema50 > ema200 and ema20 > ema50:
                return {"direction": "up", "strength": "strong"}
            if close < ema200 and ema50 < ema200 and ema20 < ema50:
                return {"direction": "down", "strength": "strong"}

        # Fallback بلا EMA200 (بيانات قليلة) — نعتمدو غير 20/50
        if pd.notna(ema20) and pd.notna(ema50):
            if close > ema50 and ema20 > ema50:
                return {"direction": "up", "strength": "weak"}
            if close < ema50 and ema20 < ema50:
                return {"direction": "down", "strength": "weak"}

        return {"direction": "neutral", "strength": "none"}

    @staticmethod
    def rsi_momentum_ok(df, direction: str) -> bool:
        """
        شرط RSI 40-60 (زخم بلا تشبع) — بحال الإستراتيجية.
        للشراء: RSI بين 40-60 ويتجه للأعلى.
        للبيع: RSI بين 40-60 ويتجه للأسفل.
        """
        latest_rsi = df['rsi'].iloc[-1]
        prev_rsi = df['rsi'].iloc[-2]
        if pd.isna(latest_rsi) or pd.isna(prev_rsi):
            return False
        in_range = 40 <= latest_rsi <= 60
        if direction == "up":
            return in_range and latest_rsi > prev_rsi
        if direction == "down":
            return in_range and latest_rsi < prev_rsi
        return False

    @staticmethod
    def technical_sl_tp(df, direction: str, entry_price: float, lookback: int = 20) -> dict:
        """
        SL/TP مبني على البنية الفنية (قاع/قمة + EMA50) بدل نسبة ثابتة فقط — بحال الإستراتيجية.
        شراء: SL تحت آخر قاع واضح أو EMA50 (الأبعد يفوز أمانا)، TP بنسبة R:R 1:2.
        بيع: نفس المبدأ بالعكس.
        """
        latest = df.iloc[-1]
        ema50 = latest.get('ema_50')
        recent = df.tail(lookback)

        if direction == "buy":
            swing_low = recent['low'].min()
            candidates = [swing_low]
            if pd.notna(ema50) and ema50 < entry_price:
                candidates.append(ema50)
            sl = min(candidates) * 0.999  # هامش صغير تحت المستوى
            risk = entry_price - sl
            tp = entry_price + (risk * 2)  # R:R 1:2
        else:  # sell
            swing_high = recent['high'].max()
            candidates = [swing_high]
            if pd.notna(ema50) and ema50 > entry_price:
                candidates.append(ema50)
            sl = max(candidates) * 1.001
            risk = sl - entry_price
            tp = entry_price - (risk * 2)

        return {
            "sl": round(float(sl), 5),
            "tp": round(float(tp), 5),
            "risk_reward": 2.0,
        }

    @staticmethod
    def detect_bottom_signals(df):
        """كشف إشارات القاع"""
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        signals = {
            'is_bottom': False,
            'confidence': 0,
            'reasons': []
        }

        # RSI في منطقة التشبع البيعي
        if latest['rsi'] < 30 and prev['rsi'] >= 30:
            signals['is_bottom'] = True
            signals['confidence'] += 30
            signals['reasons'].append('RSI تشبع بيعي')

        # السعر تحت Bollinger Lower
        if latest['close'] < latest['bb_lower']:
            signals['is_bottom'] = True
            signals['confidence'] += 25
            signals['reasons'].append('السعر تحت Bollinger السفلي')

        # تقاطع MACD الإيجابي
        if prev['macd'] < prev['macd_signal'] and latest['macd'] > latest['macd_signal']:
            signals['is_bottom'] = True
            signals['confidence'] += 25
            signals['reasons'].append('تقاطع MACD إيجابي')

        # Stochastic في منطقة التشبع
        if latest['stoch_k'] < 20 and latest['stoch_d'] < 20:
            signals['confidence'] += 20
            signals['reasons'].append('Stochastic تشبع بيعي')

        return signals

    @staticmethod
    def detect_top_signals(df):
        """كشف إشارات الذروة"""
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        signals = {
            'is_top': False,
            'confidence': 0,
            'reasons': []
        }

        # RSI في منطقة التشبع الشرائي
        if latest['rsi'] > 70 and prev['rsi'] <= 70:
            signals['is_top'] = True
            signals['confidence'] += 30
            signals['reasons'].append('RSI تشبع شرائي')

        # السعر فوق Bollinger Upper
        if latest['close'] > latest['bb_upper']:
            signals['is_top'] = True
            signals['confidence'] += 25
            signals['reasons'].append('السعر فوق Bollinger العلوي')

        # تقاطع MACD السلبي
        if prev['macd'] > prev['macd_signal'] and latest['macd'] < latest['macd_signal']:
            signals['is_top'] = True
            signals['confidence'] += 25
            signals['reasons'].append('تقاطع MACD سلبي')

        # Stochastic في منطقة التشبع
        if latest['stoch_k'] > 80 and latest['stoch_d'] > 80:
            signals['confidence'] += 20
            signals['reasons'].append('Stochastic تشبع شرائي')

        return signals
