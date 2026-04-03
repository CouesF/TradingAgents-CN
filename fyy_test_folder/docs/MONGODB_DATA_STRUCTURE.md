# MongoDB 数据结构文档 (fyy_test_folder)

> **最后更新**: 2026-04-02
> **数据量**: stock_basic_info: 5502条, stock_daily_quotes: 1557万条, index_daily_quotes: 3.6万条

## 概述

本项目使用 MongoDB 存储股票和指数数据，主要集合包括：

| 集合名 | 数据量 | 说明 |
|--------|--------|------|
| stock_basic_info | 5502 | 股票基础信息 |
| stock_daily_quotes | 1557万 | 股票日线行情 |
| index_daily_quotes | 3.6万 | 大盘指数日线数据 |
| stock_financial_data | 6205 | 财务数据 |

---

## 连接方式

```python
from tradingagents.config.database_manager import get_database_manager

db_manager = get_database_manager()
db = db_manager.get_mongodb_db()  # 返回 tradingagents 数据库
```

---

## 核心集合结构

### 1. stock_basic_info (股票基础信息)

```json
{
  "code": "000001",
  "symbol": "000001",
  "full_symbol": "000001.SZ",
  "name": "平安银行",
  "industry": "银行",
  "market": "主板",
  "total_mv": 2132.71,      // 总市值(亿元)
  "circ_mv": 2132.67,       // 流通市值(亿元)
  "pe": 5.0025,             // 市盈率
  "pb": 0.4726,             // 市净率
  "roe": 7.5711             // ROE(%)
}
```

**注意**: 指数数据不在该集合中，见 `index_daily_quotes`

---

### 2. stock_daily_quotes (股票日线行情)

```json
{
  "symbol": "601668",
  "trade_date": "2024-12-11",   // 格式: YYYY-MM-DD
  "open": 5.85,
  "high": 5.9,
  "low": 5.83,
  "close": 5.84,
  "volume": 160963963.0,        // 成交量(股)
  "amount": 986332147.0,        // 成交额(元)
  "pct_chg": -0.1709            // 涨跌幅(%)
}
```

**注意**:
- `trade_date` 格式是 `YYYY-MM-DD`
- 代码 `000001` 在此集合中是平安银行，不是上证指数

---

### 3. index_daily_quotes (指数日线行情)

**用途**: 存储大盘指数日线数据，独立于股票数据避免代码冲突

**支持指数**:

| 指数名称 | 代码 | 数据起始 | 记录数 | 最新收盘 |
|---------|------|---------|--------|---------|
| 上证指数 | 000001 | 2000年 | 6358 | 3948.55 |
| 深证成指 | 399001 | 2000年 | 6358 | 13706.52 |
| 创业板指 | 399006 | 2010年 | 3844 | 3247.52 |
| 沪深300 | 000300 | 2005年 | 5879 | 4526.07 |
| 中证500 | 000905 | 2005年 | 5158 | 7750.09 |
| 中证1000 | 000852 | 2014年 | 2785 | 7764.15 |
| 上证50 | 000016 | 2004年 | 5401 | 2878.79 |
| 北证50 | 899050 | 2022年 | 949 | 1278.12 |

**字段结构**:

```json
{
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
  "data_source": "akshare"
}
```

**关键字段**:

| 字段 | 类型 | 说明 |
|------|------|------|
| symbol | str(6位) | 指数代码，如 "000001"(上证指数) |
| name | str | 指数名称 |
| exchange | str | 交易所：SSE/SZSE/BSE |
| trade_date | str | 交易日期，格式YYYY-MM-DD |
| close | float | 收盘价 |

**代码冲突说明**:

| 代码 | stock_daily_quotes | index_daily_quotes |
|------|-------------------|-------------------|
| 000001 | 平安银行 | 上证指数 |
| 000016 | 深康佳A | 上证50 |
| 000905 | 厦门港务 | 中证500 |

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

---

## 数据同步脚本

### 同步指数数据

```bash
cd ~/Project/TradingAgents-CN && source venv/bin/activate

# 同步所有指数
python fyy_test_folder/scripts/sync_index_data.py

# 同步指定指数
python fyy_test_folder/scripts/sync_index_data.py --index sh   # 上证指数
python fyy_test_folder/scripts/sync_index_data.py --index sz   # 深证成指
python fyy_test_folder/scripts/sync_index_data.py --index cyb  # 创业板指

# 查看数据状态
python fyy_test_folder/scripts/sync_index_data.py --status

# 全量更新
python fyy_test_folder/scripts/sync_index_data.py --full
```

### 指数代码对照表

| 参数 | 指数 | AKShare代码 |
|-----|------|------------|
| sh | 上证指数 | sh000001 |
| sz | 深证成指 | sz399001 |
| cyb | 创业板指 | sz399006 |
| hs300 | 沪深300 | sh000300 |
| zz500 | 中证500 | sh000905 |
| zz1000 | 中证1000 | sh000852 |
| sz50 | 上证50 | sh000016 |
| bz50 | 北证50 | bj899050 |

---

## 索引配置

```python
# 股票数据索引
db.stock_basic_info.create_index("code", unique=True)
db.stock_daily_quotes.create_index([("symbol", "trade_date")])

# 指数数据索引
db.index_daily_quotes.create_index([("symbol", 1), ("trade_date", -1)], unique=True)
db.index_daily_quotes.create_index([("trade_date", -1)])
```

---

## 常用查询

### 获取交易日历

```python
# 使用标杆股 000001 获取交易日
cursor = db.stock_daily_quotes.find(
    {"symbol": "000001", "trade_date": {"$gte": "2024-01-01"}},
    {"_id": 0, "trade_date": 1}
).sort("trade_date", 1)
trading_days = [doc["trade_date"] for doc in cursor]
```

### 获取多股票数据

```python
symbols = ["000001", "000002", "600000"]
cursor = db.stock_daily_quotes.find({
    "symbol": {"$in": symbols},
    "trade_date": {"$gte": "2024-01-01"}
}).sort("trade_date", 1)
```

### 计算Alpha/Beta

```python
# 策略收益
strategy_return = 1.9756  # 累计收益

# 指数收益
index_start = db.index_daily_quotes.find_one(
    {"symbol": "000001", "trade_date": "2024-04-01"}
)["close"]
index_end = db.index_daily_quotes.find_one(
    {"symbol": "000001", "trade_date": {"$regex": "2026-03-31|2026-04-01"}}
)["close"]
index_return = (index_end - index_start) / index_start

# Alpha = 策略收益 - Beta * 指数收益
```