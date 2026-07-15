from binance.client import Client
from binance.exceptions import BinanceAPIException
from config.settings import Settings
from utils.logger import setup_logger
import pandas as pd
import time

logger = setup_logger("binance_connector")

class BinanceConnector:
    def __init__(self):
        if Settings.TESTNET:
            self.client = Client(
                Settings.BINANCE_API_KEY, 
                Settings.BINANCE_API_SECRET, 
                testnet=True
            )
            self.client.API_URL = 'https://testnet.binance.vision/api'
        else:
            self.client = Client(
                Settings.BINANCE_API_KEY, 
                Settings.BINANCE_API_SECRET
            )
        logger.info("✅ تم الاتصال بمنصة Binance بنجاح")
    
    def get_account_balance(self, asset='USDT'):
        """جلب رصيد الحساب"""
        try:
            balance = self.client.get_asset_balance(asset=asset)
            return float(balance['free'])
        except BinanceAPIException as e:
            logger.error(f"❌ خطأ في جلب الرصيد: {e}")
            return 0
    
    def get_symbol_price(self, symbol):
        """جلب السعر الحالي"""
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except BinanceAPIException as e:
            logger.error(f"❌ خطأ في جلب السعر: {e}")
            return None
    
    def get_klines(self, symbol, interval='1h', limit=100):
        """جلب البيانات التاريخية (الشموع)"""
        try:
            klines = self.client.get_klines(
                symbol=symbol, 
                interval=interval, 
                limit=limit
            )
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 
                'taker_buy_base', 'taker_buy_quote', 'ignore'
            ])
            df['close'] = df['close'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['volume'] = df['volume'].astype(float)
            df['open'] = df['open'].astype(float)
            return df
        except BinanceAPIException as e:
            logger.error(f"❌ خطأ في جلب البيانات: {e}")
            return None
    
    def place_buy_order(self, symbol, quantity):
        """تنفيذ أمر شراء"""
        try:
            order = self.client.order_market_buy(
                symbol=symbol,
                quantity=quantity
            )
            logger.info(f"✅ تم تنفيذ أمر شراء: {symbol} - الكمية: {quantity}")
            return order
        except BinanceAPIException as e:
            logger.error(f"❌ خطأ في أمر الشراء: {e}")
            return None
    
    def place_sell_order(self, symbol, quantity):
        """تنفيذ أمر بيع"""
        try:
            order = self.client.order_market_sell(
                symbol=symbol,
                quantity=quantity
            )
            logger.info(f"✅ تم تنفيذ أمر بيع: {symbol} - الكمية: {quantity}")
            return order
        except BinanceAPIException as e:
            logger.error(f"❌ خطأ في أمر البيع: {e}")
            return None
