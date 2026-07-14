import argparse
from core.trading_engine import TradingEngine
from utils.logger import setup_logger

logger = setup_logger("main")

def main():
    parser = argparse.ArgumentParser(description='🤖 بوت التداول الذكي')
    parser.add_argument('--mode', choices=['live', 'test', 'analyze'], 
                       default='test', help='وضع التشغيل')
    parser.add_argument('--symbol', type=str, help='زوج تداول محدد')
    parser.add_argument('--amount', type=float, default=50, help='مبلغ التداول')
    parser.add_argument('--interval', type=str, default='1h', help='فترة زمنية')
    
    args = parser.parse_args()
    engine = TradingEngine()
    
    if args.mode == 'analyze':
        if args.symbol:
            analysis = engine.analyze_market(args.symbol, args.interval)
            if analysis:
                print(f"\n📊 تحليل {args.symbol}:")
                print(f"💰 السعر: {analysis['current_price']}")
                print(f"📉 القاع: {analysis['bottom_signal']}")
                print(f"📈 الذروة: {analysis['top_signal']}")
                print(f"🧠 المعلم: {analysis['mentor_prediction']}")
        else:
            print("⚠️ حدد --symbol للتحليل")
    
    elif args.mode == 'live':
        print("🚀 بدء التداول الحقيقي")
        engine.run_continuous()
    
    else:  # test
        print("🧪 وضع الاختبار")
        if args.symbol:
            result = engine.execute_strategy(args.symbol, args.amount, args.interval)
            print(f"النتيجة: {result}")
        else:
            print("⚠️ حدد --symbol للاختبار")

if __name__ == "__main__":
    main()
