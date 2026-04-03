# tencent-deepseek 配置问题总结与修复方案

## 问题总结

### 1. 症状
- 用户添加了 "tencent-deepseek" 厂家，配置了 `default_base_url: https://api.lkeap.cloud.tencent.com/v1`
- 添加了 "deepseek-v3.2" 模型，配置了 `provider: tencent-deepseek` 和 `api_base: https://api.lkeap.cloud.tencent.com/v1`
- 前端使用该模型进行分析时，后端错误地调用了 `https://api.deepseek.com/chat/completions`
- 错误信息：`Model Not Exist` (因为腾讯云API不认识 deepseek-v3.2 模型)

### 2. 根本原因分析

#### 2.1 数据库配置 ✅ 正确
- ✅ `tencent-deepseek` 厂家配置正确
- ✅ `deepseek-v3.2` 模型配置正确 (`provider: tencent-deepseek`, `api_base` 正确)
- ✅ `get_provider_and_url_by_model_sync()` 返回正确结果

#### 2.2 问题可能出现在 LLM 实例化阶段

经过分析，问题可能出现在以下位置：

1. **`tradingagents/graph/trading_graph.py` 中的 `create_llm_by_provider()` 函数**
   - 第93-106行：专门处理 `provider.lower() == "deepseek"` 的情况
   - 如果 `provider` 是 `"tencent-deepseek"`，`provider.lower()` 是 `"tencent-deepseek"`，不等于 `"deepseek"`
   - 所以应该不会进入这个分支

2. **`deepseek_adapter.py` 硬编码 `base_url`**
   - `ChatDeepSeek.__init__()` 方法中：`base_url: str = "https://api.deepseek.com"`
   - 即使传入 `backend_url` 参数，也可能被默认值覆盖

3. **可能的供应商名称处理问题**
   - 如果代码中有去除 `"tencent-"` 前缀的逻辑，`"tencent-deepseek"` 会变成 `"deepseek"`
   - 目前没有找到这样的代码，但不能排除可能性

### 3. 关键发现

#### 3.1 测试结果
运行 `test_tencent_deepseek_config.py` 显示：
- ✅ 数据库配置正确
- ✅ `get_provider_and_url_by_model_sync("deepseek-v3.2")` 返回：
  ```json
  {
    "provider": "tencent-deepseek",
    "backend_url": "https://api.lkeap.cloud.tencent.com/v1",
    "api_key": "已设置"
  }
  ```

#### 3.2 诊断结果
运行 `diagnose_tencent_deepseek.py` 发现：
- `"tencent-deepseek".lower() == "deepseek"` 为 `False`
- 去除 `"tencent-"` 前缀后：`"deepseek" == "deepseek"` 为 `True`
- 如果代码中有去除前缀的逻辑，会导致错误识别

## 修复方案

### 方案1: 更新 `trading_graph.py` 中的 `create_llm_by_provider()` 函数

**文件**: `tradingagents/graph/trading_graph.py`
**位置**: 第93-106行

**问题**: `deepseek` 分支硬编码使用 `ChatDeepSeek` 适配器

**修复**: 为 `tencent-deepseek` 添加专门的处理逻辑，或修改现有逻辑：

```python
elif provider.lower() in ["deepseek", "tencent-deepseek"]:
    # 优先使用传入的 API Key，否则从环境变量读取
    api_key_env_var = "DEEPSEEK_API_KEY" if provider.lower() == "deepseek" else "TENCENT_DEEPSEEK_API_KEY"
    deepseek_api_key = api_key or os.getenv(api_key_env_var)

    if not deepseek_api_key:
        raise ValueError(f"使用{provider}需要设置{api_key_env_var}环境变量或在数据库中配置API Key")

    return ChatOpenAI(  # 使用 ChatOpenAI 而不是 ChatDeepSeek
        model=model,
        base_url=backend_url,  # 使用传入的 backend_url
        api_key=deepseek_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout
    )
```

### 方案2: 修改 `deepseek_adapter.py` 以支持自定义 `base_url`

**文件**: `tradingagents/llm_adapters/deepseek_adapter.py`
**位置**: 第42行

**修复**: 确保传入的 `base_url` 参数不被默认值覆盖

```python
def __init__(
    self,
    model: str = "deepseek-chat",
    api_key: Optional[str] = None,
    base_url: str = "https://api.deepseek.com",  # 默认值
    temperature: float = 0.1,
    max_tokens: Optional[int] = None,
    **kwargs
):
    # ... 现有代码 ...

    # 确保如果传入了 base_url，不使用默认值
    actual_base_url = kwargs.get('base_url', base_url)

    super().__init__(
        model=model,
        openai_api_key=api_key,
        openai_api_base=actual_base_url,  # 使用实际值
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs
    )
```

### 方案3: 将 `tencent-deepseek` 添加到 OpenAI 兼容供应商列表

**文件**: `tradingagents/llm_adapters/openai_compatible_base.py`
**位置**: 第431-497行

**修复**: 在 `OPENAI_COMPATIBLE_PROVIDERS` 中添加 `tencent-deepseek`

```python
OPENAI_COMPATIBLE_PROVIDERS = {
    # ... 现有配置 ...
    "tencent-deepseek": {
        "adapter_class": ChatCustomOpenAI,  # 使用自定义 OpenAI 适配器
        "base_url": "https://api.lkeap.cloud.tencent.com/v1",  # 默认值
        "api_key_env": "TENCENT_DEEPSEEK_API_KEY",
        "models": {
            "deepseek-v3.2": {"context_length": 32768, "supports_function_calling": True},
            "deepseek-chat": {"context_length": 32768, "supports_function_calling": True}
        }
    }
}
```

### 方案4: 更新默认映射表

**文件**: `app/services/simple_analysis_service.py`
**位置**:
- 第331-369行: `_get_default_provider_by_model()` 函数
- 第305-328行: `_get_default_backend_url()` 函数

**修复**: 添加 `tencent-deepseek` 和 `deepseek-v3.2` 的映射

```python
# 在 _get_default_provider_by_model() 中添加
model_provider_map = {
    # ... 现有映射 ...
    'deepseek-v3.2': 'tencent-deepseek',
    'deepseek-chat': 'deepseek',
    'deepseek-coder': 'deepseek',
}

# 在 _get_default_backend_url() 中添加
default_urls = {
    # ... 现有映射 ...
    "tencent-deepseek": "https://api.lkeap.cloud.tencent.com/v1",
}
```

## 推荐修复步骤

### 步骤1: 立即修复（最简单）
1. 在 `trading_graph.py` 中为 `tencent-deepseek` 添加特殊处理
2. 确保使用 `ChatOpenAI` 而不是 `ChatDeepSeek`

### 步骤2: 中期修复（更系统）
1. 将 `tencent-deepseek` 添加到 `OPENAI_COMPATIBLE_PROVIDERS`
2. 更新默认映射表
3. 确保 `create_llm_by_provider()` 使用 `create_openai_compatible_llm()` 函数

### 步骤3: 长期修复（最完整）
1. 重构 `create_llm_by_provider()` 函数，统一使用 `create_openai_compatible_llm()`
2. 移除硬编码的供应商处理逻辑
3. 所有供应商都通过 `OPENAI_COMPATIBLE_PROVIDERS` 配置

## 测试验证

修复后需要验证：
1. 使用 `tencent-deepseek` 厂家和 `deepseek-v3.2` 模型进行分析
2. 检查日志确认使用了正确的 API URL
3. 验证 API 调用成功

## 相关文件

1. `tradingagents/graph/trading_graph.py` - LLM 实例化逻辑
2. `tradingagents/llm_adapters/deepseek_adapter.py` - DeepSeek 适配器
3. `tradingagents/llm_adapters/openai_compatible_base.py` - OpenAI 兼容适配器
4. `app/services/simple_analysis_service.py` - 配置查询逻辑
5. `fyy_test_folder/` - 测试和诊断工具

## 时间线

- **2026-02-25**: 发现问题，创建诊断工具
- **下一步**: 实施修复方案1（立即修复）
- **后续**: 实施修复方案2和3（系统化修复）