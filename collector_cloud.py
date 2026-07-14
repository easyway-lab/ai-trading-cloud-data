from __future__ import annotations

import argparse
import hashlib
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


VERSION = "github-1.2.0"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "public" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = ROOT / "config.json"
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"
GLOBAL_PATH = DATA_DIR / "global.json"
HEALTH_PATH = DATA_DIR / "health.json"
TECHNICAL_PATH = DATA_DIR / "technicals.json"
MARKET_OVERVIEW_PATH = DATA_DIR / "market_overview.json"
ANNOUNCEMENTS_PATH = DATA_DIR / "announcements.json"
SECTOR_MAP_PATH = DATA_DIR / "sector_map.json"
WENCAI_SECTOR_PATH = DATA_DIR / "wencai_sector_overrides.json"
CN_TZ = ZoneInfo("Asia/Shanghai")

INDEX_SYMBOLS = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
    "sh000852": "中证1000",
    "bj899050": "北证50",
}

HIGH_RISK_ANNOUNCEMENT_WORDS = (
    "退市", "立案", "处罚", "警示函", "监管措施", "诉讼", "仲裁", "冻结", "减持",
    "业绩预亏", "业绩大幅下降", "债务逾期", "违约", "终止", "暂停上市", "风险提示",
)
POSITIVE_ANNOUNCEMENT_WORDS = (
    "中标", "增持", "回购", "业绩预增", "扭亏", "签订合同", "获得批复", "分红", "权益分派",
)


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


def refresh_sector_map(config: dict[str, Any]) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("AKShare is not installed")
    attempts = int(config.get("source_retry_count", 3))
    boards = retry_frame(ak.stock_board_industry_name_em, "AKShare industry boards", attempts)
    name_column = first_column(boards, "板块名称", "行业名称", "名称")
    if not name_column:
        raise RuntimeError(f"Industry board columns changed: {list(boards.columns)}")

    items: dict[str, str] = {}
    failures: list[str] = []
    names = [str(value).strip() for value in boards[name_column].dropna().tolist()]
    for index, sector in enumerate(names[: int(config.get("sector_board_limit", 160))], 1):
        try:
            members = retry_frame(
                lambda sector=sector: ak.stock_board_industry_cons_em(symbol=sector),
                f"industry {sector}",
                max(1, min(attempts, 2)),
            )
            code_column = first_column(members, "代码", "股票代码")
            if not code_column:
                raise RuntimeError(f"member columns changed: {list(members.columns)}")
            for value in members[code_column].tolist():
                code = clean_code(value)
                if code.isdigit() and market_name(code) != "未标注":
                    items[code] = sector
        except Exception as error:
            failures.append(f"{sector}: {error}")
        if index % 20 == 0:
            print(f"Industry progress {index}/{len(names)}")
        time.sleep(0.18)

    if len(items) < int(config.get("sector_min_rows", 3000)):
        raise RuntimeError(f"Industry map only contains {len(items)} stocks; refusing to replace valid cache")
    return {
        "schemaVersion": "1.0",
        "generatedAt": now_iso(),
        "source": "AKShare东方财富行业板块",
        "records": len(items),
        "failedBoards": failures[:30],
        "items": items,
    }


def load_sector_map(config: dict[str, Any], force: bool = False) -> tuple[dict[str, Any], bool, str | None]:
    cached = load_json(SECTOR_MAP_PATH, {"items": {}})
    cached_date = str(cached.get("generatedAt", ""))[:10]
    refresh_days = int(config.get("sector_refresh_days", 7))
    refresh_due = force or not cached.get("items")
    if cached_date:
        try:
            refresh_due = refresh_due or (now_cn().date() - datetime.fromisoformat(cached_date).date()).days >= refresh_days
        except ValueError:
            refresh_due = True
    if not refresh_due:
        return cached, False, None
    try:
        fresh = refresh_sector_map(config)
        save_json(SECTOR_MAP_PATH, fresh)
        return fresh, True, None
    except Exception as error:
        if cached.get("items"):
            return cached, False, str(error)
        return {"items": {}}, False, str(error)


def overlay_sectors(rows: list[dict[str, Any]], sector_payload: dict[str, Any]) -> tuple[int, int]:
    automatic = sector_payload.get("items", {})
    overrides_payload = load_json(WENCAI_SECTOR_PATH, {"items": {}})
    overrides = overrides_payload.get("items", {}) if isinstance(overrides_payload, dict) else {}
    covered = 0
    overridden = 0
    for row in rows:
        code = row["code"]
        sector = overrides.get(code) or automatic.get(code)
        if not sector:
            continue
        row["sector"] = str(sector).strip()
        covered += 1
        if code in overrides:
            overridden += 1
    return covered, overridden


def fetch_tencent_indices(config: dict[str, Any]) -> list[dict[str, Any]]:
    symbols = config.get("index_symbols", INDEX_SYMBOLS)
    if isinstance(symbols, list):
        symbols = {symbol: INDEX_SYMBOLS.get(symbol, symbol) for symbol in symbols}
    response = requests.get(
        "https://qt.gtimg.cn/q=" + ",".join(symbols.keys()),
        timeout=int(config.get("request_timeout_seconds", 20)),
        headers={"Referer": "https://finance.qq.com/", "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    response.encoding = "gbk"
    result: list[dict[str, Any]] = []
    for block in response.text.split(";"):
        match = re.search(r'v_([^=]+)="(.*)"', block)
        if not match:
            continue
        symbol, payload = match.groups()
        fields = payload.split("~")
        if len(fields) < 38 or number(fields[3]) <= 0:
            continue
        result.append({
            "symbol": symbol,
            "name": symbols.get(symbol, fields[1].strip() or symbol),
            "price": number(fields[3]),
            "pctChange": number(fields[32]),
            "high": number(fields[33]),
            "low": number(fields[34]),
            "amountBillion": number(fields[37]) / 10_000,
            "source": "腾讯行情",
            "sourceAt": now_iso(),
        })
    if len(result) < 4:
        raise RuntimeError(f"Tencent returned only {len(result)} index quotes")
    return result


def median(values: list[float]) -> float:
    clean = sorted(value for value in values if pd.notna(value))
    if not clean:
        return 0
    middle = len(clean) // 2
    return clean[middle] if len(clean) % 2 else (clean[middle - 1] + clean[middle]) / 2


def limit_threshold(row: dict[str, Any]) -> float:
    if row.get("market") == "北交所":
        return 29.5
    if row.get("market") in ("创业板", "科创板"):
        return 19.5
    return 9.5


def build_market_emotion(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if number(row.get("close")) > 0]
    up = sum(1 for row in valid if number(row.get("pctChange")) > 0)
    down = sum(1 for row in valid if number(row.get("pctChange")) < 0)
    flat = max(0, len(valid) - up - down)
    limit_up = sum(1 for row in valid if number(row.get("pctChange")) >= limit_threshold(row))
    limit_down = sum(1 for row in valid if number(row.get("pctChange")) <= -limit_threshold(row))
    technical = [row for row in valid if number(row.get("ma20")) > 0]
    above_ma20 = sum(1 for row in technical if number(row.get("close")) >= number(row.get("ma20")))
    up_ratio = up / len(valid) if valid else 0
    above_ratio = above_ma20 / len(technical) if technical else 0.5
    median_change = median([number(row.get("pctChange")) for row in valid])
    limit_balance = (limit_up + 1) / (limit_up + limit_down + 2)
    score = round(max(0, min(100, up_ratio * 35 + above_ratio * 35 + (median_change + 3) / 6 * 20 + limit_balance * 10)))
    temperature = "冰点" if score < 25 else "偏冷" if score < 42 else "中性" if score < 60 else "偏热" if score < 78 else "高热"
    light = "红灯" if score < 38 else "黄灯" if score < 65 else "绿灯"
    return {
        "score": score,
        "temperature": temperature,
        "light": light,
        "sample": len(valid),
        "up": up,
        "down": down,
        "flat": flat,
        "upRatio": round(up_ratio, 4),
        "medianChange": round(median_change, 3),
        "limitUp": limit_up,
        "limitDown": limit_down,
        "aboveMa20": above_ma20,
        "technicalSample": len(technical),
        "aboveMa20Ratio": round(above_ratio, 4),
        "totalAmountBillion": round(sum(number(row.get("amountBillion")) for row in valid), 2),
        "method": "涨跌扩散35% + 20日线宽度35% + 中位涨跌20% + 涨跌停平衡10%",
    }


def exchange_name(code: str) -> str:
    if code.startswith("6"):
        return "上交所"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "深交所"


def exchange_search_url(code: str) -> str:
    if code.startswith("6"):
        return f"https://www.sse.com.cn/assortment/stock/list/info/announcement/index.shtml?productId={code}"
    if code.startswith(("8", "4", "92")):
        return "https://www.bse.cn/disclosure/announcement.html"
    return "https://www.szse.cn/disclosure/listed/notice/index.html"


def announcement_risk(title: str) -> tuple[str, list[str]]:
    flags = [word for word in HIGH_RISK_ANNOUNCEMENT_WORDS if word in title]
    if flags:
        return "高", flags
    positive = [word for word in POSITIVE_ANNOUNCEMENT_WORDS if word in title]
    return ("低" if positive else "中"), positive


def collect_announcements(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    if ak is None:
        raise RuntimeError("AKShare is not installed")
    days = int(config.get("announcement_days", 7))
    end = now_cn().date()
    start = end - timedelta(days=days)
    watchlist = [clean_code(code) for code in config.get("announcement_watchlist", [])]
    candidates = history_candidates(rows, int(config.get("announcement_candidate_limit", 35)))
    codes = list(dict.fromkeys([*watchlist, *candidates]))
    records: list[dict[str, Any]] = []
    failures: list[str] = []

    for index, code in enumerate(codes, 1):
        try:
            frame = ak.stock_zh_a_disclosure_report_cninfo(
                symbol=code,
                market="沪深京",
                keyword="",
                category="",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            code_column = first_column(frame, "代码")
            name_column = first_column(frame, "简称", "名称")
            title_column = first_column(frame, "公告标题", "标题")
            time_column = first_column(frame, "公告时间", "公告日期")
            url_column = first_column(frame, "公告链接", "网址")
            if not title_column or not url_column:
                raise RuntimeError(f"CNINFO columns changed: {list(frame.columns)}")
            for _, item in frame.iterrows():
                item_code = clean_code(item[code_column]) if code_column else code
                title = re.sub(r"<[^>]+>", "", str(item[title_column])).strip()
                risk, flags = announcement_risk(title)
                records.append({
                    "id": "cninfo-" + hashlib.sha1(
                        f"{item_code}|{title}|{str(item[time_column]) if time_column else ''}".encode("utf-8")
                    ).hexdigest()[:16],
                    "code": item_code,
                    "name": str(item[name_column]).strip() if name_column else item_code,
                    "title": title,
                    "source": "巨潮资讯",
                    "exchange": exchange_name(item_code),
                    "url": str(item[url_column]).replace("http://", "https://"),
                    "exchangeUrl": exchange_search_url(item_code),
                    "publishedAt": str(item[time_column]) if time_column else "",
                    "riskLevel": risk,
                    "riskFlags": flags,
                    "sourceVerified": True,
                    "needsOriginalReview": True,
                    "verification": "标题与原文链接来自法定信息披露平台；影响判断仍需阅读原文",
                })
        except Exception as error:
            failures.append(f"{code}: {error}")
        if index % 10 == 0:
            print(f"Announcement progress {index}/{len(codes)}")
        time.sleep(0.2)

    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        unique[(record["code"], record["title"], record["publishedAt"])] = record
    values = sorted(unique.values(), key=lambda item: item["publishedAt"], reverse=True)
    if codes and not values and len(failures) >= len(codes):
        raise RuntimeError("CNINFO requests all failed: " + "; ".join(failures[:3]))
    return {
        "schemaVersion": "1.0",
        "generatedAt": now_iso(),
        "windowDays": days,
        "codesChecked": len(codes),
        "records": len(values),
        "failures": failures[:50],
        "items": values,
    }


def overlay_announcement_flags(rows: list[dict[str, Any]], announcements: dict[str, Any]) -> int:
    by_code: dict[str, list[dict[str, Any]]] = {}
    for item in announcements.get("items", []):
        by_code.setdefault(item.get("code", ""), []).append(item)
    flagged = 0
    for row in rows:
        items = by_code.get(row["code"], [])
        row["newsVerified"] = bool(items)
        row["announcementRisk"] = any(item.get("riskLevel") == "高" for item in items)
        row["announcementCount"] = len(items)
        row["announcementCheckedAt"] = announcements.get("generatedAt") if items else None
        if row["announcementRisk"]:
            flagged += 1
    return flagged


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
    parser.add_argument("--refresh-sectors", action="store_true")
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
        sector_payload, sector_refreshed, sector_error = load_sector_map(config, args.refresh_sectors)
        sector_covered, sector_overridden = overlay_sectors(rows, sector_payload)
        sector_coverage = sector_covered / len(rows) if rows else 0
        sources["industry_map"] = {
            "status": "ok" if sector_covered else "error",
            "message": (
                f"行业覆盖{sector_covered}只，问财覆盖{sector_overridden}只"
                + ("，本次已刷新" if sector_refreshed else "，复用缓存")
                + (f"；刷新失败但已保留旧映射：{sector_error}" if sector_error else "")
            ),
            "updatedAt": now_iso(),
            "records": sector_covered,
        }
        if sector_error and not sector_covered:
            errors.append(f"industry_map: {sector_error}")

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

        try:
            announcements = collect_announcements(rows, config)
            save_json(ANNOUNCEMENTS_PATH, announcements)
            sources["cninfo_announcements"] = {
                "status": "ok",
                "message": f"巨潮核验{announcements.get('codesChecked', 0)}只，公告{announcements.get('records', 0)}条",
                "updatedAt": now_iso(),
                "records": announcements.get("records", 0),
            }
            if announcements.get("failures"):
                sources["cninfo_announcements"]["message"] += f"，{len(announcements['failures'])}只待下次重试"
        except Exception as error:
            announcements = load_json(ANNOUNCEMENTS_PATH, {"items": []})
            errors.append(f"cninfo_announcements: {error}")
            sources["cninfo_announcements"] = {
                "status": "error",
                "message": f"{error}；已保留上次有效公告",
                "updatedAt": now_iso(),
                "records": len(announcements.get("items", [])),
            }
        announcement_risk_count = overlay_announcement_flags(rows, announcements)

        try:
            indices = fetch_tencent_indices(config)
            index_status = "ok"
            index_message = f"重要指数{len(indices)}只"
        except Exception as error:
            previous_overview = load_json(MARKET_OVERVIEW_PATH, {})
            indices = previous_overview.get("indices", [])
            index_status = "error"
            index_message = f"{error}；已保留上次指数快照"
            errors.append(f"index_quotes: {error}")
        market_overview = {
            "schemaVersion": "1.0",
            "generatedAt": now_iso(),
            "indices": indices,
            "emotion": build_market_emotion(rows),
        }
        save_json(MARKET_OVERVIEW_PATH, market_overview)
        sources["index_quotes"] = {
            "status": index_status,
            "message": index_message,
            "updatedAt": now_iso(),
            "records": len(indices),
        }

        snapshot = {
            "schemaVersion": "1.2", "collectorVersion": VERSION, "generatedAt": now_iso(),
            "marketSource": active_market_source, "technicalCoverage": round(coverage, 4),
            "sectorCoverage": round(sector_coverage, 4),
            "announcementRiskCount": announcement_risk_count,
            "rowCount": len(rows), "rows": rows,
        }
        save_json(SNAPSHOT_PATH, snapshot)
        fresh = True
    else:
        snapshot = previous
        coverage = number(previous.get("technicalCoverage"))
        sector_coverage = number(previous.get("sectorCoverage"))
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
        "lastOverviewRefresh": load_json(MARKET_OVERVIEW_PATH, {}).get("generatedAt"),
        "lastAnnouncementRefresh": load_json(ANNOUNCEMENTS_PATH, {}).get("generatedAt"),
        "rows": usable_rows, "technicalCoverage": coverage, "sectorCoverage": sector_coverage,
        "announcementRecords": len(load_json(ANNOUNCEMENTS_PATH, {}).get("items", [])),
        "runningJob": None,
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
