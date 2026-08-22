"""Refresh the static app's MOF trade and NDC indicator JSON files."""

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
from playwright.sync_api import sync_playwright


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


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    update_trade()
    update_us_macro()
    try:
        update_ndc()
    except Exception as error:
        print(f"NDC update skipped; keeping previous official data: {error}")
