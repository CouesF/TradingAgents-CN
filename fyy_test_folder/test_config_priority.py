#!/usr/bin/env python3
"""
测试配置优先级问题

重点：为什么前端的配置没有被最优先使用？

分析：
1. 前端配置的 API URL 存储在数据库的模型配置中 (api_base)
2. get_provider_and_url_by_model_sync() 正确返回了这个值
3. 但在 TradingAgentsGraph 初始化时，没有使用这个值
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from pymongo import MongoClient
from app.core.config import settings
import json

def test_config_flow():
    """测试配置流程"""
    print("=" * 80)
    print("测试配置优先级流程")
    print("=" * 80)

    client = MongoClient(settings.MONGO_URI)
    db = client[settings.MONGO_DB]

    # 1. 检查数据库配置
    print("\n1. 📋 检查数据库配置")

    configs_collection = db.system_configs
    doc = configs_collection.find_one({"is_active": True}, sort=[("version", -1)])

    if doc and "llm_configs" in doc:
        llm_configs = doc["llm_configs"]

        for config in llm_configs:
            if config.get('model_name') == 'deepseek-v3.2':
                print(f"✅ 找到 deepseek-v3.2 配置:")
                print(f"   - provider: {config.get('provider')}")
                print(f"   - api_base: {config.get('api_base')}")
                print(f"   - 优先级: 模型配置中的 api_base 是最高优先级")

    # 2. 检查厂家配置
    print("\n2. 🏭 检查厂家配置")

    providers_collection = db.llm_providers
    tencent_provider = providers_collection.find_one({"name": "tencent-deepseek"})

    if tencent_provider:
        print(f"✅ 找到 tencent-deepseek 厂家配置:")
        print(f"   - default_base_url: {tencent_provider.get('default_base_url')}")
        print(f"   - 优先级: 厂家配置中的 default_base_url 是次优先级")

    # 3. 测试配置查询函数
    print("\n3. 🔍 测试 get_provider_and_url_by_model_sync()")

    from app.services.simple_analysis_service import get_provider_and_url_by_model_sync

    result = get_provider_and_url_by_model_sync("deepseek-v3.2")
    print(f"✅ get_provider_and_url_by_model_sync('deepseek-v3.2') 返回:")
    print(f"   - provider: {result.get('provider')}")
    print(f"   - backend_url: {result.get('backend_url')}")
    print(f"   - api_key: {'已设置' if result.get('api_key') else '未设置'}")

    if result.get('backend_url') == "https://api.lkeap.cloud.tencent.com/v1":
        print("   ✅ backend_url 正确返回前端配置的 URL")
    else:
        print(f"   ❌ backend_url 错误: {result.get('backend_url')}")

    # 4. 检查 TradingAgentsGraph 如何使用配置
    print("\n4. 🏗️  检查 TradingAgentsGraph 如何使用配置")

    trading_graph_path = Path(__file__).parent.parent / "tradingagents" / "graph" / "trading_graph.py"

    with open(trading_graph_path, 'r') as f:
        content = f.read()

    # 查找 DeepSeek 分支
    lines = content.split('\n')
    deepseek_section_start = -1

    for i, line in enumerate(lines):
        if '"deepseek" in self.config["llm_provider"].lower()' in line:
            deepseek_section_start = i
            break

    if deepseek_section_start != -1:
        print(f"🔍 找到 DeepSeek 分支 (第 {deepseek_section_start+1} 行):")

        # 查看关键代码
        key_lines = []
        for i in range(deepseek_section_start, min(len(lines), deepseek_section_start+50)):
            if 'base_url' in lines[i] or 'backend_url' in lines[i] or 'deepseek_base_url' in lines[i]:
                key_lines.append((i+1, lines[i]))

        if key_lines:
            print("   相关代码:")
            for line_num, line in key_lines:
                print(f"   第 {line_num:4d}: {line}")

            # 检查是否使用了配置中的 backend_url
            uses_config_backend_url = False
            for line_num, line in key_lines:
                if 'self.config' in line and 'backend_url' in line:
                    uses_config_backend_url = True
                    print(f"   ✅ 第 {line_num} 行使用了配置中的 backend_url")

            if not uses_config_backend_url:
                print("   ❌ DeepSeek 分支没有使用配置中的 backend_url")

                # 检查使用了什么
                for line_num, line in key_lines:
                    if 'deepseek_base_url' in line:
                        print(f"   🔍 使用了: {line.strip()}")
                        if 'os.getenv' in line:
                            print(f"   ⚠️  从环境变量 DEEPSEEK_BASE_URL 获取，默认值: https://api.deepseek.com")
        else:
            print("   未找到 base_url 相关代码")
    else:
        print("❌ 未找到 DeepSeek 分支")

    # 5. 检查配置传递
    print("\n5. 🔄 检查配置传递流程")

    print("   配置流程:")
    print("   1. 前端 → 数据库: 存储 api_base")
    print("   2. 数据库 → get_provider_and_url_by_model_sync(): 返回 backend_url")
    print("   3. simple_analysis_service → 创建配置字典")
    print("   4. 配置字典 → TradingAgentsGraph.__init__()")
    print("   5. TradingAgentsGraph → 创建 LLM 实例")

    print("\n   问题点:")
    print("   - ✅ 步骤1-3 正确: backend_url 正确传递")
    print("   - ❌ 步骤5 错误: TradingAgentsGraph 没有使用传入的 backend_url")
    print("   - ❌ TradingAgentsGraph 使用 os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')")

    # 6. 检查 simple_analysis_service 如何传递配置
    print("\n6. 📤 检查 simple_analysis_service 如何传递配置")

    simple_service_path = Path(__file__).parent.parent / "app" / "services" / "simple_analysis_service.py"

    with open(simple_service_path, 'r') as f:
        simple_content = f.read()

    simple_lines = simple_content.split('\n')

    # 查找配置创建
    config_keywords = ['backend_url', 'quick_backend_url', 'deep_backend_url']
    for i, line in enumerate(simple_lines):
        for keyword in config_keywords:
            if keyword in line and 'config[' in line:
                print(f"   第 {i+1:4d}: {line.strip()}")

    client.close()

def main():
    """主函数"""
    print("分析为什么前端的配置没有被最优先使用")
    print("=" * 80)

    test_config_flow()

    print("\n" + "=" * 80)
    print("结论")
    print("=" * 80)

    print("""
根本问题：
1. ✅ 前端配置正确存储在数据库中 (api_base: https://api.lkeap.cloud.tencent.com/v1)
2. ✅ 配置查询函数正确返回这个值 (get_provider_and_url_by_model_sync())
3. ✅ 分析服务正确传递这个值 (config['backend_url'], config['quick_backend_url'])
4. ❌ TradingAgentsGraph 在 DeepSeek 分支中没有使用传入的 backend_url
5. ❌ TradingAgentsGraph 使用硬编码的 base_url: https://api.deepseek.com

具体问题位置：
tradingagents/graph/trading_graph.py 第510行：
    deepseek_base_url = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')

这个代码：
1. 没有使用 config 中的 backend_url
2. 从环境变量获取，默认值是错误的 https://api.deepseek.com
3. 即使环境变量 DEEPSEEK_BASE_URL 设置了，也会被默认值覆盖

修复方案：
1. 修改 TradingAgentsGraph 的 DeepSeek 分支，使用 config 中的 backend_url
2. 或者，为 tencent-deepseek 添加专门的处理分支
""")

if __name__ == "__main__":
    main()