#!/usr/bin/env python3
"""
大盘指数数据同步模块

功能:
1. 独立集合 index_daily_quotes 存储指数数据，避免与股票代码冲突
2. 增量更新：检查最新日期，只同步缺失数据
3. 支持多指数：上证、深证、创业板、沪深300等

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    # 同步所有指数
    python scripts/sync_index_data.py

    # 同步指定指数
    python scripts/sync_index_data.py --index sh  # 上证指数

    # 强制全量更新
    python scripts/sync_index_data.py --full

    # 查看状态
    python scripts/sync_index_data.py --status
"""

import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from tradingagents.config.database_manager import get_database_manager

logger = logging.getLogger("IndexSync")


# 指数配置
INDEX_CONFIG = {
    # 代码: (名称, AKShare代码, 交易所)
    "sh": ("上证指数", "sh000001", "SSE"),
    "sz": ("深证成指", "sz399001", "SZSE"),
    "cyb": ("创业板指", "sz399006", "SZSE"),
    "hs300": ("沪深300", "sh000300", "SSE"),
    "zz500": ("中证500", "sh000905", "SSE"),
    "zz1000": ("中证1000", "sh000852", "SSE"),
    "sz50": ("上证50", "sh000016", "SSE"),
    "bz50": ("北证50", "bj899050", "BSE"),
}

# MongoDB集合名
COLLECTION_NAME = "index_daily_quotes"


class IndexDataSyncer:
    """大盘指数数据同步器"""

    def __init__(self, db=None):
        if db is None:
            db_manager = get_database_manager()
            self.db = db_manager.get_mongodb_db()
        else:
            self.db = db

        self.collection = self.db[COLLECTION_NAME]
        self._ensure_index()

    def _ensure_index(self):
        """创建索引"""
        self.collection.create_index([("symbol", 1), ("trade_date", -1)], unique=True)
        self.collection.create_index([("trade_date", -1)])

    def get_latest_date(self, symbol: str) -> Optional[str]:
        """获取某指数的最新日期"""
        doc = self.collection.find_one(
            {"symbol": symbol},
            {"_id": 0, "trade_date": 1},
            sort=[("trade_date", -1)]
        )
        return doc["trade_date"] if doc else None

    def fetch_index_data(self, ak_code: str, start_date: str, end_date: str, max_retries: int = 3) -> pd.DataFrame:
        """
        从AKShare获取指数数据

        Args:
            ak_code: AKShare指数代码，如 sh000001
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            max_retries: 最大重试次数
        """
        import time

        try:
            import akshare as ak
        except ImportError:
            logger.error("请安装 akshare: pip install akshare")
            return pd.DataFrame()

        # 尝试多个接口
        interfaces = [
            ("index_zh_a_hist", lambda: ak.index_zh_a_hist(symbol=ak_code, period="daily", start_date=start_date, end_date=end_date)),
            ("stock_zh_index_daily", lambda: ak.stock_zh_index_daily(symbol=ak_code)),
        ]

        for attempt in range(max_retries):
            for api_name, api_func in interfaces:
                try:
                    logger.info(f"  尝试接口 {api_name} (第{attempt+1}次)...")
                    df = api_func()

                    if df is None or df.empty:
                        continue

                    # 标准化列名
                    column_map = {
                        "日期": "trade_date",
                        "date": "trade_date",
                        "开盘": "open",
                        "open": "open",
                        "收盘": "close",
                        "close": "close",
                        "最高": "high",
                        "high": "high",
                        "最低": "low",
                        "low": "low",
                        "成交量": "volume",
                        "volume": "volume",
                        "成交额": "amount",
                        "amount": "amount",
                        "振幅": "amplitude",
                        "涨跌幅": "pct_chg",
                        "涨跌额": "change",
                        "换手率": "turnover",
                    }

                    df = df.rename(columns=column_map)

                    # 日期格式转换
                    if "trade_date" in df.columns:
                        if df["trade_date"].dtype == "object":
                            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
                        else:
                            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")

                    # 过滤日期范围
                    start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
                    end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
                    df = df[(df["trade_date"] >= start_fmt) & (df["trade_date"] <= end_fmt)]

                    if not df.empty:
                        logger.info(f"  成功获取 {len(df)} 条记录")
                        return df

                except Exception as e:
                    logger.warning(f"  接口 {api_name} 失败: {e}")
                    time.sleep(1)  # 等待1秒后重试

        logger.error(f"所有接口均失败: {ak_code}")
        return pd.DataFrame()

    def sync_index(self, index_key: str, full_sync: bool = False) -> Dict:
        """
        同步单个指数数据

        Args:
            index_key: 指数键名，如 sh, sz, cyb
            full_sync: 是否全量同步

        Returns:
            同步结果统计
        """
        if index_key not in INDEX_CONFIG:
            logger.error(f"未知指数: {index_key}")
            return {"status": "error", "message": f"未知指数: {index_key}"}

        name, ak_code, exchange = INDEX_CONFIG[index_key]
        symbol = ak_code[2:]  # 去掉 sh/sz/bj 前缀，如 000001
        full_symbol = f"{symbol}.{exchange[:2]}"  # 如 000001.SH

        logger.info(f"同步指数: {name} ({symbol})")

        # 确定日期范围
        today = datetime.now().strftime("%Y-%m-%d")

        if full_sync:
            start_date = "2000-01-01"  # 全量从2000年开始
        else:
            latest = self.get_latest_date(symbol)
            if latest:
                # 从最新日期的下一天开始
                next_day = (datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1))
                start_date = next_day.strftime("%Y-%m-%d")
            else:
                # 无历史数据，从2000年开始
                start_date = "2000-01-01"
                logger.info(f"  首次同步，从 {start_date} 开始")

        if start_date > today:
            logger.info(f"  数据已是最新，无需更新")
            return {"status": "uptodate", "symbol": symbol, "name": name}

        # 格式化日期为 YYYYMMDD
        start_fmt = start_date.replace("-", "")
        end_fmt = today.replace("-", "")

        logger.info(f"  获取数据: {start_date} ~ {today}")

        # 获取数据
        df = self.fetch_index_data(ak_code, start_fmt, end_fmt)

        if df.empty:
            return {"status": "nodata", "symbol": symbol, "name": name}

        # 写入MongoDB
        records = []
        for _, row in df.iterrows():
            record = {
                "symbol": symbol,
                "full_symbol": full_symbol,
                "name": name,
                "exchange": exchange,
                "trade_date": row.get("trade_date"),
                "open": float(row.get("open", 0)) if pd.notna(row.get("open")) else None,
                "close": float(row.get("close", 0)) if pd.notna(row.get("close")) else None,
                "high": float(row.get("high", 0)) if pd.notna(row.get("high")) else None,
                "low": float(row.get("low", 0)) if pd.notna(row.get("low")) else None,
                "volume": float(row.get("volume", 0)) if pd.notna(row.get("volume")) else None,
                "amount": float(row.get("amount", 0)) if pd.notna(row.get("amount")) else None,
                "pct_chg": float(row.get("pct_chg", 0)) if pd.notna(row.get("pct_chg")) else None,
                "change": float(row.get("change", 0)) if pd.notna(row.get("change")) else None,
                "amplitude": float(row.get("amplitude", 0)) if pd.notna(row.get("amplitude")) else None,
                "turnover": float(row.get("turnover", 0)) if pd.notna(row.get("turnover")) else None,
                "data_source": "akshare",
                "updated_at": datetime.now(),
            }
            records.append(record)

        # 批量写入 (upsert)
        if records:
            from pymongo import UpdateOne

            operations = [
                UpdateOne(
                    {"symbol": r["symbol"], "trade_date": r["trade_date"]},
                    {"$set": r},
                    upsert=True
                )
                for r in records
            ]

            result = self.collection.bulk_write(operations, ordered=False)
            logger.info(f"  写入: {result.upserted_count} 新增, {result.modified_count} 更新")

        return {
            "status": "success",
            "symbol": symbol,
            "name": name,
            "count": len(records),
            "start": start_date,
            "end": today,
        }

    def sync_all(self, full_sync: bool = False) -> List[Dict]:
        """同步所有指数"""
        results = []
        for key in INDEX_CONFIG:
            result = self.sync_index(key, full_sync)
            results.append(result)
        return results

    def get_index_data(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """
        获取指数历史数据

        Args:
            symbol: 指数代码，如 000001 (上证)
            start: 开始日期
            end: 结束日期
        """
        cursor = self.collection.find(
            {"symbol": symbol, "trade_date": {"$gte": start, "$lte": end}},
            {"_id": 0}
        ).sort("trade_date", 1)

        rows = list(cursor)
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def get_index_return(self, symbol: str, start: str, end: str) -> Optional[float]:
        """计算指数区间收益率"""
        df = self.get_index_data(symbol, start, end)
        if len(df) < 2:
            return None

        start_close = df["close"].iloc[0]
        end_close = df["close"].iloc[-1]
        return (end_close - start_close) / start_close * 100

    def show_status(self):
        """显示各指数数据状态"""
        print("\n" + "=" * 70)
        print("指数数据状态".center(70))
        print("=" * 70)

        print(f"{'指数名称':<12} {'代码':<10} {'最新日期':<12} {'记录数':<10} {'最新收盘':<12}")
        print("-" * 70)

        for key, (name, ak_code, exchange) in INDEX_CONFIG.items():
            symbol = ak_code[2:]

            # 获取最新日期和记录数
            latest = self.get_latest_date(symbol)
            count = self.collection.count_documents({"symbol": symbol})

            # 获取最新收盘价
            if latest:
                doc = self.collection.find_one(
                    {"symbol": symbol, "trade_date": latest},
                    {"_id": 0, "close": 1}
                )
                latest_close = doc["close"] if doc else "N/A"
            else:
                latest_close = "N/A"

            latest_str = latest if latest else "无数据"
            close_str = f"{latest_close:.2f}" if isinstance(latest_close, (int, float)) else latest_close

            print(f"{name:<12} {symbol:<10} {latest_str:<12} {count:<10} {close_str:<12}")

        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="大盘指数数据同步")
    parser.add_argument("--index", "-i", type=str, default=None,
                        help="指定指数 (sh/sz/cyb/hs300/zz500/zz1000/sz50/bz50)")
    parser.add_argument("--full", "-f", action="store_true",
                        help="全量同步（默认增量）")
    parser.add_argument("--status", "-s", action="store_true",
                        help="只显示数据状态，不同步")
    parser.add_argument("--all", "-a", action="store_true",
                        help="同步所有指数（默认）")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    syncer = IndexDataSyncer()

    # 只显示状态
    if args.status:
        syncer.show_status()
        return

    # 同步
    if args.index:
        result = syncer.sync_index(args.index, args.full)
        print(f"\n同步结果: {result}")
    else:
        results = syncer.sync_all(args.full)
        print("\n同步汇总:")
        for r in results:
            status = r.get("status")
            if status == "success":
                print(f"  {r['name']}: 新增/更新 {r['count']} 条")
            elif status == "uptodate":
                print(f"  {r['name']}: 已是最新")
            else:
                print(f"  {r.get('name', 'unknown')}: {status}")

    # 显示最终状态
    syncer.show_status()


if __name__ == "__main__":
    main()