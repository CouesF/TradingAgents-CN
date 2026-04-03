#!/usr/bin/env python3
"""
诊断 tencent-deepseek 配置问题

这个脚本模拟实际的调用流程，找出为什么使用了错误的API URL。
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import os
from pymongo import MongoClient
from app.core.config import settings
from tradingagents.graph.trading_graph import create_llm_by_provider
import json

def test_create_llm_logic():
    """测试 create_llm_by_provider 函数的逻辑"""
    print("=" * 80)
    print("测试 create_llm_by_provider 函数逻辑")
    print("=" * 80)

    # 模拟配置
    test_cases = [
        {
            "name": "tencent-deepseek",
            "provider": "tencent-deepseek",
            "model": "deepseek-v3.2",
            "backend_url": "https://api.lkeap.cloud.tencent.com/v1",
            "api_key": "test-key-123"
        },
        {
            "name": "deepseek (默认)",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "backend_url": None,
            "api_key": "test-key-456"
        }
    ]

    for test in test_cases:
        print(f"\n🔍 测试: {test['name']}")
        print(f"  provider: {test['provider']}")
        print(f"  model: {test['model']}")
        print(f"  backend_url: {test['backend_url']}")

        # 检查 provider.lower() 的值
        provider_lower = test['provider'].lower()
        print(f"  provider.lower(): {provider_lower}")

        # 检查是否会进入 deepseek 分支
        if provider_lower == "deepseek":
            print(f"  ⚠️  会进入 trading_graph.py 的 deepseek 分支")
            print(f"    将使用 ChatDeepSeek 适配器")
            print(f"    适配器硬编码的 base_url: https://api.deepseek.com")
            print(f"    传入的 backend_url: {test['backend_url']}")

            if test['backend_url'] and test['backend_url'] != "https://api.deepseek.com":
                print(f"  ❌ 问题: 即使传入了自定义 backend_url，ChatDeepSeek 适配器可能会忽略它")
        else:
            print(f"  ✅ 不会进入 deepseek 分支")

            if provider_lower == "tencent-deepseek":
                print(f"  🔍 tencent-deepseek 的处理:")
                print(f"    1. 不在 trading_graph.py 的特殊处理列表中")
                print(f"    2. 会进入 else 分支")
                print(f"    3. 使用 ChatOpenAI 而不是 create_openai_compatible_llm")

def check_deepseek_adapter():
    """检查 deepseek_adapter.py 的代码"""
    print("\n" + "=" * 80)
    print("检查 deepseek_adapter.py 代码")
    print("=" * 80)

    deepseek_adapter_path = Path(__file__).parent.parent / "tradingagents" / "llm_adapters" / "deepseek_adapter.py"

    if deepseek_adapter_path.exists():
        with open(deepseek_adapter_path, 'r') as f:
            content = f.read()

        # 查找 base_url 硬编码
        if 'base_url = "https://api.deepseek.com"' in content:
            print("✅ 找到硬编码的 base_url: https://api.deepseek.com")

        # 查找 __init__ 方法
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if 'def __init__' in line:
                print(f"\n🔍 ChatDeepSeek.__init__ 方法:")
                # 打印接下来的几行
                for j in range(i, min(i+20, len(lines))):
                    print(f"  {j+1}: {lines[j]}")
                    if lines[j].strip() == 'super().__init__(':
                        # 找到 super 调用
                        for k in range(j, min(j+10, len(lines))):
                            print(f"  {k+1}: {lines[k]}")
                            if ')' in lines[k] and lines[k].count('(') < lines[k].count(')'):
                                break
                        break
                break
    else:
        print("❌ 找不到 deepseek_adapter.py")

def simulate_actual_call():
    """模拟实际的调用"""
    print("\n" + "=" * 80)
    print("模拟实际调用")
    print("=" * 80)

    # 从数据库获取实际配置
    client = MongoClient(settings.MONGO_URI)
    db = client[settings.MONGO_DB]

    # 1. 获取模型配置
    configs_collection = db.system_configs
    doc = configs_collection.find_one({"is_active": True}, sort=[("version", -1)])

    if doc and "llm_configs" in doc:
        llm_configs = doc["llm_configs"]

        # 查找 deepseek-v3.2
        for config in llm_configs:
            if config.get('model_name') == 'deepseek-v3.2':
                print(f"✅ 找到 deepseek-v3.2 配置:")
                print(f"  provider: {config.get('provider')}")
                print(f"  api_base: {config.get('api_base')}")

                # 获取厂家配置
                providers_collection = db.llm_providers
                provider_name = config.get('provider')
                provider_doc = providers_collection.find_one({"name": provider_name})

                if provider_doc:
                    print(f"\n🔍 厂家配置 {provider_name}:")
                    print(f"  default_base_url: {provider_doc.get('default_base_url')}")

                    # 检查 provider 名称
                    if provider_name == "tencent-deepseek":
                        print(f"  ✅ provider 是 tencent-deepseek")

                        # 模拟 create_llm_by_provider 调用
                        print(f"\n🔍 模拟 create_llm_by_provider 调用:")
                        print(f"  参数: provider={provider_name}, model=deepseek-v3.2")

                        # 检查 provider.lower()
                        if provider_name.lower() == "deepseek":
                            print(f"  ❌ provider.lower() == 'deepseek' 为 True!")
                            print(f"     这意味着会进入 deepseek 分支，使用错误的适配器")
                        else:
                            print(f"  ✅ provider.lower() == 'deepseek' 为 False")
                            print(f"     不会进入 deepseek 分支")
                    else:
                        print(f"  ⚠️  provider 不是 tencent-deepseek: {provider_name}")
                break

    client.close()

def check_provider_name_issue():
    """检查 provider 名称可能的问题"""
    print("\n" + "=" * 80)
    print("检查 provider 名称问题")
    print("=" * 80)

    # 可能的 provider 名称问题
    test_names = ["tencent-deepseek", "tencent_deepseek", "tencentdeepseek", "deepseek"]

    for name in test_names:
        lower_name = name.lower()
        print(f"\n🔍 测试名称: {name}")
        print(f"  lower(): {lower_name}")
        print(f"  == 'deepseek': {lower_name == 'deepseek'}")

        # 检查是否包含 "deepseek"
        if "deepseek" in lower_name:
            print(f"  ⚠️  包含 'deepseek' 子串")

            # 检查去除前缀后的结果
            if lower_name.startswith("tencent-"):
                stripped = lower_name.replace("tencent-", "")
                print(f"  去除 'tencent-' 前缀: {stripped}")
                print(f"  stripped == 'deepseek': {stripped == 'deepseek'}")

def main():
    """主函数"""
    print("诊断 tencent-deepseek 配置问题")
    print("=" * 80)

    test_create_llm_logic()
    check_deepseek_adapter()
    simulate_actual_call()
    check_provider_name_issue()

    print("\n" + "=" * 80)
    print("诊断完成")
    print("=" * 80)

    print("\n📋 可能的问题总结:")
    print("1. ✅ 数据库配置正确 (provider: tencent-deepseek, api_base: 正确)")
    print("2. ✅ get_provider_and_url_by_model_sync() 返回正确结果")
    print("3. ⚠️  问题可能在 create_llm_by_provider() 函数中")
    print("4. ❌ 如果 provider.lower() 包含 'deepseek'，可能被错误处理")
    print("5. ❌ deepseek_adapter.py 硬编码 base_url，可能覆盖传入的 backend_url")

if __name__ == "__main__":
    main()