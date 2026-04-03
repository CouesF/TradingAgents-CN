#!/usr/bin/env python3
"""
诊断基本面数据获取问题
检查 MongoDB、Tushare、AKShare 三个数据源
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from tradingagents.utils.logging_init import get_logger
logger = get_logger("default")

def check_mongodb_connection():
    """检查 MongoDB 连接"""
    print("\n" + "="*80)
    print("1️⃣ 检查 MongoDB 连接")
    print("="*80)
    
    try:
        from tradingagents.config.database_manager import get_database_manager
        db_manager = get_database_manager()
        
        if not db_manager.is_mongodb_available():
            print("❌ MongoDB 不可用")
            return False
        
        client = db_manager.get_mongodb_client()
        db = client['tradingagents']
        
        # 测试连接
        db.command('ping')
        print("✅ MongoDB 连接成功")
        
        # 检查集合
        collections = db.list_collection_names()
        print(f"📊 数据库集合数量: {len(collections)}")
        
        if 'stock_financial_data' in collections:
            count = db.stock_financial_data.count_documents({})
            print(f"✅ stock_financial_data 集合存在，包含 {count} 条记录")
        else:
            print("⚠️ stock_financial_data 集合不存在")
        
        if 'stock_basic_info' in collections:
            count = db.stock_basic_info.count_documents({})
            print(f"✅ stock_basic_info 集合存在，包含 {count} 条记录")
        else:
            print("⚠️ stock_basic_info 集合不存在")
        
        return True
        
    except Exception as e:
        print(f"❌ MongoDB 连接失败: {e}")
        return False


def check_stock_financial_data(stock_code):
    """检查指定股票的财务数据"""
    print("\n" + "="*80)
    print(f"2️⃣ 检查股票 {stock_code} 的 MongoDB 财务数据")
    print("="*80)
    
    try:
        from tradingagents.config.database_manager import get_database_manager
        db_manager = get_database_manager()
        
        if not db_manager.is_mongodb_available():
            print("❌ MongoDB 不可用，跳过检查")
            return False
        
        client = db_manager.get_mongodb_client()
        db = client['tradingagents']
        
        # 标准化股票代码
        code6 = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '').zfill(6)
        
        # 检查 stock_financial_data
        financial_doc = db.stock_financial_data.find_one({
            '$or': [
                {'symbol': code6},
                {'code': code6}
            ]
        })
        
        if financial_doc:
            print(f"✅ 找到 {code6} 的财务数据")
            print(f"   - 更新时间: {financial_doc.get('updated_at', 'N/A')}")
            print(f"   - 数据源: {financial_doc.get('source', 'N/A')}")
            
            # 检查数据结构
            if 'raw_data' in financial_doc:
                raw_data = financial_doc['raw_data']
                print(f"   - raw_data 字段: {list(raw_data.keys())}")
                
                # 检查各类财务报表
                if 'balance_sheet' in raw_data and raw_data['balance_sheet']:
                    print(f"     ✅ 资产负债表: {len(raw_data['balance_sheet'])} 期")
                else:
                    print(f"     ⚠️ 资产负债表: 无数据")
                
                if 'income_statement' in raw_data and raw_data['income_statement']:
                    print(f"     ✅ 利润表: {len(raw_data['income_statement'])} 期")
                else:
                    print(f"     ⚠️ 利润表: 无数据")
                
                if 'cashflow_statement' in raw_data and raw_data['cashflow_statement']:
                    print(f"     ✅ 现金流量表: {len(raw_data['cashflow_statement'])} 期")
                else:
                    print(f"     ⚠️ 现金流量表: 无数据")
                
                if 'financial_indicators' in raw_data and raw_data['financial_indicators']:
                    print(f"     ✅ 财务指标: {len(raw_data['financial_indicators'])} 期")
                else:
                    print(f"     ⚠️ 财务指标: 无数据")
            
            return True
        else:
            print(f"❌ 未找到 {code6} 的财务数据")
            return False
            
    except Exception as e:
        print(f"❌ 检查失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_stock_basic_info(stock_code):
    """检查股票基本信息"""
    print("\n" + "="*80)
    print(f"3️⃣ 检查股票 {stock_code} 的基本信息")
    print("="*80)
    
    try:
        from tradingagents.config.database_manager import get_database_manager
        db_manager = get_database_manager()
        
        if not db_manager.is_mongodb_available():
            print("❌ MongoDB 不可用，跳过检查")
            return False
        
        client = db_manager.get_mongodb_client()
        db = client['tradingagents']
        
        # 标准化股票代码
        code6 = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '').zfill(6)
        
        # 检查 stock_basic_info
        basic_doc = db.stock_basic_info.find_one({'code': code6})
        
        if basic_doc:
            print(f"✅ 找到 {code6} 的基本信息")
            print(f"   - 股票名称: {basic_doc.get('name', 'N/A')}")
            print(f"   - 所属行业: {basic_doc.get('industry', 'N/A')}")
            print(f"   - 市场板块: {basic_doc.get('market', 'N/A')}")
            print(f"   - 总市值: {basic_doc.get('total_mv', 'N/A')}")
            print(f"   - PE: {basic_doc.get('pe', 'N/A')}")
            print(f"   - PE_TTM: {basic_doc.get('pe_ttm', 'N/A')}")
            print(f"   - PB: {basic_doc.get('pb', 'N/A')}")
            print(f"   - ROE: {basic_doc.get('roe', 'N/A')}")
            return True
        else:
            print(f"❌ 未找到 {code6} 的基本信息")
            return False
            
    except Exception as e:
        print(f"❌ 检查失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_tushare_connection():
    """检查 Tushare 连接"""
    print("\n" + "="*80)
    print("4️⃣ 检查 Tushare 连接")
    print("="*80)
    
    try:
        from tradingagents.dataflows.providers.china.tushare import get_tushare_provider
        import asyncio
        
        provider = get_tushare_provider()
        
        if not provider.connected:
            print("❌ Tushare 未连接")
            print("   请检查:")
            print("   1. TUSHARE_TOKEN 环境变量是否设置")
            print("   2. Token 是否有效")
            print("   3. 网络连接是否正常")
            return False
        
        print("✅ Tushare 连接成功")
        print(f"   - API 地址: {provider.api_url if hasattr(provider, 'api_url') else 'N/A'}")
        
        return True
        
    except Exception as e:
        print(f"❌ Tushare 连接失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_tushare_financial_data(stock_code):
    """检查 Tushare 财务数据获取"""
    print("\n" + "="*80)
    print(f"5️⃣ 测试从 Tushare 获取 {stock_code} 的财务数据")
    print("="*80)
    
    try:
        from tradingagents.dataflows.providers.china.tushare import get_tushare_provider
        import asyncio
        
        provider = get_tushare_provider()
        
        if not provider.connected:
            print("❌ Tushare 未连接，跳过测试")
            return False
        
        # 标准化股票代码
        code6 = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '').zfill(6)
        
        print(f"📊 正在从 Tushare 获取 {code6} 的财务数据...")
        
        # 获取财务数据（异步）
        loop = asyncio.get_event_loop()
        financial_data = loop.run_until_complete(provider.get_financial_data(code6))
        
        if financial_data:
            print("✅ Tushare 财务数据获取成功")
            
            # 检查各类报表
            balance_sheet = financial_data.get('balance_sheet', [])
            income_statement = financial_data.get('income_statement', [])
            cash_flow = financial_data.get('cash_flow', [])
            
            print(f"   - 资产负债表: {len(balance_sheet)} 期")
            print(f"   - 利润表: {len(income_statement)} 期")
            print(f"   - 现金流量表: {len(cash_flow)} 期")
            
            if income_statement:
                latest = income_statement[0]
                print(f"   - 最新报告期: {latest.get('end_date', 'N/A')}")
                print(f"   - 营业收入: {latest.get('total_revenue', 'N/A')}")
                print(f"   - 净利润: {latest.get('n_income', 'N/A')}")
            
            return True
        else:
            print("❌ Tushare 未返回财务数据")
            return False
            
    except Exception as e:
        print(f"❌ Tushare 财务数据获取失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_akshare_connection():
    """检查 AKShare 连接"""
    print("\n" + "="*80)
    print("6️⃣ 检查 AKShare 连接")
    print("="*80)
    
    try:
        from tradingagents.dataflows.providers.china.akshare import get_akshare_provider
        
        provider = get_akshare_provider()
        
        if not provider.connected:
            print("❌ AKShare 未连接")
            return False
        
        print("✅ AKShare 连接成功")
        return True
        
    except Exception as e:
        print(f"❌ AKShare 连接失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_akshare_financial_data(stock_code):
    """检查 AKShare 财务数据获取"""
    print("\n" + "="*80)
    print(f"7️⃣ 测试从 AKShare 获取 {stock_code} 的财务数据")
    print("="*80)
    
    try:
        from tradingagents.dataflows.providers.china.akshare import get_akshare_provider
        import asyncio
        
        provider = get_akshare_provider()
        
        if not provider.connected:
            print("❌ AKShare 未连接，跳过测试")
            return False
        
        # 标准化股票代码
        code6 = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '').zfill(6)
        
        print(f"📊 正在从 AKShare 获取 {code6} 的财务数据...")
        
        # 获取财务数据（异步）
        loop = asyncio.get_event_loop()
        financial_data = loop.run_until_complete(provider.get_financial_data(code6))
        
        if financial_data and any(not v.empty if hasattr(v, 'empty') else bool(v) for v in financial_data.values()):
            print("✅ AKShare 财务数据获取成功")
            
            # 检查各类报表
            for key, value in financial_data.items():
                if hasattr(value, 'empty'):
                    if not value.empty:
                        print(f"   - {key}: {len(value)} 条记录")
                    else:
                        print(f"   - {key}: 空")
                elif value:
                    print(f"   - {key}: 有数据")
                else:
                    print(f"   - {key}: 无数据")
            
            return True
        else:
            print("❌ AKShare 未返回有效财务数据")
            return False
            
    except Exception as e:
        print(f"❌ AKShare 财务数据获取失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_fundamentals_report_generation(stock_code):
    """测试基本面报告生成"""
    print("\n" + "="*80)
    print(f"8️⃣ 测试 {stock_code} 基本面报告生成")
    print("="*80)
    
    try:
        from tradingagents.dataflows.optimized_china_data import OptimizedChinaDataProvider
        
        provider = OptimizedChinaDataProvider()
        
        # 标准化股票代码
        code6 = stock_code.replace('.SH', '').replace('.SZ', '').replace('.BJ', '').zfill(6)
        
        print(f"📊 正在生成 {code6} 的基本面报告...")
        
        # 获取基本面数据
        report = provider.get_fundamentals_data(code6, force_refresh=False)
        
        if report:
            print("✅ 基本面报告生成成功")
            print(f"   - 报告长度: {len(report)} 字符")
            
            # 检查报告内容
            if "由于无法获取完整的财务数据" in report:
                print("⚠️ 报告为简化版本（缺少财务数据）")
            elif "财务数据分析" in report:
                print("✅ 报告包含完整财务数据")
            
            # 打印报告前500字符
            print("\n📄 报告预览（前500字符）:")
            print("-" * 80)
            print(report[:500])
            print("-" * 80)
            
            return True
        else:
            print("❌ 基本面报告生成失败")
            return False
            
    except Exception as e:
        print(f"❌ 基本面报告生成失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='诊断基本面数据获取问题')
    parser.add_argument('--stock', type=str, default='000001',
                       help='股票代码（默认: 000001）')
    
    args = parser.parse_args()
    stock_code = args.stock
    
    print("\n" + "🔍" * 40)
    print(f"基本面数据诊断工具 - 股票代码: {stock_code}")
    print("🔍" * 40)
    
    # 执行所有检查
    results = {}
    
    results['mongodb'] = check_mongodb_connection()
    results['financial_data'] = check_stock_financial_data(stock_code)
    results['basic_info'] = check_stock_basic_info(stock_code)
    results['tushare'] = check_tushare_connection()
    results['tushare_data'] = check_tushare_financial_data(stock_code)
    results['akshare'] = check_akshare_connection()
    results['akshare_data'] = check_akshare_financial_data(stock_code)
    results['report'] = test_fundamentals_report_generation(stock_code)
    
    # 汇总结果
    print("\n" + "="*80)
    print("📊 诊断结果汇总")
    print("="*80)
    
    for key, value in results.items():
        status = "✅ 通过" if value else "❌ 失败"
        print(f"{key:20s}: {status}")
    
    # 给出建议
    print("\n" + "="*80)
    print("💡 建议")
    print("="*80)
    
    if not results['mongodb']:
        print("⚠️ MongoDB 连接失败，请检查:")
        print("   1. MongoDB 服务是否启动")
        print("   2. 连接配置是否正确")
    
    if not results['financial_data']:
        print("⚠️ MongoDB 中没有财务数据，建议:")
        print("   1. 运行财务数据同步: python scripts/sync_financial_data.py")
        print(f"   2. 或指定股票同步: python scripts/sync_financial_data.py --stock {stock_code}")
    
    if not results['tushare']:
        print("⚠️ Tushare 连接失败，请检查:")
        print("   1. TUSHARE_TOKEN 环境变量是否设置")
        print("   2. Token 是否有效（访问 https://tushare.pro 查看）")
    
    if not results['akshare']:
        print("⚠️ AKShare 连接失败，请检查网络连接")
    
    if not results['report'] or "由于无法获取完整的财务数据" in str(results.get('report', '')):
        print("⚠️ 基本面报告缺少财务数据，原因:")
        print("   - MongoDB、Tushare、AKShare 三个数据源都无法获取数据")
        print("   - 建议优先修复 MongoDB 和 Tushare 数据源")


if __name__ == '__main__':
    main()
