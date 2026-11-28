"""원격검침 포털(단지서버 /m/remote/*.php) HTML에서 숫자만 뽑는다.

페이지 구조 (코맥스 단지 포털, jQuery Mobile):
  index.php   : 항목별 누적  — href="sub.php?...&item=elec"><h3>[전기-누적&nbsp;사용량]&nbsp;44469.000&nbsp;KWh</h3>
  sub.php     : 올해 월별    — href="sub_sub.php?...&cur_month=11&item=elec"><h3>[11월&nbsp;사용량]&nbsp;287.000&nbsp;KWh</h3>
  sub_sub.php : 이번달 일별  — <h3>2026-11-28(44469.000/3.000)&nbsp;KWh</h3>   (누적/그날 사용량)
아직 검침 안 된 칸은 숫자가 비어 있다(→ None).
"""
from __future__ import annotations

import re

# (키, 이름, 단위, HA device_class)
ITEMS = [
    ("elec", "전기", "kWh", "energy"),
    ("gas", "가스", "m³", "gas"),
    ("water", "수도", "m³", "water"),
    ("hotwater", "온수", "m³", "water"),
    ("heating", "난방", "MWh", "energy"),
]
KEYS = [k for k, *_ in ITEMS]

_SP = r"(?:&nbsp;|\s)*"
_NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)?"
_RE_INDEX = re.compile(r'item=(\w+)"\s*>\s*<h3>\[[^\]]*\]' + _SP + _NUM)
_RE_MONTH = re.compile(r'cur_month=(\d{1,2})&(?:amp;)?item=\w+"\s*>\s*<h3>\[[^\]]*\]' + _SP + _NUM)
_RE_DAY = re.compile(r"(\d{4}-\d{2}-\d{2})\(\s*([0-9.,]*)\s*/\s*([0-9.,]*)\s*\)")


def num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_index(html: str) -> dict[str, float | None]:
    """{item: 누적}"""
    return {m.group(1): num(m.group(2)) for m in _RE_INDEX.finditer(html or "")}


def parse_months(html: str) -> dict[int, float | None]:
    """{월(1~12): 그 달 사용량}"""
    out: dict[int, float | None] = {}
    for m in _RE_MONTH.finditer(html or ""):
        mo = int(m.group(1))
        if 1 <= mo <= 12:
            out[mo] = num(m.group(2))
    return out


def parse_days(html: str) -> list[dict]:
    """[{date: 'YYYY-MM-DD', total: 누적, usage: 그날}] 날짜 오름차순"""
    rows = [{"date": m.group(1), "total": num(m.group(2)), "usage": num(m.group(3))}
            for m in _RE_DAY.finditer(html or "")]
    rows.sort(key=lambda r: r["date"])
    return rows
