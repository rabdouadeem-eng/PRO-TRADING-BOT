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
