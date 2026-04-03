#!/usr/bin/env python3
"""
计算策略相对于各指数的 Alpha/Beta 收益

指数列表:
- 上证指数 (000001) - Shanghai Composite
- 深证成指 (399001) - Shenzhen Component
- 创业板指 (399006) - ChiNext Index
- 北证50 (899050) - Beijing Stock Exchange 50
- 沪深300 (000300) - CSI 300
- 中证500 (000905) - CSI 500
- 上证50 (000016) - SSE 50

数据来源: MongoDB index_daily_quotes 集合
"""

import sys
from pathlib import Path
import pandas as pd
import numpy as np

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from tradingagents.config.database_manager import get_database_manager

# 指数代码映射 (对应 index_daily_quotes 集合中的 symbol)
INDEX_CODES = {
    "上证指数": "000001",
    "深证成指": "399001",
    "创业板指": "399006",
    "沪深300": "000300",
    "中证500": "000905",
    "上证50": "000016",
    "北证50": "899050",
}

# MongoDB集合名
INDEX_COLLECTION = "index_daily_quotes"

def get_index_data(db, symbol: str, start: str, end: str) -> pd.DataFrame:
    """获取指数日线数据 (从 index_daily_quotes 集合)"""
    collection = db[INDEX_COLLECTION]

    cursor = collection.find(
        {"symbol": symbol, "trade_date": {"$gte": start, "$lte": end}},
        {"_id": 0, "trade_date": 1, "close": 1, "open": 1, "high": 1, "low": 1}
    ).sort("trade_date", 1)

    rows = list(cursor)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    return df


def calculate_index_return(df: pd.DataFrame) -> float:
    """计算区间收益率"""
    if len(df) < 2:
        return None
    start_close = df['close'].iloc[0]
    end_close = df['close'].iloc[-1]
    return (end_close - start_close) / start_close * 100


def calculate_beta(strategy_returns: list, index_returns: list) -> float:
    """计算Beta系数"""
    if len(strategy_returns) != len(index_returns) or len(strategy_returns) < 2:
        return None

    # 协方差 / 方差
    cov = np.cov(strategy_returns, index_returns)[0, 1]
    var = np.var(index_returns, ddof=1)
    return cov / var if var > 0 else 0


def calculate_alpha(strategy_return: float, index_return: float, beta: float) -> float:
    """计算Alpha (CAPM模型: Alpha = R_p - Beta * R_m)"""
    if beta is None:
        return None
    return strategy_return - beta * index_return


def main():
    # 从回测报告读取策略净值曲线
    import json

    report_path = project_root / "output/rolling_2year/rolling_backtest_report_20260402_020321.json"
    summary_path = project_root / "output/rolling_2year/rolling_backtest_summary_20260402_020321.csv"

    with open(report_path) as f:
        report = json.load(f)

    summary_df = pd.read_csv(summary_path)

    strategy_total_return = float(report['total_return'].replace('%', '').replace('+', ''))
    strategy_annual_return = float(report['annualized_return'].replace('%', '').replace('+', ''))

    # 每轮收益列表
    round_returns = summary_df['平均收益%'].tolist()

    db_manager = get_database_manager()
    db = db_manager.get_mongodb_db()

    start_date = "2024-04-01"
    end_date = "2026-04-01"

    print("=" * 80)
    print("策略 Alpha/Beta 分析")
    print("=" * 80)
    print(f"回测区间: {start_date} ~ {end_date}")
    print(f"策略累计收益: {strategy_total_return:+.2f}%")
    print(f"策略年化收益: {strategy_annual_return:+.2f}%")
    print()

    results = []

    for name, code in INDEX_CODES.items():
        print(f"获取 {name} ({code}) 数据...")
        df = get_index_data(db, code, start_date, end_date)

        if df.empty:
            print(f"  [!] 无数据")
            continue

        index_return = calculate_index_return(df)
        if index_return is None:
            print(f"  [!] 数据不足")
            continue

        # 计算指数每期收益（与策略轮次对齐）
        # 策略每10天一轮，我们计算指数同期收益
        index_returns_aligned = []
        for _, row in summary_df.iterrows():
            buy_date = row['买入日']
            sell_date = row['卖出日']
            period_df = df[(df['trade_date'] >= buy_date) & (df['trade_date'] <= sell_date)]
            if len(period_df) >= 2:
                period_return = (period_df['close'].iloc[-1] - period_df['close'].iloc[0]) / period_df['close'].iloc[0] * 100
                index_returns_aligned.append(period_return)
            else:
                index_returns_aligned.append(0)

        # 对齐的轮次收益
        strategy_returns_aligned = round_returns[:len(index_returns_aligned)]

        beta = calculate_beta(strategy_returns_aligned, index_returns_aligned)
        alpha = calculate_alpha(strategy_total_return, index_return, beta) if beta else None

        # 年化Alpha
        trading_days = 483  # 从回测报告
        annualized_index = ((1 + index_return/100) ** (245/trading_days) - 1) * 100 if trading_days > 0 else 0

        print(f"  指数累计收益: {index_return:+.2f}%")
        print(f"  指数年化收益: {annualized_index:+.2f}%")
        if beta:
            print(f"  Beta: {beta:.3f}")
        if alpha:
            print(f"  Alpha (累计): {alpha:+.2f}%")
            # 年化Alpha = 策略年化 - Beta * 指数年化
            annualized_alpha = strategy_annual_return - (beta * annualized_index)
            print(f"  Alpha (年化): {annualized_alpha:+.2f}%")

        # 信息比率 (IR) = Alpha / 跟踪误差
        if len(strategy_returns_aligned) > 1:
            excess_returns = [s - i for s, i in zip(strategy_returns_aligned, index_returns_aligned)]
            tracking_error = np.std(excess_returns, ddof=1)
            info_ratio = np.mean(excess_returns) / tracking_error if tracking_error > 0 else 0
            print(f"  信息比率 (IR): {info_ratio:.2f}")

        print()

        results.append({
            '指数': name,
            '指数收益%': round(index_return, 2),
            '指数年化%': round(annualized_index, 2),
            'Beta': round(beta, 3) if beta else None,
            'Alpha累计%': round(alpha, 2) if alpha else None,
            'Alpha年化%': round(annualized_alpha, 2) if alpha and beta else None,
        })

    # 汇总表格
    print("=" * 80)
    print("Alpha/Beta 汇总")
    print("=" * 80)
    results_df = pd.DataFrame(results)
    print(results_df.to_string(index=False))

    # 保存
    output_path = project_root / "output/rolling_2year/alpha_beta_analysis.csv"
    results_df.to_csv(output_path, index=False, encoding='utf-8-sig')
    print(f"\n已保存: {output_path}")


if __name__ == "__main__":
    main()