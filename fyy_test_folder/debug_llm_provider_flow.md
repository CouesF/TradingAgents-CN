# LLM 提供商配置流程调试记录

## 问题描述

**时间**: 2026-02-25
**报告人**: fyy
**问题**: tencent-deepseek 配置的 API URL 未生效

### 症状
1. 用户通过前端添加了 "tencent-deepseek" 厂家
2. 在模型目录中添加了 "deepseek-v3.2" 模型
3. 在大模型配置页面配置了 API URL: `https://api.lkeap.cloud.tencent.com/v1`
4. 在前端分析使用新建的这个模型的时候，没有用到这里配置的 API base URL
5. 后端报错显示使用了 `https://api.deepseek.com/chat/completions`

### 错误日志
```
2026-02-25 00:50:55,843 | httpx                | INFO | HTTP Request: POST https://api.deepseek.com/chat/completions "HTTP/1.1 400 Bad Request"
2026-02-25 00:50:55,843 | llm_adapters         | ERROR | ❌ [DeepSeek] 调用失败: Error code: 400 - {'error': {'message': 'Model Not Exist', 'type': 'invalid_request_error', 'param': None, 'code': 'invalid_request_error'}}
```

## 调试过程

### 1. 检查代码逻辑

#### 1.1 配置优先级逻辑
文件: `app/services/simple_analysis_service.py`

函数 `get_provider_and_url_by_model_sync()` 中的优先级逻辑:

```python
# 确定 backend_url
backend_url = None
if api_base:
    backend_url = api_base
    logger.info(f"✅ [同步查询] 模型 {model_name} 使用自定义 API: {api_base}")
elif provider_doc and provider_doc.get("default_base_url"):
    backend_url = provider_doc["default_base_url"]
    logger.info(f"✅ [同步查询] 模型 {model_name} 使用厂家默认 API: {backend_url}")
else:
    backend_url = _get_default_backend_url(provider)
    logger.warning(f"⚠️ [同步查询] 厂家 {provider} 没有配置 default_base_url，使用硬编码默认值")
```

优先级:
1. **模型配置中的 `api_base`** - 最高优先级
2. **厂家配置中的 `default_base_url`** - 次优先级
3. **硬编码的默认 URL** - 最低优先级

#### 1.2 默认 URL 映射
函数 `_get_default_backend_url()`:

```python
default_urls = {
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "dashscope": "https://dashscope.aliyuncs.com/api/v1",
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",  # DeepSeek的默认URL
    "anthropic": "https://api.anthropic.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "qianfan": "https://qianfan.baidubce.com/v2",
    "302ai": "https://api.302.ai/v1",
}
```

注意: `tencent-deepseek` 不在这个映射中，所以会使用默认值 `"https://dashscope.aliyuncs.com/compatible-mode/v1"`

#### 1.3 默认提供商映射
函数 `_get_default_provider_by_model()`:

```python
model_provider_map = {
    # DeepSeek
    'deepseek-chat': 'deepseek',
    'deepseek-coder': 'deepseek',
}
```

注意: `deepseek-v3.2` 不在这个映射中，所以会使用默认值 `'dashscope'`

### 2. 检查数据库配置

运行 `scripts/check_llm_providers.py` 结果:

```
厂家: tencent-deepseek
  display_name: tencent-deepseek
  default_base_url: https://api.lkeap.cloud.tencent.com/v1
  api_key_env: None
  enabled: None
```

✅ tencent-deepseek 厂家配置存在，且 `default_base_url` 正确

### 3. 问题分析

#### 3.1 可能的原因

1. **模型配置中的 provider 字段不正确**
   - 如果 `deepseek-v3.2` 的 `provider` 字段是 `'deepseek'` 而不是 `'tencent-deepseek'`，则会使用错误的厂家配置

2. **模型配置中没有设置 api_base**
   - 即使 provider 正确，如果没有设置 `api_base`，需要依赖厂家的 `default_base_url`

3. **配置查询逻辑问题**
   - `get_provider_and_url_by_model_sync()` 函数可能没有正确查询到配置

#### 3.2 关键检查点

1. 检查 `deepseek-v3.2` 模型配置中的 `provider` 字段
2. 检查 `deepseek-v3.2` 模型配置中的 `api_base` 字段
3. 检查配置查询函数的日志输出

### 4. 测试脚本

创建了测试脚本 `test_tencent_deepseek_config.py` 来诊断问题:

1. **test_database_config()** - 检查数据库配置
2. **test_config_logic()** - 测试配置逻辑
3. **fix_suggestions()** - 提供修复建议

### 5. 运行测试

```bash
# 激活虚拟环境
source venv/bin/activate

# 运行测试
python3 fyy_test_folder/test_tencent_deepseek_config.py
```

## 修复方案

### 方案1: 更新模型配置的 provider 字段

如果 `deepseek-v3.2` 的 `provider` 字段是 `'deepseek'`，需要更新为 `'tencent-deepseek'`:

```python
# 更新 system_configs.llm_configs 中的 deepseek-v3.2 配置
db.system_configs.update_one(
    {"is_active": True, "llm_configs.model_name": "deepseek-v3.2"},
    {"$set": {"llm_configs.$.provider": "tencent-deepseek"}}
)
```

### 方案2: 在模型配置中直接设置 api_base

在模型配置中添加 `api_base` 字段:

```python
# 更新 system_configs.llm_configs 中的 deepseek-v3.2 配置
db.system_configs.update_one(
    {"is_active": True, "llm_configs.model_name": "deepseek-v3.2"},
    {"$set": {"llm_configs.$.api_base": "https://api.lkeap.cloud.tencent.com/v1"}}
)
```

### 方案3: 添加默认映射

在 `_get_default_provider_by_model()` 函数中添加映射:

```python
# 添加 deepseek-v3.2 到 tencent-deepseek 的映射
model_provider_map = {
    # ... 现有映射
    'deepseek-v3.2': 'tencent-deepseek',
}
```

在 `_get_default_backend_url()` 函数中添加映射:

```python
default_urls = {
    # ... 现有映射
    "tencent-deepseek": "https://api.lkeap.cloud.tencent.com/v1",
}
```

## 预防措施

1. **前端验证**: 在添加模型时，确保 provider 字段正确
2. **配置检查**: 添加配置验证功能，检查模型和厂家的对应关系
3. **更详细的日志**: 在 `get_provider_and_url_by_model_sync()` 中添加更详细的日志输出
4. **默认映射更新**: 及时更新默认映射表，支持新的厂家和模型

## 相关文件

- `app/services/simple_analysis_service.py` - 配置查询逻辑
- `app/models/config.py` - 配置模型定义
- `tradingagents/llm_adapters/openai_compatible_base.py` - LLM适配器
- `fyy_test_folder/test_tencent_deepseek_config.py` - 测试脚本
- `fyy_test_folder/query_deepseek_config.py` - 配置查询脚本