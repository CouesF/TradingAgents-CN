#!/usr/bin/env python3
"""
多因子选股筛选器 v4 提效版

基于 v3-fast，新增:
  1. 大盘状态检测 (Market Regime Filter)
     - 使用 index_daily_quotes 中的指数数据判断市场环境
     - 默认基准为沪深300(000300)
     - bull : 价格在 MA20 和 MA60 之上 → 建议满仓
     - neutral: 价格在 MA60 之上但 MA20 之下 → 建议减仓
     - bear : 价格在 MA60 之下 → 建议空仓
  2. 输出中包含大盘状态信息，供执行层参考

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate
    python scripts/stock_screener_v4_fast.py --top 30
    python scripts/stock_screener_v4_fast.py --as-of-date 2025-12-01 --top 20
"""

import sys
import argparse
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from stock_screener_v3_fast import StockScreenerV3Fast, StockScore


class StockScreenerV4Fast(StockScreenerV3Fast):
    """v4: 在 v3-fast 基础上增加大盘状态感知能力。"""

    REGIME_BENCHMARK = "000300"

    def __init__(self, db=None, as_of_date: str = None,
                 basics_cache=None, regime_benchmark: str = None):
        super().__init__(db=db, as_of_date=as_of_date, basics_cache=basics_cache)
        if regime_benchmark:
            self.REGIME_BENCHMARK = regime_benchmark
        self._regime_info: Optional[Dict] = None

    # ------------------------------------------------------------------
    # 大盘状态检测
    # ------------------------------------------------------------------
    def _load_index_rows(self, symbol: str, as_of_date: str = None, limit: int = 80) -> List[Dict]:
        """从 index_daily_quotes 读取指数数据，必要时回退到股票日线。"""
        query: Dict = {"symbol": symbol}
        if as_of_date:
            query["trade_date"] = {"$lte": as_of_date}

        rows = list(
            self.db.index_daily_quotes.find(
                query,
                {"_id": 0, "close": 1, "trade_date": 1, "name": 1},
            ).sort("trade_date", -1).limit(limit)
        )
        if rows:
            return rows

        # 兼容旧环境：若指数集合缺数据，再回退到股票行情
        return list(
            self.db.stock_daily_quotes.find(
                query,
                {"_id": 0, "close": 1, "trade_date": 1},
            ).sort("trade_date", -1).limit(limit)
        )

    def get_market_regime(self, as_of_date: str = None) -> Dict:
        """
        用基准指数的 MA20/MA60 判断大盘所处阶段。

        Returns:
            dict with keys: regime, position_ratio, benchmark,
                            close, ma20, ma60, above_ma20, above_ma60
        """
        aod = as_of_date or self.as_of_date
        rows = self._load_index_rows(self.REGIME_BENCHMARK, aod, limit=80)

        if len(rows) < 20:
            return {
                "regime": "unknown", "position_ratio": 0.6,
                "benchmark": self.REGIME_BENCHMARK,
                "close": 0, "ma20": 0, "ma60": 0,
                "above_ma20": False, "above_ma60": False,
            }

        closes = np.array([float(r["close"]) for r in reversed(rows)])
        n = len(closes)
        last_close = float(closes[-1])
        ma20 = float(np.mean(closes[-20:]))
        ma60 = float(np.mean(closes[-min(60, n):]))

        above_ma20 = last_close > ma20
        above_ma60 = last_close > ma60

        if above_ma20 and above_ma60:
            regime, position_ratio = "bull", 1.0
        elif above_ma60:
            regime, position_ratio = "neutral", 0.6
        else:
            regime, position_ratio = "bear", 0.4

        return {
            "regime": regime,
            "position_ratio": position_ratio,
            "benchmark": self.REGIME_BENCHMARK,
            "benchmark_name": rows[0].get("name", self.REGIME_BENCHMARK) if rows else self.REGIME_BENCHMARK,
            "close": round(last_close, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "above_ma20": above_ma20,
            "above_ma60": above_ma60,
        }

    @property
    def regime_info(self) -> Optional[Dict]:
        """上一次 screen() 计算出的大盘状态（调 screen 之后可用）"""
        return self._regime_info

    # ------------------------------------------------------------------
    # 覆写 screen：先检测大盘再选股
    # ------------------------------------------------------------------
    def screen(self, top_n=30, **kwargs) -> List[StockScore]:
        self._regime_info = self.get_market_regime()
        ri = self._regime_info

        _labels = {
            "bull": "多头(满仓)", "neutral": "震荡(减仓)",
            "bear": "空头(空仓)", "unknown": "未知",
        }
        self.logger.info(
            "大盘状态: %s | %s(%s) 收盘=%.2f MA20=%.2f MA60=%.2f | 建议仓位=%.0f%%",
            _labels.get(ri["regime"], ri["regime"]),
            ri.get("benchmark_name", ri["benchmark"]), ri["benchmark"],
            ri["close"], ri["ma20"], ri["ma60"],
            ri["position_ratio"] * 100,
        )
        return super().screen(top_n=top_n, **kwargs)

    # ------------------------------------------------------------------
    # 覆写打印：在结果前显示大盘状态
    # ------------------------------------------------------------------
    def print_results(self, results: List[StockScore]):
        if self._regime_info:
            ri = self._regime_info
            print(f"\n{'─' * 70}")
            print(f"【大盘状态】{ri['regime'].upper()} | "
                  f"{ri.get('benchmark_name', ri['benchmark'])}({ri['benchmark']}) 收盘={ri['close']} "
                  f"MA20={ri['ma20']} MA60={ri['ma60']} | "
                  f"建议仓位={ri['position_ratio'] * 100:.0f}%")
            print(f"{'─' * 70}")
        super().print_results(results)


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="多因子选股筛选器 v4 提效版")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--min-amount", type=float, default=3000)
    parser.add_argument("--min-days", type=int, default=120)
    parser.add_argument("--min-trend", type=float, default=30)
    parser.add_argument("--min-fund", type=float, default=20)
    parser.add_argument("--max-per-industry", type=int, default=2)
    parser.add_argument("--market", type=str, default=None)
    parser.add_argument("--as-of-date", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--no-print", action="store_true")
    parser.add_argument("--benchmark", type=str, default=None,
                        help="大盘基准代码，来自 index_daily_quotes (默认 000300)")
    args = parser.parse_args()

    screener = StockScreenerV4Fast(
        as_of_date=args.as_of_date,
        regime_benchmark=args.benchmark,
    )
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
        print(f"\n代码列表:")
        print(" ".join(codes))

    if screener.regime_info:
        ri = screener.regime_info
        print(f"\n大盘状态: {ri['regime']} | 建议仓位: {ri['position_ratio'] * 100:.0f}%")


if __name__ == "__main__":
    main()
