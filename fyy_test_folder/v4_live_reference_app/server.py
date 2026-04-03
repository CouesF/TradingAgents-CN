#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from stock_screener_v4_fast import StockScreenerV4Fast  # noqa: E402


PORT = 8876
FIRST_SELECTION_DATE = "2026-04-01"
INTERVAL = 10
HOLD_DAYS = 10
TOP_N = 20
MAX_PER_INDUSTRY = 2
BENCHMARK = "000300"
AUTO_RUN_MINUTES = 16 * 60 + 30

STATE_PATH = APP_DIR / "app_state.json"
STATUS_PATH = APP_DIR / "automation_status.json"
LATEST_SCREENING_PATH = APP_DIR / "latest_screening.json"
GENERATED_DIR = APP_DIR / "generated"
AUTOMATION_LOCK = threading.Lock()
REALTIME_LOCK = threading.Lock()
REALTIME_CACHE_TTL_SECONDS = 30
REALTIME_CACHE = {
    "fetched_at_ts": 0.0,
    "quotes": {},
    "symbols": [],
    "source": "akshare_individual_info_em",
    "error": None,
}


INITIAL_STATE = {
    "firstSelectionDate": "2026-04-01",
    "tradingDays": [
        "2026-04-01", "2026-04-02", "2026-04-03", "2026-04-07", "2026-04-08",
        "2026-04-09", "2026-04-10", "2026-04-11", "2026-04-14", "2026-04-15",
        "2026-04-16", "2026-04-17", "2026-04-18", "2026-04-21", "2026-04-22",
        "2026-04-23", "2026-04-24", "2026-04-25", "2026-04-28", "2026-04-29",
        "2026-04-30", "2026-05-06", "2026-05-07", "2026-05-08", "2026-05-11",
        "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15", "2026-05-18",
        "2026-05-19", "2026-05-20", "2026-05-21", "2026-05-22", "2026-05-25",
        "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29", "2026-06-01",
        "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-08",
        "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12", "2026-06-15",
        "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22",
        "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26"
    ],
    "rounds": [
        {
            "id": "r1-2026-04-01",
            "num": 1,
            "selDate": "2026-04-01",
            "buyDate": "2026-04-02",
            "sellDate": "2026-04-15",
            "regime": "bear",
            "picks": [
                {
                    "symbol": "600750",
                    "name": "江中药业",
                    "quantity": 300,
                    "buyPrice": 27,
                    "lastPrice": "",
                    "plannedPrice": "",
                    "status": "active",
                    "conditionalSet": False,
                },
                {
                    "symbol": "688150",
                    "name": "莱特光电",
                    "quantity": 200,
                    "buyPrice": 39.82,
                    "lastPrice": "",
                    "plannedPrice": "",
                    "status": "active",
                    "conditionalSet": False,
                },
            ],
        }
    ],
    "logs": [
        {
            "date": "2026-04-02",
            "time": "04/02 09:35",
            "actionId": "buy-600750",
            "title": "买入 600750 江中药业 300股 @27",
            "result": "done",
        },
        {
            "date": "2026-04-02",
            "time": "04/02 09:35",
            "actionId": "buy-688150",
            "title": "买入 688150 莱特光电 200股 @39.82",
            "result": "done",
        },
        {
            "date": "2026-04-02",
            "time": "04/02 09:36",
            "actionId": "fill-buy-price",
            "title": "填写实际买入价",
            "result": "done",
        },
    ],
}


def now_cn() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    return datetime.now()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        write_json(path, default)
        return json.loads(json.dumps(default))
    return json.loads(path.read_text(encoding="utf-8"))


def empty_pick() -> dict:
    return {
        "symbol": "",
        "name": "",
        "quantity": "",
        "buyPrice": "",
        "lastPrice": "",
        "plannedPrice": "",
        "status": "watch",
        "conditionalSet": False,
    }


def _safe_float(value) -> float | None:
    try:
        if value in (None, "", "None"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def simplify_quote_error(exc: Exception | str) -> str:
    text = str(exc)
    lowered = text.lower()
    if "remotedisconnected" in lowered or "connection aborted" in lowered:
        return "上游行情源连接中断"
    if "timed out" in lowered or "timeout" in lowered:
        return "上游行情源响应超时"
    if "max retries exceeded" in lowered:
        return "上游行情源重试次数超限"
    return "上游行情源暂时不可用"


def dedupe_logs(logs: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for log in logs or []:
        key = f"{log.get('date', '')}__{log.get('actionId', '')}"
        deduped[key] = log
    return list(deduped.values())


def normalize_state(state: dict) -> dict:
    merged = {
        "firstSelectionDate": "",
        "tradingDays": [],
        "rounds": [],
        "logs": [],
        **state,
    }
    merged["logs"] = dedupe_logs(merged.get("logs", []))
    days = merged["tradingDays"]
    first = merged["firstSelectionDate"]
    if not days or first not in days:
        return merged

    existing = {item["selDate"]: item for item in merged.get("rounds", []) if item.get("selDate")}
    rounds = []
    start = days.index(first)
    round_num = 1
    for sel_idx in range(start, len(days), INTERVAL):
        if sel_idx + 1 >= len(days):
            break
        sel_date = days[sel_idx]
        buy_date = days[sel_idx + 1]
        sell_date = days[min(sel_idx + 1 + HOLD_DAYS, len(days) - 1)]
        prev = existing.get(sel_date, {})
        picks = [{**empty_pick(), **pick} for pick in prev.get("picks", [])]
        rounds.append(
            {
                "id": prev.get("id") or f"r{round_num}-{sel_date}",
                "num": round_num,
                "selDate": sel_date,
                "buyDate": buy_date,
                "sellDate": sell_date,
                "regime": prev.get("regime", ""),
                "picks": picks,
            }
        )
        round_num += 1

    merged["rounds"] = rounds
    return merged


def load_state() -> dict:
    return normalize_state(read_json(STATE_PATH, INITIAL_STATE))


def save_state(state: dict) -> dict:
    normalized = normalize_state(state)
    write_json(STATE_PATH, normalized)
    return normalized


def read_status() -> dict:
    return read_json(
        STATUS_PATH,
        {
            "status": "idle",
            "message": "服务端将在需要时自动判断是否执行选股。",
        },
    )


def read_latest_screening() -> dict:
    return read_json(
        LATEST_SCREENING_PATH,
        {
            "selected_picks": [],
            "message": "最近一次自动选股结果会写到这里。",
        },
    )


def get_tracked_symbols(state: dict) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for round_item in state.get("rounds", []):
        for pick in round_item.get("picks", []):
            symbol = str(pick.get("symbol") or "").strip()
            if not symbol:
                continue
            if pick.get("status") not in {"active", "watch"}:
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def fetch_realtime_quote_for_symbol(symbol: str) -> dict | None:
    import akshare as ak  # type: ignore

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            df = ak.stock_individual_info_em(symbol=symbol)
            if df is None or getattr(df, "empty", True):
                raise ValueError("empty quote response")

            info = {}
            for _, row in df.iterrows():  # type: ignore
                item = str(row.get("item") or "")
                info[item] = row.get("value")

            close = _safe_float(info.get("最新"))
            if close is None:
                raise ValueError("missing latest price")

            return {
                "symbol": symbol,
                "close": close,
                "open": _safe_float(info.get("今开")),
                "high": _safe_float(info.get("最高")),
                "low": _safe_float(info.get("最低")),
                "pre_close": _safe_float(info.get("昨收")),
                "name": str(info.get("股票简称") or ""),
                "fetched_at": now_cn().isoformat(),
                "source": "akshare_individual_info_em",
            }
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.6)

    return {
        "symbol": symbol,
        "error": simplify_quote_error(last_error or "unknown error"),
        "fetched_at": now_cn().isoformat(),
        "source": "akshare_individual_info_em",
    }


def get_realtime_quotes(state: dict, force: bool = False) -> dict:
    symbols = get_tracked_symbols(state)
    if not symbols:
        return {
            "quotes": {},
            "symbols": [],
            "fetched_at": None,
            "source": "akshare_individual_info_em",
            "error": None,
        }

    now_ts = time.time()
    with REALTIME_LOCK:
        cache_symbols = REALTIME_CACHE.get("symbols", [])
        if (
            not force
            and REALTIME_CACHE.get("quotes") is not None
            and cache_symbols == symbols
            and now_ts - float(REALTIME_CACHE.get("fetched_at_ts", 0.0)) < REALTIME_CACHE_TTL_SECONDS
        ):
            return {
                "quotes": REALTIME_CACHE["quotes"],
                "symbols": cache_symbols,
                "fetched_at": datetime.fromtimestamp(
                    REALTIME_CACHE["fetched_at_ts"],
                    tz=now_cn().tzinfo,
                ).isoformat() if REALTIME_CACHE.get("fetched_at_ts") else None,
                "source": REALTIME_CACHE["source"],
                "error": REALTIME_CACHE["error"],
            }

        quotes = {}
        failed_symbols = []
        for symbol in symbols:
            quote = fetch_realtime_quote_for_symbol(symbol)
            if not quote:
                continue
            if quote.get("error"):
                failed_symbols.append(symbol)
            else:
                quotes[symbol] = quote

        REALTIME_CACHE["quotes"] = quotes
        REALTIME_CACHE["symbols"] = symbols
        REALTIME_CACHE["fetched_at_ts"] = now_ts
        if failed_symbols and quotes:
            REALTIME_CACHE["error"] = f"{len(failed_symbols)} 只股票行情暂时获取失败，已保留可用价格"
        elif failed_symbols:
            REALTIME_CACHE["error"] = "实时行情源暂时不可用，请稍后重试"
        else:
            REALTIME_CACHE["error"] = None

        return {
            "quotes": quotes,
            "symbols": symbols,
            "fetched_at": now_cn().isoformat(),
            "source": REALTIME_CACHE["source"],
            "error": REALTIME_CACHE["error"],
        }


def load_trade_days(screener: StockScreenerV4Fast, today: str) -> list[str]:
    rows = list(
        screener.db.index_daily_quotes.find(
            {"symbol": BENCHMARK, "trade_date": {"$gte": FIRST_SELECTION_DATE, "$lte": today}},
            {"_id": 0, "trade_date": 1},
        ).sort("trade_date", 1)
    )
    if not rows:
        rows = list(
            screener.db.stock_daily_quotes.find(
                {"symbol": BENCHMARK, "trade_date": {"$gte": FIRST_SELECTION_DATE, "$lte": today}},
                {"_id": 0, "trade_date": 1},
            ).sort("trade_date", 1)
        )
    return sorted({row["trade_date"] for row in rows if row.get("trade_date")})


def build_rounds(days: list[str]) -> list[dict]:
    if FIRST_SELECTION_DATE not in days:
        return []
    start = days.index(FIRST_SELECTION_DATE)
    rounds = []
    round_num = 1
    for sel_idx in range(start, len(days), INTERVAL):
        if sel_idx + 1 >= len(days):
            break
        buy_idx = sel_idx + 1
        sell_idx = min(buy_idx + HOLD_DAYS, len(days) - 1)
        rounds.append(
            {
                "num": round_num,
                "sel_date": days[sel_idx],
                "buy_date": days[buy_idx],
                "sell_date": days[sell_idx],
            }
        )
        round_num += 1
    return rounds


def target_count(regime: str) -> int:
    if regime == "bear":
        return 2
    if regime == "neutral":
        return 3
    return 5


def serialize_result(result) -> dict:
    row = asdict(result)
    return {
        "rank": row["rank"],
        "symbol": row["symbol"],
        "name": row["name"],
        "industry": row["industry"],
        "market": row["market"],
        "close": round(row["close"], 2),
        "trend_score": round(row["trend_score"], 1),
        "fundamental_score": round(row["fundamental_score"], 1),
        "composite_score": round(row["composite_score"], 1),
        "tag": row["tag"],
    }


def sync_screening_to_state(state: dict, selection_date: str, regime: str, selected: list[dict]) -> dict:
    rounds = state.get("rounds", [])
    for round_item in rounds:
        if round_item.get("selDate") != selection_date:
            continue

        existing_map = {pick.get("symbol"): pick for pick in round_item.get("picks", []) if pick.get("symbol")}
        synced_picks = []
        for item in selected:
            prev = existing_map.get(item["symbol"], {})
            synced_picks.append(
                {
                    **empty_pick(),
                    **prev,
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "plannedPrice": item.get("close", ""),
                    "status": prev.get("status", "watch"),
                }
            )
        round_item["regime"] = regime
        round_item["picks"] = synced_picks
        break
    return save_state(state)


def ensure_automation(state: dict | None = None) -> tuple[dict, dict]:
    with AUTOMATION_LOCK:
        now = now_cn()
        today = now.strftime("%Y-%m-%d")
        state = state or load_state()
        status = {
            "generated_at": now.isoformat(),
            "today": today,
            "status": "",
            "message": "",
            "first_selection_date": FIRST_SELECTION_DATE,
            "interval_trading_days": INTERVAL,
            "run_time": "16:30",
            "trigger_mode": "background_thread",
            "latest_screening_file": "latest_screening.json",
        }
        latest = read_latest_screening()

        try:
            screener = StockScreenerV4Fast(as_of_date=today, regime_benchmark=BENCHMARK)
            trade_days = list(state.get("tradingDays") or [])
            if not trade_days:
                trade_days = load_trade_days(screener, today)
            status["trade_days_loaded"] = len(trade_days)

            if today not in trade_days:
                status["status"] = "skipped_non_trading_day"
                status["message"] = "今天不是交易日，不执行自动选股。"
                write_json(STATUS_PATH, status)
                return status, latest

            rounds = build_rounds(trade_days)
            selection_round = next((item for item in rounds if item["sel_date"] == today), None)
            minutes = now.hour * 60 + now.minute

            if not selection_round:
                first_idx = trade_days.index(FIRST_SELECTION_DATE) if FIRST_SELECTION_DATE in trade_days else None
                today_idx = trade_days.index(today)
                if first_idx is None:
                    status["status"] = "skipped_missing_anchor"
                    status["message"] = "未找到首个选股锚点日期。"
                else:
                    elapsed = today_idx - first_idx
                    remainder = elapsed % INTERVAL
                    remaining = INTERVAL - remainder if remainder else INTERVAL
                    status["status"] = "skipped_not_selection_day"
                    status["message"] = f"今天不是选股日，距离下次选股还差 {remaining} 个交易日。"
                write_json(STATUS_PATH, status)
                return status, latest

            status["selection_date"] = today
            status["next_buy_date"] = selection_round["buy_date"]
            status["sell_date"] = selection_round["sell_date"]
            status["round_num"] = selection_round["num"]

            if minutes < AUTO_RUN_MINUTES:
                status["status"] = "waiting_for_1630"
                status["message"] = "今天是选股日，但尚未到 16:30，后台线程会继续等待。"
                write_json(STATUS_PATH, status)
                return status, latest

            if latest.get("selection_date") == today:
                if latest.get("selected_picks"):
                    state = sync_screening_to_state(
                        state,
                        today,
                        latest.get("regime", ""),
                        latest.get("selected_picks", []),
                    )
                status["status"] = "ran"
                status["message"] = f"今天是第 {selection_round['num']} 轮选股日，后台已自动生成选股结果。"
                status["regime"] = latest.get("regime", "")
                status["recommended_count"] = latest.get("recommended_count", 0)
                write_json(STATUS_PATH, status)
                return status, latest

            GENERATED_DIR.mkdir(parents=True, exist_ok=True)
            csv_path = GENERATED_DIR / f"screening_{today}.csv"
            results = screener.screen(
                top_n=TOP_N,
                min_data_days=120,
                min_avg_amount=3000,
                min_trend_score=30,
                min_fundamental_score=20,
                max_per_industry=MAX_PER_INDUSTRY,
            )
            screener.export_csv(results, csv_path)
            regime_info = screener.regime_info or {}
            regime = regime_info.get("regime", "unknown")
            count = target_count(regime)
            selected = results[:count]

            latest = {
                "generated_at": now.isoformat(),
                "selection_date": today,
                "next_buy_date": selection_round["buy_date"],
                "sell_date": selection_round["sell_date"],
                "round_num": selection_round["num"],
                "regime": regime,
                "recommended_count": count,
                "regime_info": regime_info,
                "csv_output": str(csv_path),
                "selected_picks": [serialize_result(item) for item in selected],
                "all_results": [serialize_result(item) for item in results],
            }
            write_json(LATEST_SCREENING_PATH, latest)
            state = sync_screening_to_state(state, today, regime, latest["selected_picks"])

            status["status"] = "ran"
            status["message"] = f"今天是第 {selection_round['num']} 轮选股日，已自动执行选股，供明天买入使用。"
            status["regime"] = regime
            status["recommended_count"] = count
            status["csv_output"] = str(csv_path)
            write_json(STATUS_PATH, status)
            return status, latest

        except Exception as exc:  # pragma: no cover
            status["status"] = "error"
            status["message"] = f"自动选股失败: {exc}"
            status["traceback"] = traceback.format_exc()
            write_json(STATUS_PATH, status)
            return status, latest


def automation_loop() -> None:
    while True:
        try:
            ensure_automation()
        except Exception:
            traceback.print_exc()
        now = now_cn()
        sleep_seconds = max(5, 3600 - (now.minute * 60 + now.second))
        time.sleep(sleep_seconds)


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:  # pragma: no cover
        timestamp = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} - {format % args}")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/bootstrap":
            self.handle_bootstrap()
            return
        if parsed.path == "/api/realtime_quotes":
            self.handle_realtime_quotes()
            return
        if parsed.path == "/api/state":
            self.handle_state_get()
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self.handle_state_post()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def read_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def respond_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_bootstrap(self) -> None:
        state = load_state()
        status = read_status()
        latest = read_latest_screening()
        self.respond_json(
            {
                "state": state,
                "automation_status": status,
                "latest_screening": latest,
            }
        )

    def handle_state_get(self) -> None:
        self.respond_json({"state": load_state()})

    def handle_realtime_quotes(self) -> None:
        state = load_state()
        payload = get_realtime_quotes(state)
        self.respond_json(payload)

    def handle_state_post(self) -> None:
        payload = self.read_body_json()
        state = save_state(payload)
        self.respond_json({"ok": True, "state": state})


def main() -> int:
    load_state()
    ensure_automation()
    worker = threading.Thread(target=automation_loop, daemon=True, name="auto-screener")
    worker.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AppHandler)
    print(f"v4 live server running at http://0.0.0.0:{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
