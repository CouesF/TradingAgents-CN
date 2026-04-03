#!/usr/bin/env python3
"""
查询 deepseek-v3.2 配置
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from pymongo import MongoClient
from app.core.config import settings
import json

# 连接 MongoDB
client = MongoClient(settings.MONGO_URI)
db = client[settings.MONGO_DB]

print("=" * 80)
print("查询 deepseek-v3.2 配置")
print("=" * 80)

# 1. 检查 system_configs.llm_configs
configs_collection = db.system_configs
doc = configs_collection.find_one({"is_active": True}, sort=[("version", -1)])

if doc and "llm_configs" in doc:
    llm_configs = doc["llm_configs"]
    print(f"\n总共有 {len(llm_configs)} 个模型配置")

    # 查找 deepseek-v3.2
    deepseek_configs = [c for c in llm_configs if c.get('model_name') == 'deepseek-v3.2']

    if deepseek_configs:
        print(f"\n✅ 找到 deepseek-v3.2 配置:")
        for config in deepseek_configs:
            print(json.dumps(config, indent=2, ensure_ascii=False))

        # 检查 provider 字段
        for config in deepseek_configs:
            provider = config.get('provider')
            api_base = config.get('api_base')
            print(f"\n🔍 配置分析:")
            print(f"  - provider: {provider}")
            print(f"  - api_base: {api_base}")
            print(f"  - 厂家配置中的 default_base_url 将用于 provider: {provider}")
    else:
        print(f"\n❌ 未找到 deepseek-v3.2 配置")
        # 列出所有模型名称
        model_names = [c.get('model_name') for c in llm_configs]
        print(f"\n所有模型名称: {model_names}")
else:
    print("❌ 未找到活跃的系统配置")

# 2. 检查 llm_providers 中的 tencent-deepseek
print("\n" + "=" * 80)
print("检查 tencent-deepseek 厂家配置")
print("=" * 80)

providers_collection = db.llm_providers
tencent_provider = providers_collection.find_one({"name": "tencent-deepseek"})
if tencent_provider:
    print("✅ 找到 tencent-deepseek 厂家配置:")
    print(json.dumps(tencent_provider, indent=2, ensure_ascii=False, default=str))
else:
    print("❌ 未找到 tencent-deepseek 厂家配置")

# 3. 检查 provider 为 deepseek 的厂家配置
print("\n" + "=" * 80)
print("检查 deepseek 厂家配置")
print("=" * 80)

deepseek_provider = providers_collection.find_one({"name": "deepseek"})
if deepseek_provider:
    print("✅ 找到 deepseek 厂家配置:")
    print(f"  default_base_url: {deepseek_provider.get('default_base_url')}")

client.close()