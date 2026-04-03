# MongoDB 数据获取经验总结

## 问题：查询操作符失效

### 现象

在回测脚本中，使用MongoDB查询操作符（`$gte`, `$lte`, `$gt`, `$lt`, `$ne`, `$in`, `$regex`等）时，查询结果返回0条数据，即使数据库中存在符合条件的数据。

```python
# 这种查询返回0条
count = db.stock_daily_quotes.count_documents({
    'symbol': '920418',
    'trade_date': {'$gte': '2025-12-01', '$lte': '2026-03-31'}
})
# 结果: 0

# 这种查询也返回0条
count = db.stock_daily_quotes.count_documents({
    'symbol': '920418',
    'trade_date': {'$gt': '2025-11-30'}
})
# 结果: 0

# 甚至 $ne 也返回0条
count = db.stock_daily_quotes.count_documents({
    'symbol': '920418',
    'trade_date': {'$ne': '2020-07-27'}
})
# 结果: 0
```

### 验证

```python
# 但基础查询正常
count = db.stock_daily_quotes.count_documents({'symbol': '920418'})
# 结果: 1619 条

# find + sort 也正常
newest = db.stock_daily_quotes.find_one({'symbol': '920418'}, sort=[('trade_date', -1)])
# 结果: trade_date = '2026-03-31'

# 精确匹配也正常
doc = db.stock_daily_quotes.find_one({'trade_date': '2026-03-31'})
# 结果: 正常返回
```

### 根本原因

MongoDB查询操作符在某些配置或版本下可能失效。具体原因可能包括：
1. MongoDB服务器版本或配置问题
2. 集合索引问题
3. 字段类型不一致

## 解决方案：Python端过滤

### 推荐做法

获取全部数据后，在Python端进行过滤：

```python
from pymongo import MongoClient
import pandas as pd

client = MongoClient(settings.MONGO_URI)
db = client[settings.MONGO_DB]

# 1. 获取该股票的所有数据
cursor = db.stock_daily_quotes.find({'symbol': stock_id})
all_data = list(cursor)
client.close()

# 2. Python端过滤
filtered = [d for d in all_data
           if d.get('trade_date', '') >= start_date
           and d.get('trade_date', '') <= end_date]

# 3. 转换为DataFrame
if filtered:
    df = pd.DataFrame(filtered)
    df = df.rename(columns={'trade_date': 'date'})
    result = df[['date', 'open', 'close', 'high', 'low', 'volume']]
```

### 性能考虑

- 对于单只股票的数据量（通常<5000条），Python端过滤性能完全可以接受
- 避免了MongoDB查询操作符的兼容性问题
- 代码更清晰，易于调试

## 其他注意事项

### 1. 北交所股票代码前缀

AKShare获取北交所股票数据时，需要使用`bj`前缀：

```python
symbol = stock_id
if stock_id.isdigit():
    # 北交所股票: 92xxxx, 83xxxx, 87xxxx
    if stock_id.startswith('92') or stock_id.startswith('83') or stock_id.startswith('87'):
        symbol = f"bj{stock_id}"
    # 上海交易所
    elif stock_id.startswith('6'):
        symbol = f"sh{stock_id}"
    # 深圳交易所
    else:
        symbol = f"sz{stock_id}"
```

### 2. 连接管理

确保在使用完毕后关闭MongoDB连接：

```python
client = MongoClient(settings.MONGO_URI)
try:
    # 数据操作
    pass
finally:
    client.close()
```

### 3. 字段类型验证

在处理数据前验证字段类型：

```python
sample = db.stock_daily_quotes.find_one({'symbol': stock_id})
trade_date_type = type(sample.get('trade_date'))
# 确保是字符串类型，格式为 'YYYY-MM-DD'
```

## 影响范围

此问题影响以下场景：
1. 回测脚本的价格数据获取
2. 历史数据分析
3. 任何需要日期范围查询的操作

## 修复记录

- **日期**: 2026-04-01
- **修复文件**: `fyy_test_folder/backtest/run_backtest.py`
- **修复内容**: 将MongoDB查询操作符改为Python端过滤
- **影响**: 北交所股票(920xxx)回测现在可以正确获取价格数据