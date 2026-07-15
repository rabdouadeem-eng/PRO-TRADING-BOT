import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Binance API
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
    BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
    TESTNET = True  # True للاختبار، False للتداول الحقيقي
    
    # مبالغ التداول
    MIN_TRADE_AMOUNT = 10  # أقل مبلغ (USD)
    MAX_TRADE_AMOUNT = 10000
    DEFAULT_TRADE_AMOUNT = 50
    
    # الأزواج المدعومة
    SUPPORTED_PAIRS = {
        'major': ['BTCUSDT', 'ETHUSDT', 'BNBUSDT'],
        'meme': ['DOGEUSDT', 'SHIBUSDT', 'PEPEUSDT', 'FLOKIUSDT', 'WIFUSDT']
    }
    
    # إعدادات الاستراتيجية
    BOTTOM_DETECTION_THRESHOLD = 0.05  # 5%
    TOP_DETECTION_THRESHOLD = 0.08     # 8%
    RSI_OVERSOLD = 30
    RSI_OVERBOUGHT = 70
    PROFIT_TARGET = 0.05  # ربح 5%
    STOP_LOSS = 0.03      # خسارة 3%
    
    # إعدادات الذكاء الاصطناعي
    ML_CONFIDENCE_THRESHOLD = 0.75
    LEARNING_RATE = 0.001
    EPOCHS = 100
    
    # الإشعارات
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
