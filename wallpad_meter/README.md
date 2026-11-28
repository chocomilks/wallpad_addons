# Wallpad Meter 원격검침

코맥스 월패드 단지 포털의 **원격검침(전기·가스·수도·온수·난방)** 을 주기적으로 읽어
이번달 사용량·누적·일별 사용량과 **예상 요금**을 MQTT로 Home Assistant에 올리는 애드온입니다.

단지서버(포털)는 월패드만 닿기 때문에 [doorface](../doorface/README.md)와 같은 경로로 읽습니다.

```
애드온 ──SSH──▶ 망고(OpenWrt, adb 호스트) ──USB adb──▶ 월패드 busybox wget ──▶ 단지서버 /m/remote/*.php
```

망고에는 파일을 남기지 않고(스크립트를 SSH 표준입력으로 실행), 한 번에 약 11페이지(각 수 KB)를 읽고 끝납니다.
상주 프로세스가 아니라 주기(기본 3시간)마다 한 번 깨서 읽는 방식이라 망고·월패드·단지서버 부담이 거의 없습니다.

## 설치

1. 애드온 스토어에서 이 저장소를 추가하고 **Wallpad Meter 원격검침**을 설치합니다. (MQTT 브로커·통합이 있어야 함)
2. 한 번 시작한 뒤 **로그**에 나오는 공개키(`ssh-ed25519 ... wallpad-meter-addon`)를 복사해
   망고의 `/etc/dropbear/authorized_keys`에 한 줄로 추가합니다.
   (같은 키가 `sensor.wallpad_meter_status`의 `ssh_public_key` 속성에도 있습니다)
3. **구성** 탭에서 아래 값을 채우고 다시 시작합니다.

| 옵션 | 내용 |
| --- | --- |
| `mango_host` | 망고 주소 (doorface 설정의 망고 주소와 같게) |
| `portal_server` | 단지서버 주소 (보통 `10.0.0.2`) |
| `site_code`, `wallpad_mac`, `dong`, `ho`, `wallpad_ip` | 포털 주소에 붙는 값. 월패드에서 포털을 열 때 요청 URL `/m/index.php?code=…&mac=…&dong=…&ho=…&wpip=…` 에서 그대로 옮깁니다 (MAC은 콜론 없이) |
| `interval_minutes` | 읽는 주기 (기본 180분). `button.wallpad_meter_refresh`로 바로 읽기도 가능 |

## HA 엔티티 (MQTT Discovery, 기기: 월패드 원격검침)

항목 키: `elec`(전기 kWh) · `gas`(가스 ㎥) · `water`(수도 ㎥) · `hotwater`(온수 ㎥) · `heating`(난방 MWh)

| 엔티티 | 내용 |
| --- | --- |
| `sensor.wallpad_meter_<항목>_month` | 이번달 사용량(오늘 포함). 속성: 오늘·어제·지난달·월말 예상·올해 1~12월 등 |
| `sensor.wallpad_meter_<항목>_cost` | 이번달 예상 요금(원). 속성: 지난달 요금·월말 예상 요금 |
| `sensor.wallpad_meter_<항목>_total` | 누적 계량값 — **HA 에너지 대시보드**(전기·가스·물)에 그대로 넣을 수 있음 |
| `sensor.wallpad_meter_cost_total` | 5개 항목 이번달 요금 합계 |
| `sensor.wallpad_meter_status` | 정상 / 일부 실패 / 오류 / 설정 필요 (+ 오류 내용, 마지막 성공 시각) |
| `button.wallpad_meter_refresh` | 지금 다시 읽기 |

## 공유 JSON (`wallpad_meter/state`, retained)

HA 센서는 모두 이 메시지 하나에서 값을 뽑습니다. 다른 화면(예: 월패드 대기화면)도 이 JSON 하나만 읽으면 됩니다.

```json
{
  "updated": "2026-11-28T12:30:00+09:00",
  "month": "2026-11",
  "items": {
    "elec": {"name": "전기", "unit": "kWh", "month": 287.0, "total": 44469.0,
             "today": 3.0, "yesterday": 10.0, "prev_month": 324.0, "projected": 315.6,
             "months": [289.0, 230.0, "...", null],
             "cost": 46660, "prev_cost": 55010, "projected_cost": 52700, "stale": false},
    "gas": {}, "water": {}, "hotwater": {}, "heating": {}
  },
  "cost_total": 46660, "prev_cost_total": 55010, "projected_cost_total": 52700
}
```

- `month`는 그달 1일부터 오늘까지(오늘은 진행 중), `projected`는 어제까지의 하루 평균 × 그달 일수
- 아직 검침되지 않은 칸은 `null`
- 이번 회차에 못 받은 항목은 직전 값을 유지하고 `stale: true`

## 요금 계산 (예상치)

포털은 사용량만 주므로 요금은 단가로 **추정**합니다. 기본값은 아래 공개 요금표로 넣어 두었고,
**관리비 고지서의 단가로 바꾸면** 실제 청구액에 가까워집니다. 모든 단가는 구성 탭에서 고칠 수 있습니다.

| 항목 | 계산 | 기본값 기준 |
| --- | --- | --- |
| 전기 | 한전 주택용 누진 3단계(기타계절 200/400kWh, 하계 7~8월 300/450kWh) + 기본요금, 하계·동계 1,000kWh 초과분 슈퍼유저 단가, 기후환경 9원·연료비조정 +5원/kWh, 부가세 10%, 전력산업기반기금 2.7%, 10원 미만 절사 | 한전 요금표(2023-11-09 시행, 2026년 4분기 현행). `elec_contract`: `high`=주택용 고압(아파트 단일계약), `low`=주택용 저압(종합계약 세대·단독) |
| 가스 | (㎥ × MJ/㎥ × 원/MJ + 기본요금) × 1.1 | 서울 주택용 22.5268원/MJ, 기본 1,250원, 42.7MJ/㎥ (지역·월마다 다름 — 고지서의 단가·열량계수로 바꾸세요) |
| 수도 | ㎥ × 단가 + 기본요금 (부가세 없음) | 서울 가정용 2026: 상수도 580 + 하수도 480 + 물이용부담금 170 = 1,230원/㎥ |
| 온수 | ㎥ × 단가 + 기본요금 | 6,700원/㎥ — 가열 열량(약 45Mcal/㎥)×지역난방 단가 + 수도요금으로 낸 **대략값**. 단지마다 차이가 커서 고지서 급탕 단가로 바꾸길 권장 |
| 난방 | (계량값 × Mcal 환산 × 절기 단가 + 계약면적 × ㎡당 기본요금) × 1.1 | 한국지역난방공사 주택용: 춘추 110.04 / 하절기 99.02 / 동절기 115.59원/Mcal, 기본 52.40원/㎡. 계량 단위가 MWh라 860Mcal로 환산(Gcal이면 `heat_mcal_per_unit`=1000). 기본요금을 넣으려면 `heat_area_m2`에 계약면적 입력 |

- 전기는 `elec_flat_price`(원/kWh)를 0보다 크게 넣으면 누진 대신 단일 단가로 계산합니다.
- 공용전기·공동난방비 같은 관리비 배분 항목, 복지할인 등은 포함하지 않습니다(세대 사용분 기준).

## 개발

```sh
cd wallpad_meter && python3 -m unittest discover -s tests
```

테스트는 실제 포털 응답 구조를 본뜬 fixture(세대 식별값은 가짜)로 파싱·요금·셸 따옴표 처리까지 확인합니다.
