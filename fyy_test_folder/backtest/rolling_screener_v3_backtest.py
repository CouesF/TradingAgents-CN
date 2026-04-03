#!/usr/bin/env python3
"""
滚动选股回测 v3（无 Agent 版）

基于 stock_screener_v3 的纯量化前向验证。
相对 v2 rolling 新增: 轮次间去重（上一轮选过的票降权 0.7）。

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    python fyy_test_folder/backtest/rolling_screener_v3_backtest.py \
        --start 2024-04-01 --end 2026-03-31

    python fyy_test_folder/backtest/rolling_screener_v3_backtest.py \
        --start 2024-04-01 --end 2026-03-31 \
        --top 5 --interval 10 --hold 10 --check 2
"""

import sys
import argparse
import logging
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Set

import numpy as np
import pandas as pd

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from tradingagents.config.database_manager import get_database_manager
from stock_screener_v3 import StockScreenerV3

logger = logging.getLogger("RollingV3Backtest")

FRESHNESS_PENALTY = 0.7


def get_trading_days(db, start: str, end: str) -> List[str]:
    cursor = db.stock_daily_quotes.find(
        {"symbol": "000001", "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    return [doc["trade_date"] for doc in cursor]


def get_prices(db, symbol: str, dates: List[str]) -> Dict[str, Dict]:
    if not dates:
        return {}
    cursor = db.stock_daily_quotes.find(
        {"symbol": symbol, "trade_date": {"$in": dates}},
        {"_id": 0, "trade_date": 1, "open": 1, "close": 1},
    )
    result = {}
    for doc in cursor:
        result[doc["trade_date"]] = {
            "open": float(doc["open"]),
            "close": float(doc["close"]),
        }
    return result


def run_rolling_backtest(
    db,
    start_date: str,
    end_date: str,
    top_n: int = 5,
    select_interval: int = 10,
    hold_days: int = 10,
    check_interval: int = 2,
    max_per_industry: int = 2,
) -> Tuple[List[Dict], List[Dict]]:

    latest_doc = db.stock_daily_quotes.find_one(
        {"symbol": "000001"}, {"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)])
    latest_date = latest_doc["trade_date"] if latest_doc else end_date

    all_days = get_trading_days(db, start_date, latest_date)
    sel_days = [d for d in all_days if start_date <= d <= end_date]

    if len(sel_days) < select_interval:
        logger.error("交易日不足")
        return [], []

    selection_dates = sel_days[::select_interval]
    logger.info("回测区间: %s ~ %s (%d 交易日)", start_date, end_date, len(sel_days))
    logger.info("参数: Top%d | 每%d日 | 持有%d日 | 快照%d日 | 行业限%d",
                top_n, select_interval, hold_days, check_interval, max_per_industry)
    logger.info("共 %d 轮选股", len(selection_dates))

    round_details: List[Dict] = []
    round_summaries: List[Dict] = []
    prev_round_codes: Set[str] = set()

    for rd, sel_date in enumerate(selection_dates, 1):
        logger.info("%s", "=" * 65)
        logger.info("第 %d/%d 轮 | 选股截止日: %s", rd, len(selection_dates), sel_date)

        # 选股（扩大候选，然后去重后取 top_n）
        fetch_n = top_n * 3
        screener = StockScreenerV3(db=db, as_of_date=sel_date)
        picks = screener.screen(
            top_n=fetch_n,
            min_trend_score=30,
            min_fundamental_score=20,
            max_per_industry=max_per_industry,
        )
        if not picks:
            logger.warning("  无结果，跳过")
            continue

        # 轮次间去重：上一轮选过的票降权
        for p in picks:
            if p.symbol in prev_round_codes:
                p.composite_score *= FRESHNESS_PENALTY

        picks.sort(key=lambda x: -x.composite_score)
        picks = picks[:top_n]

        codes = [(p.symbol, p.name, p.composite_score, p.tag) for p in picks]
        prev_round_codes = {p.symbol for p in picks}

        logger.info("  选出: %s",
                     " | ".join(f"{c}({n})" for c, n, *_ in codes))

        try:
            sel_idx = all_days.index(sel_date)
        except ValueError:
            future = [d for d in all_days if d >= sel_date]
            if not future:
                continue
            sel_idx = all_days.index(future[0])

        buy_idx = sel_idx + 1
        if buy_idx >= len(all_days):
            continue
        buy_date = all_days[buy_idx]

        end_idx = min(buy_idx + hold_days, len(all_days) - 1)
        sell_date = all_days[end_idx]

        snapshot_indices = list(range(buy_idx + check_interval, end_idx + 1, check_interval))
        if end_idx not in snapshot_indices:
            snapshot_indices.append(end_idx)

        snapshot_dates = [all_days[i] for i in snapshot_indices]
        need_dates = [buy_date] + snapshot_dates

        round_final_returns = []

        for code, name, comp_score, tag in codes:
            prices = get_prices(db, code, need_dates)
            if buy_date not in prices:
                continue

            buy_price = prices[buy_date]["open"]

            for si in snapshot_indices:
                snap_date = all_days[si]
                held = si - buy_idx
                if snap_date not in prices:
                    continue

                snap_close = prices[snap_date]["close"]
                ret = (snap_close - buy_price) / buy_price

                round_details.append({
                    "轮次": rd, "选股日": sel_date, "买入日": buy_date,
                    "代码": code, "名称": name,
                    "综合分": comp_score, "标签": tag,
                    "买入价(开盘)": round(buy_price, 2),
                    "快照日": snap_date, "持有天数": held,
                    "快照收盘": round(snap_close, 2),
                    "收益率%": round(ret * 100, 2),
                })

                if si == end_idx:
                    round_final_returns.append(ret)

        if round_final_returns:
            avg = np.mean(round_final_returns)
            winners = sum(1 for r in round_final_returns if r > 0)
            round_summaries.append({
                "轮次": rd, "选股日": sel_date, "买入日": buy_date,
                "卖出日": sell_date,
                "选股数": len(codes), "有效票": len(round_final_returns),
                "平均收益%": round(avg * 100, 2),
                "最高收益%": round(max(round_final_returns) * 100, 2),
                "最低收益%": round(min(round_final_returns) * 100, 2),
                "胜率%": round(winners / len(round_final_returns) * 100, 1),
            })
            logger.info("  平均 %+.2f%% | 胜率 %.0f%%",
                         avg * 100, winners / len(round_final_returns) * 100)

    return round_details, round_summaries


def print_and_export(details, summaries, hold_days, output_dir=None):
    if not summaries:
        print("\n无有效结果")
        return

    if output_dir is None:
        out_dir = project_root / "output"
    else:
        out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df_sum = pd.DataFrame(summaries)
    df_det = pd.DataFrame(details)

    rets = df_sum["平均收益%"].values
    nav = 1.0
    nav_curve = [1.0]
    for r in rets:
        nav *= (1 + r / 100)
        nav_curve.append(nav)

    total_rounds = len(summaries)
    win_rounds = sum(1 for r in rets if r > 0)
    total_return = (nav - 1) * 100
    trading_days_total = total_rounds * hold_days
    annualized = ((nav ** (245 / max(trading_days_total, 1))) - 1) * 100 if nav > 0 else 0

    peak = nav_curve[0]
    max_dd = 0
    for v in nav_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    verdict = "PASS" if total_return > 0 else "FAIL"

    print("=" * 80)
    print("  滚动选股回测 v3 结果  ".center(80, "="))
    print("=" * 80)
    print("\n【每轮汇总】")
    print(df_sum.to_string(index=False))
    print(f"\n{'─' * 80}")
    print("【整体策略表现】")
    print(f"  回测轮数:       {total_rounds}")
    print(f"  盈利轮数:       {win_rounds} / {total_rounds} ({win_rounds/total_rounds*100:.1f}%)")
    print(f"  每轮平均收益:   {np.mean(rets):+.2f}%")
    print(f"  每轮中位数:     {np.median(rets):+.2f}%")
    print(f"  累计净值:       {nav:.4f}")
    print(f"  累计总收益:     {total_return:+.2f}%")
    print(f"  年化收益率:     {annualized:+.2f}%")
    print(f"  最大回撤:       {max_dd*100:.2f}%")
    print(f"  策略结论:       {verdict}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    det_path = out_dir / f"rolling_v3_detail_{ts}.csv"
    sum_path = out_dir / f"rolling_v3_summary_{ts}.csv"
    rpt_path = out_dir / f"rolling_v3_report_{ts}.json"

    df_det.to_csv(det_path, index=False, encoding="utf-8-sig")
    df_sum.to_csv(sum_path, index=False, encoding="utf-8-sig")

    report = {
        "version": "v3",
        "timestamp": ts,
        "total_rounds": total_rounds,
        "win_rounds": win_rounds,
        "round_win_rate": f"{win_rounds/total_rounds*100:.1f}%",
        "avg_round_return": f"{np.mean(rets):+.2f}%",
        "median_round_return": f"{np.median(rets):+.2f}%",
        "cumulative_nav": round(nav, 4),
        "total_return": f"{total_return:+.2f}%",
        "annualized_return": f"{annualized:+.2f}%",
        "max_drawdown": f"{max_dd*100:.2f}%",
        "nav_curve": [round(v, 4) for v in nav_curve],
        "verdict": verdict,
    }
    with open(rpt_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n已导出: {det_path}")
    print(f"        {sum_path}")
    print(f"        {rpt_path}")


def main():
    parser = argparse.ArgumentParser(description="滚动选股回测 v3（无 Agent）")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--hold", type=int, default=10)
    parser.add_argument("--check", type=int, default=2)
    parser.add_argument("--max-per-industry", type=int, default=2)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    details, summaries = run_rolling_backtest(
        db, args.start, args.end,
        top_n=args.top, select_interval=args.interval,
        hold_days=args.hold, check_interval=args.check,
        max_per_industry=args.max_per_industry,
    )
    print_and_export(details, summaries, args.hold, args.output_dir)


if __name__ == "__main__":
    main()
