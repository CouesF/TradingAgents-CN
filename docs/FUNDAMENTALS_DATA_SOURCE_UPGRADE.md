# 基本面数据源升级方案

## 问题描述

北交所股票（92开头）和部分特殊市场股票无法通过 AKShare 的 `stock_financial_analysis_indicator` 接口获取基本面数据，导致基本面分析报告中显示：

```
## ⚠️ 数据说明
由于无法获取完整的财务数据，本报告仅包含基本价格信息和行业分析。
建议：
1. 查看公司最新财报获取详细财务数据
2. 关注行业整体走势
3. 结合技术分析进行综合判断。
```

## 根本原因

1. **AKShare 接口限制**：`stock_financial_analysis_indicator` 接口主要支持沪深主板、创业板、科创板，对北交所等特殊市场支持不完整
2. **数据获取策略单一**：系统仅依赖 AKShare 单一数据源，缺乏备用方案
3. **实时获取模式**：每次分析时临时拉取数据，没有利用数据库中的存量数据

## 解决方案

### 1. 数据源优先级调整

**新的数据获取策略**：
```
Tushare（优先） → AKShare（备用） → MongoDB缓存（兜底）
```

**优势**：
- ✅ **Tushare 全市场覆盖**：支持沪深主板、创业板、科创板、北交所、新三板
- ✅ **数据质量高**：Tushare 提供官方财务数据，准确性和完整性更好
- ✅ **TTM 计算准确**：Tushare 提供多期数据，可准确计算 TTM（最近12个月）指标
- ✅ **降级机制**：Tushare 失败时自动降级到 AKShare

### 2. 修改内容

#### 2.1 修改 `scripts/sync_financial_data.py`

**主要改动**：

1. **引入 Tushare Provider**
```python
from tradingagents.dataflows.providers.china.tushare import TushareProvider
```

2. **新增 Tushare 同步函数**
```python
async def _sync_from_tushare(code: str, provider: TushareProvider, db) -> bool:
    """从 Tushare 同步财务数据"""
    # 获取财务数据（包含利润表、资产负债表、现金流量表、财务指标）
    financial_data = await provider.get_financial_data(code, limit=4)
    
    # 提取关键指标
    # - 盈利能力：ROE、ROA、毛利率、净利率
    # - 财务数据：营业收入、净利润（含TTM）、总资产、净资产
    # - 偿债能力：资产负债率、流动比率、速动比率
    
    # 计算估值指标：PE、PB、PS
    # 更新数据库
```

3. **修改主同步函数**
```python
async def sync_single_stock_financial_data(
    code: str,
    tushare_provider: Optional[TushareProvider],  # 新增
    akshare_provider: AKShareProvider,
    db,
    force_source: str = None  # 新增：支持强制指定数据源
) -> bool:
    # 优先使用 Tushare
    if force_source != 'akshare' and tushare_provider and tushare_provider.is_available():
        success = await _sync_from_tushare(code6, tushare_provider, db)
        if success:
            return True
    
    # 降级到 AKShare
    if force_source != 'tushare':
        return await _sync_from_akshare(code6, akshare_provider, db, market_type)
```

4. **新增命令行参数**
```bash
# 强制使用 Tushare
python scripts/sync_financial_data.py 920068 --source tushare

# 强制使用 AKShare
python scripts/sync_financial_data.py 600036 --source akshare

# 自动选择（默认：Tushare优先）
python scripts/sync_financial_data.py 920068
```

### 3. 数据流程

#### 3.1 数据同步流程（定时任务）

```
┌─────────────────────────────────────────────────────────────┐
│ 定时任务：scripts/sync_financial_data.py                    │
└─────────────────────────────────────────────────────────────┘
                           ↓
        ┌──────────────────┴──────────────────┐
        │                                     │
   ┌────▼────┐                          ┌────▼────┐
   │ Tushare │ (优先)                   │ AKShare │ (备用)
   └────┬────┘                          └────┬────┘
        │                                     │
        └──────────────────┬──────────────────┘
                           ↓
              ┌────────────────────────┐
              │ MongoDB 数据库          │
              │ - stock_basic_info     │
              │ - stock_financial_data │
              └────────────────────────┘
```

#### 3.2 实时分析流程（用户请求）

```
┌─────────────────────────────────────────────────────────────┐
│ 用户请求：分析股票 920068                                    │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ fundamentals_analyst_node(state)                            │
│ - state["company_of_interest"] = "920068"                  │
│ - state["trade_date"] = "2024-01-15"                       │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│ optimized_china_data.py::get_fundamentals_data()            │
└─────────────────────────────────────────────────────────────┘
                           ↓
        ┌──────────────────┴──────────────────┐
        │                                     │
   ┌────▼────────────┐              ┌────────▼─────────┐
   │ MongoDB 缓存     │ (优先)       │ 实时生成分析      │
   │ stock_financial_ │              │ _generate_       │
   │ data 集合        │              │ fundamentals_    │
   └────┬────────────┘              │ report()         │
        │                           └────────┬─────────┘
        │                                    │
        └──────────────────┬─────────────────┘
                           ↓
              ┌────────────────────────┐
              │ 基本面分析报告          │
              │ - 财务指标             │
              │ - 估值分析             │
              │ - 投资建议             │
              └────────────────────────┘
```

### 4. Tushare 接口优势

#### 4.1 全市场覆盖

| 市场 | 代码前缀 | AKShare支持 | Tushare支持 |
|------|---------|------------|------------|
| 上海主板 | 60x | ✅ | ✅ |
| 深圳主板 | 000, 001 | ✅ | ✅ |
| 创业板 | 300, 301 | ✅ | ✅ |
| 科创板 | 688 | ✅ | ✅ |
| **北交所** | **92x** | ⚠️ 部分支持 | ✅ **完整支持** |
| 新三板 | 8xx | ⚠️ 部分支持 | ✅ **完整支持** |

#### 4.2 数据完整性对比

**Tushare 提供的财务数据**：
```python
{
    "income_statement": [...],      # 利润表（多期）
    "balance_sheet": [...],         # 资产负债表（多期）
    "cashflow_statement": [...],    # 现金流量表（多期）
    "financial_indicators": [...],  # 财务指标（多期）
    "main_business": [...]          # 主营业务构成
}
```

**AKShare 提供的财务数据**：
```python
{
    "main_indicators": DataFrame    # 主要财务指标（单一接口）
}
```

#### 4.3 TTM 计算准确性

**Tushare**：
- 提供多期数据，可准确计算 TTM
- 公式：`TTM = 去年年报 + (本期累计 - 去年同期累计)`
- 示例：2025Q2 TTM = 2024年报 + (2025Q2 - 2024Q2)

**AKShare**：
- 仅提供单期数据
- 无法准确计算 TTM（只能简单年化，对季节性行业不准确）

### 5. 使用方法

#### 5.1 同步单只股票（北交所）

```bash
# 使用 Tushare（推荐）
python scripts/sync_financial_data.py 920068

# 强制使用 Tushare
python scripts/sync_financial_data.py 920068 --source tushare

# 强制使用 AKShare（可能失败）
python scripts/sync_financial_data.py 920068 --source akshare
```

#### 5.2 批量同步

```bash
# 同步所有股票（Tushare优先）
python scripts/sync_financial_data.py --all

# 同步前100只股票
python scripts/sync_financial_data.py --batch 100

# 强制使用 Tushare 同步前100只
python scripts/sync_financial_data.py --batch 100 --source tushare
```

#### 5.3 查看同步结果

```bash
# 进入 MongoDB
mongosh

# 切换数据库
use tradingagents

# 查询北交所股票财务数据
db.stock_financial_data.findOne({code: "920068"})

# 查看数据源
db.stock_financial_data.find({code: "920068"}, {data_source: 1, report_period: 1})
```

### 6. 配置 Tushare Token

#### 6.1 获取 Token

1. 访问 [Tushare官网](https://tushare.pro/)
2. 注册账号
3. 获取 Token（免费版即可）

#### 6.2 配置方式

**方式1：环境变量（推荐）**
```bash
# .env 文件
TUSHARE_TOKEN=your_tushare_token_here
```

**方式2：Web后台配置**
1. 登录 Web 管理后台
2. 进入"系统配置" → "数据源配置"
3. 配置 Tushare Token
4. 保存并激活

### 7. 验证效果

#### 7.1 同步前（使用 AKShare）

```bash
python scripts/sync_financial_data.py 920068
```

输出：
```
⚠️  920068 未获取到财务指标数据（stock_financial_analysis_indicator）
   尝试使用备用接口获取财务数据...
⚠️  920068 备用接口也未获取到数据
❌ 920068 财务数据同步失败
```

#### 7.2 同步后（使用 Tushare）

```bash
python scripts/sync_financial_data.py 920068 --source tushare
```

输出：
```
🔄 同步 920068 的财务数据...
   市场类型: 北交所
   📊 [数据源: Tushare] 尝试从Tushare获取财务数据...
   获取到Tushare财务数据，报告期: 20240930
   PE: 15.23
   PB: 2.45
   PS: 3.12
✅ 920068 Tushare财务数据同步成功
```

### 8. 注意事项

1. **Tushare 积分限制**
   - 免费版：每分钟120次调用
   - 建议：批量同步时添加延迟（已实现：0.5秒/只）

2. **数据更新频率**
   - 建议：每日同步一次（定时任务）
   - 财报发布后及时更新

3. **降级策略**
   - Tushare 失败时自动降级到 AKShare
   - AKShare 失败时使用 MongoDB 缓存数据

4. **北交所特殊处理**
   - 代码格式：`920068.BJ`（Tushare 自动转换）
   - 市场类型：自动识别

### 9. 后续优化建议

1. **定时任务**
   - 设置每日定时同步（凌晨执行）
   - 财报季重点更新

2. **监控告警**
   - 同步失败率监控
   - Tushare 积分余额监控

3. **数据质量**
   - 定期校验数据完整性
   - 异常数据标记和修复

4. **性能优化**
   - 批量同步时使用并发（控制并发数）
   - 增量更新（只更新变化的数据）

## 总结

通过引入 Tushare 作为主要数据源，系统现在可以：

✅ **支持北交所股票**：920068 等北交所股票可正常获取财务数据  
✅ **提高数据质量**：Tushare 官方数据，准确性更高  
✅ **准确计算 TTM**：多期数据支持，TTM 计算更准确  
✅ **降级保护**：Tushare 失败时自动降级到 AKShare  
✅ **灵活配置**：支持强制指定数据源  

**测试命令**：
```bash
# 测试北交所股票
python scripts/sync_financial_data.py 920068

# 查看分析结果
# 在 Web 界面或 CLI 中分析 920068，应该能看到完整的基本面数据
```
