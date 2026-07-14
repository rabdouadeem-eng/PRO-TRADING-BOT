import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import joblib
import os
from utils.logger import setup_logger

logger = setup_logger("trading_mentor")

class TradingMentor:
    """
    المعلم الذكي المدمج - يعلم الروبوت ويتحسن مع الوقت
    """
    def __init__(self):
        self.model = None
        self.scaler = StandardScaler()
        self.knowledge_base = []
        self.model_path = "models/trading_model.pkl"
        self.scaler_path = "models/scaler.pkl"
        self._load_or_create_model()
    
    def _load_or_create_model(self):
        """تحميل أو إنشاء النموذج"""
        os.makedirs("models", exist_ok=True)
        if os.path.exists(self.model_path) and os.path.exists(self.scaler_path):
            self.model = joblib.load(self.model_path)
            self.scaler = joblib.load(self.scaler_path)
            logger.info("📚 تم تحميل النموذج المدرب مسبقاً")
        else:
            self.model = RandomForestClassifier(
                n_estimators=200, 
                max_depth=15, 
                random_state=42,
                n_jobs=-1
            )
            logger.info("🆕 تم إنشاء نموذج جديد")
    
    def prepare_features(self, df):
        """تحضير الميزات للتنبؤ"""
        features = pd.DataFrame()
        features['rsi'] = df['rsi']
        features['macd'] = df['macd']
        features['macd_signal'] = df['macd_signal']
        features['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])
        features['sma_ratio'] = df['close'] / df['sma_20']
        features['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
        features['price_change'] = df['close'].pct_change()
        features['volatility'] = df['close'].rolling(20).std()
        features['stoch_k'] = df['stoch_k']
        features['atr'] = df['atr']
        return features.fillna(0)
    
    def teach(self, df, outcome):
        """
        تعليم النموذج من النتائج الفعلية
        outcome: 1 = نجاح، 0 = فشل
        """
        features = self.prepare_features(df)
        if len(features) < 10:
            return
        
        # تخزين المعرفة
        self.knowledge_base.append({
            'features': features.iloc[-1].values,
            'outcome': outcome
        })
        
        # إعادة التدريب عند توفر بيانات كافية
        if len(self.knowledge_base) >= 50 and len(self.knowledge_base) % 10 == 0:
            self._retrain()
    
    def _retrain(self):
        """إعادة تدريب النموذج"""
        try:
            X = np.array([k['features'] for k in self.knowledge_base])
            y = np.array([k['outcome'] for k in self.knowledge_base])
            
            X_scaled = self.scaler.fit_transform(X)
            self.model.fit(X_scaled, y)
            
            joblib.dump(self.model, self.model_path)
            joblib.dump(self.scaler, self.scaler_path)
            
            accuracy = self.model.score(X_scaled, y)
            logger.info(f"🎓 تم إعادة التدريب - الدقة: {accuracy:.2%}")
        except Exception as e:
            logger.error(f"❌ خطأ في إعادة التدريب: {e}")
    
    def predict(self, df):
        """التنبؤ بفرصة النجاح"""
        try:
            features = self.prepare_features(df)
            if len(features) == 0:
                return {'signal': 'hold', 'confidence': 0.5}
            
            X = features.iloc[-1:].values
            X_scaled = self.scaler.transform(X)
            
            prediction = self.model.predict(X_scaled)[0]
            probabilities = self.model.predict_proba(X_scaled)[0]
            confidence = max(probabilities)
            
            signal = 'buy' if prediction == 1 else 'sell'
            logger.info(f"🧠 تنبؤ المعلم: {signal} بثقة {confidence:.2%}")
            return {'signal': signal, 'confidence': confidence}
        except Exception as e:
            logger.warning(f"⚠️ لا يمكن التنبؤ بعد (بيانات غير كافية): {e}")
            return {'signal': 'hold', 'confidence': 0.5}
    
    def provide_advice(self, market_state):
        """تقديم نصائح التداول"""
        advice = []
        
        if market_state.get('rsi', 50) < 30:
            advice.append("💡 RSI منخفض جداً - فرصة شراء محتملة")
        elif market_state.get('rsi', 50) > 70:
            advice.append("⚠️ RSI مرتفع جداً - احذر من التصحيح")
        
        if market_state.get('volume_trend') == 'increasing':
            advice.append("📈 حجم التداول في ارتفاع - تأكيد على الحركة")
        
        if market_state.get('trend') == 'bullish':
            advice.append("🐂 الاتجاه صاعد - ابحث عن فرص شراء")
        elif market_state.get('trend') == 'bearish':
            advice.append("🐻 الاتجاه هابط - كن حذراً")
        
        return advice
