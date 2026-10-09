"""每日更新靜態總經 app 的資料：財政部貿易、美債殖利率、FRED 美國總經、國發會景氣指標。"""

from __future__ import annotations

import io
import json
import re
import calendar
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
MOF_URL = "https://web02.mof.gov.tw/njswww/webMain.aspx"
FRED_SERIES = ("PAYEMS", "UNRATE", "CPIAUCSL", "PPIFIS")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + ",".join(FRED_SERIES)


def roc_ym(day: date) -> str:
    return f"{day.year - 1911}{day.month:02d}"


def month_end(month: str) -> str:
    year, number = map(int, month[:7].split("-"))
    return f"{year:04d}-{number:02d}-{calendar.monthrange(year, number)[1]:02d}"


def mof_table(funid: str, direction_field: str, extra: dict[str, str]) -> pd.DataFrame:
    today = date.today()
    params = {
        "sys": "220", "kind": "21", "type": "1", "cycle": "41",
        "outmode": "0", "compmode": "00", "outkind": "1", "funid": funid,
        "ym": roc_ym(today - timedelta(days=500)), "ymt": roc_ym(today),
        direction_field: "1", **extra,
    }
    response = requests.get(f"{MOF_URL}?{urlencode(params)}", timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    table = max(pd.read_html(io.StringIO(response.text)), key=lambda frame: frame.size).copy()
    table.columns = [c[1] if isinstance(c, tuple) and not str(c[1]).startswith("Unnamed") else (c[-1] if isinstance(c, tuple) else c) for c in table.columns]
    table = table.rename(columns={table.columns[0]: "label"})
    table = table[table["label"].astype(str).str.match(r"^\d+年\s*\d+月$")].copy()
    parts = table["label"].str.extract(r"(\d+)年\s*(\d+)月")
    table["date"] = [f"{int(y)+1911}-{int(m):02d}-01" for y, m in parts.itertuples(index=False, name=None)]
    return table


def update_trade() -> None:
    path = DATA_DIR / "trade.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for direction, funid in (("出口", "i9121"), ("進口", "i9122")):
        frame = mof_table(funid, "fld0", {"cod00": "1"})
        value_col = next(c for c in frame.columns if str(c).strip() == "總計")
        updates = {row.date[:7]: float(row[value_col]) / 100 for _, row in frame.iterrows()}
        merged = {date_[:7]: value for date_, value in payload["series"][direction]}
        merged.update(updates)
        payload["series"][direction] = sorted([[month_end(key), value] for key, value in merged.items()])

        tech = mof_table("i8135", "fld0" if direction == "出口" else "fld1", {"codspc0": "0,5,"})
        latest = tech.iloc[-1]
        items = []
        for column in tech.columns:
            name = str(column).strip().replace("產品", "")
            if name in {"高科技", "中高科技", "中低科技", "低科技"}:
                items.append([name, float(latest[column])])
        payload["structure"][direction] = items
        payload["latestDate"] = max(payload.get("latestDate", ""), str(latest["date"]))
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def parse_ndc_file(path: Path) -> dict[str, list[list[object]]]:
    raw = pd.read_excel(path, header=None, engine="calamine")
    mask = raw.astype(str).apply(lambda col: col.str.contains("期別|時間|年月|指標", na=False))
    matches = mask.stack()[mask.stack()].index
    header = 0 if matches.empty else matches[0][0]
    columns = raw.iloc[header].astype(str).str.strip().tolist()
    frame = raw.iloc[header + 1:].copy()
    frame.columns = columns
    time_col = columns[0]
    frame[time_col] = frame[time_col].astype(str).str.strip()
    out: dict[str, list[list[object]]] = {}
    for _, row in frame.iterrows():
        digits = re.sub(r"[^\d]", "", row[time_col])
        if len(digits) not in (5, 6):
            continue
        year, month = digits[:4], digits[4:].zfill(2)
        for column in columns[1:]:
            value = pd.to_numeric(row[column], errors="coerce")
            if pd.isna(value):
                continue
            name = re.sub(r"[\s\(\)（）\-\+=]", "", str(column))
            out.setdefault(name, []).append([f"{year}-{month}-01", float(value)])
    return out


def update_ndc() -> None:
    path = DATA_DIR / "ndc.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    today = date.today()
    start = today - timedelta(days=500)
    query = f"sy={start.year}&sm={start.month}&ey={today.year}&em={today.month}&id=2%2C12&sq=0,0,0&file_type=xls"
    download_path = DATA_DIR / "ndc-latest.xls"
    # 只有國發會需要瀏覽器；延後載入，GitHub runner 不必安裝 Playwright
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://index.ndc.gov.tw/n/zh_tw/data/eco", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        with page.expect_download(timeout=60000) as info:
            page.evaluate(f"window.location.href='/n/api/v1/eco/export?{query}'")
        info.value.save_as(download_path)
        browser.close()
    updates = parse_ndc_file(download_path)
    download_path.unlink(missing_ok=True)
    for name, rows in updates.items():
        if name not in payload["series"]:
            continue
        merged = {date_: value for date_, value in payload["series"][name]}
        merged.update({date_: value for date_, value in rows})
        payload["series"][name] = sorted([[key, value] for key, value in merged.items()])
    payload["latestDate"] = max(rows[-1][0] for rows in payload["series"].values() if rows)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# FRED 的 4 個序列都是轉載 BLS；FRED 對程式下載限流（2026-08-22 起逾時），改向 BLS 官方 API 取得。
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_SERIES = {
    "PAYEMS": "CES0000000001",    # 非農就業人數（季調，千人）
    "UNRATE": "LNS14000000",      # 失業率（季調，%）
    "CPIAUCSL": "CUSR0000SA0",    # CPI-U 全項目（季調）
    "PPIFIS": "WPSFD4",           # PPI 最終需求（季調）
}


def update_us_macro_bls() -> None:
    """以 BLS 近兩年數據更新 us-macro.json；長歷史沿用既有檔案（無金鑰 API 每次最多 10 年、每日 25 次）。"""
    path = DATA_DIR / "us-macro.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    year = date.today().year
    response = requests.post(
        BLS_URL, timeout=60,
        json={"seriesid": list(BLS_SERIES.values()), "startyear": str(year - 1), "endyear": str(year)},
        headers={"User-Agent": "macro-card-app/1.0"},
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API：{body.get('status')} {body.get('message')}")
    by_id = {item["seriesID"]: item["data"] for item in body["Results"]["series"]}
    for name, series_id in BLS_SERIES.items():
        updates = {}
        for point in by_id.get(series_id, []):
            if not point["period"].startswith("M") or point["period"] == "M13" or point["value"] in ("-", ""):
                continue
            updates[f"{point['year']}-{point['period'][1:]}-01"] = float(point["value"])
        if not updates:
            raise RuntimeError(f"BLS 序列沒有資料：{series_id}")
        merged = {day: value for day, value in payload["series"][name]}
        merged.update(updates)
        payload["series"][name] = [[day, merged[day]] for day in sorted(merged)]
    payload["latestDate"] = max(rows[-1][0] for rows in payload["series"].values())
    payload["source"] = "U.S. Bureau of Labor Statistics"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def update_us_macro() -> None:
    """更新 BLS 美國就業與物價月頻資料（由 FRED 提供 CSV）。"""
    response = requests.get(FRED_URL, timeout=60, headers={"User-Agent": "macro-card-app/1.0"})
    response.raise_for_status()
    frame = pd.read_csv(io.StringIO(response.text))
    series: dict[str, list[list[object]]] = {}
    for name in FRED_SERIES:
        clean = frame[["observation_date", name]].dropna()
        series[name] = [[str(day), float(value)] for day, value in clean.itertuples(index=False, name=None)]
        if not series[name]:
            raise RuntimeError(f"FRED series is empty: {name}")
    payload = {
        "series": series,
        "latestDate": max(rows[-1][0] for rows in series.values()),
        "source": "U.S. Bureau of Labor Statistics via FRED",
    }
    (DATA_DIR / "us-macro.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
DATA_PREFIX = "const DATA="


def treasury_tenor(header: str) -> str:
    """財政部欄名轉成頁面的期限代號：'1 Mo'→'1M'、'1.5 Month'→'1.5M'、'10 Yr'→'10Y'。"""
    number, unit = header.strip().split(" ", 1)
    return number + ("M" if unit.lower().startswith("mo") else "Y")


def update_bond_curve() -> None:
    """美債殖利率直接嵌在 index.html 的 `const DATA=` 那一行；只更新 rows，頁面程式不動。"""
    page = ROOT / "index.html"
    lines = page.read_text(encoding="utf-8").split("\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(DATA_PREFIX))
    payload = json.loads(lines[index][len(DATA_PREFIX):].rstrip().rstrip(";"))
    tenors = payload["tenors"]

    today = date.today()
    years = [today.year - 1, today.year] if today.month == 1 and today.day <= 15 else [today.year]
    merged = {row[0]: row for row in payload["rows"]}
    for year in years:
        response = requests.get(TREASURY_URL.format(year=year), timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        frame = pd.read_csv(io.StringIO(response.text))
        column_of = {treasury_tenor(c): c for c in frame.columns if c != "Date"}
        missing = [t for t in tenors if t not in column_of]
        if missing:
            raise RuntimeError(f"Treasury CSV 缺少期限欄位：{missing}")
        for _, row in frame.iterrows():
            month, day, yr = map(int, str(row["Date"]).split("/"))
            values = [None if pd.isna(row[column_of[t]]) else float(row[column_of[t]]) for t in tenors]
            merged[f"{yr:04d}-{month:02d}-{day:02d}"] = [f"{yr:04d}-{month:02d}-{day:02d}", *values]

    payload["rows"] = [merged[key] for key in sorted(merged)]
    lines[index] = DATA_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";"
    page.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    import sys

    JOBS = {"trade": update_trade, "bond_curve": update_bond_curve, "us_macro": update_us_macro_bls, "ndc": update_ndc}
    parser = argparse.ArgumentParser()
    # GitHub runner 只跑連得到的來源；國發會擋雲端 IP，改由家裡的 mini 執行 --only ndc,us_macro
    parser.add_argument("--only", default=",".join(JOBS), help="逗號分隔：" + ",".join(JOBS))
    selected = [name.strip() for name in parser.parse_args().only.split(",") if name.strip()]

    DATA_DIR.mkdir(exist_ok=True)
    # 各資料源獨立：任一個失敗只保留舊檔，不能拖累其他卡片。
    # 2026-08-22 起 FRED 從 GitHub runner 連線逾時，舊寫法讓整個 job 失敗、貿易與景氣資料也沒有提交。
    results = {}
    for name in selected:
        try:
            JOBS[name]()
            results[name] = "ok"
        except Exception as error:  # noqa: BLE001
            results[name] = f"skipped: {type(error).__name__}: {error}"[:300]
            print(f"::warning title={name} update skipped::{results[name]}")
    for name, result in results.items():
        print(f"{name}: {result}")
    # 全部失敗代表環境或網路本身壞了，讓 workflow 標紅；部分成功照常提交。
    sys.exit(1 if all(r != "ok" for r in results.values()) else 0)
