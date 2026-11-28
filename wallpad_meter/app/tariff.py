"""사용량 → 예상 요금(원). 단가는 애드온 옵션으로 바꿀 수 있고, 아래는 계산 규칙과 표.

전기: 한전 주택용 요금표(2023-11-09 시행분, 2026년 4분기까지 그대로)
  - 기타계절 누진 경계 200/400kWh, 하계(7~8월) 300/450kWh
  - 하계·동계(7~8, 12~2월) 1,000kWh 초과분은 슈퍼유저 단가
  - 전기요금계 = 기본 + 전력량 + 기후환경(×kWh) + 연료비조정(×kWh), 원 미만 절사
  - 부가세 10%(원 미만 반올림) + 전력산업기반기금(10원 미만 절사), 청구액 10원 미만 절사
가스: (㎥ × MJ/㎥ × 원/MJ + 기본요금) × 1.1
수도: ㎥ × (상수도+하수도+물이용부담금) + 기본요금   (부가세 없음)
온수: ㎥ × 단가 + 기본요금                          (단가는 관리비 고지서 기준 입력 권장)
난방: (사용열량 Mcal × 절기 단가 + 계약면적 × ㎡당 기본요금) × 1.1
      사용열량 = 계량값 × heat_mcal_per_unit (MWh면 860, Gcal이면 1000)
"""
from __future__ import annotations

import math

ELEC = {
    # 아파트 단일계약 등 고압 수전 세대
    "high": {"basic": (730, 1260, 6060), "rate": (105.0, 174.0, 242.3), "super": 601.3},
    # 단독·다세대·종합계약 아파트 세대 등 저압
    "low": {"basic": (910, 1600, 7300), "rate": (120.0, 214.6, 307.3), "super": 736.2},
}


def _f(o: dict, k: str, d: float) -> float:
    try:
        return float(o.get(k, d))
    except (TypeError, ValueError):
        return d


def elec(kwh: float | None, month: int, o: dict) -> int | None:
    if kwh is None:
        return None
    flat = _f(o, "elec_flat_price", 0)
    if flat > 0:
        return int(kwh * flat)
    t = ELEC.get(o.get("elec_contract", "high"), ELEC["high"])
    r1, r2, r3 = t["rate"]
    b1, b2 = (300, 450) if month in (7, 8) else (200, 400)
    tier = 0 if kwh <= b1 else 1 if kwh <= b2 else 2
    energy = min(kwh, b1) * r1 + max(0.0, min(kwh, b2) - b1) * r2 + max(0.0, kwh - b2) * r3
    if month in (7, 8, 12, 1, 2) and kwh > 1000:
        energy += (kwh - 1000) * (t["super"] - r3)
    base = math.floor(t["basic"][tier] + energy
                      + kwh * _f(o, "elec_climate", 9.0) + kwh * _f(o, "elec_fuel_adjust", 5.0))
    vat = int(base * 0.1 + 0.5)
    fund = math.floor(base * _f(o, "elec_fund_percent", 2.7) / 100 / 10) * 10
    return math.floor((base + vat + fund) / 10) * 10


def gas(m3: float | None, month: int, o: dict) -> int | None:
    if m3 is None:
        return None
    use = m3 * _f(o, "gas_mj_per_m3", 42.7) * _f(o, "gas_price_per_mj", 22.5268)
    return int((use + _f(o, "gas_basic", 1250)) * 1.1)


def water(m3: float | None, month: int, o: dict) -> int | None:
    if m3 is None:
        return None
    return int(m3 * _f(o, "water_price", 1230) + _f(o, "water_basic", 0))


def hotwater(m3: float | None, month: int, o: dict) -> int | None:
    if m3 is None:
        return None
    return int(m3 * _f(o, "hotwater_price", 6700) + _f(o, "hotwater_basic", 0))


def heat_price(month: int, o: dict) -> float:
    if month in (12, 1, 2):
        return _f(o, "heat_price_winter", 115.59)
    if month in (6, 7, 8):
        return _f(o, "heat_price_summer", 99.02)
    return _f(o, "heat_price_spring_fall", 110.04)


def heating(units: float | None, month: int, o: dict) -> int | None:
    if units is None:
        return None
    mcal = units * _f(o, "heat_mcal_per_unit", 860)
    basic = _f(o, "heat_area_m2", 0) * _f(o, "heat_basic_per_m2", 52.4)
    return int((mcal * heat_price(month, o) + basic) * 1.1)


CALC = {"elec": elec, "gas": gas, "water": water, "hotwater": hotwater, "heating": heating}


def cost(item: str, usage: float | None, month: int, o: dict) -> int | None:
    fn = CALC.get(item)
    return fn(usage, month, o) if fn else None
