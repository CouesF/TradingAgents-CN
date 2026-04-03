#!/usr/bin/env python3
"""
查询股票600873的今日数据
"""
import sys
import os
from datetime import datetime, timedelta
from pymongo import MongoClient

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 从配置获取MongoDB连接信息
from app.core.config import settings

def query_today_data():
    """查询600873今日数据"""
    try:
        # 连接MongoDB
        print(f"连接MongoDB: {settings.MONGO_URI}")
        client = MongoClient(settings.MONGO_URI)
        db = client[settings.MONGODB_DATABASE]

        # 获取今日日期字符串
        today = datetime.now()
        today_str = today.strftime("%Y%m%d")
        yesterday = today - timedelta(days=1)
        yesterday_str = yesterday.strftime("%Y%m%d")

        print(f"当前日期: {today.strftime('%Y-%m-%d')}")
        print(f"查询代码: 600873")
        print("-" * 80)

        # 1. 查询股票基础信息
        print("1. 股票基础信息:")
        basic_info = db.stock_basic_info.find_one({"code": "600873"})
        if basic_info:
            print(f"   名称: {basic_info.get('name', 'N/A')}")
            print(f"   行业: {basic_info.get('industry', 'N/A')}")
            print(f"   市场: {basic_info.get('market', 'N/A')}")
            print(f"   总市值: {basic_info.get('total_mv', 'N/A')}亿元")
            print(f"   流通市值: {basic_info.get('circ_mv', 'N/A')}亿元")
            print(f"   市盈率: {basic_info.get('pe', 'N/A')}")
            print(f"   市净率: {basic_info.get('pb', 'N/A')}")
        else:
            print("   未找到基础信息")

        print("-" * 80)

        # 2. 查询今日实时行情
        print("2. 今日实时行情:")

        # 先尝试查询今日数据
        today_quote = db.market_quotes.find_one({
            "code": "600873",
            "trade_date": today_str
        })

        if today_quote:
            print(f"   今日数据 ({today_str}):")
            print(f"   开盘价: {today_quote.get('open', 'N/A')}")
            print(f"   收盘价: {today_quote.get('close', 'N/A')}")
            print(f"   最高价: {today_quote.get('high', 'N/A')}")
            print(f"   最低价: {today_quote.get('low', 'N/A')}")
            print(f"   涨跌幅: {today_quote.get('pct_chg', 'N/A')}%")
            print(f"   成交量: {today_quote.get('volume', 'N/A'):,.0f}股")
            print(f"   成交额: {today_quote.get('amount', 'N/A'):,.0f}元")
            print(f"   数据源: {today_quote.get('data_source', 'N/A')}")
            print(f"   更新时间: {today_quote.get('updated_at', 'N/A')}")
        else:
            # 查询最新数据
            latest_quote = db.market_quotes.find_one(
                {"code": "600873"},
                sort=[("updated_at", -1)]
            )

            if latest_quote:
                trade_date = latest_quote.get('trade_date', 'N/A')
                print(f"   今日暂无数据，最新数据日期: {trade_date}")
                print(f"   开盘价: {latest_quote.get('open', 'N/A')}")
                print(f"   收盘价: {latest_quote.get('close', 'N/A')}")
                print(f"   最高价: {latest_quote.get('high', 'N/A')}")
                print(f"   最低价: {latest_quote.get('low', 'N/A')}")
                print(f"   涨跌幅: {latest_quote.get('pct_chg', 'N/A')}%")
                print(f"   成交量: {latest_quote.get('volume', 'N/A'):,.0f}股")
                print(f"   成交额: {latest_quote.get('amount', 'N/A'):,.0f}元")
                print(f"   数据源: {latest_quote.get('data_source', 'N/A')}")
                print(f"   更新时间: {latest_quote.get('updated_at', 'N/A')}")
            else:
                print("   未找到任何行情数据")

        print("-" * 80)

        # 3. 查询今日日线数据
        print("3. 今日日线数据:")

        today_daily = db.stock_daily_quotes.find_one({
            "code": "600873",
            "trade_date": today_str
        })

        if today_daily:
            print(f"   今日日线数据 ({today_str}):")
            print(f"   开盘价: {today_daily.get('open', 'N/A')}")
            print(f"   收盘价: {today_daily.get('close', 'N/A')}")
            print(f"   最高价: {today_daily.get('high', 'N/A')}")
            print(f"   最低价: {today_daily.get('low', 'N/A')}")
            print(f"   涨跌幅: {today_daily.get('pct_chg', 'N/A')}%")
            print(f"   成交量: {today_daily.get('volume', 'N/A'):,.0f}")
            print(f"   成交额: {today_daily.get('amount', 'N/A'):,.0f}")
            print(f"   数据源: {today_daily.get('data_source', 'N/A')}")
        else:
            # 查询最新日线数据
            latest_daily = db.stock_daily_quotes.find_one(
                {"code": "600873"},
                sort=[("trade_date", -1)]
            )

            if latest_daily:
                trade_date = latest_daily.get('trade_date', 'N/A')
                print(f"   今日暂无日线数据，最新数据日期: {trade_date}")
                print(f"   开盘价: {latest_daily.get('open', 'N/A')}")
                print(f"   收盘价: {latest_daily.get('close', 'N/A')}")
                print(f"   最高价: {latest_daily.get('high', 'N/A')}")
                print(f"   最低价: {latest_daily.get('low', 'N/A')}")
                print(f"   涨跌幅: {latest_daily.get('pct_chg', 'N/A')}%")
                print(f"   成交量: {latest_daily.get('volume', 'N/A'):,.0f}")
                print(f"   成交额: {latest_daily.get('amount', 'N/A'):,.0f}")
                print(f"   数据源: {latest_daily.get('data_source', 'N/A')}")
            else:
                print("   未找到任何日线数据")

        print("-" * 80)

        # 4. 查询最近3天数据
        print("4. 最近3天数据概览:")

        # 实时行情最近3天
        recent_quotes = list(db.market_quotes.find(
            {"code": "600873"}
        ).sort("trade_date", -1).limit(3))

        if recent_quotes:
            print("   实时行情:")
            for quote in recent_quotes:
                trade_date = quote.get('trade_date', 'N/A')
                close = quote.get('close', 'N/A')
                pct_chg = quote.get('pct_chg', 'N/A')
                print(f"     {trade_date}: 收盘价={close}, 涨跌幅={pct_chg}%")
        else:
            print("   无近期实时行情数据")

        # 日线数据最近3天
        recent_daily = list(db.stock_daily_quotes.find(
            {"code": "600873"}
        ).sort("trade_date", -1).limit(3))

        if recent_daily:
            print("   日线数据:")
            for daily in recent_daily:
                trade_date = daily.get('trade_date', 'N/A')
                close = daily.get('close', 'N/A')
                pct_chg = daily.get('pct_chg', 'N/A')
                print(f"     {trade_date}: 收盘价={close}, 涨跌幅={pct_chg}%")
        else:
            print("   无近期日线数据")

        client.close()

    except Exception as e:
        print(f"查询失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    query_today_data()