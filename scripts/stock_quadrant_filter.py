#!/usr/bin/env python3
"""
股票趋势象限筛选工具

根据趋势特征和波动特征将股票分为四个象限：
- 象限1：强趋势牛股 - 年线向上，高点创新高，中低波动
- 象限2：强趋势熊股 - 年线向下，低点新低，中低波动（本工具排除）
- 象限3：高波动成长 - 宽幅震荡，高波动
- 象限4：题材妖股 - 急涨急跌，极高波动

本工具筛选象限1、3、4的股票
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 加载环境变量
try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from tradingagents.config.database_manager import get_database_manager


@dataclass
class StockQuadrant:
    """股票象限分类结果"""
    symbol: str
    name: str
    quadrant: int  # 1, 3, 4
    trend_score: float  # 趋势强度评分
    volatility_score: float  # 波动率评分
    ma250_slope: float  # 年线斜率
    annual_high_ratio: float  # 当前价与年度高点的比例
    volatility_pct: float  # 波动率百分比
    recent_return_20d: float  # 近20日收益率
    recent_return_60d: float  # 近60日收益率
    total_mv: Optional[float] = None  # 总市值(亿元)
    industry: str = ""  # 行业
    reason: str = ""  # 分类原因说明
    # 基本面数据
    debt_ratio: Optional[float] = None  # 负债率(%)
    roe: Optional[float] = None  # ROE(%)
    fundamental_score: Optional[float] = None  # 基本面评分(百分位)
    fundamental_rank: Optional[int] = None  # 基本面排名


class StockQuadrantFilter:
    """股票象限筛选器"""

    # 象限分类阈值配置
    QUADRANT_THRESHOLDS = {
        # 象限1：强趋势牛股
        'q1': {
            'ma250_slope_threshold': 0.001,  # 年线斜率 > 0.001（向上）
            'high_new_high': True,  # 近期高点创新高
            'volatility_max': 0.03,  # 波动率 <= 3%（中低波动）
            'close_above_ma250': True,  # 收盘价在年线之上
        },
        # 象限3：高波动成长
        'q3': {
            'volatility_min': 0.03,  # 波动率 > 3%
            'volatility_max': 0.06,  # 波动率 <= 6%（高波动）
            'trend_oscillation': True,  # 趋势震荡（非明显单边）
        },
        # 象限4：题材妖股
        'q4': {
            'volatility_min': 0.06,  # 波动率 > 6%（极高波动）
            'rapid_change': True,  # 急涨急跌特征
            'small_cap_max': 50e9,  # 通常市值较小（<50亿）
        }
    }

    def __init__(self, db=None):
        """初始化筛选器"""
        if db is None:
            db_manager = get_database_manager()
            self.db = db_manager.get_mongodb_db()
        else:
            self.db = db

        if self.db is None:
            raise ValueError("MongoDB连接失败，请检查配置")

        self.logger = self._setup_logger()

    def _setup_logger(self):
        """设置日志"""
        import logging
        logger = logging.getLogger('StockQuadrantFilter')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            logger.addHandler(handler)
        return logger

    def get_all_stocks(self, min_data_days: int = 250) -> List[Dict]:
        """
        获取所有有足够历史数据的股票

        Args:
            min_data_days: 最少需要的交易日数据

        Returns:
            股票列表
        """
        # 获取所有股票（实际数据中没有status字段，直接查询）
        basic_info = list(self.db.stock_basic_info.find(
            {},  # 不筛选status，获取所有股票
            {"_id": 0, "code": 1, "symbol": 1, "name": 1, "industry": 1,
             "total_mv": 1, "circ_mv": 1, "market": 1, "sse": 1}
        ))

        if not basic_info:
            self.logger.warning("未找到股票基础信息")
            return []

        stocks = []
        for info in basic_info:
            # 使用 code 或 symbol 字段
            code = info.get('code') or info.get('symbol')
            if not code:
                continue

            # 检查历史数据数量
            count = self.db.stock_daily_quotes.count_documents({
                "symbol": code,
                "period": "daily"
            })

            if count >= min_data_days:
                stocks.append({
                    'code': code,
                    'name': info.get('name', ''),
                    'industry': info.get('industry', ''),
                    'market_cap': info.get('total_mv')  # 市值单位是亿元
                })

        self.logger.info(f"找到 {len(stocks)} 只股票有足够历史数据（>= {min_data_days}天）")
        return stocks

    def get_stock_history(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """
        获取股票历史数据

        Args:
            symbol: 股票代码
            days: 获取的天数

        Returns:
            DataFrame格式的历史数据
        """
        # 使用 YYYY-MM-DD 格式（实际数据库格式）
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        cursor = self.db.stock_daily_quotes.find(
            {
                "symbol": symbol,
                "period": "daily",
                "trade_date": {"$gte": start_date, "$lte": end_date}
            },
            {"_id": 0}
        ).sort("trade_date", 1)

        data = list(cursor)
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)

        # 转换数值列
        numeric_cols = ['open', 'high', 'low', 'close', 'pre_close',
                        'volume', 'amount', 'pct_chg']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        return df

    def get_financial_data(self, symbol: str) -> Dict:
        """
        获取股票财务数据

        Args:
            symbol: 股票代码

        Returns:
            财务数据字典
        """
        # 从 stock_financial_data 获取最新财务数据
        fin_data = self.db.stock_financial_data.find_one(
            {"code": symbol},
            sort=[("ann_date", -1)]
        )

        if not fin_data:
            return {}

        # 从 stock_basic_info 获取 ROE 等指标
        basic_info = self.db.stock_basic_info.find_one({"code": symbol})

        result = {
            'debt_to_assets': fin_data.get('debt_to_assets'),  # 负债率(%)
            'current_ratio': fin_data.get('current_ratio'),  # 流动比率
            'quick_ratio': fin_data.get('quick_ratio'),  # 速动比率
            'gross_margin': fin_data.get('gross_margin'),  # 毛利率(%)
            'netprofit_margin': fin_data.get('netprofit_margin'),  # 净利润率(%)
            'roe': basic_info.get('roe') if basic_info else None,  # ROE(%)
            'pe': basic_info.get('pe') if basic_info else None,  # PE
            'pb': basic_info.get('pb') if basic_info else None,  # PB
        }

        return result

    def calculate_fundamental_score(self, financial_data: Dict, all_stocks_metrics: Dict = None) -> float:
        """
        计算基本面评分（百分位排名）

        Args:
            financial_data: 财务数据字典
            all_stocks_metrics: 所有股票的指标统计（用于计算排名）

        Returns:
            基本面评分（0-100，表示百分位）
        """
        if not financial_data:
            return 0

        # 评分权重
        weights = {
            'roe': 0.35,  # ROE 最重要
            'netprofit_margin': 0.20,  # 净利润率
            'current_ratio': 0.15,  # 流动比率（流动性）
            'gross_margin': 0.15,  # 毛利率
            'debt_ratio_score': 0.15,  # 负债率（越低越好）
        }

        score = 0

        # ROE 评分（越高越好）
        roe = financial_data.get('roe')
        if roe is not None:
            if roe > 15:
                score += 100 * weights['roe']
            elif roe > 10:
                score += 80 * weights['roe']
            elif roe > 5:
                score += 60 * weights['roe']
            elif roe > 0:
                score += 40 * weights['roe']

        # 净利润率评分
        netprofit_margin = financial_data.get('netprofit_margin')
        if netprofit_margin is not None:
            if netprofit_margin > 20:
                score += 100 * weights['netprofit_margin']
            elif netprofit_margin > 10:
                score += 70 * weights['netprofit_margin']
            elif netprofit_margin > 5:
                score += 50 * weights['netprofit_margin']
            elif netprofit_margin > 0:
                score += 30 * weights['netprofit_margin']

        # 流动比率评分（>2 为优秀）
        current_ratio = financial_data.get('current_ratio')
        if current_ratio is not None:
            if current_ratio > 2:
                score += 100 * weights['current_ratio']
            elif current_ratio > 1.5:
                score += 80 * weights['current_ratio']
            elif current_ratio > 1:
                score += 60 * weights['current_ratio']
            elif current_ratio > 0.5:
                score += 40 * weights['current_ratio']

        # 毛利率评分
        gross_margin = financial_data.get('gross_margin')
        if gross_margin is not None:
            if gross_margin > 30:
                score += 100 * weights['gross_margin']
            elif gross_margin > 20:
                score += 70 * weights['gross_margin']
            elif gross_margin > 10:
                score += 50 * weights['gross_margin']
            elif gross_margin > 0:
                score += 30 * weights['gross_margin']

        # 负债率评分（越低越好）
        debt_ratio = financial_data.get('debt_to_assets')
        if debt_ratio is not None:
            if debt_ratio < 30:
                score += 100 * weights['debt_ratio_score']
            elif debt_ratio < 50:
                score += 80 * weights['debt_ratio_score']
            elif debt_ratio < 70:
                score += 50 * weights['debt_ratio_score']
            elif debt_ratio < 80:
                score += 30 * weights['debt_ratio_score']

        return round(score, 2)

    def calculate_indicators(self, df: pd.DataFrame) -> Dict:
        """
        计算趋势和波动指标

        Args:
            df: 历史数据DataFrame

        Returns:
            指标字典
        """
        if df.empty or len(df) < 250:
            return {}

        indicators = {}

        # 1. 移动平均线
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        df['ma120'] = df['close'].rolling(120).mean()
        df['ma250'] = df['close'].rolling(250).mean()

        # 2. 年线斜率（最近60天的年线变化）
        if df['ma250'].iloc[-60:].notna().sum() >= 30:
            ma250_recent = df['ma250'].iloc[-60:].dropna()
            slope = (ma250_recent.iloc[-1] - ma250_recent.iloc[0]) / ma250_recent.iloc[0] / 60
            indicators['ma250_slope'] = slope
        else:
            indicators['ma250_slope'] = 0

        # 3. 当前价与年线关系
        latest_close = df['close'].iloc[-1]
        latest_ma250 = df['ma250'].iloc[-1]
        if latest_ma250 > 0:
            indicators['close_above_ma250'] = latest_close > latest_ma250
            indicators['close_ma250_ratio'] = latest_close / latest_ma250
        else:
            indicators['close_above_ma250'] = False
            indicators['close_ma250_ratio'] = 1

        # 4. 高点创新高判断
        yearly_high = df['high'].iloc[-250:].max()
        recent_high = df['high'].iloc[-20:].max()
        indicators['yearly_high'] = yearly_high
        indicators['recent_high'] = recent_high
        indicators['high_new_high'] = recent_high >= yearly_high * 0.98  # 接近年度高点
        indicators['annual_high_ratio'] = latest_close / yearly_high

        # 5. 低点新低判断
        yearly_low = df['low'].iloc[-250:].min()
        recent_low = df['low'].iloc[-20:].min()
        indicators['yearly_low'] = yearly_low
        indicators['low_new_low'] = recent_low <= yearly_low * 1.02

        # 6. 波动率计算（多种方式）
        # 方式1：收益率标准差
        returns = df['pct_chg'].iloc[-60:].dropna()
        if len(returns) > 20:
            indicators['volatility_std'] = returns.std()
        else:
            indicators['volatility_std'] = 0

        # 方式2：价格振幅（high-low）/close
        amplitude = (df['high'] - df['low']) / df['close']
        indicators['volatility_amplitude'] = amplitude.iloc[-60:].mean()

        # 方式3：ATR（Average True Range）
        tr = pd.DataFrame()
        tr['hl'] = df['high'] - df['low']
        tr['hc'] = abs(df['high'] - df['close'].shift(1))
        tr['lc'] = abs(df['low'] - df['close'].shift(1))
        tr['tr'] = tr.max(axis=1)
        indicators['atr'] = tr['tr'].iloc[-20:].mean()
        indicators['atr_pct'] = indicators['atr'] / latest_close if latest_close > 0 else 0

        # 使用ATR百分比作为主要波动率指标
        indicators['volatility_pct'] = indicators['atr_pct']

        # 7. 收益率
        if len(df) >= 20:
            indicators['return_20d'] = (df['close'].iloc[-1] - df['close'].iloc[-20]) / df['close'].iloc[-20]
        else:
            indicators['return_20d'] = 0

        if len(df) >= 60:
            indicators['return_60d'] = (df['close'].iloc[-1] - df['close'].iloc[-60]) / df['close'].iloc[-60]
        else:
            indicators['return_60d'] = 0

        # 8. 急涨急跌判断（近20天内有单日涨跌超过5%）
        recent_pct_chg = df['pct_chg'].iloc[-20:]
        indicators['rapid_up_days'] = (recent_pct_chg > 5).sum()
        indicators['rapid_down_days'] = (recent_pct_chg < -5).sum()
        indicators['rapid_change'] = (indicators['rapid_up_days'] + indicators['rapid_down_days']) >= 2

        # 9. 趋势强度评分（综合指标）
        trend_score = 0
        if indicators['close_above_ma250']:
            trend_score += 1
        if indicators['ma250_slope'] > 0:
            trend_score += 1
        if indicators['high_new_high']:
            trend_score += 1
        if indicators['return_60d'] > 0:
            trend_score += 1
        indicators['trend_score'] = trend_score

        # 10. 市值
        indicators['total_mv'] = df['total_mv'].iloc[-1] if 'total_mv' in df.columns else None

        return indicators

    def classify_quadrant(self, indicators: Dict, market_cap: float = None) -> Tuple[int, str]:
        """
        根据指标分类象限

        Args:
            indicators: 计算的指标字典
            market_cap: 市值

        Returns:
            (象限编号, 分类原因)
        """
        if not indicators:
            return (0, "数据不足")

        volatility = indicators.get('volatility_pct', 0)
        ma250_slope = indicators.get('ma250_slope', 0)
        close_above_ma250 = indicators.get('close_above_ma250', False)
        high_new_high = indicators.get('high_new_high', False)
        low_new_low = indicators.get('low_new_low', False)
        rapid_change = indicators.get('rapid_change', False)
        trend_score = indicators.get('trend_score', 0)

        # 象限1判断：强趋势牛股
        # 条件：年线向上 + 高点创新高/接近高点 + 中低波动 + 价格在年线上方
        q1_conditions = [
            ma250_slope > 0.0005,  # 年线明显向上
            close_above_ma250,  # 价格在年线上方
            volatility <= 0.04,  # 中低波动
            high_new_high or indicators.get('annual_high_ratio', 0) > 0.85,  # 接近高点
        ]

        if sum(q1_conditions) >= 3 and trend_score >= 3:
            return (1, "年线向上，趋势稳定，中低波动，具备牛股特征")

        # 象限4判断：题材妖股
        # 条件：极高波动 + 急涨急跌 + 通常小市值
        q4_conditions = [
            volatility > 0.05,  # 极高波动
            rapid_change,  # 急涨急跌
            (market_cap is None or market_cap < 100e9),  # 中小市值
        ]

        if sum(q4_conditions) >= 2:
            return (4, "极高波动，急涨急跌，具备妖股特征")

        # 象限3判断：高波动成长
        # 条件：高波动（但不到妖股级别） + 震荡特征
        q3_conditions = [
            volatility > 0.03,  # 高波动
            volatility <= 0.06,  # 但不是极高
            not (high_new_high and close_above_ma250 and ma250_slope > 0.001),  # 非强趋势
            not low_new_low,  # 非强下跌
        ]

        if sum(q3_conditions) >= 2:
            return (3, "高波动震荡，具备成长股波动特征")

        # 无法明确分类
        return (0, "特征不明显，暂不归类")

    def filter_stocks(self,
                      target_quadrants: List[int] = [1, 3, 4],
                      min_data_days: int = 250,
                      max_stocks: int = 100,
                      max_debt_ratio: float = None,
                      min_fundamental_score: float = None) -> List[StockQuadrant]:
        """
        执行筛选

        Args:
            target_quadrants: 目标象限列表
            min_data_days: 最少历史数据天数
            max_stocks: 最大返回数量
            max_debt_ratio: 最大负债率（%），如50表示负债率<50%
            min_fundamental_score: 最小基本面评分（百分位），如60表示前40%

        Returns:
            分类结果列表
        """
        self.logger.info(f"开始筛选象限 {target_quadrants} 的股票...")
        if max_debt_ratio:
            self.logger.info(f"  - 负债率筛选: < {max_debt_ratio}%")
        if min_fundamental_score:
            self.logger.info(f"  - 基本面评分筛选: >= {min_fundamental_score}（前{100-min_fundamental_score}%）")

        # 获取股票列表
        stocks = self.get_all_stocks(min_data_days=min_data_days)

        results = []
        processed = 0

        for stock in stocks:
            processed += 1
            if processed % 100 == 0:
                self.logger.info(f"已处理 {processed}/{len(stocks)} 只股票...")

            symbol = stock['code']
            name = stock['name']
            industry = stock.get('industry', '')
            market_cap = stock.get('market_cap')

            # 获取历史数据
            df = self.get_stock_history(symbol, days=365)
            if df.empty or len(df) < min_data_days:
                continue

            # 计算指标
            indicators = self.calculate_indicators(df)
            if not indicators:
                continue

            # 分类象限
            quadrant, reason = self.classify_quadrant(indicators, market_cap)

            # 只保留目标象限
            if quadrant not in target_quadrants:
                continue

            # 获取财务数据
            financial_data = self.get_financial_data(symbol)
            debt_ratio = financial_data.get('debt_to_assets')
            roe = financial_data.get('roe')
            fundamental_score = self.calculate_fundamental_score(financial_data)

            # 基本面筛选
            if max_debt_ratio is not None:
                if debt_ratio is None or debt_ratio >= max_debt_ratio:
                    continue  # 负债率不满足条件

            if min_fundamental_score is not None:
                if fundamental_score < min_fundamental_score:
                    continue  # 基本面评分不满足条件

            result = StockQuadrant(
                symbol=symbol,
                name=name,
                quadrant=quadrant,
                trend_score=indicators.get('trend_score', 0),
                volatility_score=indicators.get('volatility_pct', 0),
                ma250_slope=indicators.get('ma250_slope', 0),
                annual_high_ratio=indicators.get('annual_high_ratio', 0),
                volatility_pct=indicators.get('volatility_pct', 0),
                recent_return_20d=indicators.get('return_20d', 0),
                recent_return_60d=indicators.get('return_60d', 0),
                total_mv=market_cap,
                industry=industry,
                reason=reason,
                debt_ratio=debt_ratio,
                roe=roe,
                fundamental_score=fundamental_score
            )
            results.append(result)

        self.logger.info(f"筛选完成，找到 {len(results)} 只符合条件的股票")

        # 按象限和基本面评分排序
        results.sort(key=lambda x: (x.quadrant, -x.fundamental_score or 0, -x.trend_score))

        # 补充市值数据（从stock_basic_info查询）
        for r in results:
            if r.total_mv is None or r.total_mv == 0:
                info = self.db.stock_basic_info.find_one({"code": r.symbol})
                if info:
                    r.total_mv = info.get('total_mv')

        return results[:max_stocks]

    def print_results(self, results: List[StockQuadrant]):
        """打印筛选结果"""
        if not results:
            print("未找到符合条件的股票")
            return

        # 按象限分组输出
        for q in [1, 3, 4]:
            q_stocks = [r for r in results if r.quadrant == q]
            if not q_stocks:
                continue

            quadrant_names = {
                1: "象限1：强趋势牛股",
                3: "象限3：高波动成长",
                4: "象限4：题材妖股"
            }

            print(f"\n{'='*100}")
            print(f"{quadrant_names[q]}")
            print(f"{'='*100}")
            print(f"{'代码':<8} {'名称':<10} {'行业':<8} {'负债率':<8} {'ROE':<8} {'基本面':<8} {'波动率':<8} {'20日收益':<10} {'市值(亿)':<10}")
            print(f"{'-'*100}")

            for r in q_stocks:
                mv_display = f"{r.total_mv/1e9:.1f}" if r.total_mv else "N/A"
                debt_display = f"{r.debt_ratio:.1f}%" if r.debt_ratio else "N/A"
                roe_display = f"{r.roe:.1f}%" if r.roe else "N/A"
                fund_display = f"{r.fundamental_score:.0f}" if r.fundamental_score else "N/A"
                print(f"{r.symbol:<8} {r.name:<10} {r.industry:<8} "
                      f"{debt_display:<8} {roe_display:<8} {fund_display:<8} "
                      f"{r.volatility_pct*100:.2f}%{' ':<3} "
                      f"{r.recent_return_20d*100:.1f}%{' ':<5} {mv_display:<10}")

            print(f"\n共 {len(q_stocks)} 只股票")

    def export_to_csv(self, results: List[StockQuadrant], output_path: str = None):
        """导出结果到CSV"""
        if not results:
            return

        if output_path is None:
            output_path = project_root / "output" / "stock_quadrant_filter.csv"

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        df = pd.DataFrame([{
            '代码': r.symbol,
            '名称': r.name,
            '象限': r.quadrant,
            '行业': r.industry,
            '负债率': f"{r.debt_ratio:.1f}%" if r.debt_ratio else 'N/A',
            'ROE': f"{r.roe:.1f}%" if r.roe else 'N/A',
            '基本面评分': r.fundamental_score if r.fundamental_score else 'N/A',
            '趋势评分': r.trend_score,
            '波动率': f"{r.volatility_pct*100:.2f}%",
            '年度高点比例': f"{r.annual_high_ratio*100:.1f}%",
            '20日收益': f"{r.recent_return_20d*100:.1f}%",
            '60日收益': f"{r.recent_return_60d*100:.1f}%",
            '市值(亿)': r.total_mv/1e9 if r.total_mv else '',
            '分类原因': r.reason
        } for r in results])

        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        self.logger.info(f"结果已导出到: {output_path}")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='股票趋势象限筛选工具')
    parser.add_argument('--quadrants', type=str, default='1,3,4',
                        help='筛选的象限，逗号分隔，如 "1,3,4"')
    parser.add_argument('--min-days', type=int, default=250,
                        help='最少历史数据天数')
    parser.add_argument('--max-stocks', type=int, default=100,
                        help='最大返回数量')
    parser.add_argument('--max-debt', type=float, default=None,
                        help='最大负债率(%)，如50表示负债率<50%%')
    parser.add_argument('--min-fund-score', type=float, default=None,
                        help='最小基本面评分(百分位)，如60表示前40%%')
    parser.add_argument('--output', type=str, default=None,
                        help='CSV输出路径')
    parser.add_argument('--no-print', action='store_true',
                        help='不打印结果')

    args = parser.parse_args()

    # 解析象限参数
    target_quadrants = [int(q) for q in args.quadrants.split(',')]

    # 创建筛选器
    filter_tool = StockQuadrantFilter()

    # 执行筛选
    results = filter_tool.filter_stocks(
        target_quadrants=target_quadrants,
        min_data_days=args.min_days,
        max_stocks=args.max_stocks,
        max_debt_ratio=args.max_debt,
        min_fundamental_score=args.min_fund_score
    )

    # 输出结果
    if not args.no_print:
        filter_tool.print_results(results)

    # 导出CSV
    if args.output or not args.no_print:
        filter_tool.export_to_csv(results, args.output)


if __name__ == '__main__':
    main()