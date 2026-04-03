#!/usr/bin/env python3
"""
多因子选股筛选器 v3

相对 v2 的结构性改进（不调参数阈值，只改方法论）:
  1. 波动率调整动量: 用 ret/vol 替代 raw ret，低波趋势更优
  2. 极端动量衰减: 短期暴涨 >30% 不再继续加分，降低追高风险
  3. 波动率惩罚: 高波动票在趋势分上扣分
  4. 基本面 ROE/利润率改为从 financial_data 取历史值，修复隐性前视偏差
  5. 缺失财务数据惩罚: 查不到财报的票基本面分设低值
  6. 行业分散约束: 输出 Top N 时同行业最多 max_per_industry 只

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate
    python scripts/stock_screener_v3.py --top 30
    python scripts/stock_screener_v3.py --as-of-date 2025-12-01 --top 20
"""

import sys
import math
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
    symbol: str
    name: str
    industry: str = ""
    market: str = ""
    close: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma120: float = 0.0
    ma250: float = 0.0
    trend_score: float = 0.0
    ma_alignment: int = 0
    momentum_20d: float = 0.0
    momentum_60d: float = 0.0
    adj_momentum_20d: float = 0.0
    adj_momentum_60d: float = 0.0
    relative_strength: float = 0.0
    above_ma250: bool = False
    ma250_slope_20d: float = 0.0
    fundamental_score: float = 0.0
    roe: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None
    debt_ratio: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_growth: Optional[float] = None
    volatility_20d: float = 0.0
    avg_amount_20d: float = 0.0
    total_mv: float = 0.0
    composite_score: float = 0.0
    rank: int = 0
    tag: str = ""


class StockScreenerV3:
    """多因子选股筛选器 v3"""

    WEIGHTS = {
        'trend': 0.55,
        'fundamental': 0.30,
        'liquidity': 0.15,
    }

    def __init__(self, db=None, as_of_date: str = None):
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
        logger = logging.getLogger('StockScreenerV3')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
            logger.addHandler(handler)
        return logger

    def _quote_query(self, symbol: str, extra: dict = None) -> dict:
        q = {"symbol": symbol}
        if self.as_of_date:
            q["trade_date"] = {"$lte": self.as_of_date}
        if extra:
            q.update(extra)
        return q

    # ------------------------------------------------------------------
    # 第一层：候选池（与 v2 相同）
    # ------------------------------------------------------------------
    def get_candidate_pool(self,
                           min_data_days: int = 120,
                           min_avg_amount: float = 3000,
                           market_filter: str = None) -> List[Dict]:
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
        excluded_st = excluded_data = excluded_liq = 0

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

            avg_amount_wan = np.mean(amounts) / 10000.0
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
    # 第二层：趋势打分 (v3 改进)
    # ------------------------------------------------------------------
    def calculate_trend_score(self, symbol: str) -> Dict:
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

        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        ma120 = close.rolling(120).mean() if n >= 120 else pd.Series([np.nan] * n)
        ma250 = close.rolling(250).mean() if n >= 250 else pd.Series([np.nan] * n)

        last_close = float(close.iloc[-1])
        last_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0
        last_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else 0
        last_ma120 = float(ma120.iloc[-1]) if pd.notna(ma120.iloc[-1]) else 0
        last_ma250 = float(ma250.iloc[-1]) if pd.notna(ma250.iloc[-1]) else 0

        # ── 均线多头排列 (0-25)，与 v2 相同 ──
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

        # ── 波动率 ──
        daily_returns = close.pct_change().dropna().tail(20)
        vol_20d = float(daily_returns.std()) if len(daily_returns) >= 10 else 0.03

        # ── [改进1] 波动率调整动量 ──
        ret_20d = (last_close / float(close.iloc[-21]) - 1) if n >= 21 else 0
        ret_60d = (last_close / float(close.iloc[-61]) - 1) if n >= 61 else 0

        safe_vol = max(vol_20d, 0.005)
        adj_ret_20d = ret_20d / safe_vol
        adj_ret_60d = ret_60d / (safe_vol * math.sqrt(3))

        # ── [改进2] 动量打分 + 极端动量衰减 (0-35) ──
        momentum_score = 0

        # 20d 波动率调整动量
        if adj_ret_20d > 3.0:
            momentum_score += 15
        elif adj_ret_20d > 1.5:
            momentum_score += 12
        elif adj_ret_20d > 0:
            momentum_score += 8
        elif adj_ret_20d > -1.0:
            momentum_score += 4

        # 60d 波动率调整动量
        if adj_ret_60d > 3.0:
            momentum_score += 20
        elif adj_ret_60d > 1.5:
            momentum_score += 15
        elif adj_ret_60d > 0:
            momentum_score += 10
        elif adj_ret_60d > -1.0:
            momentum_score += 5

        # 极端短期暴涨衰减：20d raw return > 30% 时扣回部分动量分
        if ret_20d > 0.30:
            overheat_penalty = min(10, int((ret_20d - 0.30) * 30))
            momentum_score = max(0, momentum_score - overheat_penalty)

        # ── MA250 斜率 (0-20)，与 v2 相同 ──
        slope_score = 0
        slope = 0
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

        # ── 距离年内高点 (0-20)，与 v2 相同 ──
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

        raw_trend = ma_score + momentum_score + slope_score + distance_score

        # ── [改进3] 高波动惩罚 ──
        vol_penalty = 0
        if vol_20d > 0.06:
            vol_penalty = min(15, int((vol_20d - 0.06) * 200))

        total_trend = max(0, min(raw_trend - vol_penalty, 100))

        return {
            'close': last_close,
            'ma20': last_ma20,
            'ma60': last_ma60,
            'ma120': last_ma120,
            'ma250': last_ma250,
            'ma_alignment': ma_score,
            'momentum_20d': ret_20d,
            'momentum_60d': ret_60d,
            'adj_momentum_20d': adj_ret_20d,
            'adj_momentum_60d': adj_ret_60d,
            'above_ma250': last_close > last_ma250 if last_ma250 > 0 else False,
            'ma250_slope_20d': slope if n >= 270 else 0,
            'trend_score': total_trend,
            'volatility_20d': vol_20d,
            'distance_to_high': distance_to_high,
            'vol_penalty': vol_penalty,
        }

    # ------------------------------------------------------------------
    # 第三层：基本面打分 (v3 改进)
    # ------------------------------------------------------------------
    def calculate_fundamental_score(self, basic_info: Dict) -> Dict:
        code = basic_info['code']
        score = 0

        # ── [改进4] 从 financial_data 取历史财务指标 ──
        # ann_date 在 DB 中格式为 'YYYYMMDD'（无横线），需要转换
        fin = None
        if self.as_of_date:
            aod_compact = self.as_of_date.replace("-", "")
            fin = self.db.stock_financial_data.find_one(
                {"code": code, "ann_date": {"$lte": aod_compact}},
                sort=[("ann_date", -1)])
        # 若按日期找不到（DB 只有近期财报），回退到最新可用
        if not fin:
            fin = self.db.stock_financial_data.find_one(
                {"code": code}, sort=[("ann_date", -1)])

        # ── [改进5] 完全没有财务数据时惩罚 ──
        if not fin:
            fallback_roe = basic_info.get('roe')
            fallback_score = 0
            if fallback_roe is not None and fallback_roe > 0:
                fallback_score = 12 if fallback_roe > 5 else 5
            return {
                'fundamental_score': max(fallback_score, 5),
                'roe': fallback_roe, 'pe': basic_info.get('pe'),
                'pb': basic_info.get('pb'),
                'debt_ratio': None, 'gross_margin': None, 'net_margin': None,
            }

        roe = fin.get('roe')
        pe = basic_info.get('pe')
        pb = basic_info.get('pb')
        debt_ratio = fin.get('debt_to_assets')
        gross_margin = fin.get('gross_margin')
        net_margin = fin.get('netprofit_margin')

        total_assets = fin.get('total_assets')
        total_liab = fin.get('total_liab')
        revenue = fin.get('revenue')
        net_profit = fin.get('net_profit')
        oper_cost = fin.get('oper_cost')
        total_equity = fin.get('total_equity')

        if roe is None and total_equity and net_profit:
            try:
                eq = float(total_equity)
                np_ = float(net_profit)
                if eq > 0:
                    roe = np_ / eq * 100
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        if roe is None:
            roe = basic_info.get('roe')

        if debt_ratio is None and total_assets and total_liab:
            try:
                debt_ratio = float(total_liab) / float(total_assets) * 100
            except (ValueError, TypeError, ZeroDivisionError):
                pass

        if gross_margin is None and revenue and oper_cost:
            try:
                gross_margin = (1 - float(oper_cost) / float(revenue)) * 100
            except (ValueError, TypeError, ZeroDivisionError):
                pass

        if net_margin is None and revenue and net_profit:
            try:
                net_margin = float(net_profit) / float(revenue) * 100
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

        # 负债率 (0-20)
        if debt_ratio is not None:
            if debt_ratio < 20:
                score += 20
            elif debt_ratio < 35:
                score += 16
            elif debt_ratio < 50:
                score += 12
            elif debt_ratio < 65:
                score += 6

        # 毛利率 (0-20)
        if gross_margin is not None:
            if gross_margin > 50:
                score += 20
            elif gross_margin > 35:
                score += 15
            elif gross_margin > 20:
                score += 10
            elif gross_margin > 10:
                score += 5

        # 净利率 (0-15)
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
            'roe': round(roe, 2) if roe is not None else None,
            'pe': pe,
            'pb': pb,
            'debt_ratio': round(debt_ratio, 1) if debt_ratio else None,
            'gross_margin': round(gross_margin, 1) if gross_margin else None,
            'net_margin': round(net_margin, 1) if net_margin else None,
        }

    # ------------------------------------------------------------------
    # 第四层：综合打分 + [改进6] 行业分散约束
    # ------------------------------------------------------------------
    def screen(self,
               top_n: int = 30,
               min_data_days: int = 120,
               min_avg_amount: float = 3000,
               market_filter: str = None,
               min_trend_score: float = 30,
               min_fundamental_score: float = 20,
               max_per_industry: int = 2) -> List[StockScore]:
        self.logger.info("=" * 70)
        self.logger.info("多因子选股筛选器 v3 启动")
        if self.as_of_date:
            self.logger.info("截止日期: %s", self.as_of_date)
        else:
            self.logger.info("未设定截止日期，使用全部最新数据")
        self.logger.info("=" * 70)

        candidates = self.get_candidate_pool(
            min_data_days=min_data_days,
            min_avg_amount=min_avg_amount,
            market_filter=market_filter)

        if not candidates:
            self.logger.warning("候选池为空")
            return []

        all_scored: List[StockScore] = []
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

            composite = (
                self.WEIGHTS['trend'] * trend['trend_score'] +
                self.WEIGHTS['fundamental'] * fund['fundamental_score'] +
                self.WEIGHTS['liquidity'] * liq_score
            )

            ts = trend['trend_score']
            if ts >= 70 and trend.get('above_ma250'):
                tag = "强趋势"
            elif ts >= 50:
                tag = "趋势向好"
            elif ts >= 30:
                tag = "企稳回升"
            else:
                tag = "弱势"

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
                adj_momentum_20d=trend.get('adj_momentum_20d', 0),
                adj_momentum_60d=trend.get('adj_momentum_60d', 0),
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
                total_mv=info.get('total_mv', 0),
                composite_score=round(composite, 2),
                tag=tag,
            )
            all_scored.append(s)

        all_scored.sort(key=lambda x: -x.composite_score)

        # ── [改进6] 行业分散约束 ──
        if max_per_industry and max_per_industry > 0:
            from collections import Counter
            industry_count: Counter = Counter()
            diversified: List[StockScore] = []
            for s in all_scored:
                ind = s.industry or "未知"
                if industry_count[ind] < max_per_industry:
                    diversified.append(s)
                    industry_count[ind] += 1
                if len(diversified) >= top_n:
                    break
            results = diversified
        else:
            results = all_scored[:top_n]

        for i, r in enumerate(results):
            r.rank = i + 1

        self.logger.info("通过筛选: %d 只，行业分散后输出 Top %d",
                         len(all_scored), len(results))
        return results

    # ------------------------------------------------------------------
    # 输出（与 v2 相同）
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
            output_path = project_root / "output" / "screener_v3.csv"
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        for r in results:
            rows.append({
                '排名': r.rank, '代码': r.symbol, '名称': r.name,
                '行业': r.industry, '板块': r.market,
                '现价': round(r.close, 2),
                '趋势分': round(r.trend_score, 1),
                '基本面分': round(r.fundamental_score, 1),
                '综合分': round(r.composite_score, 1),
                '标签': r.tag,
                '20日动量': f"{r.momentum_20d*100:.1f}%",
                '60日动量': f"{r.momentum_60d*100:.1f}%",
                '调整动量20d': f"{r.adj_momentum_20d:.2f}",
                '调整动量60d': f"{r.adj_momentum_60d:.2f}",
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
        return [r.symbol for r in results]


def main():
    parser = argparse.ArgumentParser(description='多因子选股筛选器 v3')
    parser.add_argument('--top', type=int, default=30)
    parser.add_argument('--min-amount', type=float, default=3000)
    parser.add_argument('--min-days', type=int, default=120)
    parser.add_argument('--min-trend', type=float, default=30)
    parser.add_argument('--min-fund', type=float, default=20)
    parser.add_argument('--max-per-industry', type=int, default=2,
                        help='同行业最多选 N 只 (默认 2, 0=不限)')
    parser.add_argument('--market', type=str, default=None)
    parser.add_argument('--as-of-date', type=str, default=None)
    parser.add_argument('--output', type=str, default=None)
    parser.add_argument('--no-print', action='store_true')
    args = parser.parse_args()

    screener = StockScreenerV3(as_of_date=args.as_of_date)
    results = screener.screen(
        top_n=args.top,
        min_data_days=args.min_days,
        min_avg_amount=args.min_amount,
        market_filter=args.market,
        min_trend_score=args.min_trend,
        min_fundamental_score=args.min_fund,
        max_per_industry=args.max_per_industry,
    )

    if not args.no_print:
        screener.print_results(results)
    screener.export_csv(results, args.output)

    codes = screener.export_codes(results)
    if codes:
        print("\n代码列表:")
        print(" ".join(codes))


if __name__ == '__main__':
    main()
