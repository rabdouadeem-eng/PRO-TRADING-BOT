import requests
from config.settings import Settings
from utils.logger import setup_logger

logger = setup_logger("notifications")

def send_telegram_notification(message):
    """إرسال إشعار عبر تيليجرام"""
    if not Settings.TELEGRAM_BOT_TOKEN or not Settings.TELEGRAM_CHAT_ID:
        return
    
    try:
        url = f"https://api.telegram.org/bot{Settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": Settings.TELEGRAM_CHAT_ID,
            "text": f"🤖 بوت التداول\n\n{message}",
            "parse_mode": "HTML"
        }
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logger.error(f"❌ فشل إرسال الإشعار: {e}")
