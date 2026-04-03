#!/usr/bin/env python3
"""
多因子选股筛选器 v3 提效版

目标:
  1. 不改变 v3 的打分思想和阈值
  2. 只重构执行路径，降低 MongoDB 往返次数和逐票 pandas 开销

核心优化:
  1. 每只股票只查询一次 recent quotes（最多 300 条），同时完成:
     - 历史天数检查
     - 20 日均成交额检查
     - 趋势分计算
  2. 财务数据按轮次一次性预加载到内存，逐票只做 dict 查找
  3. 趋势指标改为纯 Python / numpy 计算，去掉逐票 DataFrame / rolling

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate
    python scripts/stock_screener_v3_fast.py --top 30
    python scripts/stock_screener_v3_fast.py --as-of-date 2025-12-01 --top 20
"""

import sys
import math
import time
import logging
import argparse
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import numpy as np

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from stock_screener_v3 import StockScreenerV3, StockScore


class StockScreenerV3Fast(StockScreenerV3):
    """保持 v3 打分规则不变的提效版。"""

    def __init__(self, db=None, as_of_date: str = None,
                 basics_cache: Optional[List[Dict]] = None):
        super().__init__(db=db, as_of_date=as_of_date)
        self._financial_map: Optional[Dict[str, Dict]] = None
        self._basics_cache = basics_cache

    def _safe_float(self, value, default=None):
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _load_financial_map(self) -> Dict[str, Dict]:
        if self._financial_map is not None:
            return self._financial_map

        aod_compact = self.as_of_date.replace("-", "") if self.as_of_date else None
        projection = {
            "_id": 0,
            "code": 1,
            "symbol": 1,
            "ann_date": 1,
            "roe": 1,
            "debt_to_assets": 1,
            "gross_margin": 1,
            "netprofit_margin": 1,
            "total_assets": 1,
            "total_liab": 1,
            "revenue": 1,
            "net_profit": 1,
            "oper_cost": 1,
            "total_equity": 1,
        }

        latest_map: Dict[str, Dict] = {}
        asof_map: Dict[str, Dict] = {}

        cursor = self.db.stock_financial_data.find(projection=projection).sort([
            ("code", 1),
            ("ann_date", -1),
        ])

        for doc in cursor:
            code = doc.get("code") or doc.get("symbol")
            if not code:
                continue

            if code not in latest_map:
                latest_map[code] = doc

            if aod_compact and code not in asof_map:
                ann_date = doc.get("ann_date")
                if ann_date and ann_date <= aod_compact:
                    asof_map[code] = doc

        if aod_compact:
            self._financial_map = {
                code: asof_map.get(code) or latest_map.get(code)
                for code in latest_map.keys()
            }
        else:
            self._financial_map = latest_map

        return self._financial_map

    def _fetch_quote_rows(self, symbol: str, limit: int = 300) -> List[Dict]:
        return list(
            self.db.stock_daily_quotes.find(
                self._quote_query(symbol),
                {
                    "_id": 0,
                    "trade_date": 1,
                    "close": 1,
                    "high": 1,
                    "amount": 1,
                },
            )
            .sort("trade_date", -1)
            .limit(limit)
        )

    def _avg_amount_wan_from_rows(self, rows_desc: List[Dict]) -> Optional[float]:
        recent = rows_desc[:20]
        if len(recent) < 10:
            return None

        amounts = []
        for row in recent:
            amt = self._safe_float(row.get("amount"))
            if amt is not None:
                amounts.append(amt)

        if not amounts:
            return None

        return float(np.mean(amounts) / 10000.0)

    def _calculate_trend_from_rows(self, rows_desc: List[Dict]) -> Dict:
        if len(rows_desc) < 60:
            return {}

        rows = rows_desc[::-1]
        closes = np.array([self._safe_float(r.get("close"), np.nan) for r in rows], dtype=float)
        highs = np.array([self._safe_float(r.get("high"), np.nan) for r in rows], dtype=float)
        n = len(closes)

        if np.isnan(closes[-1]):
            return {}

        last_close = float(closes[-1])
        last_ma20 = float(np.nanmean(closes[-20:])) if n >= 20 else 0.0
        last_ma60 = float(np.nanmean(closes[-60:])) if n >= 60 else 0.0
        last_ma120 = float(np.nanmean(closes[-120:])) if n >= 120 else 0.0
        last_ma250 = float(np.nanmean(closes[-250:])) if n >= 250 else 0.0

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

        daily_returns = closes[1:] / closes[:-1] - 1
        daily_returns = daily_returns[~np.isnan(daily_returns)]
        last_20_returns = daily_returns[-20:]
        vol_20d = float(np.std(last_20_returns, ddof=1)) if len(last_20_returns) >= 10 else 0.03

        ret_20d = (last_close / float(closes[-21]) - 1) if n >= 21 and closes[-21] > 0 else 0.0
        ret_60d = (last_close / float(closes[-61]) - 1) if n >= 61 and closes[-61] > 0 else 0.0

        safe_vol = max(vol_20d, 0.005)
        adj_ret_20d = ret_20d / safe_vol
        adj_ret_60d = ret_60d / (safe_vol * math.sqrt(3))

        momentum_score = 0
        if adj_ret_20d > 3.0:
            momentum_score += 15
        elif adj_ret_20d > 1.5:
            momentum_score += 12
        elif adj_ret_20d > 0:
            momentum_score += 8
        elif adj_ret_20d > -1.0:
            momentum_score += 4

        if adj_ret_60d > 3.0:
            momentum_score += 20
        elif adj_ret_60d > 1.5:
            momentum_score += 15
        elif adj_ret_60d > 0:
            momentum_score += 10
        elif adj_ret_60d > -1.0:
            momentum_score += 5

        if ret_20d > 0.30:
            overheat_penalty = min(10, int((ret_20d - 0.30) * 30))
            momentum_score = max(0, momentum_score - overheat_penalty)

        slope_score = 0
        slope = 0.0
        if n >= 270:
            ma250_20ago = float(np.nanmean(closes[-270:-20]))
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

        high_window = highs[-min(252, n):]
        high_252 = float(np.nanmax(high_window)) if len(high_window) else 0.0
        distance_to_high = last_close / high_252 if high_252 > 0 else 0.0
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

        vol_penalty = 0
        if vol_20d > 0.06:
            vol_penalty = min(15, int((vol_20d - 0.06) * 200))

        total_trend = max(0, min(raw_trend - vol_penalty, 100))

        return {
            "close": last_close,
            "ma20": last_ma20,
            "ma60": last_ma60,
            "ma120": last_ma120,
            "ma250": last_ma250,
            "ma_alignment": ma_score,
            "momentum_20d": ret_20d,
            "momentum_60d": ret_60d,
            "adj_momentum_20d": adj_ret_20d,
            "adj_momentum_60d": adj_ret_60d,
            "above_ma250": last_close > last_ma250 if last_ma250 > 0 else False,
            "ma250_slope_20d": slope if n >= 270 else 0,
            "trend_score": total_trend,
            "volatility_20d": vol_20d,
            "distance_to_high": distance_to_high,
            "vol_penalty": vol_penalty,
        }

    def _calculate_fundamental_from_doc(self, basic_info: Dict, fin: Optional[Dict]) -> Dict:
        score = 0

        if not fin:
            fallback_roe = basic_info.get("roe")
            fallback_score = 0
            if fallback_roe is not None and fallback_roe > 0:
                fallback_score = 12 if fallback_roe > 5 else 5
            return {
                "fundamental_score": max(fallback_score, 5),
                "roe": fallback_roe,
                "pe": basic_info.get("pe"),
                "pb": basic_info.get("pb"),
                "debt_ratio": None,
                "gross_margin": None,
                "net_margin": None,
            }

        roe = self._safe_float(fin.get("roe"), basic_info.get("roe"))
        pe = basic_info.get("pe")
        pb = basic_info.get("pb")
        debt_ratio = self._safe_float(fin.get("debt_to_assets"))
        gross_margin = self._safe_float(fin.get("gross_margin"))
        net_margin = self._safe_float(fin.get("netprofit_margin"))

        total_assets = self._safe_float(fin.get("total_assets"))
        total_liab = self._safe_float(fin.get("total_liab"))
        revenue = self._safe_float(fin.get("revenue"))
        net_profit = self._safe_float(fin.get("net_profit"))
        oper_cost = self._safe_float(fin.get("oper_cost"))
        total_equity = self._safe_float(fin.get("total_equity"))

        if roe is None and total_equity and net_profit:
            try:
                if total_equity > 0:
                    roe = net_profit / total_equity * 100
            except ZeroDivisionError:
                pass

        if debt_ratio is None and total_assets and total_liab:
            try:
                debt_ratio = total_liab / total_assets * 100
            except ZeroDivisionError:
                pass

        if gross_margin is None and revenue and oper_cost:
            try:
                gross_margin = (1 - oper_cost / revenue) * 100
            except ZeroDivisionError:
                pass

        if net_margin is None and revenue and net_profit:
            try:
                net_margin = net_profit / revenue * 100
            except ZeroDivisionError:
                pass

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

        if debt_ratio is not None:
            if debt_ratio < 20:
                score += 20
            elif debt_ratio < 35:
                score += 16
            elif debt_ratio < 50:
                score += 12
            elif debt_ratio < 65:
                score += 6

        if gross_margin is not None:
            if gross_margin > 50:
                score += 20
            elif gross_margin > 35:
                score += 15
            elif gross_margin > 20:
                score += 10
            elif gross_margin > 10:
                score += 5

        if net_margin is not None:
            if net_margin > 25:
                score += 15
            elif net_margin > 15:
                score += 12
            elif net_margin > 8:
                score += 8
            elif net_margin > 0:
                score += 4

        if pe is not None and pe > 0:
            if 5 < pe < 15:
                score += 15
            elif 15 <= pe < 30:
                score += 10
            elif 30 <= pe < 60:
                score += 5

        return {
            "fundamental_score": min(score, 100),
            "roe": round(roe, 2) if roe is not None else None,
            "pe": pe,
            "pb": pb,
            "debt_ratio": round(debt_ratio, 1) if debt_ratio is not None else None,
            "gross_margin": round(gross_margin, 1) if gross_margin is not None else None,
            "net_margin": round(net_margin, 1) if net_margin is not None else None,
        }

    def screen(
        self,
        top_n: int = 30,
        min_data_days: int = 120,
        min_avg_amount: float = 3000,
        market_filter: str = None,
        min_trend_score: float = 30,
        min_fundamental_score: float = 20,
        max_per_industry: int = 2,
    ) -> List[StockScore]:
        t0 = time.perf_counter()
        self.logger.info("=" * 70)
        self.logger.info("多因子选股筛选器 v3-fast 启动")
        if self.as_of_date:
            self.logger.info("截止日期: %s", self.as_of_date)
        else:
            self.logger.info("未设定截止日期，使用全部最新数据")
        self.logger.info("=" * 70)

        if self._basics_cache is not None:
            basics = self._basics_cache
            if market_filter:
                basics = [b for b in basics if b.get("market") == market_filter]
            self.logger.info("stock_basic_info 共 %d 条记录 (缓存)", len(basics))
        else:
            query = {}
            if market_filter:
                query["market"] = market_filter
            basics = list(
                self.db.stock_basic_info.find(
                    query,
                    {
                        "_id": 0,
                        "code": 1,
                        "name": 1,
                        "industry": 1,
                        "market": 1,
                        "total_mv": 1,
                        "roe": 1,
                        "pe": 1,
                        "pb": 1,
                        "pe_ttm": 1,
                    },
                )
            )
            self.logger.info("stock_basic_info 共 %d 条记录", len(basics))

        t_fin0 = time.perf_counter()
        financial_map = self._load_financial_map()
        self.logger.info("财务缓存加载完成: %d 只股票, 用时 %.2fs",
                         len(financial_map), time.perf_counter() - t_fin0)

        excluded_st = 0
        valid_infos: List[Dict] = []
        for info in basics:
            code = info.get("code", "")
            name = info.get("name", "")
            if not name or "ST" in name.upper() or "退" in name:
                excluded_st += 1
                continue
            if code.startswith("200") or code.startswith("900"):
                excluded_st += 1
                continue
            valid_infos.append(info)

        self.logger.info("预过滤后: %d 只 (排除 ST/B股 %d)",
                         len(valid_infos), excluded_st)

        # ── 并发获取行情 ──
        N_WORKERS = 16
        quote_cache: Dict[str, List[Dict]] = {}

        def _fetch_one(code: str) -> Tuple[str, List[Dict]]:
            return code, self._fetch_quote_rows(code, limit=300)

        t_io0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_one, info["code"]): info["code"]
                for info in valid_infos
            }
            done_count = 0
            for fut in as_completed(futures):
                code, rows = fut.result()
                quote_cache[code] = rows
                done_count += 1
                if done_count % 500 == 0:
                    self.logger.info("行情并发: %d/%d ...", done_count, len(futures))

        self.logger.info("行情获取完成: %d 只, 用时 %.2fs (%d线程)",
                         len(quote_cache), time.perf_counter() - t_io0, N_WORKERS)

        # ── 串行打分 ──
        excluded_data = excluded_liq = 0
        all_scored: List[StockScore] = []

        for info in valid_infos:
            code = info["code"]
            rows_desc = quote_cache.get(code, [])

            if len(rows_desc) < min_data_days:
                excluded_data += 1
                continue

            avg_amount_wan = self._avg_amount_wan_from_rows(rows_desc)
            if avg_amount_wan is None:
                excluded_data += 1
                continue
            if avg_amount_wan < min_avg_amount:
                excluded_liq += 1
                continue

            trend = self._calculate_trend_from_rows(rows_desc)
            if not trend or trend["trend_score"] < min_trend_score:
                continue

            fund = self._calculate_fundamental_from_doc(info, financial_map.get(code))
            if fund["fundamental_score"] < min_fundamental_score:
                continue

            if avg_amount_wan > 50000:
                liq_score = 100
            elif avg_amount_wan > 20000:
                liq_score = 80
            elif avg_amount_wan > 10000:
                liq_score = 60
            elif avg_amount_wan > 5000:
                liq_score = 40
            else:
                liq_score = 20

            composite = (
                self.WEIGHTS["trend"] * trend["trend_score"]
                + self.WEIGHTS["fundamental"] * fund["fundamental_score"]
                + self.WEIGHTS["liquidity"] * liq_score
            )

            ts = trend["trend_score"]
            if ts >= 70 and trend.get("above_ma250"):
                tag = "强趋势"
            elif ts >= 50:
                tag = "趋势向好"
            elif ts >= 30:
                tag = "企稳回升"
            else:
                tag = "弱势"

            all_scored.append(
                StockScore(
                    symbol=code,
                    name=info["name"],
                    industry=info.get("industry", ""),
                    market=info.get("market", ""),
                    close=trend["close"],
                    ma20=trend["ma20"],
                    ma60=trend["ma60"],
                    ma120=trend["ma120"],
                    ma250=trend["ma250"],
                    trend_score=trend["trend_score"],
                    ma_alignment=trend["ma_alignment"],
                    momentum_20d=trend["momentum_20d"],
                    momentum_60d=trend["momentum_60d"],
                    adj_momentum_20d=trend.get("adj_momentum_20d", 0),
                    adj_momentum_60d=trend.get("adj_momentum_60d", 0),
                    above_ma250=trend.get("above_ma250", False),
                    ma250_slope_20d=trend.get("ma250_slope_20d", 0),
                    fundamental_score=fund["fundamental_score"],
                    roe=fund.get("roe"),
                    pe=fund.get("pe"),
                    pb=fund.get("pb"),
                    debt_ratio=fund.get("debt_ratio"),
                    gross_margin=fund.get("gross_margin"),
                    net_margin=fund.get("net_margin"),
                    volatility_20d=trend["volatility_20d"],
                    avg_amount_20d=avg_amount_wan,
                    total_mv=info.get("total_mv", 0),
                    composite_score=round(composite, 2),
                    tag=tag,
                )
            )

        self.logger.info(
            "候选池: %d 只 (排除 ST/B股 %d, 数据不足 %d, 流动性不足 %d)",
            len(all_scored),
            excluded_st,
            excluded_data,
            excluded_liq,
        )

        all_scored.sort(key=lambda x: -x.composite_score)

        if max_per_industry and max_per_industry > 0:
            industry_count: Counter = Counter()
            diversified: List[StockScore] = []
            for item in all_scored:
                industry = item.industry or "未知"
                if industry_count[industry] < max_per_industry:
                    diversified.append(item)
                    industry_count[industry] += 1
                if len(diversified) >= top_n:
                    break
            results = diversified
        else:
            results = all_scored[:top_n]

        for i, result in enumerate(results):
            result.rank = i + 1

        self.logger.info("通过筛选: %d 只，行业分散后输出 Top %d",
                         len(all_scored), len(results))
        self.logger.info("总耗时: %.2fs", time.perf_counter() - t0)
        return results


def main():
    parser = argparse.ArgumentParser(description="多因子选股筛选器 v3 提效版")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--min-amount", type=float, default=3000)
    parser.add_argument("--min-days", type=int, default=120)
    parser.add_argument("--min-trend", type=float, default=30)
    parser.add_argument("--min-fund", type=float, default=20)
    parser.add_argument("--max-per-industry", type=int, default=2,
                        help="同行业最多选 N 只 (默认 2, 0=不限)")
    parser.add_argument("--market", type=str, default=None)
    parser.add_argument("--as-of-date", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--no-print", action="store_true")
    args = parser.parse_args()

    screener = StockScreenerV3Fast(as_of_date=args.as_of_date)
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


if __name__ == "__main__":
    main()
