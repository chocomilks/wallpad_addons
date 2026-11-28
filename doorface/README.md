# Doorface — 현관 얼굴인식 애드온

코맥스 월패드(CDP-1020CB 계열, 안드로이드)의 **세대현관 도어카메라 원본 영상(720×480)** 을 캡처해서
얼굴을 인식하고, 등록된 가족이면 HA 스크립트를 실행해 현관문을 여는 Home Assistant 애드온입니다.

```
HA 버튼/자동화(PIR 등) ─▶ 애드온 ─SSH─▶ 망고(OpenWrt) ─USB adb─▶ 월패드
                                              #201 모니터링 호출 + cmxmm videoCapture(127.0.0.1:29720)
        ◀── 프레임(JPEG) ──────────────────────
  얼굴 검출(SCRFD) → 정렬 → 임베딩(ArcFace) → 사람별 정책 판정 → script.doorface_open 실행
```

- 사진은 남기지 않습니다. 기록에는 시각·결과·사람·유사도만 저장됩니다. (튜닝용 저장은 켰을 때만 7일 보관)
- 관리 화면은 HA 사이드바의 **현관 얼굴인식** 패널(ingress)에서 열립니다. 폰 HA 앱에서도 같습니다.

## 전제 조건

- 월패드에 adb(root)로 접근 가능하고, 망고(OpenWrt)에 adb가 설치돼 USB로 상시 연결된 상태
- 망고에서 `adb shell` 이 동작할 것 (`adb devices` 에 device로 보임)
- HA 설치 형태: HAOS / Supervised (amd64)

## 설치

1. HA → 설정 → 애드온 → 애드온 스토어 → ⋮ → 저장소 → `https://github.com/chocomilks/wallpad_addons` 추가
2. **Doorface 현관 얼굴인식** 설치 → 시작
   - 첫 시작 때 인식 모델(InsightFace buffalo_l, 약 280MB)을 내려받습니다. 상단 칩이 `준비됨`이 될 때까지 기다리세요.
   - 저사양이면 옵션에서 `model: buffalo_s` (가벼움, 정확도 약간 낮음). 모델을 바꾸면 등록 사진 임베딩은 자동 재계산됩니다.
3. **망고 SSH 허용** (아래) → 패널 **설정** 탭에서 망고 주소 입력 → 공개키를 망고에 등록 → **연결 테스트**

### 망고 SSH 허용

애드온은 집 공유기 쪽(망고의 와이파이 리피터 주소, 예: `192.168.0.x`)으로 망고에 SSH 접속합니다.
OpenWrt 기본 방화벽은 wan 쪽 SSH를 막으므로 HA 주소만 열어줍니다. (망고 셸에서)

```sh
# HA 주소만 SSH 허용 (192.168.0.67 은 예시 — 실제 HA 주소로)
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-SSH-from-HA'
uci set firewall.@rule[-1].src='wan'
uci set firewall.@rule[-1].src_ip='192.168.0.67'
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].dest_port='22'
uci set firewall.@rule[-1].target='ACCEPT'
uci commit firewall && /etc/init.d/firewall restart

# dropbear 가 lan 에만 묶여 있으면 해제
uci show dropbear | grep -i interface && { uci delete dropbear.@dropbear[0].Interface; uci commit dropbear; /etc/init.d/dropbear restart; }

# 패널 설정 탭의 공개키를 한 줄 추가
echo 'ssh-ed25519 AAAA... doorface-addon' >> /etc/dropbear/authorized_keys
```

망고의 집 공유기 쪽 주소는 `ip -4 addr` 에서 리피터 인터페이스(wwan/apcli 등) 주소이거나, 공유기 관리화면의 접속 기기 목록에서 확인합니다. 공유기에서 고정 IP로 잡아두세요.

## 문 열기 스크립트

판정이 나면 애드온이 HA 스크립트(기본 `script.doorface_open`)를 실행하고 `person`, `similarity` 변수를 넘깁니다.
실제 문 열기(RS485)와 재시도는 스크립트에서 처리합니다 — 문을 여는 권한은 HA에만 둡니다.

```yaml
script:
  doorface_open:
    alias: 얼굴인식 현관 열기
    mode: single
    fields:
      person: {}
      similarity: {}
    sequence:
      - repeat:            # 무선/RS485 씹힘 대비 2회 (열기 명령은 중복돼도 무해)
          count: 2
          sequence:
            - action: button.press          # ← 기존 세대현관 열기 엔티티로 교체
              target:
                entity_id: button.entrance_door_open
            - delay: 1
      - action: notify.notify               # ← 원하는 알림으로 교체
        data:
          message: "{{ person }} 얼굴 인식으로 현관 열림 ({{ similarity }})"
```

**자동 열기** 스위치(패널 상단 / `switch.doorface_auto_open`)가 꺼져 있으면 판정만 하고 문은 열지 않습니다.
처음 며칠은 끈 채로 기록을 보며 기준을 맞추는 걸 권장합니다.

## 사용

| 탭 | 내용 |
|---|---|
| 테스트 | 대기 후 스캔, 프레임별 유사도·품질·연속 일치 표시 (문 안 엶) |
| 사람 | 추가/삭제, 정책(얼굴+폰 근접 / 얼굴 단독 엄격), 폰 근접 엔티티·통과 상태값, 사진 업로드·삭제 |
| 등록 촬영 | N초 뒤 도어카메라로 연속 촬영 → 좋은 사진 골라 사람에게 등록 |
| 설정 | 망고 연결, 판정 기준 슬라이더, 캡처 시간, 문 열기 스크립트, 튜닝용 저장 |
| 기록 | 최근 500건 (사진 없음) |

### 판정 방식

- 프레임마다 얼굴을 최대 4개까지 → 품질(크기·정면도) 통과 → 등록 사진들과 코사인 유사도
- 얼굴별로 1등 사람이 기준 이상이고 2등과 `최소 차이` 이상 벌어지면 그 사람 통과
- 등록된 사람마다 통과 횟수를 따로 세서 **연속 N장**이면 일치 (한 프레임 놓침은 허용, 2프레임 연속 불일치면 초기화)
- 여러 명이 함께 잡혀도 등록된 누구든 기준을 채우면 열림 — 모르는 사람이 같이 있어도 막지 않음
- 정책
  - **얼굴 + 폰 근접**: 기본 기준(0.50, 연속 2) + 지정한 HA 엔티티 상태가 통과값(예: `home`, `현관`)일 때만 열림.
    폰이 아직 안 잡혔으면 판정 창 안에서 계속 재시도
  - **얼굴 단독(엄격)**: 폰 없이 열리는 대신 더 높은 기준(0.62, 연속 4)
- 판정 창(기본 12초) 안에 결론이 안 나면 아무것도 하지 않습니다.
- **현관문 열림센서**(설정 탭)를 지정하면 실제 스캔에 문 검사가 붙습니다: 문이 열려 있거나 문 사용 후 금지 시간(기본 180초) 안이면 건너뛰고(나가는 가족 오인 방지),
  스캔 중 문이 열리면 즉시 중지, 판정 순간 문이 열려 있으면 열기 신호를 보내지 않습니다. 센서 상태를 모를 때는 설정으로 고릅니다: 건너뜀(기본) 또는 얼굴+폰 정책인 사람만 열 수 있게 하고 스캔(얼굴 단독 정책은 열지 않음).

### HA 엔티티 (MQTT Discovery, MQTT 애드온 필요)

- `button.doorface_scan` 얼굴인식 스캔 — 누르면 실제 스캔(자동화 트리거용: PIR 감지 → 이 버튼 press)
- `switch.doorface_auto_open` 얼굴인식 자동열기
- `sensor.doorface_last` 얼굴인식 최근결과 (+ 속성: 결과/사람/유사도/프레임/소요시간)
- `binary_sensor.doorface_scanning` 얼굴인식 스캔중
- 이벤트 `doorface_result` — 스캔마다 결과 데이터와 함께 발생

PIR 예시:

```yaml
automation:
  - alias: 현관 PIR → 얼굴인식
    trigger:
      - trigger: state
        entity_id: binary_sensor.entrance_pir
        to: "on"
    action:
      - action: button.press
        target:
          entity_id: button.doorface_scan
```

## 알려진 점

- `#201` 모니터링 통화는 스캔 후 월패드 타임아웃으로 종료됩니다(끊는 명령은 아직 미확인).
- 캡처 속도는 망고 adb 왕복 때문에 초당 약 2~3장입니다.
- 2D 얼굴인식이라 사진·화면 위조에 완전하지 않습니다. 가족은 **얼굴 + 폰 근접** 정책을 권장하고,
  얼굴 단독은 기준을 엄격하게 두세요.
- 인식 모델(InsightFace buffalo 팩)은 비상업적 용도 라이선스입니다. 개인 가정용으로만 사용하세요.
