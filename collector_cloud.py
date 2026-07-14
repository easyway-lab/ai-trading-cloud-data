from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    import akshare as ak
except Exception:
    ak = None

try:
    import baostock as bs
except Exception:
    bs = None


VERSION = "github-1.0.0"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "public" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = ROOT / "config.json"
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"
GLOBAL_PATH = DATA_DIR / "global.json"
HEALTH_PATH = DATA_DIR / "health.json"
TECHNICAL_PATH = DATA_DIR / "technicals.json"
CN_TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def now_iso() -> str:
    return now_cn().isoformat(timespec="seconds")


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "-" or (isinstance(value, float) and pd.isna(value)):
        return default
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def clean_code(value: Any) -> str:
    code = re.sub(r"\.(SH|SZ|BJ|NQ)$", "", str(value).strip().upper())
    return code.zfill(6) if code.isdigit() else code


def market_name(code: str) -> str:
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    if code.startswith("6"):
        return "沪市主板"
    if code.startswith(("00", "001", "002", "003")):
        return "深市主板"
    return "未标注"


def bs_code(code: str) -> str:
    return ("sh." if code.startswith("6") else "sz.") + code


def tencent_code(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def first_column(frame: pd.DataFrame, *names: str) -> str | None:
    normalized = {str(column).replace(" ", ""): str(column) for column in frame.columns}
    for name in names:
        if name.replace(" ", "") in normalized:
            return normalized[name.replace(" ", "")]
    return None


def fetch_akshare() -> list[dict[str, Any]]:
    if ak is None:
        raise RuntimeError("AKShare is not installed")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            frame = ak.stock_zh_a_spot_em()
            break
        except Exception as error:
            last_error = error
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"AKShare failed after retries: {last_error}")

    columns = {
        "code": first_column(frame, "代码"), "name": first_column(frame, "名称"),
        "close": first_column(frame, "最新价"), "pct": first_column(frame, "涨跌幅"),
        "turnover": first_column(frame, "换手率"), "amount": first_column(frame, "成交额"),
        "ratio": first_column(frame, "量比"), "open": first_column(frame, "今开"),
        "high": first_column(frame, "最高"), "low": first_column(frame, "最低"),
        "volume": first_column(frame, "成交量"),
    }
    if not columns["code"] or not columns["name"] or not columns["close"]:
        raise RuntimeError(f"AKShare columns changed: {list(frame.columns)}")

    rows: list[dict[str, Any]] = []
    generated = now_iso()
    for _, record in frame.iterrows():
        code = clean_code(record[columns["code"]])
        close = number(record[columns["close"]])
        if not code.isdigit() or close <= 0:
            continue
        rows.append({
            "id": f"github-{code}", "name": str(record[columns["name"]]).strip(), "code": code,
            "market": market_name(code), "sector": "未标注", "close": close,
            "pctChange": number(record[columns["pct"]]) if columns["pct"] else 0,
            "turnoverRate": number(record[columns["turnover"]]) if columns["turnover"] else 0,
            "amountBillion": number(record[columns["amount"]]) / 100_000_000 if columns["amount"] else 0,
            "volumeRatio": number(record[columns["ratio"]]) if columns["ratio"] else 0,
            "open": number(record[columns["open"]], close) if columns["open"] else close,
            "high": number(record[columns["high"]], close) if columns["high"] else close,
            "low": number(record[columns["low"]], close) if columns["low"] else close,
            "volume": number(record[columns["volume"]]) if columns["volume"] else 0,
            "ma5": 0, "ma10": 0, "ma20": 0, "high20": 0, "low20": 0,
            "themeScore": 0, "fundamentalScore": 0, "newsVerified": False, "announcementRisk": False,
            "source": "GitHub云端·AKShare", "sourceAt": generated,
        })
    return rows


def history_candidates(rows: list[dict[str, Any]], limit: int) -> list[str]:
    eligible = [
        row for row in rows
        if row["market"] in ("沪市主板", "深市主板")
        and row["amountBillion"] >= 2
        and 0.5 <= row["turnoverRate"] <= 18
        and -5 <= row["pctChange"] <= 9.8
        and "ST" not in row["name"].upper()
    ]
    eligible.sort(key=lambda row: (row["amountBillion"], row["volumeRatio"]), reverse=True)
    return [row["code"] for row in eligible[:limit]]


def refresh_technicals(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    if bs is None:
        raise RuntimeError("BaoStock is not installed")
    codes = history_candidates(rows, int(config.get("history_candidate_limit", 350)))
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    end = now_cn().date()
    start = end - timedelta(days=int(config.get("history_calendar_days", 100)))
    mapping: dict[str, Any] = {}
    failures = 0
    try:
        for index, code in enumerate(codes, 1):
            query = bs.query_history_k_data_plus(
                bs_code(code), "date,high,low,close",
                start_date=start.isoformat(), end_date=end.isoformat(), frequency="d", adjustflag="2",
            )
            values: list[tuple[float, float, float]] = []
            if query.error_code == "0":
                while query.next():
                    item = query.get_row_data()
                    close = number(item[3])
                    if close > 0:
                        values.append((close, number(item[1]), number(item[2])))
            else:
                failures += 1
            if values:
                closes = [item[0] for item in values]
                recent20 = values[-20:]
                mapping[code] = {
                    "ma5": round(sum(closes[-5:]) / len(closes[-5:]), 4),
                    "ma10": round(sum(closes[-10:]) / len(closes[-10:]), 4),
                    "ma20": round(sum(closes[-20:]) / len(closes[-20:]), 4),
                    "high20": round(max(item[1] for item in recent20), 4),
                    "low20": round(min(item[2] for item in recent20), 4),
                    "days": len(values),
                }
            if index % 50 == 0:
                print(f"BaoStock progress {index}/{len(codes)}")
    finally:
        bs.logout()
    payload = {"generatedAt": now_iso(), "candidateCount": len(codes), "failures": failures, "items": mapping}
    save_json(TECHNICAL_PATH, payload)
    return payload


def overlay_technicals(rows: list[dict[str, Any]], technicals: dict[str, Any]) -> float:
    items = technicals.get("items", {})
    covered = 0
    for row in rows:
        value = items.get(row["code"])
        if not value:
            continue
        for key in ("ma5", "ma10", "ma20", "high20", "low20"):
            row[key] = number(value.get(key))
        if number(value.get("days")) >= 20:
            covered += 1
    return covered / len(rows) if rows else 0


def overlay_previous_technicals(rows: list[dict[str, Any]], previous: dict[str, Any]) -> None:
    mapping = {row.get("code"): row for row in previous.get("rows", [])}
    for row in rows:
        old = mapping.get(row["code"], {})
        for key in ("ma5", "ma10", "ma20", "high20", "low20"):
            if number(old.get(key)) > 0:
                row[key] = number(old[key])


def fetch_tencent(codes: list[str], timeout: int) -> dict[str, dict[str, float]]:
    clean = [clean_code(code) for code in codes if clean_code(code).isdigit()]
    if not clean:
        return {}
    response = requests.get(
        "https://qt.gtimg.cn/q=" + ",".join(tencent_code(code) for code in clean),
        timeout=timeout, headers={"Referer": "https://finance.qq.com/"},
    )
    response.raise_for_status()
    response.encoding = "gbk"
    result: dict[str, dict[str, float]] = {}
    for block in response.text.split(";"):
        match = re.search(r'v_[^=]+="(.*)"', block)
        if not match:
            continue
        fields = match.group(1).split("~")
        if len(fields) < 38:
            continue
        code = clean_code(fields[2])
        result[code] = {
            "close": number(fields[3]), "open": number(fields[5]), "pctChange": number(fields[32]),
            "high": number(fields[33]), "low": number(fields[34]),
            "amountBillion": number(fields[37]) / 10_000,
            "turnoverRate": number(fields[38]) if len(fields) > 38 else 0,
        }
    return result


def apply_tencent(rows: list[dict[str, Any]], quotes: dict[str, dict[str, float]]) -> None:
    for row in rows:
        quote = quotes.get(row["code"])
        if not quote or quote["close"] <= 0:
            continue
        row.update({key: value for key, value in quote.items() if value})
        row["source"] = "GitHub云端·AKShare+腾讯"
        row["sourceAt"] = now_iso()


def fetch_finnhub(config: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("FINNHUB_API_KEY", "").strip()
    if not token:
        return {"schemaVersion": "1.0", "generatedAt": now_iso(), "configured": False, "quotes": []}
    quotes = []
    for symbol in config.get("finnhub_symbols", []):
        response = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": symbol, "token": token}, timeout=int(config.get("request_timeout_seconds", 20)),
        )
        if response.status_code == 429:
            break
        response.raise_for_status()
        value = response.json()
        if number(value.get("c")) > 0:
            quotes.append({
                "symbol": symbol, "price": number(value.get("c")), "pctChange": number(value.get("dp")),
                "high": number(value.get("h")), "low": number(value.get("l")),
            })
        time.sleep(1.05)
    return {"schemaVersion": "1.0", "generatedAt": now_iso(), "configured": True, "quotes": quotes}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-history", action="store_true")
    args = parser.parse_args()
    config = load_json(CONFIG_PATH, {})
    previous = load_json(SNAPSHOT_PATH, {"rows": []})
    previous_health = load_json(HEALTH_PATH, {})
    sources = previous_health.get("sources", {})
    last_error = None

    try:
        rows = fetch_akshare()
        sources["akshare"] = {"status": "ok", "message": f"全市场{len(rows)}只", "updatedAt": now_iso(), "records": len(rows)}
    except Exception as error:
        rows = previous.get("rows", [])
        last_error = str(error)
        sources["akshare"] = {"status": "error", "message": str(error), "updatedAt": now_iso(), "records": len(rows)}

    if rows:
        overlay_previous_technicals(rows, previous)
        technicals = load_json(TECHNICAL_PATH, {"items": {}})
        refresh_due = args.refresh_history or not technicals.get("items") or now_cn().hour >= 15
        technical_date = str(technicals.get("generatedAt", ""))[:10]
        if refresh_due and technical_date != now_cn().date().isoformat():
            try:
                technicals = refresh_technicals(rows, config)
                sources["baostock"] = {"status": "ok", "message": f"候选技术结构{len(technicals.get('items', {}))}只", "updatedAt": now_iso(), "records": len(technicals.get("items", {}))}
            except Exception as error:
                last_error = str(error)
                sources["baostock"] = {"status": "error", "message": str(error), "updatedAt": now_iso()}
        elif technicals.get("items"):
            sources["baostock"] = {"status": "ok", "message": f"复用技术缓存{len(technicals.get('items', {}))}只", "updatedAt": now_iso(), "records": len(technicals.get("items", {}))}
        coverage = overlay_technicals(rows, technicals)

        try:
            quotes = fetch_tencent(config.get("tencent_watchlist", []), int(config.get("request_timeout_seconds", 20)))
            apply_tencent(rows, quotes)
            sources["tencent"] = {"status": "ok", "message": f"重点标的{len(quotes)}只", "updatedAt": now_iso(), "records": len(quotes)}
        except Exception as error:
            sources["tencent"] = {"status": "error", "message": str(error), "updatedAt": now_iso()}

        snapshot = {
            "schemaVersion": "1.0", "collectorVersion": VERSION, "generatedAt": now_iso(),
            "technicalCoverage": round(coverage, 4), "rowCount": len(rows), "rows": rows,
        }
        save_json(SNAPSHOT_PATH, snapshot)
    else:
        snapshot = previous
        coverage = number(previous.get("technicalCoverage"))

    try:
        global_snapshot = fetch_finnhub(config)
        save_json(GLOBAL_PATH, global_snapshot)
        sources["finnhub"] = {
            "status": "ok" if global_snapshot["configured"] else "unconfigured",
            "message": f"外围标的{len(global_snapshot['quotes'])}只" if global_snapshot["configured"] else "未设置FINNHUB_API_KEY",
            "updatedAt": now_iso(), "records": len(global_snapshot["quotes"]),
        }
    except Exception as error:
        global_snapshot = load_json(GLOBAL_PATH, {"configured": False, "quotes": []})
        sources["finnhub"] = {"status": "error", "message": str(error), "updatedAt": now_iso()}

    health = {
        "ok": bool(snapshot.get("rows")), "version": VERSION,
        "lastFullRefresh": snapshot.get("generatedAt"), "lastGlobalRefresh": global_snapshot.get("generatedAt"),
        "rows": len(snapshot.get("rows", [])), "technicalCoverage": coverage,
        "runningJob": None, "lastError": last_error, "sources": sources,
    }
    save_json(HEALTH_PATH, health)
    print(json.dumps({"rows": health["rows"], "coverage": coverage, "lastError": last_error}, ensure_ascii=False))


if __name__ == "__main__":
    main()
