"""
TradingAgents-CN API 测试示例（requests）

包含：
- 单股分析提交 + 轮询 + 获取结果
- 批量分析提交 + 逐个轮询 + 获取结果

运行前请先设置 TOKEN。
"""

import time
import requests

BASE_URL = "http://127.0.0.1:3000"
resp = requests.post(f"{BASE_URL}/api/auth/login", json={"username": "admin", "password": "admin123"})
resp.raise_for_status()
access_token = resp.json()["data"]["access_token"]
print(access_token)

# BASE_URL = "http://localhost:42536"
TOKEN = access_token
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
# "research_depth"
    # - 快速: 1级 - 快速分析 (2-4分钟)
    # - 基础: 2级 - 基础分析 (4-6分钟)
    # - 标准: 3级 - 标准分析 (6-10分钟，推荐)
    # - 深度: 4级 - 深度分析 (10-15分钟)
    # - 全面: 5级 - 全面分析 (15-25分钟)
def submit_single():
    payload = {
        "stock_code": "000002",
        "parameters": {
            "market_type": "A股",
            "analysis_date": "2025-08-20",
            "research_depth": "快速",
            "selected_analysts": ["fundamentals","market"], #["market", "fundamentals", "news", "social"]
            "include_sentiment": False,
            "include_risk": True,
            "language": "zh-CN",
            "quick_analysis_model": "deepseek-chat",
            "deep_analysis_model": "deepseek-chat"
        }
    }

    resp = requests.post(f"{BASE_URL}/api/analysis/single", json=payload, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["task_id"]


def submit_batch():
    payload = {
        "title": "测试批量分析",
        "description": "自动化测试",
        "stock_codes": ["688389", "688278"],
        "parameters": {
            "market_type": "A股",
            "research_depth": "标准",
            "selected_analysts": ["market", "fundamentals"],
            "include_sentiment": True,
            "include_risk": True,
            "language": "zh-CN",
            "quick_analysis_model": "deepseek-chat",
            "deep_analysis_model": "deepseek-chat"
        }
    }

    resp = requests.post(f"{BASE_URL}/api/analysis/batch", json=payload, headers=HEADERS)
    resp.raise_for_status()
    print(f"Batch submitted: {resp.json()['data']['mapping']}")
    return resp.json()["data"]["mapping"]


def poll_status(task_id: str, timeout_sec: int = 300):
    start = time.time()
    while time.time() - start < timeout_sec:
        resp = requests.get(f"{BASE_URL}/api/analysis/tasks/{task_id}/status", headers=HEADERS)
        if resp.status_code != 200:
            time.sleep(2)
            continue
        data = resp.json()["data"]
        if data["status"] == "completed":
            return True
        if data["status"] == "failed":
            print(f"Task {task_id} failed with data: {data}")
            return False
        time.sleep(3)
    return False


def fetch_result(task_id: str):
    resp = requests.get(f"{BASE_URL}/api/analysis/tasks/{task_id}/result", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()["data"]


def run_single_flow():
    task_id = submit_single()
    ok = poll_status(task_id)
    if not ok:
        raise RuntimeError(f"single task failed: {task_id}")
    result = fetch_result(task_id)
    print("single summary:", result.get("summary"))


def run_batch_flow():
    mapping = submit_batch()
    for item in mapping:
        task_id = item["task_id"]
        ok = poll_status(task_id)
        if not ok:
            raise RuntimeError(f"batch task failed: {task_id}")
        result = fetch_result(task_id)
        print(item.get("stock_code"), result.get("summary"))


if __name__ == "__main__":
    #run_single_flow()
    run_batch_flow()
