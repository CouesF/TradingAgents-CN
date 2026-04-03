#!/usr/bin/env python3
"""
AKShare 财务数据测试脚本

用于测试不同股票代码的财务数据获取功能
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import akshare as ak
import pandas as pd
from datetime import datetime


def print_separator(title=""):
    """打印分隔线"""
    if title:
        print(f"\n{'=' * 80}")
        print(f"  {title}")
        print(f"{'=' * 80}")
    else:
        print(f"{'=' * 80}")


def test_stock_type(code: str):
    """判断股票类型"""
    code = str(code).zfill(6)
    
    print(f"\n📊 股票代码: {code}")
    
    # 判断市场类型
    if code.startswith('6'):
        market = "上海A股"
    elif code.startswith('0') or code.startswith('3'):
        market = "深圳A股"
    elif code.startswith('92') or code.startswith('8'):
        market = "北交所/新三板"
    elif len(code) == 5 or code.startswith('9'):
        market = "港股"
    else:
        market = "未知市场"
    
    print(f"   市场类型: {market}")
    return market


def test_a_share_financial_indicator(code: str):
    """测试 A 股财务指标接口"""
    print_separator("测试 A 股财务指标接口")
    
    code = str(code).zfill(6)
    
    try:
        print(f"🔍 调用: ak.stock_financial_analysis_indicator(symbol='{code}')")
        df = ak.stock_financial_analysis_indicator(symbol=code)
        
        if df is None:
            print("❌ 返回 None")
            return None
        
        if df.empty:
            print("❌ 返回空 DataFrame")
            return None
        
        print(f"✅ 成功获取 {len(df)} 期数据")
        print(f"\n📋 列名: {list(df.columns)}")
        print(f"\n📅 报告期范围: {df['报告期'].min()} ~ {df['报告期'].max()}")
        
        # 显示最新一期数据
        print(f"\n📊 最新一期数据 ({df.iloc[-1]['报告期']}):")
        latest = df.iloc[-1]
        
        key_metrics = [
            '报告期', '基本每股收益', '每股净资产', '净资产收益率',
            '营业收入', '净利润', '总资产', '股东权益合计',
            '资产负债率', '销售毛利率', '销售净利率'
        ]
        
        for metric in key_metrics:
            if metric in latest:
                value = latest[metric]
                print(f"   {metric}: {value}")
        
        return df
        
    except Exception as e:
        print(f"❌ 错误: {type(e).__name__}: {e}")
        return None


def test_stock_individual_info(code: str):
    """测试股票个股信息接口（获取股本等）"""
    print_separator("测试股票个股信息接口")
    
    code = str(code).zfill(6)
    
    try:
        print(f"🔍 调用: ak.stock_individual_info_em(symbol='{code}')")
        df = ak.stock_individual_info_em(symbol=code)
        
        if df is None:
            print("❌ 返回 None")
            return None
        
        if df.empty:
            print("❌ 返回空 DataFrame")
            return None
        
        print(f"✅ 成功获取 {len(df)} 项信息")
        print(f"\n📋 可用字段:")
        
        for _, row in df.iterrows():
            item = row.get('item', 'N/A')
            value = row.get('value', 'N/A')
            print(f"   {item}: {value}")
        
        return df
        
    except Exception as e:
        print(f"❌ 错误: {type(e).__name__}: {e}")
        return None


def test_hk_stock_fundamental(code: str):
    """测试港股基本面接口"""
    print_separator("测试港股基本面接口")
    
    # 港股代码可能需要不同格式
    code_variants = [
        code,
        str(code).zfill(5),  # 5位港股代码
        f"{code}.HK",
        f"HK.{code}",
    ]
    
    for variant in code_variants:
        print(f"\n🔍 尝试代码格式: {variant}")
        
        # 尝试不同的港股接口
        interfaces = [
            ("stock_hk_spot_em", lambda: ak.stock_hk_spot_em()),
            ("stock_hk_main_board_spot_em", lambda: ak.stock_hk_main_board_spot_em()),
        ]
        
        for interface_name, interface_func in interfaces:
            try:
                print(f"   测试接口: {interface_name}")
                df = interface_func()
                
                if df is not None and not df.empty:
                    # 查找匹配的股票
                    matches = df[df['代码'].astype(str).str.contains(str(code).lstrip('0'))]
                    
                    if not matches.empty:
                        print(f"   ✅ 找到匹配股票:")
                        for _, row in matches.iterrows():
                            print(f"      代码: {row.get('代码', 'N/A')}")
                            print(f"      名称: {row.get('名称', 'N/A')}")
                            print(f"      最新价: {row.get('最新价', 'N/A')}")
                            print(f"      涨跌幅: {row.get('涨跌幅', 'N/A')}")
                        return matches
                    else:
                        print(f"   ⚠️  未找到匹配股票")
                else:
                    print(f"   ❌ 返回空数据")
                    
            except Exception as e:
                print(f"   ❌ 错误: {type(e).__name__}: {e}")
    
    return None


def test_alternative_financial_interfaces(code: str):
    """测试其他可能的财务数据接口"""
    print_separator("测试其他财务数据接口")
    
    code = str(code).zfill(6)
    
    # 尝试其他可能的接口
    interfaces = [
        ("stock_financial_abstract", lambda: ak.stock_financial_abstract(stock=code)),
        ("stock_financial_report_sina", lambda: ak.stock_financial_report_sina(stock=code, symbol="资产负债表")),
    ]
    
    for interface_name, interface_func in interfaces:
        try:
            print(f"\n🔍 测试接口: {interface_name}")
            df = interface_func()
            
            if df is not None and not df.empty:
                print(f"   ✅ 成功获取 {len(df)} 条数据")
                print(f"   列名: {list(df.columns)[:10]}...")  # 只显示前10列
                print(f"\n   前3行数据:")
                print(df.head(3).to_string())
                return df
            else:
                print(f"   ❌ 返回空数据")
                
        except Exception as e:
            print(f"   ❌ 错误: {type(e).__name__}: {e}")
    
    return None


def main(code: str):
    """主测试函数"""
    print_separator(f"AKShare 财务数据测试 - {code}")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 1. 判断股票类型
    market = test_stock_type(code)
    
    # 2. 根据市场类型选择测试
    if market in ["上海A股", "深圳A股"]:
        # A 股测试
        test_a_share_financial_indicator(code)
        test_stock_individual_info(code)
        test_alternative_financial_interfaces(code)
        
    elif market == "港股":
        # 港股测试
        print("\n⚠️  检测到港股代码，A股财务接口不适用")
        test_hk_stock_fundamental(code)
        
        # 尝试 A 股接口（验证确实不可用）
        print("\n🔍 验证 A 股接口是否适用:")
        test_a_share_financial_indicator(code)
        
    else:
        # 未知市场，全部尝试
        print("\n⚠️  未知市场类型，尝试所有接口")
        test_a_share_financial_indicator(code)
        test_stock_individual_info(code)
        test_hk_stock_fundamental(code)
        test_alternative_financial_interfaces(code)
    
    print_separator("测试完成")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/test_akshare_financial.py <股票代码>")
        print("\n示例:")
        print("  python scripts/test_akshare_financial.py 600036  # A股")
        print("  python scripts/test_akshare_financial.py 000001  # A股")
        print("  python scripts/test_akshare_financial.py 920068  # 港股")
        sys.exit(1)
    
    code = sys.argv[1]
    main(code)
