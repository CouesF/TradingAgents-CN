#!/usr/bin/env python3
"""
TradingAgents 回测脚本

功能：
1. 支持选择时间范围和股票代码
2. 使用真实交易日历（排除节假日）
3. 调用多智能体进行分析决策
4. 模拟投资组合并计算收益指标
5. 支持多标的批量回测

使用方法：
    # 单标的
    python fyy_test_folder/backtest/run_backtest.py --stock 600036 --start 2024-01-01 --end 2024-03-01
    python fyy_test_folder/backtest/run_backtest.py -s AAPL -st 2024-01-01 -e 2024-03-01 -i 7 -c 100000

    # 多标的（批量回测）
    python fyy_test_folder/backtest/run_backtest.py -s 000001 600036 002957 --start 2025-12-01 --end 2026-03-31

    # 指定星期（Wednesday + Friday 策略）
    python fyy_test_folder/backtest/run_backtest.py -s 920885 920098 --start 2025-12-01 --end 2026-03-31 --days wednesday friday
"""

import sys
import os
import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 添加项目根目录
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

# 导入项目模块
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.utils.logging_manager import get_logger

# 导入交易日历
from trading_calendar import TradingCalendar, detect_market

logger = get_logger('backtest')


class BacktestEngine:
    """回测引擎"""

    # 星期映射
    WEEKDAY_MAP = {
        0: 'monday',
        1: 'tuesday',
        2: 'wednesday',
        3: 'thursday',
        4: 'friday',
        5: 'saturday',
        6: 'sunday'
    }
    WEEKDAY_NAMES = {
        'monday': 0, 'mon': 0,
        'tuesday': 1, 'tue': 1, 'tues': 1,
        'wednesday': 2, 'wed': 2,
        'thursday': 3, 'thu': 3, 'thurs': 3,
        'friday': 4, 'fri': 4,
        'saturday': 5, 'sat': 5,
        'sunday': 6, 'sun': 6
    }

    def __init__(
        self,
        stock_id: str,
        start_date: str,
        end_date: str,
        interval: int = 1,  # 每N个交易日分析一次
        day_filter: List[str] = None,  # 星期过滤器，如 ['wednesday', 'friday']
        config: Dict = None,
        debug: bool = False,
        workers: int = 6  # 并行工作线程数
    ):
        """
        初始化回测引擎

        Args:
            stock_id: 股票代码
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            interval: 分析间隔（每N个交易日分析一次）
            day_filter: 星期过滤器，如 ['wednesday', 'friday'] 或 ['wed', 'fri']
            config: TradingAgentsGraph 配置
            debug: 调试模式
            workers: 并行工作线程数
        """
        self.stock_id = stock_id
        self.start_date = start_date
        self.end_date = end_date
        self.interval = interval
        self.day_filter = self._parse_day_filter(day_filter)
        self.config = config or DEFAULT_CONFIG.copy()
        self.debug = debug
        self.workers = workers

        # 检测市场类型
        self.market = detect_market(stock_id)
        logger.info(f"检测到市场类型: {self.market}")

        # 初始化交易日历
        self.calendar = TradingCalendar()

        # 回测结果
        self._results_lock = threading.Lock()
        self.results: List[Dict] = []
        self.trading_days: List[str] = []

        # 初始化 TradingAgentsGraph（延迟初始化，节省资源）
        self._ta = None

    def _parse_day_filter(self, day_filter: List[str]) -> List[int]:
        """
        解析星期过滤器

        Args:
            day_filter: 星期名称列表，如 ['wednesday', 'friday']

        Returns:
            星期数字列表，如 [2, 4]
        """
        if not day_filter:
            return []

        weekdays = []
        for day in day_filter:
            day_lower = day.lower()
            if day_lower in self.WEEKDAY_NAMES:
                weekdays.append(self.WEEKDAY_NAMES[day_lower])

        return sorted(set(weekdays))

    @property
    def ta(self):
        """延迟初始化 TradingAgentsGraph"""
        if self._ta is None:
            logger.info("初始化 TradingAgentsGraph...")
            self._ta = TradingAgentsGraph(debug=self.debug, config=self.config)
        return self._ta

    def get_trading_days(self) -> List[str]:
        """
        获取交易日列表（排除节假日）

        Returns:
            交易日列表
        """
        trading_days = self.calendar.get_trading_days(
            self.market, self.start_date, self.end_date
        )

        # 按星期过滤
        if self.day_filter:
            original_count = len(trading_days)
            trading_days = [
                d for d in trading_days
                if datetime.strptime(d, "%Y-%m-%d").weekday() in self.day_filter
            ]
            logger.info(f"星期过滤: {original_count} → {len(trading_days)} 个交易日")
            logger.info(f"仅分析: {[self.WEEKDAY_MAP[w].capitalize() for w in self.day_filter]}")

        # 按间隔筛选（仅当未指定星期过滤时）
        if self.interval > 1 and not self.day_filter:
            trading_days = trading_days[::self.interval]
            logger.info(f"间隔筛选: 每{self.interval}个交易日")

        logger.info(f"将分析 {len(trading_days)} 个时间点")
        return trading_days

    def run_single_analysis(self, trade_date: str) -> Dict:
        """
        运行单日分析

        Args:
            trade_date: 交易日期

        Returns:
            分析结果
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"分析 {self.stock_id} @ {trade_date}")
        logger.info(f"{'='*60}")

        try:
            # 调用多智能体分析
            state, decision = self.ta.propagate(self.stock_id, trade_date)

            result = {
                "date": trade_date,
                "stock_id": self.stock_id,
                "action": decision.get("action", "持有"),
                "target_price": decision.get("target_price"),
                "confidence": decision.get("confidence", 0.7),
                "risk_score": decision.get("risk_score", 0.5),
                "reasoning": decision.get("reasoning", "")[:200],  # 截断
                "model_info": decision.get("model_info", ""),
                "success": True,
                "error": None
            }

            logger.info(f"决策: {result['action']}")
            logger.info(f"目标价: {result['target_price']}")
            logger.info(f"置信度: {result['confidence']:.2f}")
            logger.info(f"风险评分: {result['risk_score']:.2f}")

            return result

        except Exception as e:
            logger.error(f"分析失败: {e}")
            return {
                "date": trade_date,
                "stock_id": self.stock_id,
                "action": "持有",
                "target_price": None,
                "confidence": 0.5,
                "risk_score": 0.5,
                "reasoning": f"分析失败: {str(e)}",
                "success": False,
                "error": str(e)
            }

    def get_price_data(self) -> pd.DataFrame:
        """获取价格数据"""
        try:
            # 扩展日期范围
            extended_start = (datetime.strptime(self.start_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
            extended_end = (datetime.strptime(self.end_date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")

            if self.market == 'china':
                return self._get_china_price_data(extended_start, extended_end)
            elif self.market == 'hk':
                return self._get_hk_price_data(extended_start, extended_end)
            else:
                return self._get_us_price_data(extended_start, extended_end)

        except Exception as e:
            logger.error(f"获取价格数据失败: {e}")
            return pd.DataFrame()

    def _get_china_price_data(self, start: str, end: str) -> pd.DataFrame:
        """获取A股价格数据 - 直接从 MongoDB 获取"""
        try:
            from pymongo import MongoClient
            from app.core.config import settings

            client = MongoClient(settings.MONGO_URI)
            db = client[settings.MONGO_DB]

            # 直接查询所有数据，然后在Python端过滤（因为MongoDB查询操作符有问题）
            cursor = db.stock_daily_quotes.find({'symbol': self.stock_id})
            all_data = list(cursor)
            client.close()

            if all_data:
                # Python端过滤
                filtered = [d for d in all_data
                           if d.get('trade_date', '') >= start and d.get('trade_date', '') <= end]

                if filtered:
                    df = pd.DataFrame(filtered)
                    df = df.rename(columns={'trade_date': 'date'})
                    logger.info(f"从MongoDB获取A股数据: {len(filtered)} 条")
                    return df[['date', 'open', 'close', 'high', 'low', 'volume']]

            logger.warning(f"MongoDB中未找到 {self.stock_id} 的价格数据")
        except Exception as e:
            logger.warning(f"MongoDB获取失败: {e}")

        # 备用：AKShare
        try:
            import akshare as ak

            symbol = self.stock_id
            if self.stock_id.isdigit():
                # 北交所股票使用bj前缀
                if self.stock_id.startswith('92') or self.stock_id.startswith('83') or self.stock_id.startswith('87'):
                    symbol = f"bj{self.stock_id}"
                elif self.stock_id.startswith('6'):
                    symbol = f"sh{self.stock_id}"
                else:
                    symbol = f"sz{self.stock_id}"

            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.replace('-', ''),
                end_date=end.replace('-', ''),
                adjust="qfq"
            )

            if not df.empty:
                df.columns = ['date', 'open', 'close', 'high', 'low', 'volume',
                             'turnover', 'amplitude', 'pct_change', 'change', 'turnover_rate']
                df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                logger.info(f"从AKShare获取A股数据: {len(df)} 条")
                return df

        except Exception as e:
            logger.error(f"AKShare获取失败: {e}")

        return pd.DataFrame()

    def _get_hk_price_data(self, start: str, end: str) -> pd.DataFrame:
        """获取港股价格数据"""
        try:
            import yfinance as yf

            symbol = self.stock_id if '.HK' in self.stock_id else f"{self.stock_id}.HK"
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start, end=end)

            if not df.empty:
                df = df.reset_index()
                df['date'] = df['Date'].dt.strftime('%Y-%m-%d')
                df = df.rename(columns={
                    'Open': 'open', 'Close': 'close',
                    'High': 'high', 'Low': 'low', 'Volume': 'volume'
                })
                logger.info(f"获取港股数据: {len(df)} 条")
                return df[['date', 'open', 'close', 'high', 'low', 'volume']]

        except Exception as e:
            logger.error(f"yfinance获取港股失败: {e}")

        return pd.DataFrame()

    def _get_us_price_data(self, start: str, end: str) -> pd.DataFrame:
        """获取美股价格数据"""
        try:
            import yfinance as yf

            ticker = yf.Ticker(self.stock_id)
            df = ticker.history(start=start, end=end)

            if not df.empty:
                df = df.reset_index()
                df['date'] = df['Date'].dt.strftime('%Y-%m-%d')
                df = df.rename(columns={
                    'Open': 'open', 'Close': 'close',
                    'High': 'high', 'Low': 'low', 'Volume': 'volume'
                })
                logger.info(f"获取美股数据: {len(df)} 条")
                return df[['date', 'open', 'close', 'high', 'low', 'volume']]

        except Exception as e:
            logger.error(f"yfinance获取美股失败: {e}")

        return pd.DataFrame()

    def simulate_portfolio(self, initial_capital: float = 100000.0) -> Dict:
        """
        模拟投资组合

        Args:
            initial_capital: 初始资金

        Returns:
            模拟结果
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"投资组合模拟 (初始资金: ¥{initial_capital:,.2f})")
        logger.info(f"{'='*60}")

        # 获取价格数据
        price_df = self.get_price_data()

        if price_df.empty:
            logger.warning("无价格数据，跳过模拟")
            return {"error": "无价格数据"}

        # 创建价格字典
        price_dict = {row['date']: row for _, row in price_df.iterrows()}
        sorted_dates = sorted(price_dict.keys())

        # ★ 按日期排序分析结果（修复并行导致的乱序问题）
        sorted_results = sorted(self.results, key=lambda r: r.get('date', ''))
        logger.info(f"分析结果已按日期排序 ({len(sorted_results)} 条)")

        # 模拟交易
        cash = initial_capital
        position = 0.0
        buy_price = 0.0
        trades = []
        portfolio_values = []

        for result in sorted_results:
            date = result['date']
            action = result['action']

            # 🔥 查找下一个交易日的开盘价（实际交易：T日决策 → T+1日开盘执行）
            next_trade_date = None
            for d in sorted_dates:
                if d > date:  # 找到下一个交易日
                    next_trade_date = d
                    break

            if next_trade_date is None:
                logger.warning(f"[{date}] 找不到下一个交易日，跳过交易")
                continue

            price_info = price_dict[next_trade_date]
            open_price = float(price_info['open'])

            # 执行交易
            if action == "买入" and position == 0 and cash > 0:
                position = cash / open_price
                buy_price = open_price
                cash = 0
                trades.append({
                    'date': next_trade_date,  # 记录实际执行日期
                    'signal_date': date,      # 信号日期
                    'action': 'BUY',
                    'price': open_price,
                    'shares': round(position, 2),
                    'value': round(position * open_price, 2)
                })
                logger.info(f"[信号:{date}] [执行:{next_trade_date}] 买入 {position:.2f}股 @ ¥{open_price:.2f} (开盘价)")

            elif action == "卖出" and position > 0:
                sell_value = position * open_price
                profit = sell_value - (position * buy_price)
                profit_pct = (open_price - buy_price) / buy_price * 100 if buy_price > 0 else 0

                trades.append({
                    'date': next_trade_date,  # 记录实际执行日期
                    'signal_date': date,      # 信号日期
                    'action': 'SELL',
                    'price': open_price,
                    'shares': round(position, 2),
                    'value': round(sell_value, 2),
                    'profit': round(profit, 2),
                    'profit_pct': round(profit_pct, 2)
                })
                logger.info(f"[信号:{date}] [执行:{next_trade_date}] 卖出 盈亏: ¥{profit:.2f} ({profit_pct:.2f}%) (开盘价)")

                cash = sell_value
                position = 0
                buy_price = 0

            if next_trade_date:
                close_price = float(price_info['close'])
                portfolio_values.append({
                    'date': next_trade_date,
                    'value': round(cash + position * close_price, 2)
                })

        # 最终价值（使用最后一个交易日的收盘价）
        last_price = float(price_dict[sorted_dates[-1]]['close']) if sorted_dates else 0
        final_value = cash + position * last_price

        # 计算指标
        total_return = (final_value - initial_capital) / initial_capital * 100

        # 最大回撤
        if portfolio_values:
            values = [pv['value'] for pv in portfolio_values]
            peak = values[0]
            max_drawdown = 0
            for v in values:
                if v > peak:
                    peak = v
                drawdown = (peak - v) / peak * 100 if peak > 0 else 0
                max_drawdown = max(max_drawdown, drawdown)
        else:
            max_drawdown = 0

        # 胜率
        sell_trades = [t for t in trades if t['action'] == 'SELL']
        winning = len([t for t in sell_trades if t.get('profit', 0) > 0])
        win_rate = winning / len(sell_trades) * 100 if sell_trades else 0

        return {
            'initial_capital': initial_capital,
            'final_value': round(final_value, 2),
            'total_return': round(total_return, 2),
            'max_drawdown': round(max_drawdown, 2),
            'total_trades': len(trades),
            'win_rate': round(win_rate, 2),
            'trades': trades
        }

    def run(self, initial_capital: float = 100000.0) -> Dict:
        """
        运行回测

        Args:
            initial_capital: 初始资金

        Returns:
            回测报告
        """
        logger.info(f"\n{'='*80}")
        logger.info(f"TradingAgents 回测")
        logger.info(f"{'='*80}")
        logger.info(f"股票: {self.stock_id} ({self.market})")
        logger.info(f"区间: {self.start_date} ~ {self.end_date}")
        logger.info(f"间隔: 每{self.interval}个交易日")
        if self.day_filter:
            day_names = [self.WEEKDAY_MAP[w].capitalize() for w in self.day_filter]
            logger.info(f"星期过滤: 仅 {', '.join(day_names)}")
        logger.info(f"资金: ¥{initial_capital:,.2f}")

        # 获取交易日
        self.trading_days = self.get_trading_days()

        if not self.trading_days:
            logger.error("没有交易日")
            return {"error": "没有交易日"}

        # 🔥 加载已有进度，跳过已完成的日期
        existing_results = self._load_existing_progress()
        if existing_results:
            completed_dates = {r["date"] for r in existing_results if r.get("success")}
            remaining_days = [d for d in self.trading_days if d not in completed_dates]
            logger.info(f"已有进度: {len(completed_dates)} 个日期已完成")
            logger.info(f"剩余待分析: {len(remaining_days)} 个日期")
            self.trading_days = remaining_days
            self.results = existing_results  # 加载已有结果

            if not self.trading_days:
                logger.info("所有日期已完成分析")
        else:
            logger.info(f"待分析: {len(self.trading_days)} 个时间点")

        logger.info(f"并行工作线程: {self.workers}")

        # 运行分析（并行）
        if self.trading_days:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                # 提交所有任务
                future_to_date = {
                    executor.submit(self.run_single_analysis, date): date
                    for date in self.trading_days
                }

                # 收集结果
                completed = 0
                for future in as_completed(future_to_date):
                    date = future_to_date[future]
                    completed += 1
                    try:
                        result = future.result()
                        with self._results_lock:
                            self.results.append(result)
                        logger.info(f"[{completed}/{len(self.trading_days)}] 完成: {date} -> {result['action']}")
                    except Exception as e:
                        logger.error(f"[{completed}/{len(self.trading_days)}] 失败: {date} -> {e}")
                        with self._results_lock:
                            self.results.append({
                                "date": date,
                                "stock_id": self.stock_id,
                                "action": "持有",
                                "success": False,
                                "error": str(e)
                            })

                    # 每完成一个任务保存中间结果
                    self._save_intermediate_results()

        # ★ 排序后再模拟组合
        self.results = sorted(self.results, key=lambda r: r.get('date', ''))

        portfolio = self.simulate_portfolio(initial_capital)

        # 生成报告（统一 schema）
        day_names = [self.WEEKDAY_MAP[w].capitalize() for w in self.day_filter] if self.day_filter else []
        report = {
            "stock_id": self.stock_id,
            "market": self.market,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "interval": self.interval,
            "analysis_days": ", ".join(day_names) if day_names else f"every {self.interval} trading days",
            "initial_capital": initial_capital,
            "total_analysis_dates": len(self.results),
            "results": self.results,
            "portfolio": portfolio,
            "config": {
                "llm_provider": self.config.get("llm_provider"),
                "deep_think_llm": self.config.get("deep_think_llm"),
                "quick_think_llm": self.config.get("quick_think_llm"),
            }
        }

        return report

    def save_report(self, report: Dict, output_path: str = None) -> str:
        """保存报告"""
        if output_path is None:
            results_dir = Path(__file__).parent / "results"
            results_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = results_dir / f"backtest_{self.stock_id}_{timestamp}.json"

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"\n报告已保存: {output_path}")
        return str(output_path)

    def _save_intermediate_results(self):
        """保存中间分析结果"""
        results_dir = Path(__file__).parent / "results"
        results_dir.mkdir(exist_ok=True)

        # 使用固定文件名保存中间结果
        output_path = results_dir / f"backtest_{self.stock_id}_progress.json"

        report = {
            "stock_id": self.stock_id,
            "market": self.market,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "interval": self.interval,
            "completed": len(self.results),
            "total": len(self.trading_days),
            "results": self.results,
            "config": {
                "llm_provider": self.config.get("llm_provider"),
                "deep_think_llm": self.config.get("deep_think_llm"),
                "quick_think_llm": self.config.get("quick_think_llm"),
            }
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"💾 中间结果已保存 ({len(self.results)}/{len(self.trading_days)})")

    def _load_existing_progress(self) -> List[Dict]:
        """加载已有进度，返回已完成的结果列表"""
        results_dir = Path(__file__).parent / "results"
        progress_file = results_dir / f"backtest_{self.stock_id}_progress.json"

        if not progress_file.exists():
            return []

        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 验证是否是同一个回测任务
            if data.get("stock_id") != self.stock_id:
                return []
            if data.get("start_date") != self.start_date:
                return []
            if data.get("end_date") != self.end_date:
                return []

            # 加载已有结果
            existing_results = data.get("results", [])
            logger.info(f"📂 加载已有进度文件: {len(existing_results)} 个结果")
            return existing_results

        except Exception as e:
            logger.warning(f"⚠️ 加载进度文件失败: {e}")
            return []


def create_config(provider: str) -> Dict:
    """创建配置，从数据库获取API Key"""
    config = DEFAULT_CONFIG.copy()

    if provider == "deepseek":
        config["llm_provider"] = "deepseek"
        config["deep_think_llm"] = "deepseek-chat"
        config["quick_think_llm"] = "deepseek-chat"
    elif provider == "tencent-deepseek":
        # 腾讯云DeepSeek
        config["llm_provider"] = "tencent-deepseek"
        config["deep_think_llm"] = "deepseek-v3.2"
        config["quick_think_llm"] = "deepseek-v3.2"
        config["backend_url"] = "https://api.lkeap.cloud.tencent.com/v1"
    elif provider == "dashscope":
        config["llm_provider"] = "dashscope"
        config["deep_think_llm"] = "qwen-plus"
        config["quick_think_llm"] = "qwen-turbo"
    elif provider == "google":
        config["llm_provider"] = "google"
        config["deep_think_llm"] = "gemini-2.0-flash"
        config["quick_think_llm"] = "gemini-2.0-flash"

    # 🔥 从数据库获取 API Key
    try:
        from pymongo import MongoClient
        from app.core.config import settings

        client = MongoClient(settings.MONGO_URI)
        db = client[settings.MONGO_DB]

        # 从 llm_providers 集合获取 API Key
        providers_collection = db.llm_providers
        provider_doc = providers_collection.find_one({"name": provider})

        if provider_doc and provider_doc.get("api_key"):
            api_key = provider_doc["api_key"]
            if api_key and api_key.strip() and api_key != "your-api-key":
                config["quick_api_key"] = api_key
                config["deep_api_key"] = api_key
                logger.info(f"✅ 从数据库获取 {provider} 的 API Key")

        # 获取 default_base_url
        if provider_doc and provider_doc.get("default_base_url"):
            if "backend_url" not in config or not config.get("backend_url"):
                config["backend_url"] = provider_doc["default_base_url"]

        client.close()
    except Exception as e:
        logger.warning(f"⚠️ 从数据库获取API Key失败: {e}")

    return config


def main():
    parser = argparse.ArgumentParser(description="TradingAgents 回测")

    parser.add_argument("--stock", "-s", nargs='+', required=True,
                        help="股票代码，支持多个，如: -s 000001 600036 或 -s 920885 920098")
    parser.add_argument("--start", "-st", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", "-e", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--interval", "-i", type=int, default=1, help="分析间隔(每N个交易日，指定星期时无效)")
    parser.add_argument("--days", "-D", nargs='+', default=None,
                        help="仅分析指定星期，如: --days wednesday friday 或 --days wed fri")
    parser.add_argument("--capital", "-c", type=float, default=100000.0, help="初始资金")
    parser.add_argument("--provider", "-p", default="tencent-deepseek",
                        choices=["deepseek", "tencent-deepseek", "dashscope", "google"],
                        help="LLM提供商 (默认: tencent-deepseek)")
    parser.add_argument("--output", "-o", help="输出路径(多标的时忽略)")
    parser.add_argument("--workers", "-w", type=int, default=6, help="并行工作线程数 (默认: 6)")
    parser.add_argument("--debug", "-d", action="store_true", help="调试模式")

    args = parser.parse_args()

    # 验证日期
    try:
        datetime.strptime(args.start, "%Y-%m-%d")
        datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        logger.error("日期格式错误，使用 YYYY-MM-DD")
        sys.exit(1)

    # 获取股票列表
    stocks = args.stock
    logger.info(f"\n{'='*80}")
    logger.info(f"多标的回测")
    logger.info(f"{'='*80}")
    logger.info(f"股票数量: {len(stocks)}")
    logger.info(f"股票列表: {', '.join(stocks)}")
    logger.info(f"时间区间: {args.start} ~ {args.end}")

    # 创建配置（所有股票共用）
    config = create_config(args.provider)

    # 汇总结果
    all_results = []

    for idx, stock_id in enumerate(stocks):
        logger.info(f"\n{'='*80}")
        logger.info(f"[{idx+1}/{len(stocks)}] 开始回测: {stock_id}")
        logger.info(f"{'='*80}")

        try:
            # 创建引擎并运行
            engine = BacktestEngine(
                stock_id=stock_id,
                start_date=args.start,
                end_date=args.end,
                interval=args.interval,
                day_filter=args.days,
                config=config,
                debug=args.debug,
                workers=args.workers
            )

            report = engine.run(args.capital)

            # 结果分开保存（每个股票单独一个文件）
            engine.save_report(report, args.output)

            # 记录结果
            portfolio = report.get('portfolio', {})
            all_results.append({
                'stock_id': stock_id,
                'total_return': portfolio.get('total_return', 0),
                'max_drawdown': portfolio.get('max_drawdown', 0),
                'win_rate': portfolio.get('win_rate', 0),
                'total_trades': portfolio.get('total_trades', 0),
                'success': True
            })

            # 打印单个股票摘要
            logger.info(f"\n{'='*60}")
            logger.info(f"[{stock_id}] 回测结果")
            logger.info(f"{'='*60}")
            logger.info(f"总收益率: {portfolio.get('total_return', 0):.2f}%")
            logger.info(f"最大回撤: {portfolio.get('max_drawdown', 0):.2f}%")
            logger.info(f"胜率: {portfolio.get('win_rate', 0):.2f}%")
            logger.info(f"交易次数: {portfolio.get('total_trades', 0)}")

        except Exception as e:
            logger.error(f"[{stock_id}] 回测失败: {e}")
            all_results.append({
                'stock_id': stock_id,
                'total_return': 0,
                'max_drawdown': 0,
                'win_rate': 0,
                'total_trades': 0,
                'success': False,
                'error': str(e)
            })

    # 打印汇总结果
    print_summary_table(all_results)


def print_summary_table(results: List[Dict]):
    """打印汇总结果表格"""
    logger.info(f"\n{'='*80}")
    logger.info(f"多标的回测汇总")
    logger.info(f"{'='*80}")

    # 表头
    header = f"{'股票代码':<12} {'收益率':<12} {'最大回撤':<12} {'胜率':<10} {'交易次数':<10} {'状态':<8}"
    logger.info(header)
    logger.info("-" * 70)

    # 每行结果
    total_return = 0
    success_count = 0

    for r in results:
        status = "成功" if r.get('success') else "失败"
        row = f"{r['stock_id']:<12} {r['total_return']:<12.2f}% {r['max_drawdown']:<12.2f}% {r['win_rate']:<10.2f}% {r['total_trades']:<10} {status:<8}"
        logger.info(row)

        if r.get('success'):
            total_return += r['total_return']
            success_count += 1

    logger.info("-" * 70)

    # 平均收益
    if success_count > 0:
        avg_return = total_return / success_count
        logger.info(f"平均收益率: {avg_return:.2f}%")

    logger.info(f"成功: {success_count}/{len(results)}")

    # 保存汇总结果
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_file = results_dir / f"backtest_summary_{timestamp}.json"

    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump({
            'timestamp': timestamp,
            'total_stocks': len(results),
            'success_count': success_count,
            'average_return': avg_return if success_count > 0 else 0,
            'results': results
        }, f, ensure_ascii=False, indent=2)

    logger.info(f"\n汇总结果已保存: {summary_file}")


if __name__ == "__main__":
    main()