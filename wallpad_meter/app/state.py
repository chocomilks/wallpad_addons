"""가져온 페이지들 → 하나의 정규화된 JSON (HA 센서와 이후 월패드 화면이 같이 쓰는 형태).

{
  "updated": "2026-11-28T12:30:00+09:00",
  "month": "2026-11",
  "items": {
    "elec": {"name": "전기", "unit": "kWh",
             "month": 287.0,        이번달 사용량(오늘 포함)
             "total": 44469.0,      누적(계량기 값)
             "today": 3.0, "yesterday": 10.0,
             "prev_month": 324.0,   지난달 사용량
             "projected": 296.6,    지난 날까지의 하루 평균으로 낸 월말 예상
             "months": [289.0, ..., null],   올해 1~12월
             "cost": 46660, "prev_cost": 55010, "projected_cost": 48250,   원
             "stale": false},       이번 검침에서 못 받아 지난 값을 유지 중이면 true
    "gas": {...}, "water": {...}, "hotwater": {...}, "heating": {...}
  },
  "cost_total": ..., "prev_cost_total": ..., "projected_cost_total": ...
}
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from . import tariff
from .parse import ITEMS, parse_days, parse_index, parse_months


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def _sum(vals) -> int | None:
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def build(pages: dict[str, str], now: datetime, o: dict, last: dict | None = None) -> tuple[dict, list[str]]:
    y, m, d = now.year, now.month, now.day
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    dim = calendar.monthrange(y, m)[1]
    today_s = now.strftime("%Y-%m-%d")
    yest_s = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    totals = parse_index(pages.get("index", ""))
    old = (last or {}).get("items", {})
    items: dict[str, dict] = {}
    failed: list[str] = []

    for key, name, unit, _dc in ITEMS:
        months = parse_months(pages.get(f"months:{key}:{y}", ""))
        days = parse_days(pages.get(f"days:{key}", ""))
        if not months and not days:
            failed.append(name)
            if key in old:
                items[key] = {**old[key], "stale": True}
            continue

        dmap = {r["date"]: r for r in days}
        today = dmap.get(today_s, {}).get("usage")
        yesterday = dmap.get(yest_s, {}).get("usage")

        month_use = months.get(m)
        if month_use is None and days:
            month_use = _sum(r["usage"] for r in days if r["date"].startswith(now.strftime("%Y-%m")))
        if pm == 12 and py != y:
            prev = parse_months(pages.get(f"months:{key}:{py}", "")).get(12)
        else:
            prev = months.get(pm)

        projected = None
        if month_use is not None and d > 1:
            done = month_use - (today or 0)
            projected = done / (d - 1) * dim

        total = totals.get(key)
        if total is None and days:
            total = days[-1]["total"]

        items[key] = {
            "name": name, "unit": unit,
            "month": _r(month_use), "total": _r(total),
            "today": _r(today), "yesterday": _r(yesterday),
            "prev_month": _r(prev), "projected": _r(projected),
            "months": [_r(months.get(i)) for i in range(1, 13)],
            "cost": tariff.cost(key, month_use, m, o),
            "prev_cost": tariff.cost(key, prev, pm, o),
            "projected_cost": tariff.cost(key, projected, m, o),
            "stale": False,
        }

    state = {
        "updated": now.isoformat(timespec="seconds"),
        "month": f"{y}-{m:02d}",
        "items": items,
        "cost_total": _sum(i.get("cost") for i in items.values()),
        "prev_cost_total": _sum(i.get("prev_cost") for i in items.values()),
        "projected_cost_total": _sum(i.get("projected_cost") for i in items.values()),
    }
    return state, failed
