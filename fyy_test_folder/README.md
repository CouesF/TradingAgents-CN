# fyy_test_folder

此文件夹包含由 fyy 创建的测试文件和配置，用于测试和调试 TradingAgents-CN 项目。

## 文件说明

### 1. `test_tencent_deepseek_config.py`
用于测试 tencent-deepseek 模型配置问题的脚本。检查：
- deepseek-v3.2 模型配置是否正确
- tencent-deepseek 厂家配置是否存在
- API base URL 是否正确配置

### 2. `query_deepseek_config.py`
查询 deepseek-v3.2 配置的脚本，用于诊断为什么前端配置的API URL没有生效。

### 3. `debug_llm_provider_flow.md`
记录LLM提供商配置流程的调试过程。

## 问题背景

用户通过前端添加了 "tencent-deepseek" 厂家，并在模型目录中添加了 "deepseek-v3.2" 模型，在大模型配置页面配置了 API URL `https://api.lkeap.cloud.tencent.com/v1`。

但在前端分析使用新建的这个模型的时候，没有用到这里配置的 API base URL，而是使用了默认的 deepseek URL，后端报错显示使用了 `https://api.deepseek.com/chat/completions`。

## 调试步骤

1. 检查数据库中的配置：
   - `llm_providers` 集合中的 `tencent-deepseek` 厂家配置
   - `system_configs.llm_configs` 中的 `deepseek-v3.2` 模型配置

2. 检查配置优先级逻辑：
   - 模型配置中的 `api_base` 字段
   - 厂家配置中的 `default_base_url` 字段
   - 硬编码的默认 URL

3. 检查 `get_provider_and_url_by_model_sync()` 函数逻辑

## 相关文件

- `app/services/simple_analysis_service.py` - 包含 `get_provider_and_url_by_model_sync()` 函数
- `app/models/config.py` - 包含配置模型定义
- `tradingagents/llm_adapters/openai_compatible_base.py` - 包含DeepSeek适配器配置

## 运行测试

```bash
# 激活虚拟环境
source venv/bin/activate

# 运行配置查询脚本
python3 fyy_test_folder/query_deepseek_config.py

# 运行测试脚本
python3 fyy_test_folder/test_tencent_deepseek_config.py
```