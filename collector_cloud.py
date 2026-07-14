from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
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


VERSION = "github-1.1.0"
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
    code = str(value).strip().upper()
    code = re.sub(r"^(SH|SZ|BJ)", "", code)
    code = re.sub(r"\.(SH|SZ|BJ|NQ)$", "", code)
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
    if code.startswith("6"):
        return "sh." + code
    if code.startswith(("0", "3")):
        return "sz." + code
    raise ValueError(f"BaoStock does not support code {code}")


def tencent_code(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return "sz" + code


def first_column(frame: pd.DataFrame, *names: str) -> str | None:
    normalized = {str(column).replace(" ", ""): str(column) for column in frame.columns}
    for name in names:
        if name.replace(" ", "") in normalized:
            return normalized[name.replace(" ", "")]
    return None


def empty_row(code: str, name: str, source: str, generated: str) -> dict[str, Any]:
    return {
        "id": f"github-{code}", "name": name, "code": code,
        "market": market_name(code), "sector": "未标注", "close": 0,
        "pctChange": 0, "turnoverRate": 0, "amountBillion": 0, "volumeRatio": 0,
        "open": 0, "high": 0, "low": 0, "volume": 0,
        "ma5": 0, "ma10": 0, "ma20": 0, "high20": 0, "low20": 0,
        "themeScore": 0, "fundamentalScore": 0, "newsVerified": False,
        "announcementRisk": False, "source": source, "sourceAt": generated,
    }


def normalize_spot_frame(frame: pd.DataFrame, source: str) -> list[dict[str, Any]]:
    columns = {
        "code": first_column(frame, "代码"), "name": first_column(frame, "名称"),
        "close": first_column(frame, "最新价", "现价"), "pct": first_column(frame, "涨跌幅", "涨幅"),
        "turnover": first_column(frame, "换手率", "换手"), "amount": first_column(frame, "成交额", "金额"),
        "ratio": first_column(frame, "量比"), "open": first_column(frame, "今开"),
        "high": first_column(frame, "最高"), "low": first_column(frame, "最低"),
        "volume": first_column(frame, "成交量", "总手"),
    }
    required = (columns["code"], columns["name"], columns["close"])
    if not all(required):
        raise RuntimeError(f"{source} columns changed: {list(frame.columns)}")

    rows: list[dict[str, Any]] = []
    generated = now_iso()
    for _, record in frame.iterrows():
        code = clean_code(record[columns["code"]])
        close = number(record[columns["close"]])
        if not code.isdigit() or close <= 0 or market_name(code) == "未标注":
            continue
        row = empty_row(code, str(record[columns["name"]]).strip(), source, generated)
        row.update({
            "close": close,
            "pctChange": number(record[columns["pct"]]) if columns["pct"] else 0,
            "turnoverRate": number(record[columns["turnover"]]) if columns["turnover"] else 0,
            "amountBillion": number(record[columns["amount"]]) / 100_000_000 if columns["amount"] else 0,
            "volumeRatio": number(record[columns["ratio"]]) if columns["ratio"] else 0,
            "open": number(record[columns["open"]], close) if columns["open"] else close,
            "high": number(record[columns["high"]], close) if columns["high"] else close,
            "low": number(record[columns["low"]], close) if columns["low"] else close,
            "volume": number(record[columns["volume"]]) if columns["volume"] else 0,
        })
        rows.append(row)
    return rows


def retry_frame(fetcher: Callable[[], pd.DataFrame], label: str, attempts: int) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetcher()
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}")


def fetch_akshare_em(config: dict[str, Any]) -> list[dict[str, Any]]:
    if ak is None:
        raise RuntimeError("AKShare is not installed")
    frame = retry_frame(
        ak.stock_zh_a_spot_em,
        "AKShare Eastmoney",
        int(config.get("source_retry_count", 3)),
    )
    return normalize_spot_frame(frame, "GitHub云端·AKShare东方财富")


def fetch_akshare_sina(config: dict[str, Any]) -> list[dict[str, Any]]:
    if ak is None:
        raise RuntimeError("AKShare is not installed")
    frame = retry_frame(
        ak.stock_zh_a_spot,
        "AKShare Sina",
        int(config.get("source_retry_count", 3)),
    )
    return normalize_spot_frame(frame, "GitHub云端·AKShare新浪")


def fetch_baostock_universe() -> list[dict[str, str]]:
    if bs is None:
        raise RuntimeError("BaoStock is not installed")
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    stocks: list[dict[str, str]] = []
    try:
        query = bs.query_stock_basic()
        if query.error_code != "0":
            raise RuntimeError(f"BaoStock stock list failed: {query.error_msg}")
        fields = list(query.fields)
        while query.next():
            item = dict(zip(fields, query.get_row_data()))
            code = clean_code(item.get("code", ""))
            if item.get("type") != "1" or item.get("status") != "1":
                continue
            if not code.startswith(("6", "0", "3")) or market_name(code) == "未标注":
                continue
            stocks.append({"code": code, "name": item.get("code_name", code)})
    finally:
        bs.logout()
    if not stocks:
        raise RuntimeError("BaoStock returned an empty stock universe")
    return stocks


def parse_tencent_response(content: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for block in content.split(";"):
        match = re.search(r'v_[^=]+="(.*)"', block)
        if not match:
            continue
        fields = match.group(1).split("~")
        if len(fields) < 38:
            continue
        code = clean_code(fields[2])
        close = number(fields[3])
        if not code.isdigit() or close <= 0:
            continue
        result[code] = {
            "name": fields[1].strip() or code,
            "close": close,
            "open": number(fields[5], close),
            "pctChange": number(fields[32]),
            "high": number(fields[33], close),
            "low": number(fields[34], close),
            "volume": number(fields[6]),
            "amountBillion": number(fields[37]) / 10_000,
            "turnoverRate": number(fields[38]) if len(fields) > 38 else 0,
        }
    return result


def fetch_tencent(
    codes: list[str], timeout: int, batch_size: int = 180, attempts: int = 2,
) -> dict[str, dict[str, Any]]:
    clean = list(dict.fromkeys(clean_code(code) for code in codes if clean_code(code).isdigit()))
    result: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for offset in range(0, len(clean), batch_size):
        batch = clean[offset:offset + batch_size]
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = requests.get(
                    "https://qt.gtimg.cn/q=" + ",".join(tencent_code(code) for code in batch),
                    timeout=timeout,
                    headers={"Referer": "https://finance.qq.com/", "User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
                response.encoding = "gbk"
                result.update(parse_tencent_response(response.text))
                last_error = None
                break
            except Exception as error:
                last_error = error
                if attempt + 1 < attempts:
                    time.sleep(2 * (attempt + 1))
        if last_error:
            failures.append(f"batch {offset // batch_size + 1}: {last_error}")
        time.sleep(0.12)
    if not result and clean:
        raise RuntimeError("Tencent quote batches all failed: " + "; ".join(failures[:3]))
    return result


def apply_tencent(rows: list[dict[str, Any]], quotes: dict[str, dict[str, Any]]) -> int:
    updated = 0
    for row in rows:
        quote = quotes.get(row["code"])
        if not quote:
            continue
        for key in ("close", "open", "pctChange", "high", "low", "volume", "amountBillion", "turnoverRate"):
            row[key] = quote.get(key, row[key])
        row["source"] = row["source"] + "+腾讯"
        row["sourceAt"] = now_iso()
        updated += 1
    return updated


def fetch_baostock_tencent(config: dict[str, Any]) -> list[dict[str, Any]]:
    stocks = fetch_baostock_universe()
    quotes = fetch_tencent(
        [item["code"] for item in stocks],
        int(config.get("request_timeout_seconds", 20)),
        int(config.get("tencent_batch_size", 180)),
        int(config.get("source_retry_count", 3)),
    )
    names = {item["code"]: item["name"] for item in stocks}
    generated = now_iso()
    rows: list[dict[str, Any]] = []
    for code, quote in quotes.items():
        row = empty_row(code, quote.get("name") or names.get(code, code), "GitHub云端·BaoStock+腾讯", generated)
        for key in ("close", "open", "pctChange", "high", "low", "volume", "amountBillion", "turnoverRate"):
            row[key] = quote.get(key, row[key])
        rows.append(row)
    return rows


def collect_market_rows(
    config: dict[str, Any], sources: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    minimum = int(config.get("market_min_rows", 3000))
    providers: list[tuple[str, Callable[[dict[str, Any]], list[dict[str, Any]]]]] = [
        ("akshare_em", fetch_akshare_em),
        ("akshare_sina", fetch_akshare_sina),
        ("baostock_tencent", fetch_baostock_tencent),
    ]
    errors: list[str] = []
    for key, provider in providers:
        started = time.monotonic()
        try:
            rows = provider(config)
            if len(rows) < minimum:
                raise RuntimeError(f"only {len(rows)} rows, minimum is {minimum}")
            sources[key] = {
                "status": "ok", "message": f"全市场{len(rows)}只",
                "updatedAt": now_iso(), "records": len(rows),
                "elapsedSeconds": round(time.monotonic() - started, 1),
            }
            return rows, key
        except Exception as error:
            message = str(error)
            errors.append(f"{key}: {message}")
            sources[key] = {
                "status": "error", "message": message, "updatedAt": now_iso(), "records": 0,
                "elapsedSeconds": round(time.monotonic() - started, 1),
            }
    raise RuntimeError("All A-share market sources failed | " + " | ".join(errors))


def history_candidates(rows: list[dict[str, Any]], limit: int) -> list[str]:
    eligible = []
    for row in rows:
        code = row["code"]
        turnover = number(row.get("turnoverRate"))
        if not code.startswith(("6", "0", "3")):
            continue
        if row["amountBillion"] < 2 or not -5 <= row["pctChange"] <= 9.8:
            continue
        if turnover and not 0.5 <= turnover <= 18:
            continue
        if "ST" in row["name"].upper():
            continue
        eligible.append(row)
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
    sources: dict[str, Any] = {}
    errors: list[str] = []
    market_failed = False
    active_market_source: str | None = None

    try:
        rows, active_market_source = collect_market_rows(config, sources)
    except Exception as error:
        rows = []
        market_failed = True
        errors.append(str(error))

    if rows:
        overlay_previous_technicals(rows, previous)
        technicals = load_json(TECHNICAL_PATH, {"items": {}})
        refresh_due = args.refresh_history or not technicals.get("items") or now_cn().hour >= 15
        technical_date = str(technicals.get("generatedAt", ""))[:10]
        if refresh_due and technical_date != now_cn().date().isoformat():
            try:
                technicals = refresh_technicals(rows, config)
                sources["baostock_history"] = {
                    "status": "ok", "message": f"沪深全板候选技术结构{len(technicals.get('items', {}))}只",
                    "updatedAt": now_iso(), "records": len(technicals.get("items", {})),
                }
            except Exception as error:
                errors.append(f"baostock_history: {error}")
                sources["baostock_history"] = {"status": "error", "message": str(error), "updatedAt": now_iso()}
        elif technicals.get("items"):
            sources["baostock_history"] = {
                "status": "ok", "message": f"复用技术缓存{len(technicals.get('items', {}))}只",
                "updatedAt": now_iso(), "records": len(technicals.get("items", {})),
            }

        if active_market_source == "akshare_sina":
            try:
                quotes = fetch_tencent(
                    [row["code"] for row in rows], int(config.get("request_timeout_seconds", 20)),
                    int(config.get("tencent_batch_size", 180)), int(config.get("source_retry_count", 3)),
                )
                updated = apply_tencent(rows, quotes)
                sources["tencent_enrichment"] = {
                    "status": "ok", "message": f"全市场补充{updated}只",
                    "updatedAt": now_iso(), "records": updated,
                }
            except Exception as error:
                errors.append(f"tencent_enrichment: {error}")
                sources["tencent_enrichment"] = {"status": "error", "message": str(error), "updatedAt": now_iso()}
        else:
            try:
                watchlist = config.get("tencent_watchlist", [])
                quotes = fetch_tencent(watchlist, int(config.get("request_timeout_seconds", 20)))
                updated = apply_tencent(rows, quotes)
                sources["tencent_watchlist"] = {
                    "status": "ok", "message": f"重点标的{updated}只",
                    "updatedAt": now_iso(), "records": updated,
                }
            except Exception as error:
                errors.append(f"tencent_watchlist: {error}")
                sources["tencent_watchlist"] = {"status": "error", "message": str(error), "updatedAt": now_iso()}

        coverage = overlay_technicals(rows, technicals)
        snapshot = {
            "schemaVersion": "1.1", "collectorVersion": VERSION, "generatedAt": now_iso(),
            "marketSource": active_market_source, "technicalCoverage": round(coverage, 4),
            "rowCount": len(rows), "rows": rows,
        }
        save_json(SNAPSHOT_PATH, snapshot)
        fresh = True
    else:
        snapshot = previous
        coverage = number(previous.get("technicalCoverage"))
        fresh = False

    try:
        global_snapshot = fetch_finnhub(config)
        save_json(GLOBAL_PATH, global_snapshot)
        sources["finnhub"] = {
            "status": "ok" if global_snapshot["configured"] else "unconfigured",
            "message": f"外围标的{len(global_snapshot['quotes'])}只" if global_snapshot["configured"] else "未设置FINNHUB_API_KEY",
            "updatedAt": now_iso(), "records": len(global_snapshot["quotes"]),
        }
    except Exception as error:
        errors.append(f"finnhub: {error}")
        global_snapshot = load_json(GLOBAL_PATH, {"configured": False, "quotes": []})
        sources["finnhub"] = {"status": "error", "message": str(error), "updatedAt": now_iso()}

    usable_rows = len(snapshot.get("rows", []))
    health = {
        "ok": usable_rows > 0, "fresh": fresh, "stale": not fresh and usable_rows > 0,
        "version": VERSION, "lastAttemptAt": now_iso(), "lastFullRefresh": snapshot.get("generatedAt"),
        "lastGlobalRefresh": global_snapshot.get("generatedAt"), "activeMarketSource": active_market_source,
        "rows": usable_rows, "technicalCoverage": coverage, "runningJob": None,
        "lastError": " | ".join(errors) if errors else None, "sources": sources,
    }
    save_json(HEALTH_PATH, health)
    print(json.dumps({
        "rows": usable_rows, "fresh": fresh, "stale": health["stale"],
        "source": active_market_source, "coverage": coverage, "lastError": health["lastError"],
    }, ensure_ascii=False))
    if market_failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
