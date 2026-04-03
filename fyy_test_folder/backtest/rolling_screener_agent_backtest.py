#!/usr/bin/env python3
"""
滚动选股 + Trading Agent 回测 (A 版)

逻辑:
  1. 在回测区间内，每隔 N 个交易日做一次选股（as_of_date=选股日）
  2. 对选出的 Top N 股票，在选股日调用一次 Trading Agent
  3. 只有 Agent 给出“买入”的股票，才会在下一个交易日开盘买入
  4. 买入后固定持有 H 个交易日，不在中途再次调用 Agent
  5. 每隔 C 个交易日记录一次收益快照
  6. H 天结束后按当日收盘价结算

说明:
  - 这是 screen + agent 的 A 版验证脚本
  - 买入执行价固定使用“决策后的下一个交易日开盘价”
  - 卖出不走 agent，按固定持有期结算

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    python fyy_test_folder/backtest/rolling_screener_agent_backtest.py \
        --start 2025-12-01 --end 2026-03-31

    python fyy_test_folder/backtest/rolling_screener_agent_backtest.py \
        --start 2025-12-01 --end 2026-03-31 \
        --top 5 --interval 10 --hold 10 --check 2
"""

import sys
import argparse
import logging
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple

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
from stock_screener_v2 import StockScreenerV2
from run_backtest import create_config

logger = logging.getLogger("RollingScreenerAgentBacktest")


def get_latest_trading_date(db) -> str:
    """获取数据库中最新交易日"""
    doc = db.stock_daily_quotes.find_one(
        {"symbol": "000001"},
        {"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)],
    )
    return doc["trade_date"] if doc else ""


def get_trading_days(db, start: str, end: str) -> List[str]:
    """用 000001 获取交易日历"""
    cursor = db.stock_daily_quotes.find(
        {"symbol": "000001", "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    return [doc["trade_date"] for doc in cursor]


def get_prices(db, symbol: str, dates: List[str]) -> Dict[str, Dict]:
    """批量获取价格"""
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


def analyze_with_agent(ta: TradingAgentsGraph, stock_id: str, trade_date: str) -> Dict:
    """调用 Trading Agent，返回标准化决策结果"""
    try:
        _, decision = ta.propagate(stock_id, trade_date)
        return {
            "action": decision.get("action", "持有"),
            "target_price": decision.get("target_price"),
            "confidence": decision.get("confidence", 0.7),
            "risk_score": decision.get("risk_score", 0.5),
            "reasoning": decision.get("reasoning", "")[:300],
            "model_info": decision.get("model_info", ""),
            "success": True,
            "error": None,
        }
    except Exception as e:
        logger.error("Agent 分析失败: %s @ %s -> %s", stock_id, trade_date, e)
        return {
            "action": "持有",
            "target_price": None,
            "confidence": 0.5,
            "risk_score": 0.5,
            "reasoning": f"分析失败: {str(e)}",
            "model_info": "",
            "success": False,
            "error": str(e),
        }


def run_backtest(
    db,
    start_date: str,
    end_date: str,
    provider: str = "tencent-deepseek",
    top_n: int = 5,
    select_interval: int = 10,
    hold_days: int = 10,
    check_interval: int = 2,
    min_trend_score: float = 30,
    min_fundamental_score: float = 20,
    debug: bool = False,
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """
    Returns:
        decision_rows: 每轮选股 + agent 决策
        detail_rows: 每只买入股票在各快照时点的收益
        summary_rows: 每轮汇总
    """
    latest_date = get_latest_trading_date(db)
    if not latest_date:
        logger.error("数据库中找不到交易日")
        return [], [], []

    all_days = get_trading_days(db, start_date, latest_date)
    selection_days = [d for d in all_days if start_date <= d <= end_date]
    selection_dates = selection_days[::select_interval]

    logger.info("回测区间: %s ~ %s", start_date, end_date)
    logger.info(
        "参数: Top%d | 每%d个交易日选一次 | 固定持有%d天 | 每%d天快照",
        top_n,
        select_interval,
        hold_days,
        check_interval,
    )
    logger.info("共 %d 轮选股", len(selection_dates))

    config = create_config(provider)
    ta = TradingAgentsGraph(debug=debug, config=config)

    decision_rows: List[Dict] = []
    detail_rows: List[Dict] = []
    summary_rows: List[Dict] = []

    for round_no, sel_date in enumerate(selection_dates, 1):
        logger.info("%s", "=" * 72)
        logger.info("第 %d/%d 轮 | 选股日: %s", round_no, len(selection_dates), sel_date)
        logger.info("%s", "=" * 72)

        screener = StockScreenerV2(db=db, as_of_date=sel_date)
        picks = screener.screen(
            top_n=top_n,
            min_trend_score=min_trend_score,
            min_fundamental_score=min_fundamental_score,
        )
        if not picks:
            logger.warning("本轮无筛选结果，跳过")
            continue

        try:
            sel_idx = all_days.index(sel_date)
        except ValueError:
            logger.warning("选股日 %s 不在交易日历中，跳过", sel_date)
            continue

        buy_idx = sel_idx + 1
        if buy_idx >= len(all_days):
            logger.warning("选股日 %s 后无交易日，跳过", sel_date)
            continue
        buy_date = all_days[buy_idx]

        end_idx = min(buy_idx + hold_days, len(all_days) - 1)
        sell_date = all_days[end_idx]

        snapshot_indices = list(range(buy_idx + check_interval, end_idx + 1, check_interval))
        if end_idx not in snapshot_indices:
            snapshot_indices.append(end_idx)
        snapshot_dates = [all_days[i] for i in snapshot_indices]
        needed_dates = [buy_date] + snapshot_dates

        logger.info("买入日: %s | 到期结算日: %s", buy_date, sell_date)

        round_final_returns: List[float] = []
        buy_signal_count = 0

        for rank, pick in enumerate(picks, 1):
            logger.info("Agent 分析 %s(%s) @ %s", pick.symbol, pick.name, sel_date)
            agent_result = analyze_with_agent(ta, pick.symbol, sel_date)

            decision_row = {
                "轮次": round_no,
                "选股日": sel_date,
                "买入日": buy_date,
                "到期日": sell_date,
                "排名": rank,
                "代码": pick.symbol,
                "名称": pick.name,
                "行业": pick.industry,
                "板块": pick.market,
                "筛选综合分": round(pick.composite_score, 2),
                "筛选标签": pick.tag,
                "Agent动作": agent_result["action"],
                "Agent置信度": round(float(agent_result["confidence"]), 3),
                "Agent风险分": round(float(agent_result["risk_score"]), 3),
                "目标价": agent_result["target_price"],
                "Agent成功": agent_result["success"],
                "Agent理由": agent_result["reasoning"],
                "模型信息": agent_result["model_info"],
                "错误": agent_result["error"],
            }
            decision_rows.append(decision_row)

            if agent_result["action"] != "买入":
                continue

            prices = get_prices(db, pick.symbol, needed_dates)
            if buy_date not in prices:
                logger.warning("%s(%s) 在买入日无价格，跳过", pick.symbol, pick.name)
                continue

            buy_signal_count += 1
            buy_open = prices[buy_date]["open"]

            for idx in snapshot_indices:
                snap_date = all_days[idx]
                if snap_date not in prices:
                    continue

                held_days = idx - buy_idx
                snap_close = prices[snap_date]["close"]
                ret = (snap_close - buy_open) / buy_open

                detail_rows.append({
                    "轮次": round_no,
                    "选股日": sel_date,
                    "决策日": sel_date,
                    "买入日": buy_date,
                    "到期日": sell_date,
                    "排名": rank,
                    "代码": pick.symbol,
                    "名称": pick.name,
                    "筛选综合分": round(pick.composite_score, 2),
                    "筛选标签": pick.tag,
                    "Agent动作": agent_result["action"],
                    "Agent置信度": round(float(agent_result["confidence"]), 3),
                    "Agent风险分": round(float(agent_result["risk_score"]), 3),
                    "买入价(次日开盘)": round(buy_open, 2),
                    "快照日": snap_date,
                    "持有天数": held_days,
                    "快照收盘": round(snap_close, 2),
                    "收益率%": round(ret * 100, 2),
                })

                if idx == end_idx:
                    round_final_returns.append(ret)

        avg_ret = float(np.mean(round_final_returns)) if round_final_returns else 0.0
        best_ret = float(max(round_final_returns)) if round_final_returns else 0.0
        worst_ret = float(min(round_final_returns)) if round_final_returns else 0.0
        winners = sum(1 for r in round_final_returns if r > 0)
        valid_positions = len(round_final_returns)
        round_win_rate = (winners / valid_positions * 100) if valid_positions else 0.0

        summary_rows.append({
            "轮次": round_no,
            "选股日": sel_date,
            "买入日": buy_date,
            "到期日": sell_date,
            "筛选数量": len(picks),
            "买入信号数": buy_signal_count,
            "有效持仓数": valid_positions,
            "平均收益%": round(avg_ret * 100, 2),
            "最高收益%": round(best_ret * 100, 2),
            "最低收益%": round(worst_ret * 100, 2),
            "持仓胜率%": round(round_win_rate, 1),
        })

        logger.info(
            "本轮完成: 筛选 %d | 买入信号 %d | 平均收益 %+0.2f%% | 胜率 %.1f%%",
            len(picks),
            buy_signal_count,
            avg_ret * 100,
            round_win_rate,
        )

    return decision_rows, detail_rows, summary_rows


def export_results(
    decision_rows: List[Dict],
    detail_rows: List[Dict],
    summary_rows: List[Dict],
    params: Dict,
    output_dir: str = None,
) -> Dict:
    """导出 CSV 和 JSON 报告"""
    if output_dir is None:
        out_dir = project_root / "output"
    else:
        out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    decision_path = out_dir / f"rolling_screener_agent_decisions_{ts}.csv"
    detail_path = out_dir / f"rolling_screener_agent_detail_{ts}.csv"
    summary_path = out_dir / f"rolling_screener_agent_summary_{ts}.csv"
    report_path = out_dir / f"rolling_screener_agent_report_{ts}.json"

    df_decision = pd.DataFrame(decision_rows)
    df_detail = pd.DataFrame(detail_rows)
    df_summary = pd.DataFrame(summary_rows)

    if not df_decision.empty:
        df_decision.to_csv(decision_path, index=False, encoding="utf-8-sig")
    if not df_detail.empty:
        df_detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    if not df_summary.empty:
        df_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    round_returns = df_summary["平均收益%"].tolist() if not df_summary.empty else []
    nav = 1.0
    nav_curve = [1.0]
    for rr in round_returns:
        nav *= (1 + rr / 100)
        nav_curve.append(nav)

    total_rounds = len(summary_rows)
    profitable_rounds = sum(1 for rr in round_returns if rr > 0)
    total_return = (nav - 1) * 100
    hold_days = params["hold"]
    annualized = ((nav ** (245 / max(total_rounds * hold_days, 1))) - 1) * 100

    peak = nav_curve[0]
    max_drawdown = 0.0
    for v in nav_curve:
        peak = max(peak, v)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - v) / peak)

    report = {
        "timestamp": ts,
        "mode": "screen + agent (A版固定持有)",
        "params": params,
        "total_rounds": total_rounds,
        "profitable_rounds": profitable_rounds,
        "round_win_rate": f"{(profitable_rounds / total_rounds * 100) if total_rounds else 0:.1f}%",
        "avg_round_return": f"{np.mean(round_returns) if round_returns else 0:+.2f}%",
        "cumulative_nav": round(nav, 4),
        "total_return": f"{total_return:+.2f}%",
        "annualized_return": f"{annualized:+.2f}%",
        "max_drawdown": f"{max_drawdown * 100:.2f}%",
        "nav_curve": [round(v, 4) for v in nav_curve],
        "files": {
            "decisions": str(decision_path),
            "detail": str(detail_path),
            "summary": str(summary_path),
        },
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    report["files"]["report"] = str(report_path)
    return report


def print_report(report: Dict, summary_rows: List[Dict]):
    """打印简要报告"""
    print("=" * 80)
    print("Screen + Agent 滚动回测 (A版)".center(80))
    print("=" * 80)
    print(
        f"区间: {report['params']['start']} ~ {report['params']['end']} | "
        f"Top{report['params']['top']} | 每{report['params']['interval']}日选股 | "
        f"持有{report['params']['hold']}日"
    )
    print()

    if summary_rows:
        df_summary = pd.DataFrame(summary_rows)
        print("【每轮汇总】")
        print(df_summary.to_string(index=False))
        print()
    else:
        print("无有效回测结果")
        print()

    print("【整体结果】")
    print(f"总轮数:       {report['total_rounds']}")
    print(f"盈利轮数:     {report['profitable_rounds']} ({report['round_win_rate']})")
    print(f"每轮平均收益: {report['avg_round_return']}")
    print(f"累计净值:     {report['cumulative_nav']:.4f}")
    print(f"累计收益:     {report['total_return']}")
    print(f"年化收益:     {report['annualized_return']}")
    print(f"最大回撤:     {report['max_drawdown']}")
    print(f"净值曲线:     {' -> '.join(f'{v:.3f}' for v in report['nav_curve'])}")
    print()
    print("已导出:")
    print(f"  决策明细: {report['files']['decisions']}")
    print(f"  收益快照: {report['files']['detail']}")
    print(f"  轮次汇总: {report['files']['summary']}")
    print(f"  策略报告: {report['files']['report']}")


def main():
    parser = argparse.ArgumentParser(description="滚动选股 + Trading Agent 回测 (A版)")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=5, help="每次筛选前 N 只")
    parser.add_argument("--interval", type=int, default=10, help="每隔 N 个交易日筛选一次")
    parser.add_argument("--hold", type=int, default=10, help="固定持有 N 个交易日")
    parser.add_argument("--check", type=int, default=2, help="每隔 N 个交易日记录一次收益")
    parser.add_argument("--provider", default="tencent-deepseek",
                        choices=["deepseek", "tencent-deepseek", "dashscope", "google"],
                        help="LLM 提供商")
    parser.add_argument("--min-trend", type=float, default=30, help="最低趋势分")
    parser.add_argument("--min-fund", type=float, default=20, help="最低基本面分")
    parser.add_argument("--output-dir", default=None, help="输出目录，默认 output/")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    decision_rows, detail_rows, summary_rows = run_backtest(
        db=db,
        start_date=args.start,
        end_date=args.end,
        provider=args.provider,
        top_n=args.top,
        select_interval=args.interval,
        hold_days=args.hold,
        check_interval=args.check,
        min_trend_score=args.min_trend,
        min_fundamental_score=args.min_fund,
        debug=args.debug,
    )

    params = {
        "start": args.start,
        "end": args.end,
        "top": args.top,
        "interval": args.interval,
        "hold": args.hold,
        "check": args.check,
        "provider": args.provider,
        "min_trend": args.min_trend,
        "min_fund": args.min_fund,
    }
    report = export_results(decision_rows, detail_rows, summary_rows, params, args.output_dir)
    print_report(report, summary_rows)


if __name__ == "__main__":
    main()
