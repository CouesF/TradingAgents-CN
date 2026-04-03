#!/usr/bin/env python3
"""
交易日历工具 - 获取真实交易日，排除周末和节假日

支持：
- A股交易日（使用AKShare获取真实交易日历）
- 美股交易日（使用pandas_market_calendars）
- 港股交易日（使用pandas_market_calendars）
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional

# 添加项目根目录
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


class TradingCalendar:
    """交易日历类"""

    def __init__(self):
        self._china_trading_days = None
        self._us_trading_days = None
        self._hk_trading_days = None

    def get_china_trading_days(self, start_date: str, end_date: str) -> List[str]:
        """
        获取A股交易日列表（排除周末和节假日）

        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)

        Returns:
            交易日列表
        """
        try:
            import akshare as ak

            # 获取A股交易日历
            df = ak.tool_trade_date_hist_sina()
            all_dates = df['trade_date'].astype(str).tolist()

            # 过滤日期范围
            trading_days = [d for d in all_dates if start_date <= d <= end_date]

            print(f"[A股交易日历] {start_date} ~ {end_date}: {len(trading_days)} 个交易日")
            return trading_days

        except Exception as e:
            print(f"[警告] 获取A股交易日历失败: {e}，使用简化方法（仅排除周末）")
            return self._get_weekdays(start_date, end_date)

    def get_us_trading_days(self, start_date: str, end_date: str) -> List[str]:
        """
        获取美股交易日列表（排除周末和节假日）

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            交易日列表
        """
        try:
            import pandas_market_calendars as mcal

            # 使用纽交所日历
            nyse = mcal.get_calendar('XNYS')
            schedule = nyse.schedule(start_date, end_date)

            trading_days = schedule.index.strftime('%Y-%m-%d').tolist()

            print(f"[美股交易日历] {start_date} ~ {end_date}: {len(trading_days)} 个交易日")
            return trading_days

        except ImportError:
            print(f"[警告] 未安装 pandas_market_calendars，使用简化方法")
            return self._get_weekdays(start_date, end_date)
        except Exception as e:
            print(f"[警告] 获取美股交易日历失败: {e}，使用简化方法")
            return self._get_weekdays(start_date, end_date)

    def get_hk_trading_days(self, start_date: str, end_date: str) -> List[str]:
        """
        获取港股交易日列表（排除周末和节假日）

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            交易日列表
        """
        try:
            import pandas_market_calendars as mcal

            # 使用港交所日历
            hkex = mcal.get_calendar('XHKG')
            schedule = hkex.schedule(start_date, end_date)

            trading_days = schedule.index.strftime('%Y-%m-%d').tolist()

            print(f"[港股交易日历] {start_date} ~ {end_date}: {len(trading_days)} 个交易日")
            return trading_days

        except ImportError:
            print(f"[警告] 未安装 pandas_market_calendars，使用简化方法")
            return self._get_weekdays(start_date, end_date)
        except Exception as e:
            print(f"[警告] 获取港股交易日历失败: {e}，使用简化方法")
            return self._get_weekdays(start_date, end_date)

    def _get_weekdays(self, start_date: str, end_date: str) -> List[str]:
        """
        简化方法：仅排除周末

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            工作日列表
        """
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        dates = []
        current = start
        while current <= end:
            if current.weekday() < 5:  # 周一到周五
                dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)

        return dates

    def get_trading_days(self, market: str, start_date: str, end_date: str) -> List[str]:
        """
        根据市场类型获取交易日

        Args:
            market: 市场类型 ('china', 'us', 'hk')
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            交易日列表
        """
        if market == 'china':
            return self.get_china_trading_days(start_date, end_date)
        elif market == 'us':
            return self.get_us_trading_days(start_date, end_date)
        elif market == 'hk':
            return self.get_hk_trading_days(start_date, end_date)
        else:
            return self._get_weekdays(start_date, end_date)


def detect_market(stock_id: str) -> str:
    """
    根据股票代码检测市场类型

    Args:
        stock_id: 股票代码

    Returns:
        市场类型 ('china', 'us', 'hk')
    """
    # A股：6位数字
    if stock_id.isdigit() and len(stock_id) == 6:
        return 'china'

    # 港股：数字.HK 或 0开头5位数字
    if '.HK' in stock_id.upper():
        return 'hk'
    if stock_id.isdigit() and len(stock_id) == 5 and stock_id.startswith('0'):
        return 'hk'

    # 默认美股
    return 'us'


# 测试
if __name__ == "__main__":
    calendar = TradingCalendar()

    print("\n=== A股交易日 ===")
    china_days = calendar.get_china_trading_days("2024-01-01", "2024-01-31")
    print(f"示例: {china_days[:5]}")

    print("\n=== 美股交易日 ===")
    us_days = calendar.get_us_trading_days("2024-01-01", "2024-01-31")
    print(f"示例: {us_days[:5]}")

    print("\n=== 港股交易日 ===")
    hk_days = calendar.get_hk_trading_days("2024-01-01", "2024-01-31")
    print(f"示例: {hk_days[:5]}")