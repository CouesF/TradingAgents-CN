#!/usr/bin/env python3
"""
TradingAgents 回测脚本

功能：
1. 支持选择时间范围 (start_date, end_date)
2. 支持选择股票代码 (stock_id)
3. 使用 TradingAgentsGraph 进行多日期分析
4. 计算投资回报率和策略表现

使用方法：
    python fyy_test_folder/backtest.py --stock 000001 --start 2024-01-01 --end 2024-03-01
    python fyy_test_folder/backtest.py --stock 600036 --start 2024-01-01 --end 2024-06-01 --interval 7
"""

import sys
import os
import argparse
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import pandas as pd

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.utils.logging_manager import get_logger

logger = get_logger('backtest')


class BacktestEngine:
    """回测引擎"""

    def __init__(
        self,
        stock_id: str,
        start_date: str,
        end_date: str,
        interval_days: int = 1,
        config: Dict = None,
        debug: bool = False
    ):
        """
        初始化回测引擎

        Args:
            stock_id: 股票代码 (如: 000001, 600036, AAPL)
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            interval_days: 分析间隔天数 (默认1天)
            config: TradingAgentsGraph 配置
            debug: 是否启用调试模式
        """
        self.stock_id = stock_id
        self.start_date = start_date
        self.end_date = end_date
        self.interval_days = interval_days
        self.config = config or DEFAULT_CONFIG.copy()
        self.debug = debug

        # 回测结果
        self.results: List[Dict] = []
        self.trade_history: List[Dict] = []

        # 初始化 TradingAgentsGraph
        logger.info(f"初始化 TradingAgentsGraph...")
        self.ta = TradingAgentsGraph(debug=debug, config=self.config)

    def generate_trade_dates(self) -> List[str]:
        """
        生成交易日期列表

        Returns:
            交易日期列表 (YYYY-MM-DD格式)
        """
        start = datetime.strptime(self.start_date, "%Y-%m-%d")
        end = datetime.strptime(self.end_date, "%Y-%m-%d")

        dates = []
        current = start
        while current <= end:
            # 跳过周末
            if current.weekday() < 5:  # 0-4 是周一到周五
                dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=self.interval_days)

        return dates

    def run_single_analysis(self, trade_date: str) -> Dict:
        """
        运行单日分析

        Args:
            trade_date: 交易日期

        Returns:
            分析结果字典
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"分析 {self.stock_id} @ {trade_date}")
        logger.info(f"{'='*60}")

        try:
            # 运行 TradingAgentsGraph 分析
            state, decision = self.ta.propagate(self.stock_id, trade_date)

            result = {
                "date": trade_date,
                "stock_id": self.stock_id,
                "action": decision.get("action", "持有"),
                "target_price": decision.get("target_price"),
                "confidence": decision.get("confidence", 0.7),
                "risk_score": decision.get("risk_score", 0.5),
                "reasoning": decision.get("reasoning", ""),
                "model_info": decision.get("model_info", ""),
                "success": True,
                "error": None
            }

            logger.info(f"决策: {result['action']}")
            logger.info(f"目标价格: {result['target_price']}")
            logger.info(f"置信度: {result['confidence']}")
            logger.info(f"风险评分: {result['risk_score']}")

            return result

        except Exception as e:
            logger.error(f"分析失败: {e}")
            return {
                "date": trade_date,
                "stock_id": self.stock_id,
                "action": "持有",
                "target_price": None,
                "confidence": 0,
                "risk_score": 0.5,
                "reasoning": f"分析失败: {str(e)}",
                "success": False,
                "error": str(e)
            }

    def get_price_data(self, date: str) -> Optional[Dict]:
        """
        获取指定日期的价格数据

        Args:
            date: 日期

        Returns:
            价格数据字典
        """
        try:
            from tradingagents.utils.stock_utils import StockUtils
            market_info = StockUtils.get_market_info(self.stock_id)

            if market_info['is_china']:
                # A股数据 - 使用AKShare
                try:
                    import akshare as ak
                    # 格式化股票代码
                    symbol = self.stock_id
                    if self.stock_id.isdigit():
                        if self.stock_id.startswith('6'):
                            symbol = f"sh{self.stock_id}"
                        else:
                            symbol = f"sz{self.stock_id}"

                    # 获取单日数据
                    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y%m%d")
                    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")

                    df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                            start_date=start, end_date=end, adjust="qfq")
                    if not df.empty:
                        # 取最近的一天
                        row = df.iloc[-1]
                        return {
                            "date": date,
                            "open": float(row['开盘']),
                            "close": float(row['收盘']),
                            "high": float(row['最高']),
                            "low": float(row['最低']),
                            "volume": float(row['成交量'])
                        }
                except Exception as e:
                    logger.warning(f"AKShare获取价格失败: {e}")

            elif market_info['is_hk']:
                # 港股数据
                try:
                    import yfinance as yf
                    symbol = self.stock_id if '.HK' in self.stock_id else f"{self.stock_id}.HK"
                    ticker = yf.Ticker(symbol)
                    df = ticker.history(start=date, end=(datetime.strptime(date, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
                    if not df.empty:
                        row = df.iloc[0]
                        return {
                            "date": date,
                            "open": float(row['Open']),
                            "close": float(row['Close']),
                            "high": float(row['High']),
                            "low": float(row['Low']),
                            "volume": float(row['Volume'])
                        }
                except Exception as e:
                    logger.warning(f"yfinance获取港股价格失败: {e}")

            else:
                # 美股数据
                try:
                    import yfinance as yf
                    ticker = yf.Ticker(self.stock_id)
                    df = ticker.history(start=date, end=(datetime.strptime(date, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
                    if not df.empty:
                        row = df.iloc[0]
                        return {
                            "date": date,
                            "open": float(row['Open']),
                            "close": float(row['Close']),
                            "high": float(row['High']),
                            "low": float(row['Low']),
                            "volume": float(row['Volume'])
                        }
                except Exception as e:
                    logger.warning(f"yfinance获取美股价格失败: {e}")

            return None

        except Exception as e:
            logger.warning(f"获取价格数据失败: {e}")
            return None

    def get_all_price_data(self) -> pd.DataFrame:
        """
        获取回测期间的所有价格数据

        Returns:
            价格数据DataFrame
        """
        try:
            from tradingagents.utils.stock_utils import StockUtils
            market_info = StockUtils.get_market_info(self.stock_id)

            # 扩展开始日期，确保有足够的历史数据
            extended_start = (datetime.strptime(self.start_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
            extended_end = (datetime.strptime(self.end_date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")

            if market_info['is_china']:
                try:
                    import akshare as ak
                    symbol = self.stock_id
                    if self.stock_id.isdigit():
                        if self.stock_id.startswith('6'):
                            symbol = f"sh{self.stock_id}"
                        else:
                            symbol = f"sz{self.stock_id}"

                    df = ak.stock_zh_a_hist(
                        symbol=symbol,
                        period="daily",
                        start_date=extended_start.replace('-', ''),
                        end_date=extended_end.replace('-', ''),
                        adjust="qfq"
                    )

                    if not df.empty:
                        df.columns = ['date', 'open', 'close', 'high', 'low', 'volume',
                                     'turnover', 'amplitude', 'pct_change', 'change', 'turnover_rate']
                        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
                        return df
                except Exception as e:
                    logger.error(f"AKShare获取A股数据失败: {e}")

            elif market_info['is_hk']:
                try:
                    import yfinance as yf
                    symbol = self.stock_id if '.HK' in self.stock_id else f"{self.stock_id}.HK"
                    ticker = yf.Ticker(symbol)
                    df = ticker.history(start=extended_start, end=extended_end)
                    if not df.empty:
                        df = df.reset_index()
                        df['date'] = df['Date'].dt.strftime('%Y-%m-%d')
                        df = df.rename(columns={
                            'Open': 'open', 'Close': 'close',
                            'High': 'high', 'Low': 'low', 'Volume': 'volume'
                        })
                        return df[['date', 'open', 'close', 'high', 'low', 'volume']]
                except Exception as e:
                    logger.error(f"yfinance获取港股数据失败: {e}")

            else:
                try:
                    import yfinance as yf
                    ticker = yf.Ticker(self.stock_id)
                    df = ticker.history(start=extended_start, end=extended_end)
                    if not df.empty:
                        df = df.reset_index()
                        df['date'] = df['Date'].dt.strftime('%Y-%m-%d')
                        df = df.rename(columns={
                            'Open': 'open', 'Close': 'close',
                            'High': 'high', 'Low': 'low', 'Volume': 'volume'
                        })
                        return df[['date', 'open', 'close', 'high', 'low', 'volume']]
                except Exception as e:
                    logger.error(f"yfinance获取美股数据失败: {e}")

            return pd.DataFrame()

        except Exception as e:
            logger.error(f"获取所有价格数据失败: {e}")
            return pd.DataFrame()

    def calculate_returns(self) -> Dict:
        """
        计算投资回报

        Returns:
            回报统计字典
        """
        if not self.results:
            return {"total_return": 0, "win_rate": 0, "total_trades": 0}

        # 统计交易
        buy_signals = [r for r in self.results if r["action"] == "买入"]
        sell_signals = [r for r in self.results if r["action"] == "卖出"]
        hold_signals = [r for r in self.results if r["action"] == "持有"]

        total_trades = len(buy_signals) + len(sell_signals)

        stats = {
            "total_trades": total_trades,
            "buy_count": len(buy_signals),
            "sell_count": len(sell_signals),
            "hold_count": len(hold_signals),
            "success_count": len([r for r in self.results if r["success"]]),
            "fail_count": len([r for r in self.results if not r["success"]]),
            "avg_confidence": sum(r["confidence"] for r in self.results) / len(self.results),
            "avg_risk_score": sum(r["risk_score"] for r in self.results) / len(self.results),
        }

        return stats

    def simulate_portfolio(self, initial_capital: float = 100000.0) -> Dict:
        """
        模拟投资组合

        Args:
            initial_capital: 初始资金

        Returns:
            投资组合模拟结果
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"开始投资组合模拟 (初始资金: ¥{initial_capital:,.2f})")
        logger.info(f"{'='*60}")

        # 获取价格数据
        price_df = self.get_all_price_data()

        if price_df.empty:
            logger.warning("无法获取价格数据，跳过投资组合模拟")
            return {"error": "无法获取价格数据"}

        # 创建价格字典
        price_dict = {}
        for _, row in price_df.iterrows():
            price_dict[row['date']] = {
                'open': row['open'],
                'close': row['close'],
                'high': row['high'],
                'low': row['low']
            }

        # 模拟交易
        cash = initial_capital
        position = 0.0  # 持仓数量
        buy_price = 0.0  # 买入价格
        trades = []  # 交易记录
        portfolio_values = []  # 组合价值记录

        for result in self.results:
            date = result['date']
            action = result['action']

            # 查找最近可用价格
            price_info = None
            check_date = date
            for _ in range(5):  # 最多向前查找5天
                if check_date in price_dict:
                    price_info = price_dict[check_date]
                    break
                # 尝试下一个交易日
                next_day = datetime.strptime(check_date, "%Y-%m-%d") + timedelta(days=1)
                check_date = next_day.strftime("%Y-%m-%d")

            if not price_info:
                logger.warning(f"日期 {date} 附近没有价格数据")
                continue

            close_price = price_info['close']

            # 执行交易决策
            if action == "买入" and position == 0 and cash > 0:
                # 全仓买入
                position = cash / close_price
                buy_price = close_price
                cash = 0
                trades.append({
                    'date': date,
                    'action': 'BUY',
                    'price': close_price,
                    'shares': position,
                    'value': position * close_price
                })
                logger.info(f"[{date}] 买入 {position:.2f} 股 @ ¥{close_price:.2f}")

            elif action == "卖出" and position > 0:
                # 全部卖出
                sell_value = position * close_price
                profit = sell_value - (position * buy_price)
                profit_pct = (close_price - buy_price) / buy_price * 100 if buy_price > 0 else 0

                trades.append({
                    'date': date,
                    'action': 'SELL',
                    'price': close_price,
                    'shares': position,
                    'value': sell_value,
                    'profit': profit,
                    'profit_pct': profit_pct,
                    'buy_price': buy_price
                })
                logger.info(f"[{date}] 卖出 {position:.2f} 股 @ ¥{close_price:.2f}, 盈亏: ¥{profit:.2f} ({profit_pct:.2f}%)")

                cash = sell_value
                position = 0
                buy_price = 0

            # 记录组合价值
            portfolio_value = cash + position * close_price
            portfolio_values.append({
                'date': date,
                'value': portfolio_value,
                'cash': cash,
                'position_value': position * close_price,
                'position': position,
                'price': close_price
            })

        # 计算最终价值
        if position > 0:
            last_date = list(price_dict.keys())[-1]
            final_price = price_dict[last_date]['close']
            final_value = cash + position * final_price
        else:
            final_value = cash

        # 计算指标
        total_return = (final_value - initial_capital) / initial_capital * 100

        # 计算最大回撤
        if len(portfolio_values) > 1:
            values = [pv['value'] for pv in portfolio_values]
            peak = values[0]
            max_drawdown = 0
            for v in values:
                if v > peak:
                    peak = v
                drawdown = (peak - v) / peak * 100 if peak > 0 else 0
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
        else:
            max_drawdown = 0

        # 计算胜率
        sell_trades = [t for t in trades if t['action'] == 'SELL']
        winning_trades = [t for t in sell_trades if t.get('profit', 0) > 0]
        win_rate = len(winning_trades) / len(sell_trades) * 100 if sell_trades else 0

        # 平均盈亏
        avg_profit = sum(t.get('profit', 0) for t in sell_trades) / len(sell_trades) if sell_trades else 0

        result = {
            'initial_capital': initial_capital,
            'final_value': final_value,
            'total_return': total_return,
            'max_drawdown': max_drawdown,
            'total_trades': len(trades),
            'buy_trades': len([t for t in trades if t['action'] == 'BUY']),
            'sell_trades': len(sell_trades),
            'win_rate': win_rate,
            'avg_profit': avg_profit,
            'trades': trades,
            'portfolio_values': portfolio_values
        }

        logger.info(f"\n{'='*60}")
        logger.info(f"投资组合模拟结果")
        logger.info(f"{'='*60}")
        logger.info(f"初始资金: ¥{initial_capital:,.2f}")
        logger.info(f"最终价值: ¥{final_value:,.2f}")
        logger.info(f"总收益率: {total_return:.2f}%")
        logger.info(f"最大回撤: {max_drawdown:.2f}%")
        logger.info(f"胜率: {win_rate:.2f}%")
        logger.info(f"交易次数: {len(trades)} (买入: {len([t for t in trades if t['action'] == 'BUY'])}, 卖出: {len(sell_trades)})")

        return result

    def run(self, initial_capital: float = 100000.0) -> Dict:
        """
        运行完整回测

        Args:
            initial_capital: 初始资金

        Returns:
            回测结果字典
        """
        logger.info(f"\n{'='*80}")
        logger.info(f"开始回测")
        logger.info(f"{'='*80}")
        logger.info(f"股票代码: {self.stock_id}")
        logger.info(f"时间范围: {self.start_date} ~ {self.end_date}")
        logger.info(f"分析间隔: {self.interval_days} 天")
        logger.info(f"初始资金: ¥{initial_capital:,.2f}")

        # 生成交易日期
        trade_dates = self.generate_trade_dates()
        logger.info(f"交易日期数量: {len(trade_dates)}")

        # 运行分析
        for i, date in enumerate(trade_dates):
            logger.info(f"\n[{i+1}/{len(trade_dates)}] 分析 {self.stock_id} @ {date}")
            result = self.run_single_analysis(date)
            self.results.append(result)

        # 计算统计
        stats = self.calculate_returns()

        # 模拟投资组合
        portfolio = self.simulate_portfolio(initial_capital)

        # 生成报告
        report = {
            "stock_id": self.stock_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "interval_days": self.interval_days,
            "initial_capital": initial_capital,
            "total_dates": len(trade_dates),
            "results": self.results,
            "statistics": stats,
            "portfolio": portfolio,
            "config": {
                "llm_provider": self.config.get("llm_provider"),
                "deep_think_llm": self.config.get("deep_think_llm"),
                "quick_think_llm": self.config.get("quick_think_llm"),
            }
        }

        return report

    def save_report(self, output_path: str = None, initial_capital: float = 100000.0) -> str:
        """
        保存回测报告

        Args:
            output_path: 输出路径
            initial_capital: 初始资金

        Returns:
            保存的文件路径
        """
        if output_path is None:
            # 默认保存到 fyy_test_folder/results/
            results_dir = project_root / "fyy_test_folder" / "backtest_results"
            results_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = results_dir / f"backtest_{self.stock_id}_{timestamp}.json"

        # 运行回测
        report = self.run(initial_capital)

        # 保存JSON
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"\n报告已保存: {output_path}")

        return str(output_path)

    def print_summary(self):
        """打印回测摘要"""
        if not self.results:
            logger.warning("没有回测结果")
            return

        stats = self.calculate_returns()

        logger.info(f"\n{'='*80}")
        logger.info(f"回测摘要")
        logger.info(f"{'='*80}")

        logger.info(f"总分析次数: {stats['total_trades'] + stats['hold_count']}")
        logger.info(f"买入信号: {stats['buy_count']}")
        logger.info(f"卖出信号: {stats['sell_count']}")
        logger.info(f"持有信号: {stats['hold_count']}")
        logger.info(f"成功分析: {stats['success_count']}")
        logger.info(f"失败分析: {stats['fail_count']}")
        logger.info(f"平均置信度: {stats['avg_confidence']:.2f}")
        logger.info(f"平均风险评分: {stats['avg_risk_score']:.2f}")


def create_config(provider: str = "deepseek") -> Dict:
    """
    创建配置

    Args:
        provider: LLM 提供商

    Returns:
        配置字典
    """
    config = DEFAULT_CONFIG.copy()

    if provider == "deepseek":
        config["llm_provider"] = "deepseek"
        config["deep_think_llm"] = "deepseek-chat"
        config["quick_think_llm"] = "deepseek-chat"
        config["backend_url"] = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    elif provider == "dashscope":
        config["llm_provider"] = "dashscope"
        config["deep_think_llm"] = "qwen-plus"
        config["quick_think_llm"] = "qwen-turbo"
    elif provider == "google":
        config["llm_provider"] = "google"
        config["deep_think_llm"] = "gemini-2.0-flash"
        config["quick_think_llm"] = "gemini-2.0-flash"

    return config


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="TradingAgents 回测脚本")

    parser.add_argument(
        "--stock", "-s",
        type=str,
        required=True,
        help="股票代码 (如: 000001, 600036, AAPL)"
    )

    parser.add_argument(
        "--start", "-st",
        type=str,
        required=True,
        help="开始日期 (YYYY-MM-DD)"
    )

    parser.add_argument(
        "--end", "-e",
        type=str,
        required=True,
        help="结束日期 (YYYY-MM-DD)"
    )

    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=7,
        help="分析间隔天数 (默认: 7天)"
    )

    parser.add_argument(
        "--capital", "-c",
        type=float,
        default=100000.0,
        help="初始资金 (默认: 100000)"
    )

    parser.add_argument(
        "--provider", "-p",
        type=str,
        default="deepseek",
        choices=["deepseek", "dashscope", "google", "openai"],
        help="LLM 提供商 (默认: deepseek)"
    )

    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="输出文件路径"
    )

    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="启用调试模式"
    )

    args = parser.parse_args()

    # 验证日期格式
    try:
        datetime.strptime(args.start, "%Y-%m-%d")
        datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        logger.error("日期格式错误，请使用 YYYY-MM-DD 格式")
        sys.exit(1)

    # 打印配置信息
    logger.info(f"\n{'='*80}")
    logger.info(f"TradingAgents-CN 回测系统")
    logger.info(f"{'='*80}")
    logger.info(f"股票代码: {args.stock}")
    logger.info(f"时间范围: {args.start} ~ {args.end}")
    logger.info(f"分析间隔: {args.interval} 天")
    logger.info(f"初始资金: ¥{args.capital:,.2f}")
    logger.info(f"LLM 提供商: {args.provider}")
    logger.info(f"{'='*80}")

    # 创建配置
    config = create_config(args.provider)

    # 运行回测
    engine = BacktestEngine(
        stock_id=args.stock,
        start_date=args.start,
        end_date=args.end,
        interval_days=args.interval,
        config=config,
        debug=args.debug
    )

    # 保存报告
    output_path = engine.save_report(args.output, args.capital)

    # 打印摘要
    engine.print_summary()

    # 打印投资组合结果
    logger.info(f"\n{'='*80}")
    logger.info(f"使用方法:")
    logger.info(f"{'='*80}")
    logger.info(f"python fyy_test_folder/backtest.py --stock 600036 --start 2024-01-01 --end 2024-03-01")
    logger.info(f"python fyy_test_folder/backtest.py --stock AAPL --start 2024-01-01 --end 2024-03-01 --interval 7")
    logger.info(f"python fyy_test_folder/backtest.py -s 000001 -st 2024-01-01 -e 2024-06-01 -c 500000 -p dashscope")


if __name__ == "__main__":
    main()