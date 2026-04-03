# MongoDB 数据结构文档

> **最后更新**: 2026-04-02
> **数据量**: stock_basic_info: 5502条, stock_daily_quotes: 1557万条, index_daily_quotes: 3.6万条

## 连接配置

### 连接方式
```python
# 方式1：通过 DatabaseManager（推荐）
from tradingagents.config.database_manager import get_database_manager

db_manager = get_database_manager()
db = db_manager.get_mongodb_db()  # 返回 tradingagents 数据库

# 方式2：直接连接
from pymongo import MongoClient
client = MongoClient(
    host=os.getenv("MONGODB_HOST", "localhost"),
    port=int(os.getenv("MONGODB_PORT", "27017")),
    username=os.getenv("MONGODB_USERNAME"),
    password=os.getenv("MONGODB_PASSWORD"),
    authSource=os.getenv("MONGODB_AUTH_SOURCE", "admin")
)
db = client['tradingagents']
```

### 数据库名称
- **数据库**: `tradingagents`

---

## 集合结构

### 1. stock_basic_info (股票基础信息)

**数据量**: 5501 条记录

**用途**: 存储股票的基本静态信息

**实际字段示例**:
```json
{
  "_id": ObjectId("..."),
  "source": "tushare",
  "code": "000001",           // 6位股票代码
  "symbol": "000001",         // 与code相同
  "full_symbol": "000001.SZ", // 交易所完整代码
  "name": "平安银行",
  "area": "深圳",
  "category": "stock_cn",
  "industry": "银行",
  "market": "主板",
  "sse": "深圳证券交易所",
  "list_date": "19910403",    // 上市日期 YYYYMMDD格式
  "circ_mv": 2132.67,         // 流通市值(亿元)
  "total_mv": 2132.71,        // 总市值(亿元)
  "total_share": 1940591.82,  // 总股本(万股)
  "float_share": 1940560.07,  // 流通股本(万股)
  "pe": 5.0025,               // 市盈率
  "pe_ttm": 5.0025,           // TTM市盈率
  "pb": 0.4726,               // 市净率
  "ps": 1.6225,               // 市销率
  "ps_ttm": 1.6225,           // TTM市销率
  "roe": 7.5711,              // ROE
  "turnover_rate": 0.3259,    // 换手率
  "volume_ratio": 0.56,       // 量比
  "is_hs": "S",               // 沪深港通: S=深港通, H=沪港通, N=否
  "data_source": "tushare",
  "data_version": 1,
  "updated_at": "2026-03-31T13:06:28",
  "market_info": {
    "market": "CN",
    "exchange": "SZSE",
    "exchange_name": "深圳证券交易所",
    "currency": "CNY",
    "timezone": "Asia/Shanghai"
  }
}
```

**关键字段说明**:
| 字段 | 类型 | 说明 |
|------|------|------|
| code/symbol | str(6位) | 股票代码，如 "000001" |
| full_symbol | str | 交易所完整代码，如 "000001.SZ" |
| name | str | 股票名称 |
| industry | str | 行业分类 |
| market | str | 板块：主板/科创板/创业板 |
| sse | str | 交易所名称 |
| total_mv | float | 总市值(亿元) |
| circ_mv | float | 流通市值(亿元) |
| pe/pe_ttm | float | 市盈率/TTM市盈率 |
| pb | float | 市净率 |
| roe | float | ROE(%) |
| is_hs | str | 沪深港通标识 |
| data_source | str | 数据来源：tushare |

**注意**: 该集合**没有** `status` 字段表示上市状态，默认全部为上市股票

**查询示例**:
```python
# 查询某股票基础信息
doc = db.stock_basic_info.find_one({"code": "000001"})

# 查询银行行业股票
docs = db.stock_basic_info.find({"industry": "银行"})

# 查询大市值股票(>100亿)
docs = db.stock_basic_info.find({"total_mv": {"$gt": 100}})
```

---

### 2. stock_daily_quotes (日线行情)

**数据量**: 15,570,355 条记录

**用途**: 存储股票日线级别的行情数据

**实际字段示例**:
```json
{
  "_id": ObjectId("..."),
  "symbol": "601668",
  "code": "601668",
  "full_symbol": "601668.SH",
  "market": "CN",
  "trade_date": "2024-12-11",   // 注意：格式是 YYYY-MM-DD
  "period": "daily",
  "data_source": "tushare",
  "open": 5.85,
  "high": 5.9,
  "low": 5.83,
  "close": 5.84,
  "pre_close": 5.85,
  "volume": 160963963.0,        // 成交量(股)
  "amount": 986332147.0,        // 成交额(元)
  "change": -0.01,              // 涨跌额
  "pct_chg": -0.1709,           // 涨跌幅(%)
  "created_at": "2025-12-14T14:30:23",
  "updated_at": "2025-12-14T14:30:23",
  "version": 1
}
```

**关键字段说明**:
| 字段 | 类型 | 说明 |
|------|------|------|
| symbol/code | str(6位) | 股票代码 |
| trade_date | str | 交易日期，**格式YYYY-MM-DD** |
| period | str | 周期：daily/weekly/monthly |
| open/high/low/close | float | 开高低收 |
| pre_close | float | 前收盘价 |
| volume | float | 成交量(股) |
| amount | float | 成交额(元) |
| change | float | 涨跌额 |
| pct_chg | float | 涨跌幅(%) |
| data_source | str | 数据来源 |

**注意**:
- `trade_date` 格式是 `YYYY-MM-DD`，不是 `YYYYMMDD`
- 该集合**没有** `pe`, `pb`, `total_mv`, `turnover_rate` 等估值字段

**查询示例**:
```python
# 查询某股票历史数据（注意日期格式）
cursor = db.stock_daily_quotes.find({
    "symbol": "000001",
    "period": "daily",
    "trade_date": {"$gte": "2024-01-01"}  # 使用 YYYY-MM-DD 格式
}).sort("trade_date", 1)

# 查询最近一年数据
from datetime import datetime, timedelta
start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
cursor = db.stock_daily_quotes.find({
    "symbol": "000001",
    "trade_date": {"$gte": start_date}
}).sort("trade_date", 1)
```

---

### 3. index_daily_quotes (指数日线行情)

**数据量**: 36,372 条记录

**用途**: 存储大盘指数日线数据，独立于股票数据避免代码冲突

**支持指数**:
| 指数名称 | 代码 | 数据起始 | 记录数 |
|---------|------|---------|--------|
| 上证指数 | 000001 | 2000年 | 6358 |
| 深证成指 | 399001 | 2000年 | 6358 |
| 创业板指 | 399006 | 2010年 | 3844 |
| 沪深300 | 000300 | 2005年 | 5879 |
| 中证500 | 000905 | 2005年 | 5158 |
| 中证1000 | 000852 | 2014年 | 2785 |
| 上证50 | 000016 | 2004年 | 5401 |
| 北证50 | 899050 | 2022年 | 949 |

**实际字段示例**:
```json
{
  "_id": ObjectId("..."),
  "symbol": "000001",
  "full_symbol": "000001.SH",
  "name": "上证指数",
  "exchange": "SSE",
  "trade_date": "2026-04-01",
  "open": 3935.67,
  "high": 3952.18,
  "low": 3930.45,
  "close": 3948.55,
  "volume": 452367895.0,
  "amount": 52345678901.0,
  "pct_chg": 0.32,
  "change": 12.67,
  "amplitude": 0.55,
  "turnover": null,
  "data_source": "akshare",
  "updated_at": "2026-04-02T12:20:50"
}
```

**关键字段说明**:
| 字段 | 类型 | 说明 |
|------|------|------|
| symbol | str(6位) | 指数代码，如 "000001"(上证指数) |
| full_symbol | str | 完整代码，如 "000001.SH" |
| name | str | 指数名称 |
| exchange | str | 交易所：SSE/SZSE/BSE |
| trade_date | str | 交易日期，格式YYYY-MM-DD |
| open/high/low/close | float | 开高低收 |
| volume | float | 成交量 |
| amount | float | 成交额 |
| pct_chg | float | 涨跌幅(%) |
| change | float | 涨跌额 |
| amplitude | float | 振幅(%) |
| data_source | str | 数据来源：akshare |

**注意**:
- 该集合独立存储指数数据，与 `stock_daily_quotes` 分开
- 指数代码与股票代码有重叠（如 000001 是上证指数也是平安银行），但存储在不同集合中
- 同步脚本: `scripts/sync_index_data.py`

**查询示例**:
```python
# 获取上证指数历史数据
cursor = db.index_daily_quotes.find({
    "symbol": "000001",
    "trade_date": {"$gte": "2024-01-01"}
}).sort("trade_date", 1)

# 计算指数区间收益
start_doc = db.index_daily_quotes.find_one({
    "symbol": "000001", "trade_date": "2024-01-01"
})
end_doc = db.index_daily_quotes.find_one({
    "symbol": "000001", "trade_date": "2024-12-31"
})
return_pct = (end_doc["close"] - start_doc["close"]) / start_doc["close"] * 100
```

**数据同步**:
```bash
# 同步所有指数
python scripts/sync_index_data.py

# 同步指定指数
python scripts/sync_index_data.py --index sh  # 上证指数

# 查看数据状态
python scripts/sync_index_data.py --status
```

---

### 4. stock_financial_data (财务数据)

**数据量**: 6205 条记录

**用途**: 存储股票财务报表数据

**关键字段**:
| 字段 | 类型 | 说明 |
|------|------|------|
| symbol/code | str(6位) | 股票代码 |
| report_period | str | 报告期 |
| report_type | str | 报告类型 |
| ann_date | date | 公告日期 |
| data_source | str | 数据来源 |

---

### 4. stock_news (新闻数据)

**数据量**: 1328 条记录

---

### 5. 其他集合

| 集合名 | 数据量 | 说明 |
|--------|--------|------|
| index_daily_quotes | 36372 | 大盘指数日线数据 |
| analysis_reports | 2133 | 分析报告 |
| analysis_tasks | 2256 | 分析任务 |
| token_usage | 32987 | API调用记录 |
| market_quotes | 5830 | 实时行情 |
| notifications | 1000 | 通知消息 |
| operation_logs | 1858 | 操作日志 |
| model_catalog | 10 | 模型配置 |
| llm_providers | 10 | LLM提供商配置 |
| system_configs | 32 | 系统配置 |

---

## 常用查询模板

### 批量获取股票列表
```python
# 获取所有A股
stocks = db.stock_basic_info.find({})
stock_list = [(s['code'], s['name'], s['industry']) for s in stocks]

# 筛选有足够历史数据的股票
min_days = 250
for stock in stock_list:
    code = stock[0]
    count = db.stock_daily_quotes.count_documents({
        "symbol": code,
        "period": "daily"
    })
    if count >= min_days:
        print(f"{code}: {count}天数据")
```

### 获取历史数据并计算指标
```python
import pandas as pd
from datetime import datetime, timedelta

# 获取数据
start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
cursor = db.stock_daily_quotes.find({
    "symbol": "000001",
    "period": "daily",
    "trade_date": {"$gte": start_date}
}).sort("trade_date", 1)
data = list(cursor)
df = pd.DataFrame(data)

# 计算均线
df['ma250'] = df['close'].rolling(250).mean()  # 年线
df['ma20'] = df['close'].rolling(20).mean()

# 计算波动率
df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
volatility = df['pct_chg'].std()
```

### 按市值筛选
```python
# 筛选大市值股票(>100亿)
large_caps = db.stock_basic_info.find({
    "total_mv": {"$gte": 100}  # 单位是亿元
})
```

---

## 数据源

- 主要数据源: `tushare`
- 其他数据源: `akshare`, `baostock`

---

## 索引建议

```python
# 创建索引（如果尚未创建）
db.stock_basic_info.create_index("code", unique=True)
db.stock_basic_info.create_index("symbol")
db.stock_basic_info.create_index("industry")
db.stock_daily_quotes.create_index(["symbol", "trade_date"])
db.stock_daily_quotes.create_index(["symbol", "period", "data_source"])
db.stock_financial_data.create_index(["code", "report_period"])

# 指数数据索引
db.index_daily_quotes.create_index([("symbol", 1), ("trade_date", -1)], unique=True)
db.index_daily_quotes.create_index([("trade_date", -1)])
```

---

## 数据同步脚本

| 脚本 | 用途 | 命令 |
|------|------|------|
| `scripts/sync_index_data.py` | 同步大盘指数数据 | `python scripts/sync_index_data.py` |
| `scripts/sync_financial_data.py` | 同步财务数据 | `python scripts/sync_financial_data.py` |
| `scripts/data_sync.py` | 同步股票行情 | `python scripts/data_sync.py` |