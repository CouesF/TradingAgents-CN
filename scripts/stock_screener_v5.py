#!/usr/bin/env python3
"""
多因子选股筛选器 v5 — 双引擎: 主线延续 + 高低切轮动

核心变化 (相对 v3/v4):
  1. 先做行业层筛选，再做个股层筛选
  2. 双引擎:
     - 引擎 A (延续型): 在仍处于 leading 阶段的行业里选龙头
     - 引擎 B (切换型): 在刚进入 accelerating 阶段的行业里选低位启动股
  3. 显式惩罚: 过热、失败记忆
  4. 标签体系: 主线龙头 / 主线跟随 / 切换启动 / 高热警告

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate
    python scripts/stock_screener_v5.py --top 30
    python scripts/stock_screener_v5.py --as-of-date 2025-12-01 --top 20
"""

import sys
import time
import logging
import argparse
from pathlib import Path
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import numpy as np

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from tradingagents.config.database_manager import get_database_manager

logger = logging.getLogger("StockScreenerV5")


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class StockScoreV5:
    symbol: str
    name: str
    industry: str = ""
    close: float = 0.0
    composite_score: float = 0.0
    engine: str = ""
    engine_a_score: float = 0.0
    engine_b_score: float = 0.0
    tag: str = ""
    industry_phase: str = ""
    ind_ret_20d: float = 0.0
    ind_accel: float = 0.0
    ret_5d: float = 0.0
    ret_20d: float = 0.0
    ret_60d: float = 0.0
    excess_vs_ind: float = 0.0
    vol_health: float = 0.0
    overheat_penalty: float = 0.0
    failure_penalty: float = 0.0
    total_penalty: float = 0.0
    rank: int = 0


# ── 主类 ──────────────────────────────────────────────────

class StockScreenerV5:
    """双引擎选股器: 主线延续 + 高低切轮动"""

    MAX_ENGINE_B = 2

    def __init__(self, db=None, as_of_date: str = None,
                 basics_cache: Optional[List[Dict]] = None,
                 regime_benchmark: str = "000300",
                 failure_history: Optional[Dict[str, List[float]]] = None):
        if db is None:
            db_manager = get_database_manager()
            self.db = db_manager.get_mongodb_db()
        else:
            self.db = db
        if self.db is None:
            raise ValueError("MongoDB 连接失败")
        self.as_of_date = as_of_date
        self._basics_cache = basics_cache
        self.regime_benchmark = regime_benchmark
        self._failure_history = failure_history or {}
        self._last_market_ctx: Optional[Dict] = None
        self._last_industry_ctx: Optional[Dict[str, Dict]] = None

    @property
    def market_context(self) -> Optional[Dict]:
        return self._last_market_ctx

    @property
    def industry_context(self) -> Optional[Dict[str, Dict]]:
        return self._last_industry_ctx

    # ── 数据加载 ─────────────────────────────────────────

    def _quote_query(self, symbol: str) -> dict:
        q = {"symbol": symbol}
        if self.as_of_date:
            q["trade_date"] = {"$lte": self.as_of_date}
        return q

    def _fetch_quote_rows(self, symbol: str, limit: int = 300) -> List[Dict]:
        return list(
            self.db.stock_daily_quotes.find(
                self._quote_query(symbol),
                {"_id": 0, "trade_date": 1, "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1, "amount": 1},
            ).sort("trade_date", -1).limit(limit)
        )

    def _fetch_index_rows(self, symbol: str, limit: int = 80) -> List[Dict]:
        q = {"symbol": symbol}
        if self.as_of_date:
            q["trade_date"] = {"$lte": self.as_of_date}
        return list(
            self.db.index_daily_quotes.find(
                q, {"_id": 0, "trade_date": 1, "close": 1},
            ).sort("trade_date", -1).limit(limit)
        )

    def _load_financial_map(self) -> Dict[str, Dict]:
        aod_compact = self.as_of_date.replace("-", "") if self.as_of_date else None
        projection = {
            "_id": 0, "code": 1, "symbol": 1, "ann_date": 1,
            "roe": 1, "debt_to_assets": 1, "netprofit_margin": 1,
        }
        latest_map: Dict[str, Dict] = {}
        asof_map: Dict[str, Dict] = {}
        cursor = self.db.stock_financial_data.find(
            projection=projection
        ).sort([("code", 1), ("ann_date", -1)])
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
            return {c: asof_map.get(c) or latest_map.get(c) for c in latest_map}
        return latest_map

    @staticmethod
    def _safe_float(value, default=None):
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    # ── 个股指标 ─────────────────────────────────────────

    def _compute_stock_metrics(self, rows_desc: List[Dict]) -> Optional[Dict]:
        if len(rows_desc) < 60:
            return None

        rows = list(reversed(rows_desc))
        closes = np.array([self._safe_float(r.get("close"), np.nan) for r in rows])
        highs = np.array([self._safe_float(r.get("high"), np.nan) for r in rows])
        n = len(closes)

        if np.isnan(closes[-1]) or closes[-1] <= 0:
            return None
        last = float(closes[-1])

        def _ret(days):
            idx = -(days + 1)
            if n > days and not np.isnan(closes[idx]) and closes[idx] > 0:
                return float(last / closes[idx] - 1)
            return 0.0

        ret_3d = _ret(3)
        ret_5d = _ret(5)
        ret_10d = _ret(10)
        ret_20d = _ret(20)
        ret_60d = _ret(60)

        ma20 = float(np.nanmean(closes[-20:])) if n >= 20 else last
        ma60 = float(np.nanmean(closes[-60:])) if n >= 60 else ma20
        ma120 = float(np.nanmean(closes[-120:])) if n >= 120 else ma60

        above_ma20 = last > ma20
        above_ma60 = last > ma60
        ma_aligned = (ma20 > ma60) and (n < 120 or ma60 > ma120)

        ma60_slope = 0.0
        if n >= 80:
            ma60_20ago = float(np.nanmean(closes[-80:-20]))
            if ma60_20ago > 0:
                ma60_slope = (ma60 - ma60_20ago) / ma60_20ago

        # 波动率
        vol_20d = 0.03
        if n >= 22:
            dr = np.diff(closes[-22:]) / closes[-22:-1]
            dr = dr[~np.isnan(dr)]
            if len(dr) >= 10:
                vol_20d = float(np.std(dr, ddof=1))

        vol_10d = vol_20d
        if n >= 12:
            dr10 = np.diff(closes[-12:]) / closes[-12:-1]
            dr10 = dr10[~np.isnan(dr10)]
            if len(dr10) >= 5:
                vol_10d = float(np.std(dr10, ddof=1))

        vol_ratio = vol_10d / vol_20d if vol_20d > 0.001 else 1.0

        high_window = highs[-min(120, n):]
        high_120 = float(np.nanmax(high_window))
        dist_from_high = last / high_120 if high_120 > 0 else 0.0

        amounts = []
        for r in rows_desc[:20]:
            amt = self._safe_float(r.get("amount"))
            if amt is not None and amt > 0:
                amounts.append(amt)
        avg_amount_wan = float(np.mean(amounts)) / 10000 if amounts else 0.0

        ma20_gap = (last - ma20) / ma20 if ma20 > 0 else 0.0

        return {
            "close": last,
            "ret_3d": ret_3d, "ret_5d": ret_5d, "ret_10d": ret_10d,
            "ret_20d": ret_20d, "ret_60d": ret_60d,
            "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "above_ma20": above_ma20, "above_ma60": above_ma60,
            "ma_aligned": ma_aligned, "ma60_slope": ma60_slope,
            "vol_10d": vol_10d, "vol_20d": vol_20d, "vol_ratio": vol_ratio,
            "dist_from_high": dist_from_high,
            "avg_amount_wan": avg_amount_wan,
            "ma20_gap": ma20_gap,
        }

    # ── 市场层面 ─────────────────────────────────────────

    def _compute_market_context(self, all_metrics: Dict[str, Dict]) -> Dict:
        idx_rows = self._fetch_index_rows(self.regime_benchmark, limit=80)
        idx_rets: Dict[str, float] = {}
        if len(idx_rows) >= 2:
            rows_asc = list(reversed(idx_rows))
            closes = [self._safe_float(r.get("close"), np.nan) for r in rows_asc]
            nn = len(closes)
            lc = closes[-1] if closes else None
            if lc and lc > 0 and not np.isnan(lc):
                for label, days in [("ret_5d", 5), ("ret_20d", 20), ("ret_60d", 60)]:
                    idx = -(days + 1)
                    if nn > days and closes[idx] and closes[idx] > 0 and not np.isnan(closes[idx]):
                        idx_rets[label] = float(lc / closes[idx] - 1)

        if not idx_rets:
            idx_rets = {
                "ret_5d": float(np.median([m["ret_5d"] for m in all_metrics.values()])),
                "ret_20d": float(np.median([m["ret_20d"] for m in all_metrics.values()])),
                "ret_60d": float(np.median([m["ret_60d"] for m in all_metrics.values()])),
            }

        total = above = 0
        for m in all_metrics.values():
            total += 1
            if m["above_ma20"]:
                above += 1
        breadth = above / total if total > 0 else 0.5

        ctx = {**idx_rets, "breadth": breadth}
        self._last_market_ctx = ctx
        return ctx

    # ── 行业层面 ─────────────────────────────────────────

    def _compute_industry_context(
        self,
        all_metrics: Dict[str, Dict],
        info_map: Dict[str, Dict],
        market_ctx: Dict,
    ) -> Dict[str, Dict]:
        industry_stocks: Dict[str, List[Dict]] = defaultdict(list)
        for code, metrics in all_metrics.items():
            industry = info_map.get(code, {}).get("industry", "")
            if industry:
                industry_stocks[industry].append(metrics)

        MIN_STOCKS = 3
        mkt_5d = market_ctx.get("ret_5d", 0)
        mkt_20d = market_ctx.get("ret_20d", 0)

        industry_ctx: Dict[str, Dict] = {}
        excess_20d_list: List[Tuple[str, float]] = []
        excess_5d_list: List[Tuple[str, float]] = []

        for ind, stocks in industry_stocks.items():
            if len(stocks) < MIN_STOCKS:
                continue
            rets_5d = [s["ret_5d"] for s in stocks]
            rets_20d = [s["ret_20d"] for s in stocks]
            breadth = sum(1 for s in stocks if s["above_ma20"]) / len(stocks)

            ind_ret_5d = float(np.median(rets_5d))
            ind_ret_20d = float(np.median(rets_20d))
            excess_5d = ind_ret_5d - mkt_5d
            excess_20d = ind_ret_20d - mkt_20d

            industry_ctx[ind] = {
                "n_stocks": len(stocks),
                "ind_ret_5d": ind_ret_5d,
                "ind_ret_20d": ind_ret_20d,
                "excess_5d": excess_5d,
                "excess_20d": excess_20d,
                "breadth": breadth,
            }
            excess_20d_list.append((ind, excess_20d))
            excess_5d_list.append((ind, excess_5d))

        excess_20d_list.sort(key=lambda x: -x[1])
        excess_5d_list.sort(key=lambda x: -x[1])
        n_ind = len(excess_20d_list)
        if n_ind == 0:
            self._last_industry_ctx = {}
            return {}

        rank_20d = {ind: i for i, (ind, _) in enumerate(excess_20d_list)}
        rank_5d = {ind: i for i, (ind, _) in enumerate(excess_5d_list)}

        top_quarter = max(1, n_ind // 4)
        top_half = max(1, n_ind // 2)

        for ind, ctx in industry_ctx.items():
            r20 = rank_20d.get(ind, n_ind)
            r5 = rank_5d.get(ind, n_ind)
            ctx["rank_20d"] = r20
            ctx["rank_5d"] = r5
            ctx["accel"] = (r20 - r5) / max(n_ind, 1)

            if r20 < top_quarter and r5 < top_half:
                ctx["phase"] = "leading"
            elif r5 < top_quarter and r20 >= top_quarter:
                ctx["phase"] = "accelerating"
            elif r20 < top_quarter and r5 >= top_half:
                ctx["phase"] = "fading"
            else:
                ctx["phase"] = "weak"

        self._last_industry_ctx = industry_ctx
        return industry_ctx

    # ── 引擎 A: 延续型 ──────────────────────────────────

    def _score_continuation(self, sm: Dict, ic: Dict) -> float:
        if ic.get("phase") != "leading":
            return 0.0

        score = 0.0

        excess_20d = ic.get("excess_20d", 0)
        if excess_20d > 0.06:
            score += 25
        elif excess_20d > 0.03:
            score += 20
        elif excess_20d > 0.01:
            score += 15
        elif excess_20d > 0:
            score += 10

        stock_excess = sm["ret_20d"] - ic.get("ind_ret_20d", 0)
        if stock_excess > 0.10:
            score += 25
        elif stock_excess > 0.05:
            score += 20
        elif stock_excess > 0.02:
            score += 15
        elif stock_excess > 0:
            score += 10

        tq = 0
        if sm["above_ma20"] and sm["above_ma60"]:
            tq += 8
        if sm["ma_aligned"]:
            tq += 7
        if sm["ma60_slope"] > 0.01:
            tq += 5
        elif sm["ma60_slope"] > 0:
            tq += 3
        if sm["dist_from_high"] > 0.90:
            tq += 5
        elif sm["dist_from_high"] > 0.80:
            tq += 3
        score += min(tq, 25)

        vh = 15
        if sm["vol_ratio"] < 0.85:
            vh += 10
        elif sm["vol_ratio"] < 1.1:
            vh += 5
        elif sm["vol_ratio"] > 1.5:
            vh -= 10
        score += max(0, min(vh, 25))

        return score

    # ── 引擎 B: 切换型 ──────────────────────────────────

    def _score_rotation(self, sm: Dict, ic: Dict) -> float:
        if ic.get("phase") != "accelerating":
            return 0.0

        score = 0.0

        accel = ic.get("accel", 0)
        if accel > 0.3:
            score += 25
        elif accel > 0.2:
            score += 20
        elif accel > 0.1:
            score += 15
        else:
            score += 10

        breadth = ic.get("breadth", 0)
        if breadth > 0.6:
            score += 25
        elif breadth > 0.4:
            score += 20
        elif breadth > 0.3:
            score += 15
        else:
            score += 8

        r20 = sm["ret_20d"]
        if 0.02 < r20 < 0.15:
            score += 25
        elif 0 < r20 <= 0.02:
            score += 18
        elif 0.15 <= r20 < 0.25:
            score += 12
        else:
            score += 5

        bq = 0
        if sm["above_ma20"]:
            bq += 8
        if sm["ma60_slope"] >= 0:
            bq += 7
        if sm["vol_20d"] < 0.04:
            bq += 5
        elif sm["vol_20d"] < 0.06:
            bq += 3
        if sm["dist_from_high"] > 0.85:
            bq += 5
        score += min(bq, 25)

        return score

    # ── 惩罚 ─────────────────────────────────────────────

    def _compute_penalties(self, sm: Dict, code: str) -> Tuple[float, float]:
        overheat = 0.0
        r10 = sm.get("ret_10d", 0)

        if r10 > 0.25:
            overheat += 25
        elif r10 > 0.20:
            overheat += 20
        elif r10 > 0.15:
            overheat += 12
        elif r10 > 0.10:
            overheat += 5

        if sm.get("vol_ratio", 1) > 1.8:
            overheat += 10

        gap = sm.get("ma20_gap", 0)
        if gap > 0.15:
            overheat += 10
        elif gap > 0.10:
            overheat += 5

        fail_pen = 0.0
        history = self._failure_history.get(code, [])
        if history:
            recent = history[-4:]
            bad = sum(1 for r in recent if r < -0.08)
            if bad >= 2:
                fail_pen = 20
            elif bad == 1:
                fail_pen = 10

        return overheat, fail_pen

    # ── 基本面地板 ───────────────────────────────────────

    def _check_fundamental_floor(self, info: Dict, fin: Optional[Dict]) -> bool:
        passes = 0.0

        roe = self._safe_float(info.get("roe"))
        if fin:
            roe = self._safe_float(fin.get("roe"), roe)
        if roe is not None and roe > 3:
            passes += 1

        debt = self._safe_float(fin.get("debt_to_assets")) if fin else None
        if debt is not None and debt < 70:
            passes += 1
        elif debt is None:
            passes += 0.5

        npm = self._safe_float(fin.get("netprofit_margin")) if fin else None
        if npm is not None and npm > 0:
            passes += 1

        return passes >= 1.5

    # ── 主流程 ───────────────────────────────────────────

    def screen(
        self,
        top_n: int = 30,
        min_data_days: int = 60,
        min_avg_amount: float = 3000,
        max_per_industry: int = 3,
    ) -> List[StockScoreV5]:
        t0 = time.perf_counter()
        logger.info("=" * 70)
        logger.info("多因子选股筛选器 v5 (双引擎) 启动")
        if self.as_of_date:
            logger.info("截止日期: %s", self.as_of_date)
        logger.info("=" * 70)

        # 1. 加载基础信息
        if self._basics_cache is not None:
            basics = self._basics_cache
            logger.info("stock_basic_info: %d 条 (缓存)", len(basics))
        else:
            basics = list(self.db.stock_basic_info.find(
                {}, {"_id": 0, "code": 1, "name": 1, "industry": 1,
                     "market": 1, "total_mv": 1, "roe": 1, "pe": 1, "pb": 1},
            ))
            logger.info("stock_basic_info: %d 条", len(basics))

        valid_infos: List[Dict] = []
        info_map: Dict[str, Dict] = {}
        for info in basics:
            code = info.get("code", "")
            name = info.get("name", "")
            if not name or "ST" in name.upper() or "退" in name:
                continue
            if code.startswith("200") or code.startswith("900"):
                continue
            valid_infos.append(info)
            info_map[code] = info
        logger.info("预过滤: %d 只", len(valid_infos))

        # 2. 加载财务
        t_fin = time.perf_counter()
        financial_map = self._load_financial_map()
        logger.info("财务缓存: %d 只, %.2fs", len(financial_map), time.perf_counter() - t_fin)

        # 3. 并行获取行情
        N_WORKERS = 16
        quote_cache: Dict[str, List[Dict]] = {}

        def _fetch(code):
            return code, self._fetch_quote_rows(code, limit=300)

        t_io = time.perf_counter()
        with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
            futures = {pool.submit(_fetch, info["code"]): info["code"] for info in valid_infos}
            done = 0
            for fut in as_completed(futures):
                code, rows = fut.result()
                quote_cache[code] = rows
                done += 1
                if done % 500 == 0:
                    logger.info("行情并发: %d/%d", done, len(futures))
        logger.info("行情获取: %d 只, %.2fs (%d线程)",
                     len(quote_cache), time.perf_counter() - t_io, N_WORKERS)

        # 4. 计算个股指标
        all_metrics: Dict[str, Dict] = {}
        excluded_data = excluded_liq = 0
        for code, rows in quote_cache.items():
            if len(rows) < min_data_days:
                excluded_data += 1
                continue
            m = self._compute_stock_metrics(rows)
            if m is None:
                excluded_data += 1
                continue
            if m["avg_amount_wan"] < min_avg_amount:
                excluded_liq += 1
                continue
            all_metrics[code] = m
        logger.info("有效指标: %d 只 (数据不足 %d, 流动性不足 %d)",
                     len(all_metrics), excluded_data, excluded_liq)

        # 5. 市场与行业上下文
        market_ctx = self._compute_market_context(all_metrics)
        industry_ctx = self._compute_industry_context(all_metrics, info_map, market_ctx)
        logger.info("行业分析: %d 个行业 | 市场广度 %.1f%%",
                     len(industry_ctx), market_ctx["breadth"] * 100)

        phase_counts = Counter(c["phase"] for c in industry_ctx.values())
        for phase, cnt in phase_counts.most_common():
            inds = sorted(
                [i for i, c in industry_ctx.items() if c["phase"] == phase],
                key=lambda i: -industry_ctx[i].get("excess_20d", 0),
            )
            logger.info("  %s: %d 个 — %s", phase, cnt, ", ".join(inds[:5]))

        # 6. 打分
        all_scored: List[StockScoreV5] = []
        for code, sm in all_metrics.items():
            info = info_map.get(code, {})
            industry = info.get("industry", "")
            ic = industry_ctx.get(industry)
            if not ic:
                continue

            if ic["phase"] not in ("leading", "accelerating"):
                continue

            if not sm["above_ma60"]:
                continue

            if not self._check_fundamental_floor(info, financial_map.get(code)):
                continue

            score_a = self._score_continuation(sm, ic)
            score_b = self._score_rotation(sm, ic)
            overheat, fail_pen = self._compute_penalties(sm, code)
            total_penalty = overheat + fail_pen

            raw_score = max(score_a, score_b)
            if raw_score <= 0:
                continue

            composite = max(0, raw_score - total_penalty)
            engine = "A" if score_a >= score_b else "B"

            if overheat >= 15:
                tag = "高热警告"
            elif engine == "B":
                tag = "切换启动"
            elif sm["ret_20d"] - ic.get("ind_ret_20d", 0) > 0.05:
                tag = "主线龙头"
            else:
                tag = "主线跟随"

            all_scored.append(StockScoreV5(
                symbol=code,
                name=info.get("name", ""),
                industry=industry,
                close=sm["close"],
                composite_score=round(composite, 2),
                engine=engine,
                engine_a_score=round(score_a, 2),
                engine_b_score=round(score_b, 2),
                tag=tag,
                industry_phase=ic.get("phase", ""),
                ind_ret_20d=round(ic.get("ind_ret_20d", 0) * 100, 2),
                ind_accel=round(ic.get("accel", 0) * 100, 2),
                ret_5d=round(sm["ret_5d"] * 100, 2),
                ret_20d=round(sm["ret_20d"] * 100, 2),
                ret_60d=round(sm["ret_60d"] * 100, 2),
                excess_vs_ind=round((sm["ret_20d"] - ic.get("ind_ret_20d", 0)) * 100, 2),
                vol_health=round(sm.get("vol_ratio", 1), 3),
                overheat_penalty=round(overheat, 2),
                failure_penalty=round(fail_pen, 2),
                total_penalty=round(total_penalty, 2),
            ))

        # 7. 排序 + 行业分散 + 引擎 B 限额
        all_scored.sort(key=lambda x: -x.composite_score)

        ind_count: Counter = Counter()
        engine_b_count = 0
        diversified: List[StockScoreV5] = []
        for item in all_scored:
            if max_per_industry and ind_count[item.industry] >= max_per_industry:
                continue
            if item.engine == "B" and engine_b_count >= self.MAX_ENGINE_B:
                continue
            diversified.append(item)
            ind_count[item.industry] += 1
            if item.engine == "B":
                engine_b_count += 1
            if len(diversified) >= top_n:
                break
        results = diversified

        for i, r in enumerate(results, 1):
            r.rank = i

        engine_dist = Counter(r.engine for r in results)
        tag_dist = Counter(r.tag for r in results)
        logger.info("候选: %d 只 → 输出 Top %d (A:%d B:%d)",
                     len(all_scored), len(results),
                     engine_dist.get("A", 0), engine_dist.get("B", 0))
        logger.info("标签分布: %s", dict(tag_dist))
        logger.info("总耗时: %.2fs", time.perf_counter() - t0)
        return results

    # ── 打印与导出 ───────────────────────────────────────

    def print_results(self, results: List[StockScoreV5]):
        if not results:
            print("无结果")
            return
        header = (f"{'#':>3} {'代码':>8} {'名称':<8} {'行业':<8} {'标签':<6} "
                  f"{'引擎':>2} {'综合':>5} {'A':>5} {'B':>5} "
                  f"{'惩罚':>4} {'阶段':<6} {'5d%':>6} {'20d%':>6} "
                  f"{'超额%':>6} {'波比':>5}")
        print("\n" + header)
        print("─" * len(header))
        for r in results:
            print(f"{r.rank:>3} {r.symbol:>8} {r.name:<8} {r.industry:<8} {r.tag:<6} "
                  f"{r.engine:>2} {r.composite_score:>5.1f} "
                  f"{r.engine_a_score:>5.1f} {r.engine_b_score:>5.1f} "
                  f"{r.total_penalty:>4.0f} {r.industry_phase:<6} "
                  f"{r.ret_5d:>+6.1f} {r.ret_20d:>+6.1f} "
                  f"{r.excess_vs_ind:>+6.1f} {r.vol_health:>5.2f}")

    def export_csv(self, results: List[StockScoreV5], path=None):
        if path is None:
            path = project_root / "output" / "screener_v5.csv"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        import csv
        fields = [
            "排名", "代码", "名称", "行业", "标签", "引擎", "综合分",
            "A分", "B分", "惩罚", "过热惩", "失败惩", "行业阶段",
            "行业20d%", "行业加速", "5d%", "20d%", "60d%", "超额%",
            "波动比", "收盘价",
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(fields)
            for r in results:
                w.writerow([
                    r.rank, r.symbol, r.name, r.industry, r.tag, r.engine,
                    r.composite_score, r.engine_a_score, r.engine_b_score,
                    r.total_penalty, r.overheat_penalty, r.failure_penalty,
                    r.industry_phase, r.ind_ret_20d, r.ind_accel,
                    r.ret_5d, r.ret_20d, r.ret_60d, r.excess_vs_ind,
                    r.vol_health, round(r.close, 2),
                ])
        logger.info("已导出: %s", path)

    def export_codes(self, results: List[StockScoreV5]) -> List[str]:
        return [r.symbol for r in results]


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="多因子选股筛选器 v5 (双引擎)")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--min-amount", type=float, default=3000)
    parser.add_argument("--min-days", type=int, default=60)
    parser.add_argument("--max-per-industry", type=int, default=3)
    parser.add_argument("--as-of-date", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--benchmark", type=str, default="000300")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    screener = StockScreenerV5(
        as_of_date=args.as_of_date,
        regime_benchmark=args.benchmark,
    )
    results = screener.screen(
        top_n=args.top,
        min_data_days=args.min_days,
        min_avg_amount=args.min_amount,
        max_per_industry=args.max_per_industry,
    )
    screener.print_results(results)
    screener.export_csv(results, args.output)


if __name__ == "__main__":
    main()
