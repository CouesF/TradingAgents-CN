#!/usr/bin/env python3
"""
滚动选股回测 v3-fast（无 Agent 版）

基于 stock_screener_v3_fast 的纯量化前向验证。
策略逻辑与 v3 rolling 保持一致，只替换为提效版筛选器。

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    python fyy_test_folder/backtest/rolling_screener_v3_fast_backtest.py \
        --start 2024-04-01 --end 2026-03-31

    python fyy_test_folder/backtest/rolling_screener_v3_fast_backtest.py \
        --start 2024-04-01 --end 2026-03-31 \
        --top 5 --interval 10 --hold 10 --check 2
"""

import sys
import argparse
import logging
import json
import time
import hashlib
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
from stock_screener_v3_fast import StockScreenerV3Fast

logger = logging.getLogger("RollingV3FastBacktest")

FRESHNESS_PENALTY = 0.7


def snapshot_source_file(path: Path) -> Dict:
    try:
        text = path.read_text(encoding="utf-8")
        rel_path = str(path.relative_to(project_root))
        return {
            "path": rel_path,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "content": text,
        }
    except Exception as exc:
        return {"path": str(path), "error": str(exc)}


def build_run_metadata(
    *,
    timestamp: str,
    cli_args: Dict,
    total_elapsed_sec: float,
    avg_round_elapsed_sec: float,
    artifacts: Dict,
) -> Dict:
    backtest_path = Path(__file__).resolve()
    screener_path = project_root / "scripts" / "stock_screener_v3_fast.py"
    fetch_n = cli_args["top"] * 3
    return {
        "metadata_version": "1.0",
        "timestamp": timestamp,
        "strategy_version": "v3_fast",
        "run_context": {
            "backtest_script": str(backtest_path.relative_to(project_root)),
            "screener_script": str(screener_path.relative_to(project_root)),
            "cli_args": cli_args,
        },
        "timing": {
            "run_started_at": cli_args.get("_run_started_at"),
            "run_finished_at": cli_args.get("_run_finished_at"),
            "total_elapsed_sec": round(total_elapsed_sec, 2),
            "avg_round_elapsed_sec": round(avg_round_elapsed_sec, 2),
        },
        "screen_params": {
            "screen_call_top_n": fetch_n,
            "final_portfolio_top_n": cli_args["top"],
            "min_trend_score": 30,
            "min_fundamental_score": 20,
            "max_per_industry": cli_args["max_per_industry"],
            "freshness_penalty": FRESHNESS_PENALTY,
            "as_of_date": "每轮使用选股日作为 as_of_date",
        },
        "backtest_params": {
            "start_date": cli_args["start"],
            "end_date": cli_args["end"],
            "select_interval": cli_args["interval"],
            "hold_days": cli_args["hold"],
            "check_interval": cli_args["check"],
            "buy_rule": "选股次一交易日开盘买入",
            "sell_rule": "持有 hold_days 个交易日后收盘卖出",
            "position_sizing": "等权持有",
        },
        "calculation_method": {
            "summary": "使用 StockScreenerV3Fast 进行滚动前瞻回测，并在最终组合前施加新鲜度惩罚。",
            "selection_flow": [
                "预加载 basic_info 与财务数据缓存",
                "趋势打分：波动率调整动量、过热惩罚、波动率惩罚",
                "基本面打分：使用历史财务数据避免前视偏差",
                "先取 Top(top_n*3) 备选，再对上轮重复个股施加 freshness penalty",
                "重新排序后取最终 Top N 进入组合",
            ],
            "notes": [
                "包含行业分散约束 max_per_industry",
                "本脚本未包含大盘过滤和止损逻辑",
            ],
        },
        "data_sources": [
            "stock_basic_info",
            "stock_daily_quotes",
            "stock_financial_data",
        ],
        "artifacts": artifacts,
        "source_snapshots": {
            "backtest": snapshot_source_file(backtest_path),
            "screener": snapshot_source_file(screener_path),
        },
    }


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

    # 跨轮缓存：basic_info 是静态表，只读一次
    basics_cache = list(db.stock_basic_info.find(
        {},
        {"_id": 0, "code": 1, "name": 1, "industry": 1, "market": 1,
         "total_mv": 1, "roe": 1, "pe": 1, "pb": 1, "pe_ttm": 1},
    ))
    logger.info("basic_info 缓存: %d 条 (全轮复用)", len(basics_cache))

    for rd, sel_date in enumerate(selection_dates, 1):
        round_t0 = time.perf_counter()
        logger.info("%s", "=" * 65)
        logger.info("第 %d/%d 轮 | 选股截止日: %s", rd, len(selection_dates), sel_date)

        fetch_n = top_n * 3
        screener = StockScreenerV3Fast(db=db, as_of_date=sel_date,
                                       basics_cache=basics_cache)
        picks = screener.screen(
            top_n=fetch_n,
            min_trend_score=30,
            min_fundamental_score=20,
            max_per_industry=max_per_industry,
        )
        if not picks:
            logger.warning("  无结果，跳过")
            continue

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
                "本轮耗时s": round(time.perf_counter() - round_t0, 2),
            })
            logger.info("  平均 %+.2f%% | 胜率 %.0f%% | 用时 %.2fs",
                        avg * 100, winners / len(round_final_returns) * 100,
                        time.perf_counter() - round_t0)

    return round_details, round_summaries


def print_and_export(details, summaries, hold_days, output_dir=None, cli_args=None, total_elapsed_sec: float = 0.0):
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
    print("  滚动选股回测 v3-fast 结果  ".center(80, "="))
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
    print(f"  平均单轮耗时:   {df_sum['本轮耗时s'].mean():.2f}s")
    print(f"  策略结论:       {verdict}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    det_path = out_dir / f"rolling_v3_fast_detail_{ts}.csv"
    sum_path = out_dir / f"rolling_v3_fast_summary_{ts}.csv"
    rpt_path = out_dir / f"rolling_v3_fast_report_{ts}.json"
    meta_path = out_dir / f"rolling_v3_fast_meta_{ts}.json"

    df_det.to_csv(det_path, index=False, encoding="utf-8-sig")
    df_sum.to_csv(sum_path, index=False, encoding="utf-8-sig")

    report = {
        "version": "v3_fast",
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
        "avg_round_elapsed_sec": round(float(df_sum["本轮耗时s"].mean()), 2),
        "nav_curve": [round(v, 4) for v in nav_curve],
        "verdict": verdict,
        "metadata_file": str(meta_path),
    }
    with open(rpt_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    metadata = build_run_metadata(
        timestamp=ts,
        cli_args=cli_args or {},
        total_elapsed_sec=total_elapsed_sec,
        avg_round_elapsed_sec=float(df_sum["本轮耗时s"].mean()),
        artifacts={
            "detail_csv": str(det_path),
            "summary_csv": str(sum_path),
            "report_json": str(rpt_path),
            "meta_json": str(meta_path),
        },
    )
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"\n已导出: {det_path}")
    print(f"        {sum_path}")
    print(f"        {rpt_path}")
    print(f"        {meta_path}")


def main():
    parser = argparse.ArgumentParser(description="滚动选股回测 v3-fast（无 Agent）")
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
    run_started_at = datetime.now()
    run_t0 = time.perf_counter()

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    details, summaries = run_rolling_backtest(
        db, args.start, args.end,
        top_n=args.top, select_interval=args.interval,
        hold_days=args.hold, check_interval=args.check,
        max_per_industry=args.max_per_industry,
    )
    cli_args = vars(args).copy()
    cli_args["_run_started_at"] = run_started_at.isoformat(timespec="seconds")
    cli_args["_run_finished_at"] = datetime.now().isoformat(timespec="seconds")
    print_and_export(
        details,
        summaries,
        args.hold,
        args.output_dir,
        cli_args=cli_args,
        total_elapsed_sec=time.perf_counter() - run_t0,
    )


if __name__ == "__main__":
    main()
