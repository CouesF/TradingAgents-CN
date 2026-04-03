#!/usr/bin/env python3
"""
多因子选股筛选器 v2

支持 --as-of-date 参数：所有计算只使用截止日之前的数据，
消除前视偏差（look-ahead bias），使筛选结果可用于后续区间的回测验证。

使用方法：
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    # 用最新数据筛选（默认）
    python scripts/stock_screener_v2.py --top 30

    # 回测模式：假装站在 2025-12-01，只用此前数据筛选
    python scripts/stock_screener_v2.py --as-of-date 2025-12-01 --top 20

    # 筛完直接跑回测
    python scripts/stock_screener_v2.py --as-of-date 2025-12-01 --top 10
    # 然后把输出的代码喂给 run_backtest.py --start 2025-12-01 --end 2026-03-31

    # 只看科创板
    python scripts/stock_screener_v2.py --market 科创板 --top 20

    # 自定义参数
    python scripts/stock_screener_v2.py --top 50 --min-amount 5000 --output output/screener_v2.csv
"""

import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from tradingagents.config.database_manager import get_database_manager


@dataclass
class StockScore:
    """单只股票的综合评分结果"""
    symbol: str
    name: str
    industry: str = ""
    market: str = ""

    # 价格指标
    close: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma120: float = 0.0
    ma250: float = 0.0

    # 趋势打分（0-100）
    trend_score: float = 0.0
    ma_alignment: int = 0       # 均线多头排列得分
    momentum_20d: float = 0.0   # 20 日动量
    momentum_60d: float = 0.0   # 60 日动量
    relative_strength: float = 0.0  # 相对强弱（vs 全市场中位数）
    above_ma250: bool = False
    ma250_slope_20d: float = 0.0

    # 基本面打分（0-100）
    fundamental_score: float = 0.0
    roe: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None
    debt_ratio: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_growth: Optional[float] = None

    # 波动与流动性
    volatility_20d: float = 0.0
    avg_amount_20d: float = 0.0  # 20 日均成交额（万元）
    total_mv: float = 0.0       # 总市值（万元）

    # 综合得分
    composite_score: float = 0.0
    rank: int = 0
    tag: str = ""  # 分类标签


class StockScreenerV2:
    """多因子选股筛选器"""

    # 权重配置
    WEIGHTS = {
        'trend': 0.55,
        'fundamental': 0.30,
        'liquidity': 0.15,
    }

    def __init__(self, db=None, as_of_date: str = None):
        """
        Args:
            db: MongoDB database 实例
            as_of_date: 截止日期 (YYYY-MM-DD)，所有计算只用此日期及之前的数据。
                        为 None 时使用全部最新数据。
        """
        if db is None:
            db_manager = get_database_manager()
            self.db = db_manager.get_mongodb_db()
        else:
            self.db = db
        if self.db is None:
            raise ValueError("MongoDB 连接失败")
        self.as_of_date = as_of_date
        self.logger = self._setup_logger()

    def _setup_logger(self):
        logger = logging.getLogger('StockScreenerV2')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
            logger.addHandler(handler)
        return logger

    # ------------------------------------------------------------------
    # 第一层：获取候选股票池
    # ------------------------------------------------------------------
    def _quote_query(self, symbol: str, extra: dict = None) -> dict:
        """构建行情查询条件，自动加入 as_of_date 过滤"""
        q = {"symbol": symbol}
        if self.as_of_date:
            q["trade_date"] = {"$lte": self.as_of_date}
        if extra:
            q.update(extra)
        return q

    def get_candidate_pool(self,
                           min_data_days: int = 120,
                           min_avg_amount: float = 3000,
                           market_filter: str = None) -> List[Dict]:
        """
        获取候选股票池（硬性排除）

        Args:
            min_data_days: 最少历史行情天数
            min_avg_amount: 最近 20 日均成交额下限（万元）
            market_filter: 市场过滤（主板/科创板/创业板 等）
        """
        query = {}
        if market_filter:
            query["market"] = market_filter

        all_basics = list(self.db.stock_basic_info.find(
            query,
            {"_id": 0, "code": 1, "name": 1, "industry": 1, "market": 1,
             "total_mv": 1, "roe": 1, "pe": 1, "pb": 1, "pe_ttm": 1}
        ))
        self.logger.info("stock_basic_info 共 %d 条记录", len(all_basics))

        candidates = []
        excluded_st = 0
        excluded_data = 0
        excluded_liq = 0

        for info in all_basics:
            code = info.get('code', '')
            name = info.get('name', '')

            if not name or 'ST' in name.upper() or '退' in name:
                excluded_st += 1
                continue

            if code.startswith('200') or code.startswith('900'):
                excluded_st += 1
                continue

            count = self.db.stock_daily_quotes.count_documents(self._quote_query(code))
            if count < min_data_days:
                excluded_data += 1
                continue

            recent = list(self.db.stock_daily_quotes.find(
                self._quote_query(code),
                {"_id": 0, "amount": 1, "volume": 1, "trade_date": 1}
            ).sort("trade_date", -1).limit(20))

            if len(recent) < 10:
                excluded_data += 1
                continue

            amounts = []
            for r in recent:
                amt = r.get('amount')
                if amt is not None:
                    try:
                        amounts.append(float(amt))
                    except (ValueError, TypeError):
                        pass

            if not amounts:
                excluded_liq += 1
                continue

            avg_amount = np.mean(amounts)
            avg_amount_wan = avg_amount / 10000.0

            if avg_amount_wan < min_avg_amount:
                excluded_liq += 1
                continue

            candidates.append({
                'code': code,
                'name': name,
                'industry': info.get('industry', ''),
                'market': info.get('market', ''),
                'total_mv': info.get('total_mv', 0),
                'roe': info.get('roe'),
                'pe': info.get('pe'),
                'pb': info.get('pb'),
                'pe_ttm': info.get('pe_ttm'),
                'avg_amount_wan': avg_amount_wan,
            })

        self.logger.info(
            "候选池: %d 只 (排除 ST/B股 %d, 数据不足 %d, 流动性不足 %d)",
            len(candidates), excluded_st, excluded_data, excluded_liq)
        return candidates

    # ------------------------------------------------------------------
    # 第二层：趋势健康度打分
    # ------------------------------------------------------------------
    def calculate_trend_score(self, symbol: str) -> Dict:
        """计算趋势相关指标（自动尊重 as_of_date）"""
        cursor = self.db.stock_daily_quotes.find(
            self._quote_query(symbol),
            {"_id": 0, "trade_date": 1, "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 1, "amount": 1}
        ).sort("trade_date", -1).limit(300)

        rows = list(cursor)
        if len(rows) < 60:
            return {}

        rows.reverse()
        df = pd.DataFrame(rows)
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        close = df['close']
        n = len(close)

        # 均线
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        ma120 = close.rolling(120).mean() if n >= 120 else pd.Series([np.nan]*n)
        ma250 = close.rolling(250).mean() if n >= 250 else pd.Series([np.nan]*n)

        last_close = float(close.iloc[-1])
        last_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0
        last_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0
        last_ma120 = float(ma120.iloc[-1]) if pd.notna(ma120.iloc[-1]) else 0
        last_ma250 = float(ma250.iloc[-1]) if pd.notna(ma250.iloc[-1]) else 0

        # 均线多头排列打分 (0-25)
        ma_score = 0
        if last_ma20 > 0 and last_close > last_ma20:
            ma_score += 5
        if last_ma60 > 0 and last_ma20 > last_ma60:
            ma_score += 5
        if last_ma120 > 0 and last_ma60 > last_ma120:
            ma_score += 5
        if last_ma250 > 0 and last_ma120 > last_ma250:
            ma_score += 5
        if last_ma250 > 0 and last_close > last_ma250:
            ma_score += 5

        # 动量 (0-35)
        ret_20d = (last_close / float(close.iloc[-21]) - 1) if n >= 21 else 0
        ret_60d = (last_close / float(close.iloc[-61]) - 1) if n >= 61 else 0

        momentum_score = 0
        # 20d 动量
        if ret_20d > 0.15:
            momentum_score += 15
        elif ret_20d > 0.05:
            momentum_score += 12
        elif ret_20d > 0:
            momentum_score += 8
        elif ret_20d > -0.05:
            momentum_score += 4
        # 负动量不得分

        # 60d 动量
        if ret_60d > 0.20:
            momentum_score += 20
        elif ret_60d > 0.10:
            momentum_score += 15
        elif ret_60d > 0:
            momentum_score += 10
        elif ret_60d > -0.10:
            momentum_score += 5

        # MA250 斜率 (0-20)
        slope_score = 0
        if n >= 270:
            ma250_20ago = float(ma250.iloc[-21]) if pd.notna(ma250.iloc[-21]) else 0
            if ma250_20ago > 0 and last_ma250 > 0:
                slope = (last_ma250 - ma250_20ago) / ma250_20ago
                if slope > 0.02:
                    slope_score = 20
                elif slope > 0.01:
                    slope_score = 15
                elif slope > 0.005:
                    slope_score = 10
                elif slope > 0:
                    slope_score = 5
            else:
                slope = 0
        else:
            slope = 0

        # 距离年内高点 (0-20)
        high_252 = float(df['high'].tail(min(252, n)).max())
        distance_to_high = last_close / high_252 if high_252 > 0 else 0
        if distance_to_high > 0.95:
            distance_score = 20
        elif distance_to_high > 0.85:
            distance_score = 15
        elif distance_to_high > 0.75:
            distance_score = 10
        elif distance_to_high > 0.65:
            distance_score = 5
        else:
            distance_score = 0

        total_trend = ma_score + momentum_score + slope_score + distance_score

        # 波动率
        daily_returns = close.pct_change().dropna().tail(20)
        vol_20d = float(daily_returns.std()) if len(daily_returns) >= 10 else 0

        return {
            'close': last_close,
            'ma20': last_ma20,
            'ma60': last_ma60,
            'ma120': last_ma120,
            'ma250': last_ma250,
            'ma_alignment': ma_score,
            'momentum_20d': ret_20d,
            'momentum_60d': ret_60d,
            'above_ma250': last_close > last_ma250 if last_ma250 > 0 else False,
            'ma250_slope_20d': slope if n >= 270 else 0,
            'trend_score': min(total_trend, 100),
            'volatility_20d': vol_20d,
            'distance_to_high': distance_to_high,
        }

    # ------------------------------------------------------------------
    # 第三层：基本面质量打分
    # ------------------------------------------------------------------
    def calculate_fundamental_score(self, basic_info: Dict) -> Dict:
        """计算基本面评分"""
        code = basic_info['code']
        score = 0

        roe = basic_info.get('roe')
        pe = basic_info.get('pe')
        pb = basic_info.get('pb')

        fin_query = {"code": code}
        if self.as_of_date:
            fin_query["ann_date"] = {"$lte": self.as_of_date}
        fin = self.db.stock_financial_data.find_one(
            fin_query, sort=[("ann_date", -1)])

        debt_ratio = None
        gross_margin = None
        net_margin = None
        current_ratio = None

        if fin:
            total_assets = fin.get('total_assets')
            total_liab = fin.get('total_liab')
            if total_assets and total_liab:
                try:
                    debt_ratio = float(total_liab) / float(total_assets) * 100
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

            revenue = fin.get('revenue')
            net_profit = fin.get('net_profit')
            oper_cost = fin.get('oper_cost')

            if revenue and oper_cost:
                try:
                    gross_margin = (1 - float(oper_cost) / float(revenue)) * 100
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

            if revenue and net_profit:
                try:
                    net_margin = float(net_profit) / float(revenue) * 100
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

            total_cur_assets = fin.get('total_cur_assets')
            total_cur_liab = fin.get('total_cur_liab')
            if total_cur_assets and total_cur_liab:
                try:
                    current_ratio = float(total_cur_assets) / float(total_cur_liab)
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

        # ROE 评分 (0-30)
        if roe is not None:
            if roe > 20:
                score += 30
            elif roe > 15:
                score += 25
            elif roe > 10:
                score += 20
            elif roe > 5:
                score += 12
            elif roe > 0:
                score += 5

        # 负债率评分 (0-20, 越低越好)
        if debt_ratio is not None:
            if debt_ratio < 20:
                score += 20
            elif debt_ratio < 35:
                score += 16
            elif debt_ratio < 50:
                score += 12
            elif debt_ratio < 65:
                score += 6

        # 毛利率评分 (0-20)
        if gross_margin is not None:
            if gross_margin > 50:
                score += 20
            elif gross_margin > 35:
                score += 15
            elif gross_margin > 20:
                score += 10
            elif gross_margin > 10:
                score += 5

        # 净利率评分 (0-15)
        if net_margin is not None:
            if net_margin > 25:
                score += 15
            elif net_margin > 15:
                score += 12
            elif net_margin > 8:
                score += 8
            elif net_margin > 0:
                score += 4

        # PE 合理性 (0-15)
        if pe is not None and pe > 0:
            if 5 < pe < 15:
                score += 15
            elif 15 <= pe < 30:
                score += 10
            elif 30 <= pe < 60:
                score += 5

        return {
            'fundamental_score': min(score, 100),
            'roe': roe,
            'pe': pe,
            'pb': pb,
            'debt_ratio': round(debt_ratio, 1) if debt_ratio else None,
            'gross_margin': round(gross_margin, 1) if gross_margin else None,
            'net_margin': round(net_margin, 1) if net_margin else None,
        }

    # ------------------------------------------------------------------
    # 第四层：综合打分 & 输出
    # ------------------------------------------------------------------
    def screen(self,
               top_n: int = 30,
               min_data_days: int = 120,
               min_avg_amount: float = 3000,
               market_filter: str = None,
               min_trend_score: float = 30,
               min_fundamental_score: float = 20) -> List[StockScore]:
        """
        执行完整筛选流程

        Args:
            top_n: 输出前 N 名
            min_data_days: 最少行情天数
            min_avg_amount: 最低日均成交额（万元）
            market_filter: 市场板块过滤
            min_trend_score: 趋势分最低门槛
            min_fundamental_score: 基本面分最低门槛
        """
        self.logger.info("=" * 70)
        self.logger.info("多因子选股筛选器 v2 启动")
        if self.as_of_date:
            self.logger.info("截止日期 (as_of_date): %s — 仅使用此日期及之前的数据", self.as_of_date)
        else:
            self.logger.info("未设定截止日期，使用全部最新数据")
        self.logger.info("=" * 70)

        # 1. 获取候选池
        candidates = self.get_candidate_pool(
            min_data_days=min_data_days,
            min_avg_amount=min_avg_amount,
            market_filter=market_filter)

        if not candidates:
            self.logger.warning("候选池为空")
            return []

        # 2. 逐个打分
        results: List[StockScore] = []
        total = len(candidates)

        for i, info in enumerate(candidates):
            if (i + 1) % 100 == 0:
                self.logger.info("进度: %d/%d ...", i + 1, total)

            code = info['code']

            trend = self.calculate_trend_score(code)
            if not trend:
                continue

            if trend['trend_score'] < min_trend_score:
                continue

            fund = self.calculate_fundamental_score(info)
            if fund['fundamental_score'] < min_fundamental_score:
                continue

            # 流动性得分 (0-100)
            avg_amount = info.get('avg_amount_wan', 0)
            if avg_amount > 50000:
                liq_score = 100
            elif avg_amount > 20000:
                liq_score = 80
            elif avg_amount > 10000:
                liq_score = 60
            elif avg_amount > 5000:
                liq_score = 40
            else:
                liq_score = 20

            # 综合得分
            composite = (
                self.WEIGHTS['trend'] * trend['trend_score'] +
                self.WEIGHTS['fundamental'] * fund['fundamental_score'] +
                self.WEIGHTS['liquidity'] * liq_score
            )

            # 分类标签
            ts = trend['trend_score']
            if ts >= 70 and trend.get('above_ma250'):
                tag = "强趋势"
            elif ts >= 50:
                tag = "趋势向好"
            elif ts >= 30:
                tag = "企稳回升"
            else:
                tag = "弱势"

            total_mv = info.get('total_mv', 0)

            s = StockScore(
                symbol=code,
                name=info['name'],
                industry=info.get('industry', ''),
                market=info.get('market', ''),
                close=trend['close'],
                ma20=trend['ma20'],
                ma60=trend['ma60'],
                ma120=trend['ma120'],
                ma250=trend['ma250'],
                trend_score=trend['trend_score'],
                ma_alignment=trend['ma_alignment'],
                momentum_20d=trend['momentum_20d'],
                momentum_60d=trend['momentum_60d'],
                above_ma250=trend.get('above_ma250', False),
                ma250_slope_20d=trend.get('ma250_slope_20d', 0),
                fundamental_score=fund['fundamental_score'],
                roe=fund.get('roe'),
                pe=fund.get('pe'),
                pb=fund.get('pb'),
                debt_ratio=fund.get('debt_ratio'),
                gross_margin=fund.get('gross_margin'),
                net_margin=fund.get('net_margin'),
                volatility_20d=trend['volatility_20d'],
                avg_amount_20d=avg_amount,
                total_mv=total_mv,
                composite_score=round(composite, 2),
                tag=tag,
            )
            results.append(s)

        # 3. 排序
        results.sort(key=lambda x: -x.composite_score)
        for i, r in enumerate(results):
            r.rank = i + 1

        self.logger.info("通过筛选: %d 只，输出 Top %d", len(results), top_n)
        return results[:top_n]

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def print_results(self, results: List[StockScore]):
        if not results:
            print("无结果")
            return

        header = (f"{'排名':<5} {'代码':<8} {'名称':<10} {'行业':<8} {'板块':<6} "
                  f"{'现价':<8} {'趋势分':<7} {'基本面':<7} {'综合分':<7} "
                  f"{'20d动量':<9} {'60d动量':<9} {'ROE':<7} {'负债率':<7} {'标签':<8}")
        print("=" * 130)
        print(header)
        print("-" * 130)

        for r in results:
            roe_s = f"{r.roe:.1f}%" if r.roe else "N/A"
            debt_s = f"{r.debt_ratio:.0f}%" if r.debt_ratio else "N/A"
            print(f"{r.rank:<5} {r.symbol:<8} {r.name:<10} {r.industry:<8} {r.market:<6} "
                  f"{r.close:<8.2f} {r.trend_score:<7.0f} {r.fundamental_score:<7.0f} {r.composite_score:<7.1f} "
                  f"{r.momentum_20d*100:<9.1f}% {r.momentum_60d*100:<8.1f}% {roe_s:<7} {debt_s:<7} {r.tag:<8}")

        print("=" * 130)
        print(f"共 {len(results)} 只")

    def export_csv(self, results: List[StockScore], output_path: str = None):
        if not results:
            return
        if output_path is None:
            output_path = project_root / "output" / "screener_v2.csv"
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        for r in results:
            rows.append({
                '排名': r.rank,
                '代码': r.symbol,
                '名称': r.name,
                '行业': r.industry,
                '板块': r.market,
                '现价': round(r.close, 2),
                '趋势分': round(r.trend_score, 1),
                '基本面分': round(r.fundamental_score, 1),
                '综合分': round(r.composite_score, 1),
                '标签': r.tag,
                '20日动量': f"{r.momentum_20d*100:.1f}%",
                '60日动量': f"{r.momentum_60d*100:.1f}%",
                'MA多头排列': r.ma_alignment,
                '站上年线': '是' if r.above_ma250 else '否',
                'ROE': f"{r.roe:.1f}" if r.roe else '',
                'PE': f"{r.pe:.1f}" if r.pe else '',
                '负债率': f"{r.debt_ratio:.1f}%" if r.debt_ratio else '',
                '毛利率': f"{r.gross_margin:.1f}%" if r.gross_margin else '',
                '净利率': f"{r.net_margin:.1f}%" if r.net_margin else '',
                '20日波动率': f"{r.volatility_20d*100:.2f}%",
                '日均成交额(万)': round(r.avg_amount_20d, 0),
                '总市值(万)': round(r.total_mv, 0) if r.total_mv else '',
            })

        df = pd.DataFrame(rows)
        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        self.logger.info("已导出: %s", output_path)

    def export_codes(self, results: List[StockScore]) -> List[str]:
        """导出纯代码列表，可直接喂给回测或 TradingAgents"""
        return [r.symbol for r in results]


def main():
    parser = argparse.ArgumentParser(description='多因子选股筛选器 v2')
    parser.add_argument('--top', type=int, default=30, help='输出前 N 只 (默认 30)')
    parser.add_argument('--min-amount', type=float, default=3000,
                        help='最低日均成交额（万元, 默认 3000）')
    parser.add_argument('--min-days', type=int, default=120,
                        help='最少历史数据天数 (默认 120)')
    parser.add_argument('--min-trend', type=float, default=30,
                        help='最低趋势分 (默认 30)')
    parser.add_argument('--min-fund', type=float, default=20,
                        help='最低基本面分 (默认 20)')
    parser.add_argument('--market', type=str, default=None,
                        help='板块过滤（主板/科创板/创业板）')
    parser.add_argument('--as-of-date', type=str, default=None,
                        help='截止日期 (YYYY-MM-DD)，所有数据截至此日期，消除未来数据偏差')
    parser.add_argument('--output', type=str, default=None, help='CSV 输出路径')
    parser.add_argument('--no-print', action='store_true', help='不打印表格')

    args = parser.parse_args()

    screener = StockScreenerV2(as_of_date=args.as_of_date)
    results = screener.screen(
        top_n=args.top,
        min_data_days=args.min_days,
        min_avg_amount=args.min_amount,
        market_filter=args.market,
        min_trend_score=args.min_trend,
        min_fundamental_score=args.min_fund,
    )

    if not args.no_print:
        screener.print_results(results)

    screener.export_csv(results, args.output)

    # 输出纯代码列表方便复制
    codes = screener.export_codes(results)
    if codes:
        print("\n代码列表 (可直接用于回测):")
        print(" ".join(codes))


if __name__ == '__main__':
    main()
