import numpy as np
import pandas as pd
from strategies.technical_indicators import TechnicalIndicators
from utils.logger import setup_logger

logger = setup_logger("bottom_top_detector")

class BottomTopDetector:
    def __init__(self):
        self.price_history = []
        self.indicators = TechnicalIndicators()

    def detect_bottom(self, df, lookback=20):
        df = self.indicators.add_all_indicators(df)
        bottom_score = 0
        reasons = []

        recent_lows = df['low'].tail(lookback)
        current_low = df['low'].iloc[-1]
        min_low = recent_lows.min()
        if current_low <= min_low * 1.02:
            bottom_score += 30
            reasons.append(f"السعر قريب من أدنى قاع ({lookback} شمعة)")

        if df['close'].iloc[-1] > df['open'].iloc[-1]:
            if df['close'].iloc[-2] < df['open'].iloc[-2]:
                bottom_score += 20
                reasons.append("انعكاس صعودي (شمعة خضراء بعد حمراء)")

        tech_signals = self.indicators.detect_bottom_signals(df)
        bottom_score += tech_signals['confidence']
        reasons.extend(tech_signals['reasons'])

        recent_volume = df['volume'].tail(5).mean()
        prev_volume = df['volume'].iloc[-10:-5].mean()
        if recent_volume > prev_volume * 1.5:
            bottom_score += 15
            reasons.append("ارتفاع حجم التداول")

        sma_distance = (df['close'].iloc[-1] - df['sma_20'].iloc[-1]) / df['sma_20'].iloc[-1]
        if sma_distance < -0.05:
            bottom_score += 20
            reasons.append(f"السعر تحت المتوسط بـ {abs(sma_distance)*100:.1f}%")

        is_bottom = bottom_score >= 50
        logger.info(f"📊 نقاط القاع: {bottom_score}")

        return {
            'is_bottom': is_bottom,
            'score': bottom_score,
            'reasons': reasons,
            'confidence': min(bottom_score / 100, 1.0),
        }

    def detect_top(self, df, lookback=20):
        df = self.indicators.add_all_indicators(df)
        top_score = 0
        reasons = []

        recent_highs = df['high'].tail(lookback)
        current_high = df['high'].iloc[-1]
        max_high = recent_highs.max()
        if current_high >= max_high * 0.98:
            top_score += 30
            reasons.append(f"السعر قريب من أعلى قمة ({lookback} شمعة)")

        if df['close'].iloc[-1] < df['open'].iloc[-1]:
            if df['close'].iloc[-2] > df['open'].iloc[-2]:
                top_score += 20
                reasons.append("انعكاس هبوطي (شمعة حمراء بعد خضراء)")

        tech_signals = self.indicators.detect_top_signals(df)
        top_score += tech_signals['confidence']
        reasons.extend(tech_signals['reasons'])

        if len(df) >= 20:
            price_higher = df['close'].iloc[-1] > df['close'].iloc[-10]
            rsi_lower = df['rsi'].iloc[-1] < df['rsi'].iloc[-10]
            if price_higher and rsi_lower:
                top_score += 25
                reasons.append("تباعد RSI هبوطي")

        sma_distance = (df['close'].iloc[-1] - df['sma_20'].iloc[-1]) / df['sma_20'].iloc[-1]
        if sma_distance > 0.08:
            top_score += 20
            reasons.append(f"السعر فوق المتوسط بـ {sma_distance*100:.1f}%")

        is_top = top_score >= 50
        logger.info(f"📊 نقاط الذروة: {top_score}")

        return {
            'is_top': is_top,
            'score': top_score,
            'reasons': reasons,
            'confidence': min(top_score / 100, 1.0),
        }
