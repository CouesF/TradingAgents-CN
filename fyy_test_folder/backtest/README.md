# TradingAgents 回测系统

基于多智能体的股票分析回测系统。

## 虚拟环境

本项目使用虚拟环境，请先激活：

```bash
# 进入项目根目录
cd /home/fungle/Project/TradingAgents-CN

# 激活虚拟环境
source venv/bin/activate

# 确认Python版本 (Python 3.11.14)
python --version
```

**虚拟环境路径**: `/home/fungle/Project/TradingAgents-CN/venv`

**已安装的关键包**:
- akshare 1.17.94 (A股数据)
- yfinance 0.2.66 (美股/港股数据)
- langchain-core 1.2.0
- langgraph 1.0.5
- dashscope 1.25.3
- pandas 2.3.3

## LLM 提供商配置

### 腾讯云 DeepSeek (默认推荐)

本项目默认使用腾讯云 DeepSeek API：

| 配置项 | 值 |
|--------|-----|
| Provider | `tencent-deepseek` |
| 模型 | `deepseek-v3.2` |
| API Base | `https://api.lkeap.cloud.tencent.com/v1` |

使用方式：
```bash
# 默认使用腾讯云DeepSeek
python fyy_test_folder/backtest/run_backtest.py -s 601318 -st 2026-01-30 -e 2026-03-30 --days tuesday friday

# 或明确指定
python fyy_test_folder/backtest/run_backtest.py -s 601318 -st 2026-01-30 -e 2026-03-30 -p tencent-deepseek
```

### 其他提供商

| Provider | 模型 | 说明 |
|----------|------|------|
| `tencent-deepseek` | deepseek-v3.2 | 腾讯云DeepSeek (默认) |
| `dashscope` | qwen-plus/qwen-turbo | 阿里云百炼 |
| `google` | gemini-2.0-flash | Google AI |
| `deepseek` | deepseek-chat | DeepSeek官方 |

## 目录结构

```
fyy_test_folder/backtest/
├── trading_calendar.py   # 交易日历（排除节假日）
├── run_backtest.py       # 主回测脚本
├── results/              # 回测结果存储
└── README.md             # 本文档
```

## 功能特点

1. **真实交易日历** - 自动排除周末和节假日，避免浪费API调用
2. **多市场支持** - 支持A股、港股、美股
3. **多智能体分析** - 使用 TradingAgentsGraph 进行全面分析
4. **投资组合模拟** - 模拟买卖并计算收益率

## 使用方法

### 激活环境并运行

```bash
# 1. 激活虚拟环境
cd /home/fungle/Project/TradingAgents-CN
source venv/bin/activate

# 2. 运行回测
# A股回测（每7个交易日分析一次）
python fyy_test_folder/backtest/run_backtest.py --stock 600036 --start 2024-01-01 --end 2024-03-01

# 美股回测
python fyy_test_folder/backtest/run_backtest.py --stock AAPL --start 2024-01-01 --end 2024-03-01

# 自定义参数
python fyy_test_folder/backtest/run_backtest.py \
    --stock 000001 \
    --start 2024-01-01 \
    --end 2024-06-01 \
    --interval 5 \
    --capital 500000 \
    --provider dashscope
```

### 参数说明

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| --stock | -s | 股票代码 | 必需 |
| --start | -st | 开始日期 (YYYY-MM-DD) | 必需 |
| --end | -e | 结束日期 (YYYY-MM-DD) | 必需 |
| --interval | -i | 分析间隔（每N个交易日） | 7 |
| --days | -D | 仅分析指定星期（如: `wednesday friday`） | 无 |
| --capital | -c | 初始资金 | 100000 |
| --provider | -p | LLM提供商 (tencent-deepseek/dashscope/google/deepseek) | tencent-deepseek |
| --output | -o | 输出文件路径 | 自动生成 |
| --debug | -d | 调试模式 | False |

### 星期过滤器 (--days)

使用 `--days` 参数可以仅在指定星期进行分析，节省API调用成本：

```bash
# 仅在周三和周五分析
python fyy_test_folder/backtest/run_backtest.py \
    -s 600036 -st 2024-01-01 -e 2024-03-01 \
    --days wednesday friday

# 仅在周一分析
python fyy_test_folder/backtest/run_backtest.py \
    -s 600036 -st 2024-01-01 -e 2024-03-01 \
    --days monday

# 支持简写
python fyy_test_folder/backtest/run_backtest.py \
    -s 600036 -st 2024-01-01 -e 2024-03-01 \
    --days wed fri
```

**支持的星期名称**:
- 完整名称: `monday`, `tuesday`, `wednesday`, `thursday`, `friday`
- 简写: `mon`, `tue`, `wed`, `thu`, `fri`

### 股票代码格式

- **A股**: 6位数字，如 `600036`, `000001`
- **港股**: 数字.HK 或 5位数字，如 `0700.HK`, `00700`
- **美股**: 股票代码，如 `AAPL`, `NVDA`

## 工作流程

```
1. 获取真实交易日（排除节假日）
        ↓
2. 按间隔筛选分析日期
        ↓
3. 对每个日期调用 TradingAgentsGraph.propagate()
        ↓
4. 获取决策: 买入/持有/卖出
        ↓
5. 模拟投资组合
        ↓
6. 计算收益指标（收益率、最大回撤、胜率）
        ↓
7. 保存报告到 results/
```

## 输出示例

```
================================================================================
TradingAgents 回测
================================================================================
股票: 600036 (china)
区间: 2024-01-01 ~ 2024-03-01
间隔: 每7个交易日
资金: ¥100,000.00

[A股交易日历] 2024-01-01 ~ 2024-03-01: 40 个交易日
将分析 6 个时间点

[1/6]
============================================================
分析 600036 @ 2024-01-02
============================================================
决策: 买入
目标价: 35.5
置信度: 0.75
风险评分: 0.40

...

============================================================
回测结果
============================================================
总收益率: 8.52%
最大回撤: 3.21%
胜率: 66.67%
交易次数: 3
```

## 结果文件

回测结果保存在 `results/` 目录：

```
results/
└── backtest_600036_20240315_143052.json
```

JSON 内容包括：
- 分析配置
- 每日决策明细
- 投资组合模拟结果
- 交易记录

## 注意事项

1. **API 调用成本**: 每次分析会调用 LLM API，请控制分析间隔
2. **节假日处理**: A股使用 AKShare 获取真实交易日，美股/港股需要安装 `pandas_market_calendars`
3. **环境变量**: 确保 `.env` 文件中配置了对应的 API Key

## 快速测试

```bash
# 进入项目目录并激活虚拟环境
cd /home/fungle/Project/TradingAgents-CN
source venv/bin/activate

# 测试交易日历
python fyy_test_folder/backtest/trading_calendar.py

# 运行回测示例（使用腾讯云DeepSeek，仅周二周五分析）
python fyy_test_folder/backtest/run_backtest.py \
    -s 601318 \
    -st 2026-01-30 \
    -e 2026-03-30 \
    --days tuesday friday \
    -p tencent-deepseek
```

## 完整示例

```bash
# 完整流程示例
cd /home/fungle/Project/TradingAgents-CN

# 1. 激活虚拟环境
source venv/bin/activate

# 2. 确认环境
python --version  # 应显示 Python 3.11.14

# 3. 运行回测（使用腾讯云DeepSeek）
python fyy_test_folder/backtest/run_backtest.py \
    -s 601318 \
    -st 2026-01-30 \
    -e 2026-03-30 \
    --days tue fri \
    -c 100000

# 4. 查看结果
ls fyy_test_folder/backtest/results/
```