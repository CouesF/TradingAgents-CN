#!/usr/bin/env python3
"""
滚动选股回测 v5 — 双引擎 + 行业轮动 + 失败记忆

核心变化 (相对 v4.1):
  1. 选股器替换为 StockScreenerV5 (双引擎: 主线延续 + 高低切轮动)
  2. 市场状态改用"市场广度"(全A MA20 上方占比) 替代简单 MA20/MA60 体制判断
  3. 跨轮失败记忆: 近 4 轮被选中后亏损 >8% 的个股自动惩罚
  4. ATR 自适应止损 (延续 v4.1 修正版, 使用选股日而非买入日)
  5. 报告新增引擎分布、行业阶段分布、标签分布

使用方法:
    cd ~/Project/TradingAgents-CN && source venv/bin/activate

    # 默认: ATR 自适应止损
    python fyy_test_folder/backtest/rolling_screener_v5_backtest.py \
        --start 2024-04-01 --end 2026-03-31

    # 固定止损对照
    python fyy_test_folder/backtest/rolling_screener_v5_backtest.py \
        --start 2024-04-01 --end 2026-03-31 \
        --stop-mode fixed --stop-loss -8
"""

import sys
import argparse
import logging
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict, Counter

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
from stock_screener_v5 import StockScreenerV5, StockScoreV5

logger = logging.getLogger("RollingV5Backtest")

# ── 默认参数 ─────────────────────────────────────────────

DEFAULT_STOP_LOSS_PCT = -0.08
DEFAULT_STOP_MODE = "atr"
DEFAULT_ATR_WINDOW = 20
DEFAULT_ATR_MULTIPLIER = 2.5
DEFAULT_MIN_STOP_LOSS_PCT = -0.12
DEFAULT_MAX_STOP_LOSS_PCT = -0.05

BREADTH_BULL = 0.50
BREADTH_BEAR = 0.30


# ── 工具函数 ─────────────────────────────────────────────

def get_trading_days(db, start: str, end: str,
                     calendar_symbol: str = "000300") -> List[str]:
    cursor = db.index_daily_quotes.find(
        {"symbol": calendar_symbol, "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    days = [doc["trade_date"] for doc in cursor]
    if days:
        return days
    cursor = db.stock_daily_quotes.find(
        {"symbol": "000001", "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    return [doc["trade_date"] for doc in cursor]


def get_prices_range(db, symbol: str, start_date: str,
                     end_date: str) -> Dict[str, Dict]:
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


def get_recent_price_rows(db, symbol: str, end_date: str,
                          limit: int = 40) -> List[Dict]:
    cursor = db.stock_daily_quotes.find(
        {"symbol": symbol, "trade_date": {"$lte": end_date}},
        {"_id": 0, "trade_date": 1, "open": 1, "high": 1, "low": 1, "close": 1},
    ).sort("trade_date", -1).limit(limit)
    return list(cursor)


def calculate_adaptive_stop_loss_pct(
    rows_desc: List[Dict],
    fallback_stop_loss_pct: float,
    atr_window: int = DEFAULT_ATR_WINDOW,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    min_stop_loss_pct: float = DEFAULT_MIN_STOP_LOSS_PCT,
    max_stop_loss_pct: float = DEFAULT_MAX_STOP_LOSS_PCT,
) -> Tuple[float, Optional[float]]:
    if len(rows_desc) < atr_window + 1:
        return fallback_stop_loss_pct, None

    rows = list(reversed(rows_desc))
    trs: List[float] = []
    prev_close = None
    for row in rows:
        try:
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close

    if len(trs) < atr_window:
        return fallback_stop_loss_pct, None

    try:
        last_close = float(rows[-1]["close"])
    except (KeyError, TypeError, ValueError):
        return fallback_stop_loss_pct, None
    if last_close <= 0:
        return fallback_stop_loss_pct, None

    atr = float(np.mean(trs[-atr_window:]))
    atr_pct = atr / last_close

    tight_abs = min(abs(max_stop_loss_pct), abs(min_stop_loss_pct))
    wide_abs = max(abs(max_stop_loss_pct), abs(min_stop_loss_pct))
    adaptive_abs = float(np.clip(atr_pct * atr_multiplier, tight_abs, wide_abs))
    return -adaptive_abs, atr_pct


# ── 市场广度 → 仓位信号 ─────────────────────────────────

def breadth_regime(breadth: float) -> Tuple[str, float]:
    """根据全 A 市场广度返回 (regime, position_ratio)。"""
    if breadth >= BREADTH_BULL:
        return "broad_bull", 1.0
    elif breadth >= BREADTH_BEAR:
        return "selective", 0.6
    else:
        return "risk_off", 0.4


# ── 主逻辑 ───────────────────────────────────────────────

def run_rolling_backtest(
    db,
    start_date: str,
    end_date: str,
    top_n: int = 5,
    select_interval: int = 10,
    hold_days: int = 10,
    check_interval: int = 2,
    max_per_industry: int = 3,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    stop_mode: str = DEFAULT_STOP_MODE,
    atr_window: int = DEFAULT_ATR_WINDOW,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
    min_stop_loss_pct: float = DEFAULT_MIN_STOP_LOSS_PCT,
    max_stop_loss_pct: float = DEFAULT_MAX_STOP_LOSS_PCT,
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
    logger.info("滚动选股回测 v5 (双引擎) 启动")
    logger.info("=" * 70)
    logger.info("回测区间: %s ~ %s (%d 交易日)", start_date, end_date, len(sel_days))
    logger.info("参数: Top%d | 每%d日 | 持有%d日 | 快照%d日 | 行业限%d",
                top_n, select_interval, hold_days, check_interval, max_per_industry)
    if stop_mode == "atr":
        logger.info(
            "止损: ATR 自适应 | 基线 %.1f%% | 窗口 %d | 倍数 %.2f | 范围 %.1f%%~%.1f%%",
            stop_loss_pct * 100, atr_window, atr_multiplier,
            min_stop_loss_pct * 100, max_stop_loss_pct * 100,
        )
    else:
        logger.info("止损: 固定 %.1f%%", stop_loss_pct * 100)
    logger.info("共 %d 轮选股", len(selection_dates))

    round_details: List[Dict] = []
    round_summaries: List[Dict] = []

    basics_cache = list(db.stock_basic_info.find(
        {},
        {"_id": 0, "code": 1, "name": 1, "industry": 1, "market": 1,
         "total_mv": 1, "roe": 1, "pe": 1, "pb": 1, "pe_ttm": 1},
    ))
    logger.info("basic_info 缓存: %d 条", len(basics_cache))

    failure_history: Dict[str, List[float]] = defaultdict(list)
    total_stop_loss_events = 0
    total_regime_counts: Counter = Counter()
    total_engine_counts: Counter = Counter()
    total_tag_counts: Counter = Counter()

    for rd, sel_date in enumerate(selection_dates, 1):
        round_t0 = time.perf_counter()
        logger.info("%s", "=" * 65)
        logger.info("第 %d/%d 轮 | 选股截止日: %s", rd, len(selection_dates), sel_date)

        # ── 选股 ──
        screener = StockScreenerV5(
            db=db,
            as_of_date=sel_date,
            basics_cache=basics_cache,
            regime_benchmark=regime_benchmark,
            failure_history=dict(failure_history),
        )

        fetch_n = top_n * 3
        picks = screener.screen(
            top_n=fetch_n,
            min_data_days=60,
            min_avg_amount=3000,
            max_per_industry=max_per_industry,
        )

        # ── 市场广度 → 仓位 ──
        mkt_ctx = screener.market_context or {}
        breadth = mkt_ctx.get("breadth", 0.5)
        regime, pos_ratio = breadth_regime(breadth)
        effective_top_n = max(2, int(top_n * pos_ratio))
        total_regime_counts[regime] += 1

        logger.info("  市场广度: %.1f%% → %s → 仓位 %d 只",
                     breadth * 100, regime, effective_top_n)

        if not picks:
            logger.warning("  无结果，跳过")
            round_summaries.append({
                "轮次": rd, "选股日": sel_date,
                "买入日": "", "卖出日": "",
                "广度%": round(breadth * 100, 1),
                "市场状态": regime,
                "选股数": 0, "有效票": 0, "止损票": 0,
                "引擎A": 0, "引擎B": 0,
                "平均收益%": 0.0, "最高收益%": 0.0, "最低收益%": 0.0,
                "胜率%": 0.0, "本轮耗时s": round(time.perf_counter() - round_t0, 2),
            })
            continue

        picks = picks[:effective_top_n]

        codes = [(p.symbol, p.name, p.composite_score, p.tag, p.engine,
                  p.industry, p.industry_phase) for p in picks]

        eng_a = sum(1 for p in picks if p.engine == "A")
        eng_b = sum(1 for p in picks if p.engine == "B")
        total_engine_counts["A"] += eng_a
        total_engine_counts["B"] += eng_b
        for p in picks:
            total_tag_counts[p.tag] += 1

        logger.info("  选出 %d 只 (A:%d B:%d): %s",
                     len(picks), eng_a, eng_b,
                     " | ".join(f"{c}({n})[{e}]" for c, n, _, _, e, *_ in codes))

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

        # ── 逐股: 止损 + 收益 ──
        round_final_returns: List[float] = []
        round_sl_count = 0

        for code, name, comp_score, tag, engine, industry, ind_phase in codes:
            prices = get_prices_range(db, code, buy_date, sell_date)

            if buy_date not in prices:
                logger.warning("    %s(%s) 买入日无数据，跳过", code, name)
                continue

            buy_price = prices[buy_date]["open"]

            effective_stop_loss_pct = stop_loss_pct
            atr_pct = None
            if stop_mode == "atr":
                stop_rows = get_recent_price_rows(
                    db, code, sel_date,
                    limit=max(atr_window + 5, 40),
                )
                effective_stop_loss_pct, atr_pct = calculate_adaptive_stop_loss_pct(
                    stop_rows,
                    fallback_stop_loss_pct=stop_loss_pct,
                    atr_window=atr_window,
                    atr_multiplier=atr_multiplier,
                    min_stop_loss_pct=min_stop_loss_pct,
                    max_stop_loss_pct=max_stop_loss_pct,
                )

            exit_idx = end_idx
            exit_reason = "到期"
            for di in range(buy_idx + 1, end_idx + 1):
                day = all_days[di] if di < len(all_days) else None
                if day is None or day not in prices:
                    continue
                day_ret = (prices[day]["close"] - buy_price) / buy_price
                if day_ret <= effective_stop_loss_pct:
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
                    "综合分": comp_score, "标签": tag, "引擎": engine,
                    "行业": industry, "行业阶段": ind_phase,
                    "广度%": round(breadth * 100, 1),
                    "市场状态": regime,
                    "止损模式": stop_mode,
                    "止损线%": round(effective_stop_loss_pct * 100, 2),
                    "ATR%": round(atr_pct * 100, 2) if atr_pct is not None else "",
                    "买入价(开盘)": round(buy_price, 2),
                    "快照日": snap_date, "持有天数": held,
                    "快照收盘": round(snap_close, 2),
                    "收益率%": round(ret * 100, 2),
                    "退出": exit_reason if is_exit_point else "",
                })

            exit_on_snapshot = any(
                (si < len(all_days) and all_days[si] == exit_date)
                for si in snapshot_indices
            )
            if exit_reason == "止损" and not exit_on_snapshot:
                held = exit_idx - buy_idx
                round_details.append({
                    "轮次": rd, "选股日": sel_date, "买入日": buy_date,
                    "代码": code, "名称": name,
                    "综合分": comp_score, "标签": tag, "引擎": engine,
                    "行业": industry, "行业阶段": ind_phase,
                    "广度%": round(breadth * 100, 1),
                    "市场状态": regime,
                    "止损模式": stop_mode,
                    "止损线%": round(effective_stop_loss_pct * 100, 2),
                    "ATR%": round(atr_pct * 100, 2) if atr_pct is not None else "",
                    "买入价(开盘)": round(buy_price, 2),
                    "快照日": exit_date, "持有天数": held,
                    "快照收盘": round(exit_close, 2),
                    "收益率%": round(exit_ret * 100, 2),
                    "退出": "止损",
                })

            round_final_returns.append(exit_ret)

            # 更新失败记忆
            failure_history[code].append(exit_ret)
            if len(failure_history[code]) > 4:
                failure_history[code] = failure_history[code][-4:]

            if exit_reason == "止损":
                round_sl_count += 1
                total_stop_loss_events += 1
                logger.info(
                    "    %s(%s)[%s] 止损: 第%d天 %.2f%% | 线 %.2f%%%s",
                    code, name, engine,
                    exit_idx - buy_idx, exit_ret * 100,
                    effective_stop_loss_pct * 100,
                    f" | ATR {atr_pct * 100:.2f}%" if atr_pct is not None else "",
                )

        # ── 本轮汇总 ──
        if round_final_returns:
            avg = np.mean(round_final_returns)
            winners = sum(1 for r in round_final_returns if r > 0)
            round_summaries.append({
                "轮次": rd, "选股日": sel_date,
                "买入日": buy_date, "卖出日": sell_date,
                "广度%": round(breadth * 100, 1),
                "市场状态": regime,
                "选股数": len(codes),
                "有效票": len(round_final_returns),
                "止损票": round_sl_count,
                "引擎A": eng_a, "引擎B": eng_b,
                "平均收益%": round(avg * 100, 2),
                "最高收益%": round(max(round_final_returns) * 100, 2),
                "最低收益%": round(min(round_final_returns) * 100, 2),
                "胜率%": round(winners / len(round_final_returns) * 100, 1),
                "本轮耗时s": round(time.perf_counter() - round_t0, 2),
            })
            logger.info(
                "  %s(%.0f%%) | 平均 %+.2f%% | 胜率 %.0f%% | 止损 %d | 用时 %.2fs",
                regime, breadth * 100, avg * 100,
                winners / len(round_final_returns) * 100,
                round_sl_count, time.perf_counter() - round_t0,
            )
        else:
            round_summaries.append({
                "轮次": rd, "选股日": sel_date,
                "买入日": buy_date, "卖出日": sell_date,
                "广度%": round(breadth * 100, 1),
                "市场状态": regime,
                "选股数": len(codes), "有效票": 0, "止损票": 0,
                "引擎A": eng_a, "引擎B": eng_b,
                "平均收益%": 0.0, "最高收益%": 0.0, "最低收益%": 0.0,
                "胜率%": 0.0,
                "本轮耗时s": round(time.perf_counter() - round_t0, 2),
            })

    logger.info("=" * 70)
    logger.info("回测完成 | 止损总次数: %d | 引擎分布 A:%d B:%d",
                total_stop_loss_events,
                total_engine_counts.get("A", 0),
                total_engine_counts.get("B", 0))

    return round_details, round_summaries


# ── 报告与导出 ────────────────────────────────────────────

def print_and_export(
    details,
    summaries,
    hold_days,
    stop_loss_pct,
    stop_mode,
    atr_window,
    atr_multiplier,
    min_stop_loss_pct,
    max_stop_loss_pct,
    output_dir=None,
):
    if not summaries:
        print("\n无有效结果")
        return

    out_dir = Path(output_dir) if output_dir else project_root / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    df_sum = pd.DataFrame(summaries)
    df_det = pd.DataFrame(details)

    nav = 1.0
    nav_curve = [1.0]
    for _, row in df_sum.iterrows():
        if row["有效票"] > 0:
            nav *= (1 + row["平均收益%"] / 100)
        nav_curve.append(nav)

    total_rounds = len(summaries)
    active_rounds = sum(1 for _, r in df_sum.iterrows() if r["有效票"] > 0)
    active_rets = df_sum.loc[df_sum["有效票"] > 0, "平均收益%"].values
    win_rounds = sum(1 for r in active_rets if r > 0)
    total_return = (nav - 1) * 100
    trading_days_total = active_rounds * hold_days
    annualized = ((nav ** (245 / max(trading_days_total, 1))) - 1) * 100 if nav > 0 else 0
    total_sl = int(df_sum["止损票"].sum())

    peak = nav_curve[0]
    max_dd = 0.0
    for v in nav_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    verdict = "PASS" if total_return > 0 else "FAIL"

    regime_counts = df_sum["市场状态"].value_counts()
    total_eng_a = int(df_sum["引擎A"].sum())
    total_eng_b = int(df_sum["引擎B"].sum())

    tag_counts: Counter = Counter()
    if len(df_det) > 0 and "标签" in df_det.columns:
        exit_rows = df_det[df_det["退出"] != ""]
        if len(exit_rows) > 0:
            tag_counts = Counter(exit_rows["标签"])

    print("=" * 80)
    print("  滚动选股回测 v5 (双引擎) 结果  ".center(80, "="))
    print("=" * 80)
    print("\n【每轮汇总】")
    print(df_sum.to_string(index=False))
    print(f"\n{'─' * 80}")
    print("【整体策略表现】")
    print(f"  回测总轮数:     {total_rounds}")
    print(f"  实际操作轮数:   {active_rounds}")
    print(f"  盈利轮数:       {win_rounds} / {active_rounds} ({win_rounds / max(active_rounds, 1) * 100:.1f}%)")
    if len(active_rets) > 0:
        print(f"  每轮平均收益:   {np.mean(active_rets):+.2f}%")
        print(f"  每轮中位数:     {np.median(active_rets):+.2f}%")
    print(f"  累计净值:       {nav:.4f}")
    print(f"  累计总收益:     {total_return:+.2f}%")
    print(f"  年化收益率:     {annualized:+.2f}%")
    print(f"  最大回撤:       {max_dd * 100:.2f}%")
    print(f"  止损触发次数:   {total_sl}")
    if stop_mode == "atr":
        print(f"  止损模式:       ATR 自适应")
        print(f"  基线止损:       {stop_loss_pct * 100:.1f}%")
        print(f"  ATR 参数:       窗口 {atr_window} | 倍数 {atr_multiplier:.2f}")
        print(f"  止损范围:       {min_stop_loss_pct * 100:.1f}% ~ {max_stop_loss_pct * 100:.1f}%")
    else:
        print(f"  止损模式:       固定止损")
        print(f"  止损线:         {stop_loss_pct * 100:.1f}%")
    if active_rounds > 0:
        print(f"  平均单轮耗时:   {df_sum['本轮耗时s'].mean():.2f}s")
    print(f"  策略结论:       {verdict}")

    print(f"\n{'─' * 80}")
    print("【引擎分布】")
    print(f"  引擎 A (延续型): {total_eng_a} 只")
    print(f"  引擎 B (切换型): {total_eng_b} 只")

    print(f"\n{'─' * 80}")
    print("【标签分布】")
    for tag, cnt in tag_counts.most_common():
        print(f"  {tag}: {cnt}")

    print(f"\n{'─' * 80}")
    print("【市场状态分布】")
    for regime, cnt in regime_counts.items():
        subset = df_sum[df_sum["市场状态"] == regime]
        active = subset[subset["有效票"] > 0]
        avg_ret = active["平均收益%"].mean() if len(active) > 0 else 0
        print(f"  {regime:>14s}: {cnt} 轮 | 平均收益 {avg_ret:+.2f}%")

    # ── 按引擎拆分收益 ──
    if len(df_det) > 0 and "引擎" in df_det.columns:
        print(f"\n{'─' * 80}")
        print("【引擎维度收益】")
        exit_rows = df_det[df_det["退出"] != ""].copy()
        if len(exit_rows) > 0:
            for eng in ["A", "B"]:
                sub = exit_rows[exit_rows["引擎"] == eng]
                if len(sub) > 0:
                    avg_r = sub["收益率%"].mean()
                    med_r = sub["收益率%"].median()
                    wr = (sub["收益率%"] > 0).mean() * 100
                    print(f"  引擎 {eng}: {len(sub)} 票 | 均值 {avg_r:+.2f}% | "
                          f"中位 {med_r:+.2f}% | 胜率 {wr:.0f}%")

    # ── 导出 ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    det_path = out_dir / f"rolling_v5_detail_{ts}.csv"
    sum_path = out_dir / f"rolling_v5_summary_{ts}.csv"
    rpt_path = out_dir / f"rolling_v5_report_{ts}.json"

    df_det.to_csv(det_path, index=False, encoding="utf-8-sig")
    df_sum.to_csv(sum_path, index=False, encoding="utf-8-sig")

    report = {
        "version": "v5",
        "description": "双引擎选股回测: 引擎 A (主线延续) + 引擎 B (高低切轮动)。"
                       "市场广度替代 MA 体制判断; 行业层先筛后选; 跨轮失败记忆惩罚。",
        "timestamp": ts,
        "params": {
            "stop_loss_pct": stop_loss_pct,
            "hold_days": hold_days,
            "stop_mode": stop_mode,
            "atr_window": atr_window,
            "atr_multiplier": atr_multiplier,
            "min_stop_loss_pct": min_stop_loss_pct,
            "max_stop_loss_pct": max_stop_loss_pct,
            "breadth_bull": BREADTH_BULL,
            "breadth_bear": BREADTH_BEAR,
        },
        "total_rounds": total_rounds,
        "active_rounds": active_rounds,
        "win_rounds": win_rounds,
        "round_win_rate": f"{win_rounds / max(active_rounds, 1) * 100:.1f}%",
        "avg_round_return": f"{np.mean(active_rets):+.2f}%" if len(active_rets) > 0 else "N/A",
        "median_round_return": f"{np.median(active_rets):+.2f}%" if len(active_rets) > 0 else "N/A",
        "cumulative_nav": round(nav, 4),
        "total_return": f"{total_return:+.2f}%",
        "annualized_return": f"{annualized:+.2f}%",
        "max_drawdown": f"{max_dd * 100:.2f}%",
        "total_stop_loss_events": total_sl,
        "engine_distribution": {"A": total_eng_a, "B": total_eng_b},
        "tag_distribution": dict(tag_counts),
        "avg_round_elapsed_sec": round(float(df_sum["本轮耗时s"].mean()), 2) if active_rounds > 0 else 0,
        "regime_distribution": {regime: int(cnt) for regime, cnt in regime_counts.items()},
        "nav_curve": [round(v, 4) for v in nav_curve],
        "verdict": verdict,
    }
    with open(rpt_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n已导出: {det_path}")
    print(f"        {sum_path}")
    print(f"        {rpt_path}")


# ── 入口 ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="滚动选股回测 v5（双引擎 + 行业轮动 + 失败记忆）")
    parser.add_argument("--start", required=True, help="回测起始日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="回测结束日 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=5, help="每轮选前 N 只 (默认 5)")
    parser.add_argument("--interval", type=int, default=10,
                        help="选股间隔交易日 (默认 10)")
    parser.add_argument("--hold", type=int, default=10, help="持有交易日 (默认 10)")
    parser.add_argument("--check", type=int, default=2, help="快照间隔交易日 (默认 2)")
    parser.add_argument("--max-per-industry", type=int, default=3)
    parser.add_argument("--stop-loss", type=float, default=-8,
                        help="固定止损基线百分比 (默认 -8, 即 -8%%)")
    parser.add_argument("--stop-mode", choices=["atr", "fixed"],
                        default=DEFAULT_STOP_MODE,
                        help="止损模式: atr=ATR自适应, fixed=固定止损")
    parser.add_argument("--atr-window", type=int, default=DEFAULT_ATR_WINDOW)
    parser.add_argument("--atr-multiplier", type=float, default=DEFAULT_ATR_MULTIPLIER)
    parser.add_argument("--min-stop-loss", type=float, default=-12)
    parser.add_argument("--max-stop-loss", type=float, default=-5)
    parser.add_argument("--benchmark", type=str, default="000300",
                        help="市场基准指数代码 (默认 000300)")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    stop_loss_pct = args.stop_loss / 100.0 if args.stop_loss < -1 else args.stop_loss
    min_stop_loss_pct = args.min_stop_loss / 100.0 if args.min_stop_loss < -1 else args.min_stop_loss
    max_stop_loss_pct = args.max_stop_loss / 100.0 if args.max_stop_loss < -1 else args.max_stop_loss

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    details, summaries = run_rolling_backtest(
        db, args.start, args.end,
        top_n=args.top,
        select_interval=args.interval,
        hold_days=args.hold,
        check_interval=args.check,
        max_per_industry=args.max_per_industry,
        stop_loss_pct=stop_loss_pct,
        stop_mode=args.stop_mode,
        atr_window=args.atr_window,
        atr_multiplier=args.atr_multiplier,
        min_stop_loss_pct=min_stop_loss_pct,
        max_stop_loss_pct=max_stop_loss_pct,
        regime_benchmark=args.benchmark,
    )
    print_and_export(
        details, summaries,
        args.hold, stop_loss_pct, args.stop_mode,
        args.atr_window, args.atr_multiplier,
        min_stop_loss_pct, max_stop_loss_pct,
        args.output_dir,
    )


if __name__ == "__main__":
    main()
