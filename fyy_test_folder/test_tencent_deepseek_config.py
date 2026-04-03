#!/usr/bin/env python3
"""
测试 tencent-deepseek 配置问题

问题描述：
用户通过前端添加了 "tencent-deepseek" 厂家，并在模型目录中添加了 "deepseek-v3.2" 模型，
在大模型配置页面配置了 API URL https://api.lkeap.cloud.tencent.com/v1。

但在前端分析使用新建的这个模型的时候，没有用到这里配置的 API base URL，
而是使用了默认的 deepseek URL，后端报错显示使用了 https://api.deepseek.com/chat/completions。

这个脚本用于测试和诊断问题。
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from pymongo import MongoClient
from app.core.config import settings
from app.services.simple_analysis_service import (
    get_provider_and_url_by_model_sync,
    _get_default_provider_by_model,
    _get_default_backend_url
)
import json

# 连接 MongoDB
client = MongoClient(settings.MONGO_URI)
db = client[settings.MONGO_DB]

def test_database_config():
    """测试数据库中的配置"""
    print("=" * 80)
    print("测试数据库配置")
    print("=" * 80)

    # 1. 检查 tencent-deepseek 厂家配置
    providers_collection = db.llm_providers
    tencent_provider = providers_collection.find_one({"name": "tencent-deepseek"})

    if tencent_provider:
        print(f"✅ 找到 tencent-deepseek 厂家配置")
        print(f"  - default_base_url: {tencent_provider.get('default_base_url')}")
        print(f"  - api_key: {'已设置' if tencent_provider.get('api_key') else '未设置'}")
    else:
        print("❌ 未找到 tencent-deepseek 厂家配置")

    # 2. 检查 deepseek 厂家配置（默认）
    deepseek_provider = providers_collection.find_one({"name": "deepseek"})
    if deepseek_provider:
        print(f"\n✅ 找到 deepseek 厂家配置（默认）")
        print(f"  - default_base_url: {deepseek_provider.get('default_base_url')}")
    else:
        print("\n❌ 未找到 deepseek 厂家配置")

    # 3. 检查 system_configs 中的模型配置
    configs_collection = db.system_configs
    doc = configs_collection.find_one({"is_active": True}, sort=[("version", -1)])

    if doc and "llm_configs" in doc:
        llm_configs = doc["llm_configs"]

        # 查找 deepseek-v3.2 配置
        deepseek_configs = [c for c in llm_configs if c.get('model_name') == 'deepseek-v3.2']

        if deepseek_configs:
            print(f"\n✅ 找到 {len(deepseek_configs)} 个 deepseek-v3.2 模型配置")
            for i, config in enumerate(deepseek_configs, 1):
                print(f"\n配置 {i}:")
                print(f"  - provider: {config.get('provider')}")
                print(f"  - api_base: {config.get('api_base')}")
                print(f"  - api_key: {'已设置' if config.get('api_key') else '未设置'}")

                # 检查 provider 字段
                provider = config.get('provider')
                if provider == 'tencent-deepseek':
                    print(f"  - ✅ provider 正确设置为 'tencent-deepseek'")
                elif provider == 'deepseek':
                    print(f"  - ⚠️  provider 设置为 'deepseek'，这可能导致使用错误的厂家配置")
                else:
                    print(f"  - ⚠️  provider 设置为 '{provider}'，不是期望的 'tencent-deepseek'")
        else:
            print(f"\n❌ 未找到 deepseek-v3.2 模型配置")
            # 列出所有模型名称
            model_names = sorted(list(set([c.get('model_name') for c in llm_configs])))
            print(f"所有可用的模型名称: {model_names}")

def test_config_logic():
    """测试配置逻辑"""
    print("\n" + "=" * 80)
    print("测试配置逻辑")
    print("=" * 80)

    model_name = "deepseek-v3.2"

    print(f"测试模型: {model_name}")

    # 1. 测试默认提供商映射
    default_provider = _get_default_provider_by_model(model_name)
    print(f"1. 默认提供商映射: {model_name} -> {default_provider}")

    # 2. 测试默认 backend_url
    default_url = _get_default_backend_url(default_provider)
    print(f"2. 默认 URL (对于 {default_provider}): {default_url}")

    # 3. 测试实际的查询函数
    print(f"3. 调用 get_provider_and_url_by_model_sync('{model_name}')...")
    try:
        result = get_provider_and_url_by_model_sync(model_name)
        print(f"✅ 查询结果:")
        print(f"   - provider: {result.get('provider')}")
        print(f"   - backend_url: {result.get('backend_url')}")
        print(f"   - api_key: {'已设置' if result.get('api_key') else '未设置'}")

        # 检查 backend_url
        backend_url = result.get('backend_url')
        if backend_url == "https://api.lkeap.cloud.tencent.com/v1":
            print(f"   - ✅ backend_url 正确配置为腾讯云地址")
        elif backend_url == "https://api.deepseek.com":
            print(f"   - ❌ backend_url 错误地使用了默认 deepseek 地址")
        else:
            print(f"   - ⚠️  backend_url 为其他地址: {backend_url}")

    except Exception as e:
        print(f"❌ 查询失败: {e}")

def fix_suggestions():
    """提供修复建议"""
    print("\n" + "=" * 80)
    print("修复建议")
    print("=" * 80)

    print("1. 检查 deepseek-v3.2 模型的 provider 字段:")
    print("   - 应该设置为 'tencent-deepseek' 而不是 'deepseek'")
    print("   - 检查 system_configs.llm_configs 中的 provider 字段")

    print("\n2. 如果 provider 正确，检查模型配置中的 api_base 字段:")
    print("   - 可以直接在模型配置中设置 api_base: https://api.lkeap.cloud.tencent.com/v1")

    print("\n3. 如果模型配置中没有 api_base，检查 tencent-deepseek 厂家的 default_base_url:")
    print("   - 应该设置为 https://api.lkeap.cloud.tencent.com/v1")

    print("\n4. 检查配置优先级逻辑:")
    print("   - 优先级1: 模型配置中的 api_base")
    print("   - 优先级2: 厂家配置中的 default_base_url")
    print("   - 优先级3: 硬编码的默认 URL")

    print("\n5. 可能的修复方法:")
    print("   a) 更新模型配置中的 provider 为 'tencent-deepseek'")
    print("   b) 在模型配置中直接设置 api_base")
    print("   c) 确保 tencent-deepseek 厂家的 default_base_url 正确")

def main():
    """主函数"""
    print("开始测试 tencent-deepseek 配置问题")
    print("=" * 80)

    test_database_config()
    test_config_logic()
    fix_suggestions()

    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)

    client.close()

if __name__ == "__main__":
    main()