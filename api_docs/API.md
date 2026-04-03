# TradingAgents-CN API 调用文档（节选）

> 本文档仅基于仓库源码整理，用于说明关键接口字段与示例。

## 统一说明
- API Base URL（示例）：`http://localhost:8000`
- 认证：大多数接口需要 `Authorization: Bearer <token>`
- 统一响应包装：`success/data/message`

源码参考：
- 路由定义：`app/routers/analysis.py`、`app/routers/reports.py`
- 请求/参数模型：`app/models/analysis.py`

---

## 1) POST `/api/analysis/single`
**用途**：提交单股分析任务（后台异步执行）。

**请求体（JSON）**
- `symbol` *(string, 可选)*：股票代码（推荐字段）
- `stock_code` *(string, 可选)*：兼容字段（已废弃）
- `parameters` *(object, 可选)*：分析参数（见下）

**AnalysisParameters**
- `market_type` *(string, 默认 "A股")*
- `analysis_date` *(datetime|string, 可选)*
- `research_depth` *(string, 默认 "标准")*：快速/基础/标准/深度/全面
- `selected_analysts` *(array[string], 默认 ["market","fundamentals","news","social"])*
- `custom_prompt` *(string, 可选)*
- `include_sentiment` *(bool, 默认 true)*
- `include_risk` *(bool, 默认 true)*
- `language` *(string, 默认 "zh-CN")*
- `quick_analysis_model` *(string, 默认 "qwen-turbo")*
- `deep_analysis_model` *(string, 默认 "qwen-max")*

**响应示例**
```json
{
  "success": true,
  "data": {
    "task_id": "<uuid>",
    "status": "pending",
    "symbol": "000001",
    "stock_code": "000001",
    "created_at": "2025-..."
  },
  "message": "分析任务已在后台启动"
}
```

---

## 2) POST `/api/analysis/batch`
**用途**：批量提交分析任务（并发执行）。

**请求体（JSON）**
- `title` *(string, 必填)*
- `description` *(string, 可选)*
- `symbols` *(array[string], 可选，最多10个)*
- `stock_codes` *(array[string], 可选，兼容字段)*
- `parameters` *(object, 可选)*：同上

**响应示例**
```json
{
  "success": true,
  "data": {
    "batch_id": "<uuid>",
    "total_tasks": 2,
    "task_ids": ["<task_id1>", "<task_id2>"],
    "mapping": [
      {"symbol": "000001", "stock_code": "000001", "task_id": "<task_id1>"},
      {"symbol": "000002", "stock_code": "000002", "task_id": "<task_id2>"}
    ],
    "status": "submitted"
  },
  "message": "批量分析任务已提交..."
}
```

---

## 3) GET `/api/analysis/tasks/{task_id}/status`
**用途**：查询任务状态/进度。

**响应示例**
```json
{
  "success": true,
  "data": {
    "task_id": "<task_id>",
    "status": "pending|processing|completed|failed|cancelled",
    "progress": 0,
    "message": "任务处理中...",
    "current_step": "pending",
    "start_time": "...",
    "end_time": "...",
    "elapsed_time": 12.3,
    "remaining_time": 0,
    "estimated_total_time": 0,
    "symbol": "000001",
    "stock_code": "000001",
    "stock_symbol": "000001",
    "analysts": ["..."],
    "research_depth": "快速",
    "source": "mongodb_tasks|mongodb_reports"
  },
  "message": "任务状态获取成功"
}
```

---

## 4) GET `/api/analysis/tasks/{task_id}/result`
**用途**：获取任务分析结果。

**响应示例**
```json
{
  "success": true,
  "data": {
    "analysis_id": "...",
    "stock_symbol": "000001",
    "stock_code": "000001",
    "analysis_date": "2025-08-20",
    "summary": "...",
    "recommendation": "...",
    "confidence_score": 0.0,
    "risk_level": "中等",
    "key_points": ["..."],
    "execution_time": 0,
    "tokens_used": 0,
    "analysts": ["..."],
    "research_depth": "快速",
    "detailed_analysis": {"...": "..."},
    "state": {"...": "..."},
    "decision": {"action": "..."},
    "reports": {
      "market_report": "...",
      "fundamentals_report": "...",
      "final_trade_decision": "..."
    }
  },
  "message": "分析结果获取成功"
}
```

---

## 5) GET `/api/reports/list`
**用途**：报告列表查询（分页/筛选）。

**查询参数**
- `page` *(int, 默认 1)*
- `page_size` *(int, 默认 20)*
- `search_keyword` *(string, 可选)*
- `market_filter` *(string, 可选: A股/港股/美股)*
- `start_date` *(string, 可选)*
- `end_date` *(string, 可选)*
- `stock_code` *(string, 可选)*

**响应示例**
```json
{
  "success": true,
  "data": {
    "reports": [
      {
        "id": "<object_id>",
        "analysis_id": "...",
        "title": "股票名(000001) 分析报告",
        "stock_code": "000001",
        "stock_name": "...",
        "market_type": "A股",
        "model_info": "...",
        "type": "single",
        "format": "markdown",
        "status": "completed",
        "created_at": "...",
        "analysis_date": "2025-08-20",
        "analysts": ["..."],
        "research_depth": 1,
        "summary": "...",
        "file_size": 1234,
        "source": "unknown",
        "task_id": "..."
      }
    ],
    "total": 100,
    "page": 1,
    "page_size": 20
  },
  "message": "报告列表获取成功"
}
```

---

## 6) GET `/api/reports/{report_id}/detail`
**用途**：报告详情（report_id 可为 ObjectId / analysis_id / task_id）。

**响应示例**
```json
{
  "success": true,
  "data": {
    "id": "<object_id|task_id>",
    "analysis_id": "...",
    "stock_symbol": "000001",
    "stock_name": "...",
    "model_info": "...",
    "analysis_date": "2025-08-20",
    "status": "completed",
    "created_at": "...",
    "updated_at": "...",
    "analysts": ["..."],
    "research_depth": 1,
    "summary": "...",
    "reports": {"...": "..."},
    "source": "analysis_tasks|unknown",
    "task_id": "...",
    "recommendation": "...",
    "confidence_score": 0.0,
    "risk_level": "中等",
    "key_points": ["..."],
    "execution_time": 0,
    "tokens_used": 0
  },
  "message": "报告详情获取成功"
}
```

---

## Python `requests` 示例（提交任务 + 轮询 + 取结果）

### 单股分析
```python
import time
import requests

BASE_URL = "http://localhost:8000"
TOKEN = "<YOUR_TOKEN>"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

payload = {
    "stock_code": "000002",
    "parameters": {
        "market_type": "A股",
        "analysis_date": "2025-08-20",
        "research_depth": "快速",
        "selected_analysts": ["market"],
        "include_sentiment": False,
        "include_risk": False,
        "language": "zh-CN",
        "quick_analysis_model": "qwen-turbo",
        "deep_analysis_model": "qwen-max"
    }
}

resp = requests.post(f"{BASE_URL}/api/analysis/single", json=payload, headers=HEADERS)
resp.raise_for_status()
submit_data = resp.json()
# 注意：接口包装在 data 中
TASK_ID = submit_data["data"]["task_id"]

while True:
    status_resp = requests.get(f"{BASE_URL}/api/analysis/tasks/{TASK_ID}/status", headers=HEADERS)
    status_resp.raise_for_status()
    status_data = status_resp.json()["data"]
    if status_data["status"] == "completed":
        break
    if status_data["status"] == "failed":
        raise RuntimeError("task failed")
    time.sleep(3)

result_resp = requests.get(f"{BASE_URL}/api/analysis/tasks/{TASK_ID}/result", headers=HEADERS)
result_resp.raise_for_status()
result = result_resp.json()["data"]
print(result["summary"])
```

### 批量分析
```python
import time
import requests

BASE_URL = "http://localhost:8000"
TOKEN = "<YOUR_TOKEN>"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

payload = {
    "title": "测试批量分析",
    "description": "自动化测试",
    "stock_codes": ["000001", "000002"],
    "parameters": {
        "market_type": "A股",
        "research_depth": "标准",
        "selected_analysts": ["market", "fundamentals"],
        "include_sentiment": True,
        "include_risk": True,
        "language": "zh-CN"
    }
}

resp = requests.post(f"{BASE_URL}/api/analysis/batch", json=payload, headers=HEADERS)
resp.raise_for_status()
batch_data = resp.json()["data"]

for item in batch_data["mapping"]:
    task_id = item["task_id"]
    while True:
        status_resp = requests.get(f"{BASE_URL}/api/analysis/tasks/{task_id}/status", headers=HEADERS)
        if status_resp.status_code != 200:
            time.sleep(2)
            continue
        status_data = status_resp.json()["data"]
        if status_data["status"] == "completed":
            break
        if status_data["status"] == "failed":
            raise RuntimeError(f"task failed: {task_id}")
        time.sleep(3)

    result_resp = requests.get(f"{BASE_URL}/api/analysis/tasks/{task_id}/result", headers=HEADERS)
    result_resp.raise_for_status()
    result = result_resp.json()["data"]
    print(item["stock_code"], result["summary"])
```
