#!/usr/bin/env python3
"""
滚动选股回测 v4-fast

基于 v3-fast 回测，新增两大风控机制:
  1. 大盘状态过滤 (Market Regime Filter)
     - bull  → 满仓 top_n 只
     - neutral → 减仓至 top_n * 0.6
     - bear  → 本轮空仓跳过（资金留现金）
  2. 个股止损线 (Stop-Loss)
     - 持有期内每日检查浮亏，超过阈值自动止损退出
     - 默认止损线: -8%

说明:
  - 交易日历和大盘判断都优先使用 index_daily_quotes
  - 默认大盘基准改为沪深300(000300)，不再误用股票 000001

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    # 默认参数
    python fyy_test_folder/backtest/rolling_screener_v4_fast_backtest.py \
        --start 2024-04-01 --end 2026-03-31

    # 自定义止损和仓位
    python fyy_test_folder/backtest/rolling_screener_v4_fast_backtest.py \
        --start 2024-04-01 --end 2026-03-31 \
        --stop-loss -10 --top 5 --hold 10
"""

import sys
import argparse
import logging
import json
import time
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set

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
from stock_screener_v4_fast import StockScreenerV4Fast

logger = logging.getLogger("RollingV4FastBacktest")

FRESHNESS_PENALTY = 0.7
DEFAULT_STOP_LOSS_PCT = -0.08


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
    screener_path = project_root / "scripts" / "stock_screener_v4_fast.py"
    return {
        "metadata_version": "1.0",
        "timestamp": timestamp,
        "strategy_version": "v4_fast",
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
            "screen_call_top_n_formula": "effective_top_n * 3",
            "portfolio_top_n": cli_args["top"],
            "min_trend_score": 30,
            "min_fundamental_score": 20,
            "max_per_industry": cli_args["max_per_industry"],
            "freshness_penalty": FRESHNESS_PENALTY,
            "regime_benchmark": cli_args["benchmark"],
            "regime_rules": {
                "bull": "满仓 top_n",
                "neutral": "减仓到 max(2, int(top_n * 0.6))",
                "bear": "轻仓到 max(2, int(top_n * 0.4))",
            },
            "stop_loss_pct": cli_args["stop_loss_pct_normalized"],
        },
        "backtest_params": {
            "start_date": cli_args["start"],
            "end_date": cli_args["end"],
            "select_interval": cli_args["interval"],
            "hold_days": cli_args["hold"],
            "check_interval": cli_args["check"],
            "buy_rule": "选股次一交易日开盘买入",
            "sell_rule": "到期卖出或触发止损后提前退出",
            "position_sizing": "等权持有",
        },
        "calculation_method": {
            "summary": "使用 StockScreenerV4Fast 做滚动前瞻回测，加入指数大盘过滤、行业分散、新鲜度惩罚与个股止损。",
            "selection_flow": [
                "从 index_daily_quotes 读取大盘指数，按 MA20/MA60 识别 bull/neutral/bear",
                "根据大盘状态动态调整 effective_top_n",
                "使用 StockScreenerV4Fast 选出 effective_top_n * 3 个备选",
                "对上轮重复个股施加 freshness penalty 后重排",
                "取最终 effective_top_n 个标的，次日开盘买入",
                "持有期间逐日检查止损，按检查点记录收益与退出状态",
            ],
            "notes": [
                "交易日历优先来自 index_daily_quotes",
                "大盘默认基准为沪深300(000300)",
                "止损线按当日收盘相对买入开盘价计算",
            ],
        },
        "data_sources": [
            "stock_basic_info",
            "stock_daily_quotes",
            "stock_financial_data",
            "index_daily_quotes",
        ],
        "artifacts": artifacts,
        "source_snapshots": {
            "backtest": snapshot_source_file(backtest_path),
            "screener": snapshot_source_file(screener_path),
        },
    }


# ── 工具函数 ─────────────────────────────────────────────

def get_trading_days(db, start: str, end: str, calendar_symbol: str = "000300") -> List[str]:
    cursor = db.index_daily_quotes.find(
        {"symbol": calendar_symbol, "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    days = [doc["trade_date"] for doc in cursor]
    if days:
        return days

    # 兼容旧环境：若指数集合缺数据，再回退到股票日线
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


def get_prices_range(db, symbol: str, start_date: str, end_date: str) -> Dict[str, Dict]:
    """获取日期区间内所有交易日的开盘/收盘价（用于每日止损检查）"""
    cursor = db.stock_daily_quotes.find(
        {"symbol": symbol, "trade_date": {"$gte": start_date, "$lte": end_date}},
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
    max_per_industry: int = 2,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    regime_benchmark: str = "000300",
) -> Tuple[List[Dict], List[Dict]]:

    latest_doc = db.index_daily_quotes.find_one(
        {"symbol": regime_benchmark}, {"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)])
    if latest_doc:
        latest_date = latest_doc["trade_date"]
    else:
        latest_doc = db.stock_daily_quotes.find_one(
            {"symbol": "000001"}, {"_id": 0, "trade_date": 1},
            sort=[("trade_date", -1)])
        latest_date = latest_doc["trade_date"] if latest_doc else end_date

    all_days = get_trading_days(db, start_date, latest_date, regime_benchmark)
    sel_days = [d for d in all_days if start_date <= d <= end_date]

    if len(sel_days) < select_interval:
        logger.error("交易日不足")
        return [], []

    selection_dates = sel_days[::select_interval]
    logger.info("=" * 70)
    logger.info("滚动选股回测 v4-fast 启动")
    logger.info("=" * 70)
    logger.info("回测区间: %s ~ %s (%d 交易日)", start_date, end_date, len(sel_days))
    logger.info("参数: Top%d | 每%d日 | 持有%d日 | 快照%d日 | 行业限%d",
                top_n, select_interval, hold_days, check_interval, max_per_industry)
    logger.info("止损线: %.1f%% | 大盘基准: %s", stop_loss_pct * 100, regime_benchmark)
    logger.info("共 %d 轮选股", len(selection_dates))

    round_details: List[Dict] = []
    round_summaries: List[Dict] = []
    prev_round_codes: Set[str] = set()

    basics_cache = list(db.stock_basic_info.find(
        {},
        {"_id": 0, "code": 1, "name": 1, "industry": 1, "market": 1,
         "total_mv": 1, "roe": 1, "pe": 1, "pb": 1, "pe_ttm": 1},
    ))
    logger.info("basic_info 缓存: %d 条", len(basics_cache))

    total_skip_bear = 0
    total_stop_loss_events = 0

    for rd, sel_date in enumerate(selection_dates, 1):
        round_t0 = time.perf_counter()
        logger.info("%s", "=" * 65)
        logger.info("第 %d/%d 轮 | 选股截止日: %s", rd, len(selection_dates), sel_date)

        # ── [v4 新增] 大盘状态检测 ──
        screener = StockScreenerV4Fast(
            db=db, as_of_date=sel_date,
            basics_cache=basics_cache,
            regime_benchmark=regime_benchmark,
        )
        regime_info = screener.get_market_regime(sel_date)
        regime = regime_info["regime"]

        if regime == "bear":
            effective_top_n = max(2, int(top_n * 0.4))
            total_skip_bear += 1
            logger.info("  大盘空头 (%s(%s) 收盘=%.2f < MA60=%.2f) → 轻仓 %d 只",
                        regime_info.get("benchmark_name", regime_info["benchmark"]),
                        regime_info["benchmark"], regime_info["close"],
                        regime_info["ma60"], effective_top_n)
        elif regime == "neutral":
            effective_top_n = max(2, int(top_n * 0.6))
            logger.info("  大盘震荡 (%s(%s) 收盘=%.2f < MA20=%.2f, > MA60=%.2f) → 减仓至 %d 只",
                        regime_info.get("benchmark_name", regime_info["benchmark"]),
                        regime_info["benchmark"], regime_info["close"],
                        regime_info["ma20"], regime_info["ma60"], effective_top_n)
        else:
            effective_top_n = top_n
            logger.info("  大盘多头 (%s(%s) 收盘=%.2f > MA20=%.2f > MA60=%.2f) → 满仓 %d 只",
                        regime_info.get("benchmark_name", regime_info["benchmark"]),
                        regime_info["benchmark"], regime_info["close"],
                        regime_info["ma20"], regime_info["ma60"], effective_top_n)

        # ── 选股 ──
        fetch_n = effective_top_n * 3
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
        picks = picks[:effective_top_n]

        codes = [(p.symbol, p.name, p.composite_score, p.tag) for p in picks]
        prev_round_codes = {p.symbol for p in picks}

        logger.info("  选出: %s",
                    " | ".join(f"{c}({n})" for c, n, *_ in codes))

        # ── 确定交易日 ──
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

        # ── 逐股处理：止损 + 收益记录 ──
        round_final_returns = []
        round_sl_count = 0

        for code, name, comp_score, tag in codes:
            # 获取持有期全部日线数据（用于每日止损检查）
            prices = get_prices_range(db, code, buy_date, sell_date)

            if buy_date not in prices:
                logger.warning("    %s(%s) 买入日无数据，跳过", code, name)
                continue

            buy_price = prices[buy_date]["open"]

            # ── [v4 新增] 每日止损扫描 ──
            exit_idx = end_idx
            exit_reason = "到期"

            for di in range(buy_idx + 1, end_idx + 1):
                day = all_days[di] if di < len(all_days) else None
                if day is None or day not in prices:
                    continue
                day_ret = (prices[day]["close"] - buy_price) / buy_price
                if day_ret <= stop_loss_pct:
                    exit_idx = di
                    exit_reason = "止损"
                    break

            exit_date = all_days[exit_idx] if exit_idx < len(all_days) else sell_date
            exit_close = prices.get(exit_date, {}).get("close")

            if exit_close is None:
                for fallback in range(exit_idx, buy_idx, -1):
                    fd = all_days[fallback] if fallback < len(all_days) else None
                    if fd and fd in prices:
                        exit_date = fd
                        exit_close = prices[fd]["close"]
                        break
                if exit_close is None:
                    continue

            exit_ret = (exit_close - buy_price) / buy_price

            # ── 记录快照明细（截至退出日）──
            for si in snapshot_indices:
                snap_date = all_days[si] if si < len(all_days) else None
                if snap_date is None:
                    continue
                if snap_date > exit_date:
                    break
                if snap_date not in prices:
                    continue

                held = si - buy_idx
                snap_close = prices[snap_date]["close"]
                ret = (snap_close - buy_price) / buy_price
                is_exit_point = (snap_date == exit_date)

                round_details.append({
                    "轮次": rd, "选股日": sel_date, "买入日": buy_date,
                    "代码": code, "名称": name,
                    "综合分": comp_score, "标签": tag,
                    "大盘状态": regime,
                    "买入价(开盘)": round(buy_price, 2),
                    "快照日": snap_date, "持有天数": held,
                    "快照收盘": round(snap_close, 2),
                    "收益率%": round(ret * 100, 2),
                    "退出": exit_reason if is_exit_point else "",
                })

            # 止损日不在快照日上时，补一条退出记录
            exit_on_snapshot = any(
                (si < len(all_days) and all_days[si] == exit_date)
                for si in snapshot_indices
            )
            if exit_reason == "止损" and not exit_on_snapshot:
                held = exit_idx - buy_idx
                round_details.append({
                    "轮次": rd, "选股日": sel_date, "买入日": buy_date,
                    "代码": code, "名称": name,
                    "综合分": comp_score, "标签": tag,
                    "大盘状态": regime,
                    "买入价(开盘)": round(buy_price, 2),
                    "快照日": exit_date, "持有天数": held,
                    "快照收盘": round(exit_close, 2),
                    "收益率%": round(exit_ret * 100, 2),
                    "退出": "止损",
                })

            round_final_returns.append(exit_ret)
            if exit_reason == "止损":
                round_sl_count += 1
                total_stop_loss_events += 1
                logger.info("    %s(%s) 止损退出: 第%d天 %.2f%%",
                            code, name, exit_idx - buy_idx, exit_ret * 100)

        # ── 本轮汇总 ──
        if round_final_returns:
            avg = np.mean(round_final_returns)
            winners = sum(1 for r in round_final_returns if r > 0)
            round_summaries.append({
                "轮次": rd, "选股日": sel_date,
                "买入日": buy_date, "卖出日": sell_date,
                "大盘状态": regime,
                "选股数": len(codes),
                "有效票": len(round_final_returns),
                "止损票": round_sl_count,
                "平均收益%": round(avg * 100, 2),
                "最高收益%": round(max(round_final_returns) * 100, 2),
                "最低收益%": round(min(round_final_returns) * 100, 2),
                "胜率%": round(winners / len(round_final_returns) * 100, 1),
                "本轮耗时s": round(time.perf_counter() - round_t0, 2),
            })
            logger.info("  %s | 平均 %+.2f%% | 胜率 %.0f%% | 止损 %d | 用时 %.2fs",
                        regime, avg * 100,
                        winners / len(round_final_returns) * 100,
                        round_sl_count,
                        time.perf_counter() - round_t0)
        else:
            round_summaries.append({
                "轮次": rd, "选股日": sel_date,
                "买入日": buy_date, "卖出日": sell_date,
                "大盘状态": regime,
                "选股数": len(codes), "有效票": 0, "止损票": 0,
                "平均收益%": 0.0, "最高收益%": 0.0, "最低收益%": 0.0,
                "胜率%": 0.0,
                "本轮耗时s": round(time.perf_counter() - round_t0, 2),
            })

    logger.info("=" * 70)
    logger.info("回测完成 | 空头轻仓轮数: %d | 止损总次数: %d",
                total_skip_bear, total_stop_loss_events)

    return round_details, round_summaries


# ── 报告与导出 ────────────────────────────────────────────

def print_and_export(details, summaries, hold_days, stop_loss_pct, output_dir=None, cli_args=None, total_elapsed_sec: float = 0.0):
    if not summaries:
        print("\n无有效结果")
        return

    out_dir = Path(output_dir) if output_dir else project_root / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    df_sum = pd.DataFrame(summaries)
    df_det = pd.DataFrame(details)

    # ── NAV 曲线（空仓轮 = 资金不动，NAV 不变）──
    nav = 1.0
    nav_curve = [1.0]
    for _, row in df_sum.iterrows():
        if row["有效票"] > 0:
            nav *= (1 + row["平均收益%"] / 100)
        nav_curve.append(nav)

    total_rounds = len(summaries)
    active_rounds = sum(1 for _, r in df_sum.iterrows() if r["有效票"] > 0)
    bear_rounds = int(df_sum[df_sum["大盘状态"] == "bear"].shape[0]) if "大盘状态" in df_sum.columns else 0
    skipped_rounds = total_rounds - active_rounds
    active_rets = df_sum.loc[df_sum["有效票"] > 0, "平均收益%"].values
    win_rounds = sum(1 for r in active_rets if r > 0)
    total_return = (nav - 1) * 100
    trading_days_total = active_rounds * hold_days
    annualized = ((nav ** (245 / max(trading_days_total, 1))) - 1) * 100 if nav > 0 else 0
    total_sl = int(df_sum["止损票"].sum())

    peak = nav_curve[0]
    max_dd = 0
    for v in nav_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    verdict = "PASS" if total_return > 0 else "FAIL"

    print("=" * 80)
    print("  滚动选股回测 v4-fast 结果  ".center(80, "="))
    print("=" * 80)
    print("\n【每轮汇总】")
    print(df_sum.to_string(index=False))
    print(f"\n{'─' * 80}")
    print("【整体策略表现】")
    print(f"  回测总轮数:     {total_rounds}")
    print(f"  实际操作轮数:   {active_rounds} (空头轻仓: {bear_rounds})")
    print(f"  盈利轮数:       {win_rounds} / {active_rounds} ({win_rounds / max(active_rounds, 1) * 100:.1f}%)")
    if len(active_rets) > 0:
        print(f"  每轮平均收益:   {np.mean(active_rets):+.2f}%")
        print(f"  每轮中位数:     {np.median(active_rets):+.2f}%")
    print(f"  累计净值:       {nav:.4f}")
    print(f"  累计总收益:     {total_return:+.2f}%")
    print(f"  年化收益率:     {annualized:+.2f}%")
    print(f"  最大回撤:       {max_dd * 100:.2f}%")
    print(f"  止损触发次数:   {total_sl}")
    print(f"  止损线:         {stop_loss_pct * 100:.1f}%")
    if active_rounds > 0:
        print(f"  平均单轮耗时:   {df_sum['本轮耗时s'].mean():.2f}s")
    print(f"  策略结论:       {verdict}")

    # ── 大盘状态分布 ──
    regime_counts = df_sum["大盘状态"].value_counts()
    print(f"\n{'─' * 80}")
    print("【大盘状态分布】")
    for regime, cnt in regime_counts.items():
        subset = df_sum[df_sum["大盘状态"] == regime]
        active = subset[subset["有效票"] > 0]
        avg_ret = active["平均收益%"].mean() if len(active) > 0 else 0
        print(f"  {regime:>8s}: {cnt} 轮 | 平均收益 {avg_ret:+.2f}%")

    # ── 导出 ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    det_path = out_dir / f"rolling_v4_fast_detail_{ts}.csv"
    sum_path = out_dir / f"rolling_v4_fast_summary_{ts}.csv"
    rpt_path = out_dir / f"rolling_v4_fast_report_{ts}.json"
    meta_path = out_dir / f"rolling_v4_fast_meta_{ts}.json"

    df_det.to_csv(det_path, index=False, encoding="utf-8-sig")
    df_sum.to_csv(sum_path, index=False, encoding="utf-8-sig")

    report = {
        "version": "v4_fast",
        "timestamp": ts,
        "params": {
            "stop_loss_pct": stop_loss_pct,
            "hold_days": hold_days,
        },
        "total_rounds": total_rounds,
        "active_rounds": active_rounds,
        "bear_rounds": bear_rounds,
        "skipped_rounds": skipped_rounds,
        "win_rounds": win_rounds,
        "round_win_rate": f"{win_rounds / max(active_rounds, 1) * 100:.1f}%",
        "avg_round_return": f"{np.mean(active_rets):+.2f}%" if len(active_rets) > 0 else "N/A",
        "median_round_return": f"{np.median(active_rets):+.2f}%" if len(active_rets) > 0 else "N/A",
        "cumulative_nav": round(nav, 4),
        "total_return": f"{total_return:+.2f}%",
        "annualized_return": f"{annualized:+.2f}%",
        "max_drawdown": f"{max_dd * 100:.2f}%",
        "total_stop_loss_events": total_sl,
        "avg_round_elapsed_sec": round(float(df_sum["本轮耗时s"].mean()), 2),
        "regime_distribution": {
            regime: int(cnt) for regime, cnt in regime_counts.items()
        },
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


# ── 入口 ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="滚动选股回测 v4-fast（大盘过滤 + 止损）")
    parser.add_argument("--start", required=True, help="回测起始日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="回测结束日 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=5, help="每轮选前 N 只 (默认 5)")
    parser.add_argument("--interval", type=int, default=10, help="选股间隔交易日 (默认 10)")
    parser.add_argument("--hold", type=int, default=10, help="持有交易日 (默认 10)")
    parser.add_argument("--check", type=int, default=2, help="快照间隔交易日 (默认 2)")
    parser.add_argument("--max-per-industry", type=int, default=2)
    parser.add_argument("--stop-loss", type=float, default=-8,
                        help="止损线百分比 (默认 -8, 即 -8%%)")
    parser.add_argument("--benchmark", type=str, default="000300",
                        help="大盘基准代码，来自 index_daily_quotes (默认 000300=沪深300)")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    run_started_at = datetime.now()
    run_t0 = time.perf_counter()

    stop_loss_pct = args.stop_loss / 100.0 if args.stop_loss < -1 else args.stop_loss

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    details, summaries = run_rolling_backtest(
        db, args.start, args.end,
        top_n=args.top, select_interval=args.interval,
        hold_days=args.hold, check_interval=args.check,
        max_per_industry=args.max_per_industry,
        stop_loss_pct=stop_loss_pct,
        regime_benchmark=args.benchmark,
    )
    cli_args = vars(args).copy()
    cli_args["stop_loss_pct_normalized"] = stop_loss_pct
    cli_args["_run_started_at"] = run_started_at.isoformat(timespec="seconds")
    cli_args["_run_finished_at"] = datetime.now().isoformat(timespec="seconds")
    print_and_export(
        details,
        summaries,
        args.hold,
        stop_loss_pct,
        args.output_dir,
        cli_args=cli_args,
        total_elapsed_sec=time.perf_counter() - run_t0,
    )


if __name__ == "__main__":
    main()
