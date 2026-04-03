#!/usr/bin/env python3
"""
滚动选股 + Trading Agent 回测 v3 (A版)

基于 stock_screener_v3 + Trading Agent 的前向验证。
  1. 每隔 N 个交易日用 v3 筛选器选股
  2. 对每只选出的票在选股日调用一次 Trading Agent
  3. 只有 Agent 给出"买入"的才在次日开盘买入
  4. 固定持有 H 个交易日，不中途再次调用 Agent
  5. 每 C 天快照一次收益

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    python fyy_test_folder/backtest/rolling_screener_v3_agent_backtest.py \
        --start 2024-04-01 --end 2026-03-31

    python fyy_test_folder/backtest/rolling_screener_v3_agent_backtest.py \
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(project_root / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from tradingagents.config.database_manager import get_database_manager
from tradingagents.graph.trading_graph import TradingAgentsGraph
from stock_screener_v3 import StockScreenerV3
from run_backtest import create_config

logger = logging.getLogger("RollingV3AgentBacktest")

FRESHNESS_PENALTY = 0.7


def get_trading_days(db, start, end):
    cursor = db.stock_daily_quotes.find(
        {"symbol": "000001", "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    return [doc["trade_date"] for doc in cursor]


def get_prices(db, symbol, dates):
    if not dates:
        return {}
    cursor = db.stock_daily_quotes.find(
        {"symbol": symbol, "trade_date": {"$in": dates}},
        {"_id": 0, "trade_date": 1, "open": 1, "close": 1},
    )
    return {doc["trade_date"]: {"open": float(doc["open"]), "close": float(doc["close"])} for doc in cursor}


def analyze_with_agent(ta, stock_id, trade_date):
    try:
        _, decision = ta.propagate(stock_id, trade_date)
        return {
            "action": decision.get("action", "持有"),
            "target_price": decision.get("target_price"),
            "confidence": decision.get("confidence", 0.7),
            "risk_score": decision.get("risk_score", 0.5),
            "reasoning": decision.get("reasoning", "")[:300],
            "model_info": decision.get("model_info", ""),
            "success": True, "error": None,
        }
    except Exception as e:
        logger.error("Agent 失败: %s @ %s -> %s", stock_id, trade_date, e)
        return {
            "action": "持有", "target_price": None,
            "confidence": 0.5, "risk_score": 0.5,
            "reasoning": f"失败: {e}", "model_info": "",
            "success": False, "error": str(e),
        }


def run_backtest(
    db, start_date, end_date,
    provider="tencent-deepseek",
    top_n=5, select_interval=10, hold_days=10, check_interval=2,
    max_per_industry=2, debug=False,
):
    latest_doc = db.stock_daily_quotes.find_one(
        {"symbol": "000001"}, {"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)])
    latest_date = latest_doc["trade_date"] if latest_doc else end_date

    all_days = get_trading_days(db, start_date, latest_date)
    sel_days = [d for d in all_days if start_date <= d <= end_date]
    selection_dates = sel_days[::select_interval]

    logger.info("回测区间: %s ~ %s | %d 轮", start_date, end_date, len(selection_dates))

    config = create_config(provider)
    ta = TradingAgentsGraph(debug=debug, config=config)

    decision_rows, detail_rows, summary_rows = [], [], []
    prev_round_codes: Set[str] = set()

    for round_no, sel_date in enumerate(selection_dates, 1):
        logger.info("%s 第 %d/%d 轮 | %s %s",
                     "=" * 30, round_no, len(selection_dates), sel_date, "=" * 30)

        fetch_n = top_n * 3
        screener = StockScreenerV3(db=db, as_of_date=sel_date)
        picks = screener.screen(
            top_n=fetch_n, min_trend_score=30, min_fundamental_score=20,
            max_per_industry=max_per_industry,
        )
        if not picks:
            continue

        for p in picks:
            if p.symbol in prev_round_codes:
                p.composite_score *= FRESHNESS_PENALTY
        picks.sort(key=lambda x: -x.composite_score)
        picks = picks[:top_n]
        prev_round_codes = {p.symbol for p in picks}

        try:
            sel_idx = all_days.index(sel_date)
        except ValueError:
            continue
        buy_idx = sel_idx + 1
        if buy_idx >= len(all_days):
            continue
        buy_date = all_days[buy_idx]
        end_idx = min(buy_idx + hold_days, len(all_days) - 1)
        sell_date = all_days[end_idx]

        snapshot_indices = list(range(buy_idx + check_interval, end_idx + 1, check_interval))
        if end_idx not in snapshot_indices:
            snapshot_indices.append(end_idx)
        needed_dates = [buy_date] + [all_days[i] for i in snapshot_indices]

        round_final_returns = []
        buy_signal_count = 0

        for rank, pick in enumerate(picks, 1):
            logger.info("  Agent: %s(%s) @ %s", pick.symbol, pick.name, sel_date)
            agent_result = analyze_with_agent(ta, pick.symbol, sel_date)

            decision_rows.append({
                "轮次": round_no, "选股日": sel_date, "买入日": buy_date,
                "到期日": sell_date, "排名": rank,
                "代码": pick.symbol, "名称": pick.name,
                "行业": pick.industry, "板块": pick.market,
                "筛选综合分": round(pick.composite_score, 2),
                "筛选标签": pick.tag,
                "Agent动作": agent_result["action"],
                "Agent置信度": round(float(agent_result["confidence"]), 3),
                "Agent风险分": round(float(agent_result["risk_score"]), 3),
                "Agent成功": agent_result["success"],
                "Agent理由": agent_result["reasoning"],
            })

            if agent_result["action"] != "买入":
                continue

            prices = get_prices(db, pick.symbol, needed_dates)
            if buy_date not in prices:
                continue

            buy_signal_count += 1
            buy_open = prices[buy_date]["open"]

            for idx in snapshot_indices:
                snap_date = all_days[idx]
                if snap_date not in prices:
                    continue
                held = idx - buy_idx
                snap_close = prices[snap_date]["close"]
                ret = (snap_close - buy_open) / buy_open

                detail_rows.append({
                    "轮次": round_no, "选股日": sel_date,
                    "买入日": buy_date, "到期日": sell_date,
                    "代码": pick.symbol, "名称": pick.name,
                    "筛选综合分": round(pick.composite_score, 2),
                    "筛选标签": pick.tag,
                    "Agent动作": agent_result["action"],
                    "买入价(次日开盘)": round(buy_open, 2),
                    "快照日": snap_date, "持有天数": held,
                    "快照收盘": round(snap_close, 2),
                    "收益率%": round(ret * 100, 2),
                })
                if idx == end_idx:
                    round_final_returns.append(ret)

        avg_ret = float(np.mean(round_final_returns)) if round_final_returns else 0
        valid = len(round_final_returns)
        winners = sum(1 for r in round_final_returns if r > 0)

        summary_rows.append({
            "轮次": round_no, "选股日": sel_date,
            "买入日": buy_date, "到期日": sell_date,
            "筛选数": len(picks), "买入信号": buy_signal_count,
            "有效持仓": valid,
            "平均收益%": round(avg_ret * 100, 2),
            "最高收益%": round(max(round_final_returns) * 100, 2) if round_final_returns else 0,
            "最低收益%": round(min(round_final_returns) * 100, 2) if round_final_returns else 0,
            "胜率%": round(winners / valid * 100, 1) if valid else 0,
        })
        logger.info("  结果: 买入%d | 平均 %+.2f%% | 胜率 %.0f%%",
                     buy_signal_count, avg_ret * 100,
                     (winners / valid * 100) if valid else 0)

    return decision_rows, detail_rows, summary_rows


def export_and_print(decision_rows, detail_rows, summary_rows, params, output_dir=None):
    if output_dir is None:
        out_dir = project_root / "output"
    else:
        out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    df_dec = pd.DataFrame(decision_rows) if decision_rows else pd.DataFrame()
    df_det = pd.DataFrame(detail_rows) if detail_rows else pd.DataFrame()
    df_sum = pd.DataFrame(summary_rows) if summary_rows else pd.DataFrame()

    dec_path = out_dir / f"rolling_v3_agent_decisions_{ts}.csv"
    det_path = out_dir / f"rolling_v3_agent_detail_{ts}.csv"
    sum_path = out_dir / f"rolling_v3_agent_summary_{ts}.csv"
    rpt_path = out_dir / f"rolling_v3_agent_report_{ts}.json"

    if not df_dec.empty:
        df_dec.to_csv(dec_path, index=False, encoding="utf-8-sig")
    if not df_det.empty:
        df_det.to_csv(det_path, index=False, encoding="utf-8-sig")
    if not df_sum.empty:
        df_sum.to_csv(sum_path, index=False, encoding="utf-8-sig")

    rets = df_sum["平均收益%"].tolist() if not df_sum.empty else []
    nav = 1.0
    nav_curve = [1.0]
    for rr in rets:
        nav *= (1 + rr / 100)
        nav_curve.append(nav)

    total_rounds = len(summary_rows)
    profitable = sum(1 for r in rets if r > 0)
    total_return = (nav - 1) * 100
    hold = params.get("hold", 10)
    annualized = ((nav ** (245 / max(total_rounds * hold, 1))) - 1) * 100 if nav > 0 else 0
    peak = nav_curve[0]
    max_dd = 0.0
    for v in nav_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    report = {
        "version": "v3", "mode": "screen+agent (A版)",
        "timestamp": ts, "params": params,
        "total_rounds": total_rounds,
        "profitable_rounds": profitable,
        "round_win_rate": f"{(profitable/total_rounds*100) if total_rounds else 0:.1f}%",
        "avg_round_return": f"{np.mean(rets) if rets else 0:+.2f}%",
        "cumulative_nav": round(nav, 4),
        "total_return": f"{total_return:+.2f}%",
        "annualized_return": f"{annualized:+.2f}%",
        "max_drawdown": f"{max_dd*100:.2f}%",
        "nav_curve": [round(v, 4) for v in nav_curve],
    }
    with open(rpt_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 80)
    print("Screen + Agent 滚动回测 v3 (A版)".center(80))
    print("=" * 80)
    if not df_sum.empty:
        print("\n【每轮汇总】")
        print(df_sum.to_string(index=False))
    print(f"\n{'─'*80}")
    print(f"总轮数: {total_rounds} | 盈利: {profitable} ({report['round_win_rate']})")
    print(f"平均: {report['avg_round_return']} | 累计: {report['total_return']} | 年化: {report['annualized_return']}")
    print(f"最大回撤: {report['max_drawdown']} | 净值: {nav:.4f}")
    print(f"\n已导出: {dec_path}\n        {det_path}\n        {sum_path}\n        {rpt_path}")

    return report


def main():
    parser = argparse.ArgumentParser(description="滚动选股+Agent回测 v3 (A版)")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--hold", type=int, default=10)
    parser.add_argument("--check", type=int, default=2)
    parser.add_argument("--max-per-industry", type=int, default=2)
    parser.add_argument("--provider", default="tencent-deepseek",
                        choices=["deepseek", "tencent-deepseek", "dashscope", "google"])
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    dec, det, summ = run_backtest(
        db, args.start, args.end,
        provider=args.provider, top_n=args.top,
        select_interval=args.interval, hold_days=args.hold,
        check_interval=args.check, max_per_industry=args.max_per_industry,
        debug=args.debug,
    )
    params = {
        "start": args.start, "end": args.end,
        "top": args.top, "interval": args.interval,
        "hold": args.hold, "check": args.check,
        "max_per_industry": args.max_per_industry,
        "provider": args.provider,
    }
    export_and_print(dec, det, summ, params, args.output_dir)


if __name__ == "__main__":
    main()
