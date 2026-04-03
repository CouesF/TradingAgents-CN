#!/usr/bin/env python3
"""
滚动选股回测 (Walk-Forward Backtest)

逻辑:
  1. 在回测区间内，每隔 N 个交易日做一次选股（用 as_of_date 消除前视偏差）
  2. 选股后下一个交易日开盘买入（等权分配资金）
  3. 持有 H 个交易日，每隔 C 个交易日记录一次收益快照
  4. H 天后卖出，进入下一轮
  5. 最终汇总：每轮收益、整体累计净值曲线、胜率、年化

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    # 默认参数: 每10交易日选5只，持有10天，每2天快照
    python fyy_test_folder/backtest/rolling_screener_backtest.py \
        --start 2025-12-01 --end 2026-03-31

    # 自定义参数
    python fyy_test_folder/backtest/rolling_screener_backtest.py \
        --start 2025-12-01 --end 2026-03-31 \
        --top 5 --interval 10 --hold 10 --check 2
"""

import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple
import json
import time
import hashlib

import pandas as pd
import numpy as np

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from tradingagents.config.database_manager import get_database_manager

sys.path.insert(0, str(project_root / "scripts"))
from stock_screener_v2 import StockScreenerV2

logger = logging.getLogger("RollingBacktest")


def snapshot_source_file(path: Path) -> Dict:
    """保存本次回测依赖源码快照，便于复现结果。"""
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
    screener_path = project_root / "scripts" / "stock_screener_v2.py"
    return {
        "metadata_version": "1.0",
        "timestamp": timestamp,
        "strategy_version": "v2",
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
            "top_n": cli_args["top"],
            "min_trend_score": 30,
            "min_fundamental_score": 20,
            "min_data_days": 120,
            "min_avg_amount_wan": 3000,
            "market_filter": None,
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
            "summary": "使用 StockScreenerV2 每隔固定交易日重新选股，按开盘买入并滚动持有。",
            "selection_flow": [
                "候选池过滤：剔除 ST/B 股、数据不足、流动性不足标的",
                "趋势打分：均线、多周期动量、年线斜率等",
                "基本面打分：ROE、负债率、毛利率、净利率、PE 等",
                "综合分排序后取 Top N",
                "下一交易日开盘买入，期间按固定检查点记录收益",
            ],
            "notes": [
                "交易日历来自 stock_daily_quotes 中 symbol=000001 的交易日期",
                "本脚本未加入止损、大盘过滤和行业分散约束",
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


# ── 工具函数 ─────────────────────────────────────────────

def get_trading_days(db, start: str, end: str) -> List[str]:
    """用沪市标杆股 000001 获取交易日历"""
    cursor = db.stock_daily_quotes.find(
        {"symbol": "000001", "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    return [doc["trade_date"] for doc in cursor]


def get_prices(db, symbol: str, dates: List[str]) -> Dict[str, Dict]:
    """批量获取某股票在多个日期的开盘/收盘价"""
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


# ── 主逻辑 ───────────────────────────────────────────────

def run_rolling_backtest(
    db,
    start_date: str,
    end_date: str,
    top_n: int = 5,
    select_interval: int = 10,
    hold_days: int = 10,
    check_interval: int = 2,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Returns:
        round_details: 每轮每只股票的详细快照记录
        round_summaries: 每轮汇总
    """
    # 获取完整交易日（向后多取30天给最后几轮留持有空间）
    all_days = get_trading_days(db, start_date, "2026-12-31")
    sel_days = [d for d in all_days if start_date <= d <= end_date]

    if len(sel_days) < select_interval:
        logger.error("交易日不足，无法执行回测")
        return [], []

    # 确定每轮选股日
    selection_dates = sel_days[::select_interval]
    logger.info("回测区间: %s ~ %s (%d 个交易日)", start_date, end_date, len(sel_days))
    logger.info("选股间隔: %d 交易日 | 每次选前 %d 只 | 持有 %d 天 | 快照间隔 %d 天",
                select_interval, top_n, hold_days, check_interval)
    logger.info("共 %d 轮选股: %s", len(selection_dates),
                ", ".join(selection_dates))

    round_details: List[Dict] = []
    round_summaries: List[Dict] = []

    for rd, sel_date in enumerate(selection_dates, 1):
        sep = "=" * 65
        logger.info("\n%s", sep)
        logger.info("第 %d/%d 轮 | 选股截止日: %s", rd, len(selection_dates), sel_date)
        logger.info("%s", sep)

        # ① 选股
        screener = StockScreenerV2(db=db, as_of_date=sel_date)
        picks = screener.screen(top_n=top_n, min_trend_score=30, min_fundamental_score=20)
        if not picks:
            logger.warning("  无选股结果，跳过本轮")
            continue

        codes = [(p.symbol, p.name, p.composite_score, p.tag) for p in picks]
        logger.info("  选出 %d 只: %s",
                     len(codes), " | ".join(f"{c}({n})" for c, n, *_ in codes))

        # ② 确定买入日（选股次日）
        try:
            sel_idx = all_days.index(sel_date)
        except ValueError:
            future = [d for d in all_days if d >= sel_date]
            if not future:
                continue
            sel_idx = all_days.index(future[0])

        buy_idx = sel_idx + 1
        if buy_idx >= len(all_days):
            logger.warning("  选股日之后无交易日")
            continue
        buy_date = all_days[buy_idx]

        # ③ 确定持有区间 & 快照日
        end_idx = min(buy_idx + hold_days, len(all_days) - 1)
        sell_date = all_days[end_idx]

        snapshot_indices = list(range(
            buy_idx + check_interval, end_idx + 1, check_interval))
        if end_idx not in snapshot_indices:
            snapshot_indices.append(end_idx)

        snapshot_dates = [all_days[i] for i in snapshot_indices]
        need_dates = [buy_date] + snapshot_dates

        logger.info("  买入日: %s → 卖出日: %s", buy_date, sell_date)
        logger.info("  快照日: %s", ", ".join(snapshot_dates))

        # ④ 逐股获取价格 & 计算收益
        round_final_returns = []

        for code, name, comp_score, tag in codes:
            prices = get_prices(db, code, need_dates)

            if buy_date not in prices:
                logger.warning("    %s(%s) 买入日无数据，跳过", code, name)
                continue

            buy_price = prices[buy_date]["open"]  # 开盘价买入

            for si in snapshot_indices:
                snap_date = all_days[si]
                held = si - buy_idx

                if snap_date not in prices:
                    continue

                snap_close = prices[snap_date]["close"]
                ret = (snap_close - buy_price) / buy_price

                record = {
                    "轮次": rd,
                    "选股日": sel_date,
                    "买入日": buy_date,
                    "代码": code,
                    "名称": name,
                    "综合分": comp_score,
                    "标签": tag,
                    "买入价(开盘)": round(buy_price, 2),
                    "快照日": snap_date,
                    "持有天数": held,
                    "快照收盘": round(snap_close, 2),
                    "收益率%": round(ret * 100, 2),
                }
                round_details.append(record)

                if si == end_idx:
                    round_final_returns.append(ret)

        # ⑤ 本轮汇总
        if round_final_returns:
            avg = np.mean(round_final_returns)
            winners = sum(1 for r in round_final_returns if r > 0)
            round_summaries.append({
                "轮次": rd,
                "选股日": sel_date,
                "买入日": buy_date,
                "卖出日": sell_date,
                "选股数": len(codes),
                "有效票": len(round_final_returns),
                "平均收益%": round(avg * 100, 2),
                "最高收益%": round(max(round_final_returns) * 100, 2),
                "最低收益%": round(min(round_final_returns) * 100, 2),
                "胜率%": round(winners / len(round_final_returns) * 100, 1),
            })
            emoji = "+" if avg > 0 else ""
            logger.info("  本轮结果: 平均 %s%.2f%% | 胜率 %.0f%% | 最高 %.2f%% 最低 %.2f%%",
                         emoji, avg * 100,
                         winners / len(round_final_returns) * 100,
                         max(round_final_returns) * 100,
                         min(round_final_returns) * 100)

    return round_details, round_summaries


def print_final_report(
    round_details: List[Dict],
    round_summaries: List[Dict],
    hold_days: int,
    output_dir: str = None,
    cli_args: Dict = None,
    total_elapsed_sec: float = 0.0,
):
    if not round_summaries:
        print("\n[!] 无有效回测结果")
        return

    sep = "=" * 75

    # ── 每轮概览 ──
    print(f"\n{sep}")
    print("  滚动选股回测结果  ".center(75, "="))
    print(sep)

    df_sum = pd.DataFrame(round_summaries)
    print("\n【每轮汇总】")
    print(df_sum.to_string(index=False))

    # ── 整体统计 ──
    total_rounds = len(round_summaries)
    avg_returns = df_sum["平均收益%"].values
    overall_avg = np.mean(avg_returns)
    overall_median = np.median(avg_returns)
    win_rounds = sum(1 for r in avg_returns if r > 0)

    # 累计净值：假设每轮等权投入，收益滚动累乘
    nav = 1.0
    nav_curve = [1.0]
    for r in avg_returns:
        nav *= (1 + r / 100)
        nav_curve.append(nav)

    total_return = (nav - 1) * 100
    trading_days_total = total_rounds * hold_days
    if trading_days_total > 0:
        annualized = ((nav ** (245 / trading_days_total)) - 1) * 100
    else:
        annualized = 0

    # 最大回撤
    peak = nav_curve[0]
    max_dd = 0
    for v in nav_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    print(f"\n{'─' * 75}")
    print("【整体策略表现】")
    print(f"  回测轮数:       {total_rounds} 轮")
    print(f"  盈利轮数:       {win_rounds} / {total_rounds} (轮次胜率 {win_rounds/total_rounds*100:.1f}%)")
    print(f"  每轮平均收益:   {overall_avg:+.2f}%")
    print(f"  每轮收益中位数: {overall_median:+.2f}%")
    print(f"  最好一轮:       {max(avg_returns):+.2f}%")
    print(f"  最差一轮:       {min(avg_returns):+.2f}%")
    print(f"  累计净值:       {nav:.4f}")
    print(f"  累计总收益:     {total_return:+.2f}%")
    print(f"  年化收益率:     {annualized:+.2f}%")
    print(f"  最大回撤:       {max_dd*100:.2f}%")
    print(f"  净值曲线:       {' → '.join(f'{v:.3f}' for v in nav_curve)}")

    verdict = "PASS" if total_return > 0 else "FAIL"
    print(f"\n  策略结论:       {verdict} — 累计 {total_return:+.2f}%")
    print(sep)

    # ── 详细快照 ──
    df_detail = pd.DataFrame(round_details)
    print("\n【详细快照（每只股票每个检查点）】")
    print(df_detail.to_string(index=False))
    print()

    # ── 导出 ──
    if output_dir is None:
        output_dir = Path(project_root) / "output"
    else:
        output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail_path = output_dir / f"rolling_backtest_detail_{ts}.csv"
    summary_path = output_dir / f"rolling_backtest_summary_{ts}.csv"
    report_path = output_dir / f"rolling_backtest_report_{ts}.json"
    meta_path = output_dir / f"rolling_backtest_meta_{ts}.json"

    df_detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    df_sum.to_csv(summary_path, index=False, encoding="utf-8-sig")

    report = {
        "timestamp": ts,
        "total_rounds": total_rounds,
        "win_rounds": win_rounds,
        "round_win_rate": f"{win_rounds/total_rounds*100:.1f}%",
        "avg_round_return": f"{overall_avg:+.2f}%",
        "cumulative_nav": round(nav, 4),
        "total_return": f"{total_return:+.2f}%",
        "annualized_return": f"{annualized:+.2f}%",
        "max_drawdown": f"{max_dd*100:.2f}%",
        "nav_curve": [round(v, 4) for v in nav_curve],
        "verdict": verdict,
        "metadata_file": str(meta_path),
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    metadata = build_run_metadata(
        timestamp=ts,
        cli_args=cli_args or {},
        total_elapsed_sec=total_elapsed_sec,
        avg_round_elapsed_sec=float(df_sum.get("本轮耗时s", pd.Series(dtype=float)).mean() or 0.0),
        artifacts={
            "detail_csv": str(detail_path),
            "summary_csv": str(summary_path),
            "report_json": str(report_path),
            "meta_json": str(meta_path),
        },
    )
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"已导出:")
    print(f"  详细快照: {detail_path}")
    print(f"  轮次汇总: {summary_path}")
    print(f"  策略报告: {report_path}")
    print(f"  运行元数据: {meta_path}")


# ── 入口 ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="滚动选股回测 — 每 N 天选股，往后持有 H 天，每 C 天快照")
    parser.add_argument("--start", type=str, required=True,
                        help="回测起始日 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, required=True,
                        help="回测结束日 (YYYY-MM-DD)")
    parser.add_argument("--top", type=int, default=5,
                        help="每次选前 N 只 (默认 5)")
    parser.add_argument("--interval", type=int, default=10,
                        help="选股间隔交易日 (默认 10)")
    parser.add_argument("--hold", type=int, default=10,
                        help="持有交易日 (默认 10)")
    parser.add_argument("--check", type=int, default=2,
                        help="收益快照间隔交易日 (默认 2)")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="输出目录 (默认 output/)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    run_started_at = datetime.now()
    run_t0 = time.perf_counter()

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    details, summaries = run_rolling_backtest(
        db,
        start_date=args.start,
        end_date=args.end,
        top_n=args.top,
        select_interval=args.interval,
        hold_days=args.hold,
        check_interval=args.check,
    )

    cli_args = vars(args).copy()
    cli_args["_run_started_at"] = run_started_at.isoformat(timespec="seconds")
    cli_args["_run_finished_at"] = datetime.now().isoformat(timespec="seconds")
    print_final_report(
        details,
        summaries,
        args.hold,
        args.output_dir,
        cli_args=cli_args,
        total_elapsed_sec=time.perf_counter() - run_t0,
    )


if __name__ == "__main__":
    main()
