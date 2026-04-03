#!/usr/bin/env python3
"""
同步股票财务数据

功能：
1. 优先从 Tushare 获取股票财务指标（支持北交所等所有市场）
2. Tushare 失败时降级到 AKShare
3. 更新 stock_basic_info 集合的财务字段
4. 创建/更新 stock_financial_data 集合

使用方法：
    python scripts/sync_financial_data.py 600036  # 同步单只股票
    python scripts/sync_financial_data.py --all   # 同步所有股票
    python scripts/sync_financial_data.py --batch 100  # 批量同步前100只
    python scripts/sync_financial_data.py --source tushare  # 强制使用Tushare
    python scripts/sync_financial_data.py --source akshare  # 强制使用AKShare
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
from tradingagents.dataflows.providers.china.akshare import AKShareProvider
from tradingagents.dataflows.providers.china.tushare import TushareProvider
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def sync_single_stock_financial_data(
    code: str,
    tushare_provider: Optional[TushareProvider],
    akshare_provider: AKShareProvider,
    db,
    force_source: str = None
) -> bool:
    """
    同步单只股票的财务数据
    
    Args:
        code: 股票代码
        tushare_provider: Tushare提供器（优先使用）
        akshare_provider: AKShare提供器（备用）
        db: 数据库连接
        force_source: 强制使用的数据源 ('tushare' 或 'akshare')
    
    Returns:
        bool: 是否成功
    """
    code6 = str(code).zfill(6)
    
    try:
        logger.info(f"🔄 同步 {code6} 的财务数据...")
        
        # 判断市场类型
        market_type = _get_market_type(code6)
        logger.info(f"   市场类型: {market_type}")
        
        # 🔥 优先使用 Tushare（支持所有市场，包括北交所）
        if force_source != 'akshare' and tushare_provider and tushare_provider.is_available():
            logger.info(f"   📊 [数据源: Tushare] 尝试从Tushare获取财务数据...")
            success = await _sync_from_tushare(code6, tushare_provider, db)
            if success:
                return True
            else:
                logger.warning(f"⚠️  {code6} Tushare获取失败，尝试降级到AKShare...")
        
        # 降级到 AKShare
        if force_source != 'tushare':
            logger.info(f"   📊 [数据源: AKShare] 尝试从AKShare获取财务数据...")
            return await _sync_from_akshare(code6, akshare_provider, db, market_type)
        
        return False
        
    except Exception as e:
        logger.error(f"❌ {code6} 财务数据同步失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


async def _sync_from_tushare(code: str, provider: TushareProvider, db) -> bool:
    """从 Tushare 同步财务数据"""
    try:
        # 获取财务数据
        financial_data = await provider.get_financial_data(code, limit=4)
        
        if not financial_data:
            logger.warning(f"⚠️  {code} Tushare未返回财务数据")
            return False
        
        # 提取标准化的财务数据
        code6 = code.zfill(6)
        report_period = financial_data.get('report_period', '')
        
        logger.info(f"   获取到Tushare财务数据，报告期: {report_period}")
        
        # 构建财务数据字典
        financial_dict = {
            "code": code6,
            "symbol": code6,
            "report_period": report_period,
            "data_source": "tushare",
            "updated_at": datetime.utcnow(),
            
            # 盈利能力指标
            "roe": financial_data.get('roe'),
            "roa": financial_data.get('roa'),
            "gross_margin": financial_data.get('gross_margin'),
            "netprofit_margin": financial_data.get('netprofit_margin'),
            
            # 财务数据（万元）
            "revenue": financial_data.get('revenue'),
            "revenue_ttm": financial_data.get('revenue_ttm'),
            "net_profit": financial_data.get('net_profit'),
            "net_profit_ttm": financial_data.get('net_profit_ttm'),
            "total_assets": financial_data.get('total_assets'),
            "total_hldr_eqy_exc_min_int": financial_data.get('total_equity'),
            
            # 偿债能力指标
            "debt_to_assets": financial_data.get('debt_to_assets'),
            "current_ratio": financial_data.get('current_ratio'),
            "quick_ratio": financial_data.get('quick_ratio'),
            "cash_ratio": financial_data.get('cash_ratio'),
        }
        
        # 获取股本数据（从 stock_basic_info）
        basic_info = await db.stock_basic_info.find_one({"code": code6})
        if basic_info:
            total_share = basic_info.get('total_share')
            if total_share:
                financial_dict['total_share'] = total_share
        
        # 计算市值和估值指标
        quote = await db.market_quotes.find_one(
            {"$or": [{"code": code6}, {"symbol": code6}]}
        )
        
        if quote and financial_dict.get('total_share'):
            price = quote.get('close')
            if price:
                # 计算市值（万元）
                market_cap = price * financial_dict['total_share']
                financial_dict['money_cap'] = market_cap
                
                # 计算 PE（优先使用 TTM）
                net_profit_for_pe = financial_dict.get('net_profit_ttm') or financial_dict.get('net_profit')
                if net_profit_for_pe and net_profit_for_pe > 0:
                    pe = market_cap / net_profit_for_pe
                    financial_dict['pe'] = round(pe, 2)
                    logger.info(f"   PE: {pe:.2f}")
                
                # 计算 PB
                if financial_dict.get('total_hldr_eqy_exc_min_int') and financial_dict['total_hldr_eqy_exc_min_int'] > 0:
                    pb = market_cap / financial_dict['total_hldr_eqy_exc_min_int']
                    financial_dict['pb'] = round(pb, 2)
                    logger.info(f"   PB: {pb:.2f}")
                
                # 计算 PS（优先使用 TTM）
                revenue_for_ps = financial_dict.get('revenue_ttm') or financial_dict.get('revenue')
                if revenue_for_ps and revenue_for_ps > 0:
                    ps = market_cap / revenue_for_ps
                    financial_dict['ps'] = round(ps, 2)
                    logger.info(f"   PS: {ps:.2f}")
        
        # 更新 stock_basic_info
        await db.stock_basic_info.update_one(
            {"code": code6},
            {"$set": {
                "total_share": financial_dict.get('total_share'),
                "net_profit": financial_dict.get('net_profit'),
                "net_profit_ttm": financial_dict.get('net_profit_ttm'),
                "revenue_ttm": financial_dict.get('revenue_ttm'),
                "total_hldr_eqy_exc_min_int": financial_dict.get('total_hldr_eqy_exc_min_int'),
                "money_cap": financial_dict.get('money_cap'),
                "pe": financial_dict.get('pe'),
                "pb": financial_dict.get('pb'),
                "ps": financial_dict.get('ps'),
                "roe": financial_dict.get('roe'),
                "updated_at": datetime.utcnow()
            }},
            upsert=False
        )
        
        # 更新 stock_financial_data（保存原始数据）
        await db.stock_financial_data.update_one(
            {"code": code6, "report_period": report_period},
            {"$set": {**financial_dict, "raw_data": financial_data.get('raw_data', {})}},
            upsert=True
        )
        
        logger.info(f"✅ {code6} Tushare财务数据同步成功")
        return True
        
    except Exception as e:
        logger.error(f"❌ {code} Tushare同步失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


async def _sync_from_akshare(code: str, provider: AKShareProvider, db, market_type: str) -> bool:
    """从 AKShare 同步财务数据"""
    try:
        # 1. 获取财务指标数据
        import akshare as ak

        def fetch_financial_indicator():
            return ak.stock_financial_analysis_indicator(symbol=code)

        try:
            df = await asyncio.to_thread(fetch_financial_indicator)

            if df is None or df.empty:
                logger.warning(f"⚠️  {code} 未获取到财务指标数据（stock_financial_analysis_indicator）")
                
                # 对于北交所等特殊市场，尝试使用备用接口
                if market_type in ["北交所", "新三板"]:
                    logger.info(f"   尝试使用备用接口获取财务数据...")
                    return await _sync_using_alternative_interface(code, db)
                
                return False

            # 获取最新一期数据
            latest = df.iloc[-1].to_dict()

            logger.info(f"   获取到 {len(df)} 期财务数据，最新期: {latest.get('报告期', 'N/A')}")

            # 计算 TTM（最近12个月）营业收入和净利润
            ttm_revenue = _calculate_ttm_metric(df, '营业收入')
            ttm_net_profit = _calculate_ttm_metric(df, '净利润')

            if ttm_revenue:
                logger.info(f"   TTM营业收入: {ttm_revenue:.2f} 万元")
            if ttm_net_profit:
                logger.info(f"   TTM净利润: {ttm_net_profit:.2f} 万元")

        except Exception as e:
            logger.error(f"❌ {code6} 获取财务指标失败: {e}")
            return False

        # 2. 解析财务数据
        financial_data = {
            "code": code6,
            "symbol": code6,
            "report_period": latest.get('报告期', ''),
            "data_source": "akshare",
            "updated_at": datetime.utcnow(),

            # 盈利能力指标
            "roe": _safe_float(latest.get('净资产收益率')),  # ROE
            "roa": _safe_float(latest.get('总资产净利率')),  # ROA
            "gross_margin": _safe_float(latest.get('销售毛利率')),  # 毛利率
            "netprofit_margin": _safe_float(latest.get('销售净利率')),  # 净利率

            # 财务数据（万元）
            "revenue": _safe_float(latest.get('营业收入')),  # 营业收入（单期）
            "revenue_ttm": ttm_revenue,  # TTM营业收入（最近12个月）
            "net_profit": _safe_float(latest.get('净利润')),  # 净利润（单期）
            "net_profit_ttm": ttm_net_profit,  # TTM净利润（最近12个月）
            "total_assets": _safe_float(latest.get('总资产')),  # 总资产
            "total_hldr_eqy_exc_min_int": _safe_float(latest.get('股东权益合计')),  # 净资产

            # 每股指标
            "basic_eps": _safe_float(latest.get('基本每股收益')),  # 每股收益
            "bps": _safe_float(latest.get('每股净资产')),  # 每股净资产

            # 偿债能力指标
            "debt_to_assets": _safe_float(latest.get('资产负债率')),  # 资产负债率
            "current_ratio": _safe_float(latest.get('流动比率')),  # 流动比率

            # 运营能力指标
            "total_asset_turnover": _safe_float(latest.get('总资产周转率')),  # 总资产周转率
        }
        
        # 3. 获取股本数据
        try:
            def fetch_stock_info():
                return ak.stock_individual_info_em(symbol=code6)
            
            stock_info_df = await asyncio.to_thread(fetch_stock_info)
            
            if stock_info_df is not None and not stock_info_df.empty:
                # 提取总股本
                total_share_row = stock_info_df[stock_info_df['item'] == '总股本']
                if not total_share_row.empty:
                    total_share_str = str(total_share_row['value'].iloc[0])
                    # 解析总股本（可能是 "193.78亿" 这种格式）
                    total_share = _parse_share_value(total_share_str)
                    financial_data['total_share'] = total_share
                    logger.info(f"   总股本: {total_share} 万股")
                
                # 提取流通股本
                float_share_row = stock_info_df[stock_info_df['item'] == '流通股']
                if not float_share_row.empty:
                    float_share_str = str(float_share_row['value'].iloc[0])
                    float_share = _parse_share_value(float_share_str)
                    financial_data['float_share'] = float_share
        
        except Exception as e:
            logger.warning(f"⚠️  {code6} 获取股本数据失败: {e}")
        
        # 4. 计算市值和估值指标（如果有实时价格）
        quote = await db.market_quotes.find_one(
            {"$or": [{"code": code6}, {"symbol": code6}]}
        )
        
        if quote and financial_data.get('total_share'):
            price = quote.get('close')
            if price:
                # 计算市值（万元）
                market_cap = price * financial_data['total_share']
                financial_data['money_cap'] = market_cap

                # 计算 PE（优先使用 TTM 净利润）
                net_profit_for_pe = financial_data.get('net_profit_ttm') or financial_data.get('net_profit')
                pe_type = "TTM" if financial_data.get('net_profit_ttm') else "单期"

                if net_profit_for_pe and net_profit_for_pe > 0:
                    pe = market_cap / net_profit_for_pe
                    financial_data['pe'] = round(pe, 2)
                    logger.info(f"   PE({pe_type}): {pe:.2f}")

                # 计算 PB
                if financial_data.get('total_hldr_eqy_exc_min_int') and financial_data['total_hldr_eqy_exc_min_int'] > 0:
                    pb = market_cap / financial_data['total_hldr_eqy_exc_min_int']
                    financial_data['pb'] = round(pb, 2)
                    logger.info(f"   PB: {pb:.2f}")

                # 计算 PS（优先使用 TTM 营业收入）
                revenue_for_ps = financial_data.get('revenue_ttm') or financial_data.get('revenue')
                ps_type = "TTM" if financial_data.get('revenue_ttm') else "单期"

                if revenue_for_ps and revenue_for_ps > 0:
                    ps = market_cap / revenue_for_ps
                    financial_data['ps'] = round(ps, 2)
                    logger.info(f"   PS({ps_type}): {ps:.2f}")
        
        # 5. 更新 stock_basic_info 集合
        await db.stock_basic_info.update_one(
            {"code": code6},
            {"$set": {
                "total_share": financial_data.get('total_share'),
                "float_share": financial_data.get('float_share'),
                "net_profit": financial_data.get('net_profit'),
                "net_profit_ttm": financial_data.get('net_profit_ttm'),
                "revenue_ttm": financial_data.get('revenue_ttm'),
                "total_hldr_eqy_exc_min_int": financial_data.get('total_hldr_eqy_exc_min_int'),
                "money_cap": financial_data.get('money_cap'),
                "pe": financial_data.get('pe'),
                "pb": financial_data.get('pb'),
                "ps": financial_data.get('ps'),
                "roe": financial_data.get('roe'),
                "updated_at": datetime.utcnow()
            }},
            upsert=False  # 不创建新文档，只更新已存在的
        )
        
        # 6. 更新 stock_financial_data 集合
        await db.stock_financial_data.update_one(
            {"code": code6, "report_period": financial_data['report_period']},
            {"$set": financial_data},
            upsert=True
        )
        
        logger.info(f"✅ {code6} 财务数据同步成功")
        return True
        
    except Exception as e:
        logger.error(f"❌ {code6} 财务数据同步失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def _get_market_type(code: str) -> str:
    """判断股票市场类型"""
    code = str(code).zfill(6)
    
    if code.startswith('6'):
        return "上海A股"
    elif code.startswith('0') or code.startswith('3'):
        return "深圳A股"
    elif code.startswith('92'):
        return "北交所"
    elif code.startswith('4') or code.startswith('8'):
        return "新三板"
    else:
        return "未知市场"


async def _sync_using_alternative_interface(code: str, db) -> bool:
    """
    使用备用接口同步财务数据（用于北交所等特殊市场）
    
    使用 stock_financial_report_sina 接口获取资产负债表
    """
    import akshare as ak
    
    try:
        logger.info(f"   使用 stock_financial_report_sina 接口...")
        
        # 获取资产负债表
        def fetch_balance_sheet():
            return ak.stock_financial_report_sina(stock=code, symbol="资产负债表")
        
        df = await asyncio.to_thread(fetch_balance_sheet)
        
        if df is None or df.empty:
            logger.warning(f"⚠️  {code} 备用接口也未获取到数据")
            return False
        
        logger.info(f"   获取到 {len(df)} 期资产负债表数据")
        
        # 获取最新一期数据
        latest = df.iloc[0].to_dict()  # 第一行是最新数据
        report_period = latest.get('报告日', '')
        
        logger.info(f"   最新报告期: {report_period}")
        
        # 从资产负债表提取关键财务数据
        financial_data = {
            "code": code,
            "symbol": code,
            "report_period": report_period,
            "data_source": "akshare_sina",
            "updated_at": datetime.utcnow(),
            
            # 资产数据（单位：元，需转换为万元）
            "total_assets": _safe_float(latest.get('资产总计')) / 10000 if _safe_float(latest.get('资产总计')) else None,
            "total_hldr_eqy_exc_min_int": _safe_float(latest.get('归属于母公司股东权益合计')) / 10000 if _safe_float(latest.get('归属于母公司股东权益合计')) else None,
            
            # 每股净资产（需要股本数据才能计算）
            # 资产负债率
            "debt_to_assets": None,  # 需要计算
        }
        
        # 计算资产负债率
        total_assets = _safe_float(latest.get('资产总计'))
        total_liabilities = _safe_float(latest.get('负债合计'))
        if total_assets and total_liabilities and total_assets > 0:
            financial_data['debt_to_assets'] = round((total_liabilities / total_assets) * 100, 2)
            logger.info(f"   资产负债率: {financial_data['debt_to_assets']}%")
        
        # 获取股本数据
        try:
            def fetch_stock_info():
                return ak.stock_individual_info_em(symbol=code)
            
            stock_info_df = await asyncio.to_thread(fetch_stock_info)
            
            if stock_info_df is not None and not stock_info_df.empty:
                # 提取总股本
                total_share_row = stock_info_df[stock_info_df['item'] == '总股本']
                if not total_share_row.empty:
                    total_share_str = str(total_share_row['value'].iloc[0])
                    total_share = _parse_share_value(total_share_str)
                    financial_data['total_share'] = total_share
                    logger.info(f"   总股本: {total_share} 万股")
                
                # 提取流通股本
                float_share_row = stock_info_df[stock_info_df['item'] == '流通股']
                if not float_share_row.empty:
                    float_share_str = str(float_share_row['value'].iloc[0])
                    float_share = _parse_share_value(float_share_str)
                    financial_data['float_share'] = float_share
                    logger.info(f"   流通股: {float_share} 万股")
                
                # 计算每股净资产
                if financial_data.get('total_hldr_eqy_exc_min_int') and total_share and total_share > 0:
                    bps = financial_data['total_hldr_eqy_exc_min_int'] / total_share
                    financial_data['bps'] = round(bps, 2)
                    logger.info(f"   每股净资产: {bps:.2f} 元")
        
        except Exception as e:
            logger.warning(f"⚠️  {code} 获取股本数据失败: {e}")
        
        # 计算市值和估值指标（如果有实时价格）
        quote = await db.market_quotes.find_one(
            {"$or": [{"code": code}, {"symbol": code}]}
        )
        
        if quote and financial_data.get('total_share'):
            price = quote.get('close')
            if price:
                # 计算市值（万元）
                market_cap = price * financial_data['total_share']
                financial_data['money_cap'] = market_cap
                logger.info(f"   总市值: {market_cap:.2f} 万元")
                
                # 计算 PB
                if financial_data.get('total_hldr_eqy_exc_min_int') and financial_data['total_hldr_eqy_exc_min_int'] > 0:
                    pb = market_cap / financial_data['total_hldr_eqy_exc_min_int']
                    financial_data['pb'] = round(pb, 2)
                    logger.info(f"   PB: {pb:.2f}")
        
        # 更新 stock_basic_info 集合
        await db.stock_basic_info.update_one(
            {"code": code},
            {"$set": {
                "total_share": financial_data.get('total_share'),
                "float_share": financial_data.get('float_share'),
                "total_assets": financial_data.get('total_assets'),
                "total_hldr_eqy_exc_min_int": financial_data.get('total_hldr_eqy_exc_min_int'),
                "money_cap": financial_data.get('money_cap'),
                "pb": financial_data.get('pb'),
                "bps": financial_data.get('bps'),
                "debt_to_assets": financial_data.get('debt_to_assets'),
                "updated_at": datetime.utcnow()
            }},
            upsert=False
        )
        
        # 更新 stock_financial_data 集合
        await db.stock_financial_data.update_one(
            {"code": code, "report_period": financial_data['report_period']},
            {"$set": financial_data},
            upsert=True
        )
        
        logger.info(f"✅ {code} 财务数据同步成功（使用备用接口）")
        return True
        
    except Exception as e:
        logger.error(f"❌ {code} 备用接口同步失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def _safe_float(value) -> Optional[float]:
    """安全转换为浮点数"""
    if value is None or value == '' or str(value) == 'nan' or value == '--':
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _calculate_ttm_metric(df, metric_name: str) -> Optional[float]:
    """
    计算 TTM（最近12个月）指标值（营业收入、净利润等）

    策略：
    1. 如果最新期是年报（12月31日），直接使用年报数据
    2. 如果最新期是中报/季报，计算 TTM = 最新年报 + (本期累计 - 去年同期累计)
    3. 如果数据不足，返回 None（不使用简单年化，因为对季节性行业不准确）

    Args:
        df: AKShare 返回的财务指标 DataFrame，包含 '报告期' 和指标列
        metric_name: 指标名称（如 '营业收入'、'净利润'）

    Returns:
        TTM 指标值（万元），如果无法计算则返回 None
    """
    try:
        if df is None or df.empty or len(df) < 1:
            return None

        # 确保有必要的列
        if '报告期' not in df.columns or metric_name not in df.columns:
            return None

        # 按报告期排序（升序）
        df_sorted = df.sort_values('报告期', ascending=True).reset_index(drop=True)

        # 获取最新一期
        latest = df_sorted.iloc[-1]
        latest_period = str(latest['报告期'])
        latest_value = _safe_float(latest[metric_name])

        if latest_value is None:
            return None

        # 判断最新期是否是年报（报告期以1231结尾）
        if latest_period.endswith('1231'):
            # 年报，直接使用
            logger.debug(f"   使用年报{metric_name}作为TTM: {latest_value:.2f} 万元")
            return latest_value

        # 非年报，需要计算 TTM
        # 提取年份和月份
        try:
            year = int(latest_period[:4])
            month_day = latest_period[4:]
        except:
            return None

        # 查找最近的年报（上一年的1231）
        last_year = year - 1
        last_annual_period = f"{last_year}1231"

        # 查找去年同期
        last_same_period = f"{last_year}{month_day}"

        # 在 DataFrame 中查找
        last_annual_row = df_sorted[df_sorted['报告期'] == last_annual_period]
        last_same_row = df_sorted[df_sorted['报告期'] == last_same_period]

        if not last_annual_row.empty and not last_same_row.empty:
            last_annual_value = _safe_float(last_annual_row.iloc[0][metric_name])
            last_same_value = _safe_float(last_same_row.iloc[0][metric_name])

            if last_annual_value is not None and last_same_value is not None:
                # TTM = 最近年报 + (本期累计 - 去年同期累计)
                ttm_value = last_annual_value + (latest_value - last_same_value)
                logger.debug(f"   ✅ 计算{metric_name}TTM: {last_annual_value:.2f} + ({latest_value:.2f} - {last_same_value:.2f}) = {ttm_value:.2f} 万元")
                return ttm_value if ttm_value > 0 else None

        # 如果无法计算 TTM，返回 None（不使用简单年化，因为对季节性行业不准确）
        if not last_annual_row.empty:
            logger.warning(f"   ⚠️ {metric_name}TTM计算失败: 缺少去年同期数据（需要: {last_same_period}）")
        else:
            logger.warning(f"   ⚠️ {metric_name}TTM计算失败: 缺少基准年报（需要: {last_annual_period}）")

        return None

    except Exception as e:
        logger.warning(f"   计算{metric_name}TTM失败: {e}")
        return None


# 保留旧函数名以保持向后兼容
def _calculate_ttm_revenue(df) -> Optional[float]:
    """
    计算 TTM（最近12个月）营业收入

    已弃用：请使用 _calculate_ttm_metric(df, '营业收入')
    """
    return _calculate_ttm_metric(df, '营业收入')


def _parse_share_value(value_str: str) -> Optional[float]:
    """解析股本数值（支持 "193.78亿" 这种格式）"""
    try:
        value_str = str(value_str).strip()
        
        # 移除单位并转换
        if '亿' in value_str:
            num = float(value_str.replace('亿', ''))
            return num * 10000  # 亿 -> 万
        elif '万' in value_str:
            return float(value_str.replace('万', ''))
        else:
            # 假设是股数，转换为万股
            return float(value_str) / 10000
    except:
        return None


async def main(code: Optional[str] = None, sync_all: bool = False, batch: Optional[int] = None, force_source: str = None):
    """主函数"""
    logger.info("=" * 80)
    logger.info("🚀 同步股票财务数据")
    if force_source:
        logger.info(f"   强制使用数据源: {force_source.upper()}")
    else:
        logger.info("   数据源策略: Tushare优先，AKShare备用")
    logger.info("=" * 80)
    
    # 连接数据库
    client = AsyncIOMotorClient(settings.MONGO_URI)
    db = client[settings.MONGO_DB]
    
    # 初始化 Tushare Provider（优先）
    tushare_provider = None
    if force_source != 'akshare':
        try:
            tushare_provider = TushareProvider()
            await tushare_provider.connect()
            if tushare_provider.is_available():
                logger.info("✅ Tushare连接成功")
            else:
                logger.warning("⚠️  Tushare不可用，将使用AKShare")
        except Exception as e:
            logger.warning(f"⚠️  Tushare初始化失败: {e}，将使用AKShare")
    
    # 初始化 AKShare Provider（备用）
    akshare_provider = AKShareProvider()
    await akshare_provider.connect()
    
    try:
        if code:
            # 同步单只股票
            await sync_single_stock_financial_data(code, tushare_provider, akshare_provider, db, force_source)
        
        elif sync_all or batch:
            # 批量同步
            cursor = db.stock_basic_info.find({}, {"code": 1, "name": 1})
            stocks = await cursor.to_list(length=batch if batch else None)
            
            total = len(stocks)
            logger.info(f"📊 准备同步 {total} 只股票的财务数据")
            
            success_count = 0
            failed_count = 0
            
            for i, stock in enumerate(stocks, 1):
                stock_code = stock.get('code')
                stock_name = stock.get('name', 'N/A')
                
                logger.info(f"\n[{i}/{total}] {stock_code} ({stock_name})")
                
                success = await sync_single_stock_financial_data(
                    stock_code, tushare_provider, akshare_provider, db, force_source
                )
                
                if success:
                    success_count += 1
                else:
                    failed_count += 1
                
                # 延迟，避免API限流
                if i < total:
                    await asyncio.sleep(0.5)
            
            logger.info(f"\n" + "=" * 80)
            logger.info(f"📊 同步完成统计")
            logger.info(f"=" * 80)
            logger.info(f"   总计: {total} 只")
            logger.info(f"   成功: {success_count} 只")
            logger.info(f"   失败: {failed_count} 只")
            logger.info(f"=" * 80)
        
        else:
            logger.error("❌ 请指定股票代码、--all 或 --batch 参数")
    
    finally:
        client.close()
    
    logger.info("")
    logger.info("✅ 同步完成！")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="同步股票财务数据",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        "code",
        nargs="?",
        type=str,
        help="股票代码（6位）"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="同步所有股票"
    )
    parser.add_argument(
        "--batch",
        type=int,
        help="批量同步前N只股票"
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=['tushare', 'akshare'],
        help="强制使用指定数据源 (tushare/akshare)"
    )
    
    args = parser.parse_args()
    
    asyncio.run(main(
        code=args.code,
        sync_all=args.all,
        batch=args.batch,
        force_source=args.source
    ))

