# Garmin 스마트워치 통합 설계안 (돌봄기기 통합 대시보드)

> 목적: 실증 대상자가 착용 중인 Garmin 워치 데이터를 기존 대시보드에 **5번째 기기 종류**로 붙인다.
> 상태: **설계만 (코드 미반영).** 2026-09-14 작성, 기준 서버 버전 v3.19.2.
> 원본 자산: 별도 비공개 저장소에서 이관받음

---

## 0. 한 줄 요약

기존 4종은 **기기가 서버로 밀어 넣는다**. Garmin은 그게 안 되므로 **서버가 Garmin 클라우드에서 당겨오는
수집기(폴러) 한 개**를 새로 두고, 그 뒤부터는 McKare·FSR을 붙였던 경로(`_process_line` → 파서 →
`add_to_storage`)를 그대로 재사용한다. 화면·리포트·알림·배정 이력은 손댈 필요 없이 종류 하나가 늘어난다.

**연동의 실질적 가치**: 같은 대상자의 **EMFIT(야간 침대) + Garmin(주간 활동)** 이 같은 이름·위치로
한 화면에 나란히 뜬다. 지금은 두 시스템에 쪼개져 있다.

---

## 1. 이관받은 자산 — 무엇을 쓰고 무엇을 버리는가

`garmin` 폴더는 `cyberjunky/python-garminconnect` v0.2.38 을 내부에서 포크한 저장소에
자체 스크립트를 얹은 것이다. 커밋 4개(2026-03-10 ~ 03-31)가 그 작업분.

| 파일 | 역할 | 연동 시 |
|---|---|---|
| `garminconnect/` | 라이브러리 본체 (**폴더에 벤더링됨** — pip 설치 불필요) | ✅ 그대로 이식 |
| `health_panels.py` | Garmin 응답 → 정규화 (심박·스트레스·호흡·걸음·바디배터리·수면·요약·활동·장기추이) | ✅ **재사용 (경로 하드코딩 없음)** |
| `export_Garmin_data_30days.py` | Connect API 폴링 다계정 내보내기. `process_daily_json()` 이 핵심 | ✅ **폴러의 기반으로 재사용** |
| `export_garmin_data.py` | 오프라인 FIT(`monitoring_b`) 파싱 → **1분 단위** CSV | 🔶 보관 — 1분 해상도가 필요할 때만 (§3) |
| `slice_garmin_data.py` | CSV 날짜 자르기 | 🔶 보관 |
| `dashboard.py` | Flask 로컬 대시보드(:5000) + 동기화 신호등 + 알림 판정 | ❌ **역할이 emfit 대시보드로 흡수됨.** 상태 판정 기준(24h/72h)과 `_GarminAdapter`(SSL 우회)만 가져온다 |
| `send_alerts.py`, `launcher.vbs`, `create_garmin_token.py` | Gmail 알림 + Windows 런처 | ❌ 불필요 — 알림은 기존 디스코드로 |
| `token/.garmin_<account>` | 계정별 OAuth 토큰 | ✅ **필수** (§7) |

### 연동에 실제로 필요한 것은 3개뿐
1. 계정 토큰 폴더
2. `garth>=0.5.17,<0.6.0` + `pytz` (garminconnect는 벤더링돼 있음)
3. 조회 로직 — 위 표의 ✅ 항목

> `dashboard.py`·`send_alerts.py`·`launcher.vbs`는 이 PC에서 그대로 안 돌아간다(토큰을 홈 폴더에서만
> 찾음 / 파이썬 경로가 이전 PC / 수신자가 다른 연구자). **연동에는 필요 없으므로 고치지 않고 참고용으로만 둔다.**

---

## 2. 기존 4종과의 구조적 차이 (설계의 근거)

| | EMFIT·Radar·McKare·FSR | Garmin |
|---|---|---|
| 데이터 도착 | 기기가 서버로 **push** | 서버가 클라우드에서 **pull (폴링)** |
| 지연 | 초~분 | 워치 → 폰 앱 → Garmin 클라우드. **수십 분~수 시간** |
| "연결됨" 판정 | payload `connected` / 이벤트 | 그런 필드 없음. **마지막 동기화 시각**만 존재 |
| 시간 척도 | 10분 / 30분 | **24시간 / 72시간** |
| 인증 | 없음 (McKare만 ApiKey) | 계정별 OAuth 토큰 |
| 실패 모드 | 전원·네트워크 | 토큰 만료, 레이트리밋, TLS EOF, **사용자가 폰 동기화를 안 함** |

⚠️ **기본 끊김 기준 30분을 그대로 적용하면 전 계정이 상시 끊김으로 뜬다.** §6 참고.

---

## 3. 데이터 해상도 — 실측 확인 결과

| 경로 | 해상도 | 자동화 |
|---|---|---|
| **Connect API** (채택) | **심박 2분 간격**, 걸음 15분 버킷, 수면 단계, 일별 요약 | ✅ 가능 |
| 오프라인 FIT 내보내기 | 1분 단위 | ❌ Garmin에 요청 → 며칠 후 수동 ZIP 다운로드 |

근거 — 이관받은 내보내기 결과물 실측:
```
example-account-a_..._heart_rate.csv
2026-07-21 00:00:00,61
2026-07-21 00:02:00,60   ← 2분 간격
summary.csv 의 HR Points (all) = 720  (= 1440분 ÷ 2)
```

**결정: 2분 간격을 그대로 상시 수집한다.** 1분 해상도는 자동화가 불가능하므로, 특정 기간 정밀 분석이
필요해질 때 `export_garmin_data.py`(오프라인 FIT)를 병행한다.

### 용량 추정 — 부담 없음
| 기기 | 하루 행 수 |
|---|---|
| EMFIT 5대 (30초 주기) | 약 14,400행 |
| **Garmin 7계정 (2분 간격)** | **약 5,040행** (기존의 1/3) |

### ⚠️ 걸음수는 계정에 따라 아예 없다 (2026-09-14 PoC 실측)

30일치(7/21~8/19) + 신규 2일치를 실제로 집계한 결과:

| 계정 | 걸음 합계 | `user_summary.totalSteps` | 활동레벨 분포 | 해석 |
|---|---|---|---|---|
| 0003 | **0** | 30일 중 2일만 값 있음 | sedentary 1181 · sleeping 621 · none 113 | 거동 거의 없음 |
| 계정 A | **0** | **30일 전부 빈칸** | sedentary 2241 · sleeping 511 · **`wheelchair_pushing` 9** | **휠체어 사용자** |
| 계정 B | 180,095 | 24/30일 | sedentary 2607 · active 187 · highlyActive 26 | 보행 활발 |
| 계정 C | 174,830 | 26/30일 | sedentary 2600 · active 87 · highlyActive 50 | 보행 활발 |

### ⭐ 원인 규명: `totalSteps` 와 `totalPushes` 는 상호 배타다 (익명 계정 A·B 원본 비교)

원본 JSON 을 받아 비교한 결과, "걸음을 읽을 수 없다"가 아니라 **휠체어 사용자는 걸음 대신 밀기(pushes)로
집계된다**는 것이 원인이었다. `user_summary` 는 두 유형 모두 값을 제대로 준다.

| `user_summary` 필드 | 계정 A (휠체어) | 계정 B (보행) |
|---|---|---|
| `totalSteps` | **None** | **7,040** |
| `totalPushes` | **19** | **키 자체가 없음** |
| `totalDistanceMeters` | None | 5,012 |
| `dailyStepGoal` | 17,900 | 8,620 |
| `activeSeconds` / `highlyActiveSeconds` | 0 / 0 | 1,555 / 4,475 |
| `activeKilocalories` | 2.0 | 221.0 |
| `averageSpo2` | 96.0 | **None** |
| `restingHeartRate` | 68 | 63 |

`get_steps_data()` 의 15분 버킷에도 `steps` 와 `pushes` 필드가 **둘 다** 있다
(`{"startGMT":…, "steps":0, "pushes":0, "primaryActivityLevel":"sedentary"}`).

**결론:**
1. 활동량은 **`totalSteps` 가 있으면 걸음, 없고 `totalPushes` 가 있으면 밀기**로 읽는다.
   카드 라벨도 `🚶 걸음` / `🦽 밀기` 로 자동 전환한다 (§7).
2. 지표 가용성은 계정·기기마다 다르다 (`averageSpo2`는 일부 계정에만 있음). **없는 값은 빈칸으로 두고
   카드·CSV 에서 자동으로 빠지게** 한다 — FSR 배터리를 값 없을 때 숨기는 방식과 동일.
3. ~~"user_summary 에서 걸음·거리를 읽을 수 없어 버킷을 합산해야 한다"~~ → **철회.** 계정 A만 보고 내린
   잘못된 일반화였다. 버킷 합산은 시간대별 그래프를 그릴 때만 필요하다.

---

## 4. 아키텍처 결정

```
[Garmin 워치] → [폰 앱] → [Garmin 클라우드]
                                 ↑ 폴링 (30~60분)
                    [젯슨: garmin_poller.py]
                                 │ POST http://127.0.0.1:8080/garmin
                                 ▼
                    [emfit.service (app.py)]
                    ├─ garmin_data.jsonl 적재
                    └─ analyzer.ingest_realtime_record()
                                 │
                    기존 경로 그대로 → 카드 / 리포트 / 디스코드
```

**폴러를 젯슨에 두고 localhost 로 POST 하는 이유**: 기존 센서와 **완전히 같은 처리 경로**(로그 적재 +
실시간 캐시 갱신)를 타므로 코드 경로가 하나로 유지된다. 윈도우 PC를 24시간 켜둘 필요도 없다.

> 대안(윈도우에서 외부망으로 POST)은 PC가 꺼지면 수집이 멈추고, 그 경로에 인증을 새로 붙여야 한다.
> `/garmin` 은 **우리 폴러만 호출하는 내부 경로**이므로 `/internal/discord/status` 처럼 localhost 전용으로 막는다.

---

## 5. 데이터 모델

| 항목 | 값 |
|---|---|
| 로그 파일 | `garmin_data.jsonl` (새 파일 — 종류별 분리 원칙) → `app.py` `DATA_FILES` 에 추가 |
| payload | Garmin 원본 JSON + `server_received_at` + `data_source: "garmin"` + 계정 라벨 |
| SN 규칙 | **`garmin-example-account-a`** 형태 |
| kind | `analyzer.KIND_GARMIN = "garmin"` 신설. 배정 정보에 명시 |
| dtype | `"Garmin"` |

> ⚠️ **SN을 12자리 16진수로 만들면 안 된다.** `_is_radar_device()`([app.py:752](../app.py#L752))가 AI Radar로
> 오인한다 (ESP32 MAC과 레이더가 같은 모양이라 이미 겪은 문제 — [app.py:783](../app.py#L783) 주석 참고).
> 종류는 SN 모양으로 추측하지 않고 배정 정보의 `kind` 로 판정한다.

### 저장 컬럼 — 원본 확인 후 확정 (2026-09-14)

`add_to_storage()` 의 dtype 분기([analyzer.py:491](../analyzer.py#L491))에 `"Garmin"` 케이스를 추가하고,
아래 컬럼만 만든다. 측정하지 않는 칸(활동량 ACT, RMSSD 등)은 만들지 않는다.

| 저장 컬럼 | 출처 (검증된 경로) |
|---|---|
| 심박수(HR) | `heart_rates.heartRateValues` — `[epoch_ms, bpm]` 2분 간격 |
| 안정시심박 | `user_summary.restingHeartRate` |
| 호흡수(RR) | `user_summary.avgWakingRespirationValue` |
| 산소포화도 | `user_summary.averageSpo2` (없는 계정 있음) |
| 걸음수 / 밀기 | `user_summary.totalSteps` / `totalPushes` (상호 배타 — §3) |
| 이동거리(m) | `user_summary.totalDistanceMeters` |
| 좌식·수면·활동·고강도(초) | `sedentarySeconds` · `sleepingSeconds` · `activeSeconds` · `highlyActiveSeconds` |
| 수면점수 | `sleep_data.dailySleepDTO.sleepScores.overall.value` (+ `qualifierKey`) |
| 총수면·깊은·REM·얕은·각성(분) | `dailySleepDTO.{sleepTime,deepSleep,remSleep,lightSleep,awakeSleep}Seconds` ÷ 60 |
| 스트레스 | `averageStressLevel` · `maxStressLevel` |
| 바디배터리 | `bodyBatteryMostRecentValue` (+ Highest/Lowest) |
| 활동칼로리 | `activeKilocalories` |
| **마지막 동기화** | `user_summary.wellnessEndTimeGmt` ← **카드 상태 판정의 기준** |

빈값으로 오는 엔드포인트 (기록만): `training_readiness`, `max_metrics` — 운동선수용 지표라 두 계정 모두 비어 있다.
나머지 13개 엔드포인트는 정상 응답한다.

### 타임스탬프
Garmin은 GMT ISO 문자열(`2026-09-14T05:00:00.0`)과 epoch ms 가 섞여 온다. `add_to_storage()` 는
epoch(초/밀리초 자동 판별)를 받으므로 파서에서 변환해 넘긴다. KST 변환 로직은 `health_panels.py` 에 이미 있다.

### ⚠️ 기존 4종에 없던 문제 — 같은 날을 여러 번 폴링한다 (중복)

기존 센서는 기기가 각 측정을 **한 번만** 밀어 넣으므로 append-only 로그에 중복이 없다.
Garmin 은 **같은 날짜를 폴링할 때마다 그날 전체를 다시 준다.** 익명화한 실측 사례(계정 B, 09-14 오전):

```
wellnessEndTimeGmt = 2026-09-13T21:45  (KST 09-14 06:45)
steps_data 버킷 = 27개 (하루치는 96개)   → 오전 6시 45분까지만 동기화된 상태
totalSteps = 13, restingHeartRate = None → 부분 데이터
```
이 날을 오후에 다시 폴링하면 같은 오전 구간이 또 온다. 그대로 적재하면 **HR 시계열이 중복되고
일별 요약이 여러 벌 쌓인다.**

**대응 (2중 방어):**
1. **폴러가 증분만 보낸다** — 계정별 `마지막 전송 HR timestamp` 를 상태 파일에 기록하고, 그보다 새로운
   포인트만 POST 한다. 로그 파일도 작아진다.
2. **파서에 안전망 dedup** — `(sn, date, 시간)` 이 이미 있으면 건너뛴다. 폴러 상태 파일이 날아가거나
   과거 날짜를 다시 긁을 때(재시작·수동 백필) 중복을 막는 최후 방어선.
3. **일별 요약은 덮어쓰기가 맞다** — 부분 동기화된 값(`totalSteps=13`)이 나중에 완전한 값으로 바뀐다.
   시계열(HR)과 요약을 같은 dtype 으로 섞지 말고, **요약은 그 날짜의 마지막 레코드를 쓴다**.

> 이 항목은 EMFIT·Radar·McKare·FSR 에는 존재하지 않던 요구사항이다. 구현 시 가장 먼저 정해야 한다.

---

## 6. 코드 변경 지점 (구현 시 체크리스트)

### 신규 파일
- `garmin_poller.py` — 계정 발견 → 조회 → `/garmin` POST. systemd `emfit-garmin-poller.service` 또는 timer
- `garmin_parser.py` — `parse_garmin_payload(row)` (`data_source == "garmin"` 로 식별)

### `analyzer.py`
| 위치 | 변경 |
|---|---|
| `KIND_FSR` 옆 | `KIND_GARMIN = "garmin"` 추가 |
| `add_to_storage()` dtype 분기 | `"Garmin"` 케이스 |
| `_process_line()` dispatch 체인 | `parse_garmin_payload()` 한 줄 추가 |
| `_store_mckare_record()` 옆 | `_store_garmin_record()` 신설 — `_device_status[sn]` 에 `source: "garmin"`, `connected` 는 **동기화 경과 기준** 판정값 |
| `_ensure_fsr_device()` 패턴 | `_ensure_garmin_device()` — 새 계정 자동 등록 (§8) |

### `app.py`
| 위치 | 변경 |
|---|---|
| `DATA_FILES` | `garmin_data.jsonl` 추가 |
| 수신 경로 | `POST /garmin` (localhost 전용) |
| `_is_fsr_device()` 옆 | `_is_garmin_device()` — **`_is_radar_device()` 보다 먼저 호출** |
| `_render_fsr_card()` 옆 | `_render_garmin_card()` (§7) |
| `_build_cards_payload()` | Garmin 섹션 + 헤더 요약에 `워치 n대` |
| `_V2_SECTIONS` | `("garmin", "Garmin 워치", "활동·수면·안정시 심박", …)` |
| `_DISCORD_KIND_LABELS` | `"garmin": "Garmin 워치"` |
| `_report_frames()` | Garmin은 별도 접미사 `_Garmin.csv` 로 분리 (레이더 BED/FALL을 나눈 것과 같은 이유) |
| `/dashboard/raw` 라벨 | `garmin_data.jsonl` → `"Garmin 워치"` |

---

## 7. 대시보드 카드

```
섹션: ⌚ Garmin 워치 — 활동 · 수면 · 안정시 심박

┌─────────────────────────────┐
│ A시설 1               🟢    │
│ 사용자 A                    │
│ ❤️ 안정 │ 🦽 밀기 │ 😴 수면 │
│   68    │   19    │ 3.9h/49 │
│   🟢 동기화 정상             │
│  측정: 09-13 15:18          │
│  동기화: 22시간 전           │
│  garmin-example-account-a   │
└─────────────────────────────┘
```

- **HR/RR/ACT 3칸 형식을 쓰지 않는다.** (FSR이 전용 렌더러를 따로 만든 것과 같은 이유)
- 가운데 칸은 **한 렌더러로 두 유형을 자동 처리**한다 (§3 — 두 필드는 상호 배타):

  | 조건 | 표시 | 실측 예 |
  |---|---|---|
  | `totalSteps` 있음 | `🚶 걸음` | 계정 B → 7,040 |
  | `totalSteps` 없고 `totalPushes` 있음 | `🦽 밀기` | 계정 A → 19 |
  | 둘 다 없음 | `🛋 좌식` (`sedentarySeconds`) | — |

  | 칸 | 값 | 출처 |
  |---|---|---|
  | ❤️ 안정 | 안정시 심박 | `restingHeartRate` |
  | 🚶/🦽/🛋 | 위 표대로 자동 | `totalSteps` / `totalPushes` / `sedentarySeconds` |
  | 😴 수면 | 수면 시간 + 점수 | `dailySleepDTO` |
- **"동기화: 2시간 전"이 정상이다.** 워치는 폰을 거쳐 올라오므로 지연이 당연하다 —
  이 문구를 카드에 명시해야 사용자가 고장으로 오인하지 않는다.

### 상태 판정 (`dashboard.py:322` `_status_color()` 기준을 그대로 채택)
| 마지막 동기화 경과 | 카드 |
|---|---|
| 24시간 이내 | 🟢 동기화 정상 |
| 24~72시간 | 🟡 동기화 지연 |
| 72시간 초과 | 🔴 미동기화 |
| **인증 실패(401)** | 🔧 **토큰 갱신 필요** |

### 디스코드 알림 규칙
| 상황 | 알림 |
|---|---|
| 🟢 정상 | — |
| 🟡 24~72시간 | (선택) |
| 🔴 72시간 초과 | 워치 착용·폰 동기화 확인 요청 |
| 🔧 **인증 실패** | **즉시 알림 — 사람이 개입해야 하는 유일한 경우** |

> **핵심**: 폴러는 *인증 실패*와 *데이터 없음*을 반드시 구분해야 한다. 구분하지 않으면
> "토큰이 죽었다"와 "어르신이 워치를 안 찼다"가 같은 🔴로 보인다.
> 디스코드 임계값은 기기별 `device_overrides` 로 **1440분(24시간)** 설정. 전역 30분을 쓰면 상시 오탐.

---

## 8. 계정 ↔ 대상자 매핑

### 현황 (2026-09-14, 내보내기 결과 실측)
| 계정 | 데이터 있는 날 | 활동 성격 | 닉네임 |
|---|---|---|---|
| example-account-d | 20일 | 거동 거의 없음 (걸음 0) | 사용자 D |
| example-account-a | 29일 | **휠체어 사용** (`wheelchair_pushing`) | 사용자 A |
| example-account-b | 25일 | **보행 활발** (18만 걸음/30일) | **미지정 — 확인 필요** |
| example-account-c | 26일 | **보행 활발** (17만 걸음/30일) | **미지정 — 확인 필요** |
| 0001 · 0002 · 0007 | 0~1일 | — | 없음 (미사용으로 보임) |

### 배포 후 확정 (2026-09-14, 젯슨 실가동)

| 계정 | `/devices` 등록명 | 상태 |
|---|---|---|
| example-account-a | **사용자 A** | 🟢 정상 (밀기 19 — 휠체어) |
| example-account-b | **사용자 B** | 🟢 정상 (걸음 7,040) |
| example-account-d | (미지정) | 🔴 **09-10부터 착용 중단** — 09-09까지는 하루 720건 정상 |
| example-account-c | (미지정) | 💤 비활성 — 최근 데이터 없음 |

> ~~가설: 계정 B·C는 같은 시설의 돌봄자(요양보호사·가족)일 것이다~~ → **틀렸다.**
> 계정 B는 다른 실증 그룹 대상자였다. 걸음수가 많다는 것만으로 돌봄자로 단정한
> 추론이었다. **계정↔대상자 매핑은 데이터 모양으로 추측하지 말고 운영자에게 확인할 것.**

### 쓰지 않는 워치 처리
`/devices` 의 👁 아이콘으로 숨기면 대시보드 카드와 **디스코드(알림·`/현황`·`/상세보기`)에서 함께
빠진다** — 봇과 알림이 대시보드와 같은 기기 목록(`_discord_device_snapshot`)을 쓰고, 그 목록이
숨김 기기를 제외하기 때문이다. 수집과 과거 데이터는 그대로 유지되므로 되돌리기도 쉽다.
그냥 두면 마지막 데이터로부터 7일이 지날 때 자동으로 💤 비활성 구역으로 내려간다.

### 자동 등록 방침 (코드 수정 없이 계정 추가)
FSR의 `_ensure_fsr_device()` 패턴을 그대로 따른다:
1. 폴러가 토큰 폴더를 발견 → SN `garmin-example-account-b` 생성
2. 첫 데이터 수신 시 **자동 등록** → 대시보드에 `Garmin example-account-b` 카드로 뜸
3. `/devices` 화면에서 이름·위치 지정 → `A시설 사용자 A`로 표시

**나중에 계정을 추가할 때 서버 코드를 건드릴 필요가 없다.** 토큰 폴더만 젯슨에 올리면 된다.
데이터가 없는 계정(0001·0002·0007)은 기존 **7일 규칙**으로 자동으로 `💤 비활성 기기`로 내려가므로
화면이 지저분해지지 않는다.

---

## 9. 토큰 운영 (가장 주의할 부분)

### 확인된 상태 (2026-09-14, 파일만 확인 — 네트워크 호출 안 함)
| 계정 | oauth2 발급 | access 만료 | **refresh 만료** |
|---|---|---|---|
| 0003 | 2026-09-10 08:19 | 09-11 13:40 | **2026-10-10 08:19** |
| 계정 A·B·C | 2026-09-11 08:27 | 09-12 05:47~14:00 | **2026-10-11 08:27** |

- 9월 10~11일에 oauth2가 새로 발급된 기록 = **그 시점에 인증이 정상 작동했다는 증거.** oauth1은 살아있을 것으로 본다.
- `oauth1_token.json` 에는 **만료 필드가 없다**(`oauth_token`, `oauth_token_secret`, `domain`, `mfa_token`,
  빈 `mfa_expiration_timestamp`). oauth1 수명은 Garmin 서버만 안다 — 파일로는 알 수 없다.
  라이브러리 README는 "약 1년"이라 적고 있고, 이 프로젝트 작업 시점(2026년 3~5월)으로 보면 2027년 3~5월경 추정.

### ⚠️ 갱신은 oauth1 을 통해서만 일어난다 (garth 0.8.0 실제 구현 확인, 2026-09-14)

```python
# garth.Client.refresh_oauth2()
assert self.oauth1_token, "OAuth1 token is required for OAuth2 refresh"
self.oauth2_token = sso.exchange(self.oauth1_token, self)   # ← oauth1 으로 교환
...
if self._garth_home:
    self.dump(self._garth_home, oauth2_only=True)           # ← 갱신본을 파일에 자동 저장
```

즉 **토큰 파일에 있는 30일짜리 `refresh_token` 은 garth 가 쓰지 않는다.** 갱신 경로는 oauth1 하나뿐이다.
따라서:

| | |
|---|---|
| oauth1 살아있음 | 실행할 때마다 oauth2 가 새로 발급되고 **파일이 자동으로 덮어써진다** → 계속 굴러감 |
| oauth1 만료 | `GarthHTTPError` → **대상자 폰으로 재로그인(2FA 포함)** 필요. 우회로 없음 |

- oauth2 access 토큰은 이미 만료 상태(09-11/09-12)이므로, **다음 조회는 반드시 oauth1 을 거친다.**
  → **PoC 한 번이 곧 oauth1 생존 검증이다.** 파일만 봐서는 알 수 없고, 이 방법 외에 확인 수단이 없다.

### ✅ 2026-09-14 PoC 결과: oauth1 정상 (익명 계정 A)

```
Token login successful.  → 2026-09-13~14 조회 성공, CSV 5개 생성
```
oauth2 access 가 만료된 상태에서 성공했으므로 **oauth1 으로 교환이 이뤄졌다는 뜻** = oauth1 유효.

### ⚠️ 그런데 갱신본이 파일에 저장되지 않는다 (garth 0.5.21 실측)

`garth 0.5.21` 의 `Client.load()` / `dump()` 는 **`_garth_home` 을 두지 않는다** — `dump()` 가 경로를 인자로
받는 단순 함수이고, 갱신 시 자동 호출되지 않는다(`_garth_home` 기반 자동 저장은 0.8.x 동작). 실제로 PoC 후
`oauth2_token.json` 의 `expires_at` 이 그대로였다(09-12 14:00).

| | |
|---|---|
| 결과 | 갱신은 **메모리에서만** 일어나고, 매 실행마다 oauth1 으로 새 oauth2 를 교환한다 |
| 문제 | oauth1 이 죽는 순간 완충 없이 전부 멈춘다 + 매 실행마다 불필요한 SSO 교환 |
| **대응** | **폴러가 조회 후 `api.garth.dump(token_dir)` 를 명시적으로 호출해야 한다.** `export_Garmin_data_30days.py` 의 `login_api()` 는 credential 경로에서만 dump 하고 token 경로에서는 하지 않는다 |
| 주의 | 0.5.x 의 `dump()` 는 **oauth1 파일까지 덮어쓴다**(`oauth2_only` 옵션 없음). 내용은 안 바뀌지만 백업이 필수인 이유가 하나 더 생긴다 |

> 버전 고정 상태: `garth 0.5.21` (`garminconnect` 0.2.38 이 선언한 `>=0.5.17,<0.6.0` 범위 안). 0.8.0 은
> deprecated 경고가 뜨고 동작이 달라, 이 범위를 유지한다.

- 갱신이 파일을 덮어쓰므로, **최초 실행 전에 토큰 폴더를 1회 백업**해 둔다 (`token_backup_20260914/`).
  oauth1 은 재발급이 불가능한 유일한 자산이다.

### 운영 규칙
1. **토큰 원본은 운영 서버 한 곳.** `/home/operator/.garmin_<account>`, `chmod 600`, **`.gitignore`에 먼저 추가**(비밀 정보).
2. **같은 토큰을 두 곳에서 동시에 쓰지 않는다.** garth는 갱신할 때 토큰 파일을 **덮어쓴다** — 젯슨 폴러와
   윈도우 `dashboard.py` 를 같은 계정으로 동시 운영하면 한쪽 파일이 낡아 401이 난다.
   윈도우 `token/` 폴더는 손대지 않는 백업으로만 둔다.
3. **복사는 무해하다.** `scp` 자체는 유효성에 영향이 없다. 바뀌는 건 *사용해서 갱신될 때*뿐.
4. ~~`refresh_token_expires_at` 을 화면에 노출한다~~ → **철회 (2026-09-14 구현 시점 판단).**
   garth 0.5.x 는 그 refresh token 을 **쓰지 않는다**(oauth1 으로만 교환). 따라서 그 날짜는 실제
   안전 여부와 무관해서, 화면에 띄우면 오히려 '아직 10월까지 괜찮다'는 잘못된 안심을 준다.
   - 대신 채택한 방식: **폴링이 성공했다는 사실 자체가 oauth1 생존 증명**이다(매 실행마다 교환하므로).
     실패하면 폴러가 `auth_error` 를 올리고, 디스코드로 `🔧 토큰 갱신 필요` 가 즉시 간다.
   - 여기에 더해, 디스코드 판정은 저장된 `connected` 가 아니라 **마지막 동기화 시각**으로 다시 잰다.
     폴러 프로세스 자체가 죽어 아무 보고도 없는 경우까지 잡기 위함 (§6 구현 메모).
5. **복구 절차** (장기 중단 후 토큰이 죽은 경우): 윈도우에서 해당 계정 재로그인 → 토큰 폴더 scp → 폴러 재시작.

---

## 10. 폴링 시 지켜야 할 것

이관받은 코드에 들어 있는 sleep·stagger는 전부 **실제로 차단당해서** 넣은 것이다. 그대로 유지한다.

| 항목 | 값 | 이유 |
|---|---|---|
| 엔드포인트 호출 간격 | 0.5초 | 레이트리밋 |
| 날짜 간 간격 | 2초 | 레이트리밋 |
| 계정별 스레드 시작 간격 | 0.5초 (stagger) | 동시 TLS 핸드셰이크 → `SSLZeroReturnError` |
| 계정당 타임아웃 | 45초 | — |
| 폴링 주기 | **30~60분, 당일+전일만** | 과거 날짜를 매번 다시 긁지 않는다 |
| SSL 우회 | `_GarminAdapter` (`OP_IGNORE_UNEXPECTED_EOF`) | Garmin 서버가 close_notify 없이 연결을 닫아 Python 3.12+ 에서 오류 |

---

## 11. 단계별 진행 계획

| 단계 | 내용 | 산출물 | 서버 영향 |
|---|---|---|---|
| ~~1. PoC~~ **✅ 2026-09-14 완료** | 익명 계정 A로 조회 성공 | oauth1 생존 확인 · 지표 가용성 실측(§3) · 토큰 저장 문제 발견(§9) | 없음 |
| ~~1-b. 원본 JSON 확보~~ **✅ 2026-09-14 완료** | `fetch_garmin_raw.py`로 익명 계정 A·B의 2일치 수집 (`garmin_raw/`) | 컬럼 매핑 확정(§5) · steps/pushes 규명(§3) · 중복 폴링 문제 발견(§5) · 수면 단계 확인(§12) | 없음 |
| ~~2. 파서·저장~~ **✅ 완료** | `garmin_parser.py` · `garmin_poller.py` · `_store_garmin_record` · `POST /garmin` | 커밋 `9ba7ee5` | 로그 파일 1개 추가 |
| ~~3. 화면~~ **✅ 완료** | 카드 섹션 + 전용 렌더러 + V2 섹션 + 상세페이지 | 커밋 `e2935d8` | 대시보드 변경 |
| ~~4. 알림·리포트~~ **✅ 완료** | 디스코드 기준 + 토큰 만료 감지 + `_Garmin.csv` | 커밋 `e84e9f2` | 알림 규칙 추가 |
| ~~5. 문서~~ **✅ 완료** | MANUAL · CHANGELOG · VERSION_HISTORY, `VERSION` → **3.20.0** | — | — |
| **6. 젯슨 배포** | 패키지 설치 → 토큰 이전 → systemd timer → 회귀 확인 | **← 다음 할 일** | 실서버 |

### 6단계 배포 절차 (예정)
```bash
# 1) 패키지 (젯슨 venv)
~/emfit_server/venv/bin/pip install "garminconnect==0.2.38" "garth>=0.5.17,<0.6.0"

# 2) 토큰 이전 (윈도우에서) — 원본은 젯슨 한 곳에만 두는 것이 원칙(§9)
scp -O -r token/.garmin_<account>* operator@jetson-host:~/
#    젯슨에서: chmod 700 ~/.garmin_*; chmod 600 ~/.garmin_*/*

# 3) 코드 전송 — 다섯 파일이 한 세트다 (하나라도 빠지면 import 실패로 앱이 안 켜진다)
scp -O app.py analyzer.py garmin_parser.py garmin_poller.py operator@jetson-host:/opt/monitoring_server/

# 4) 재시작 전 검사 → 통과할 때만 재시작 (MANUAL §6.7)
# 5) 폴러 수동 1회: python3 garmin_poller.py --all-accounts --dry-run  → 이상 없으면 --dry-run 빼고 실행
# 6) systemd timer 등록 (30~60분 주기), 카드 확인, 기존 기기 회귀 확인
```

> **1단계가 가장 중요하다.** 실제 응답 JSON 구조를 확보하기 전에 파서를 쓰면 추측이 된다.
> McKare 이미지 규격을 몰라 세 가지 방식을 모두 받아둬야 했던 상황을 반복하지 않기 위함.

---

## 12. 부수적으로 발견한 실증 가치

PoC 데이터를 보다 확인한 것 — 연동의 부차 목적으로 삼을 만하다.

**Garmin 은 거동이 불편한 대상자에게도 수면 단계를 실제로 산출한다** (2026-09-14 원본 확인).

| 09-13 수면 | 계정 A (휠체어) | 계정 B (보행) |
|---|---|---|
| 총수면 | 231분 | 143분 |
| 깊은수면 | **30분** | 65분 |
| REM | **24분** | 15분 |
| 얕은수면 | 177분 | 63분 |
| 수면점수 | 49 (POOR) | 31 |

이게 의미 있는 이유: MANUAL §9의 알려진 한계 — **일부 대상자의 EMFIT 수면 단계 분류가
실패해 REM/깊은수면이 대부분 0** — 을 Garmin 이 보완할 수 있음이 데이터로 확인됐다. 휠체어를 쓰는
익명 계정 A에게 깊은수면 30분·REM 24분이 정상적으로 산출된다.

→ **"침대 센서가 수면 단계를 못 읽는 대상자에게 워치가 답을 줄 수 있는가"** 가 검증 가능한 실증 질문이 된다.
같은 사람에게 두 기기가 동시에 붙는 구조(§8 매핑)이므로 직접 대조가 가능하다.
(지금 구현할 것은 아니고, 데이터가 쌓인 뒤 분석 과제)

---

## 13. 미결정 사항

1. **새 계정의 사용자가 누구인가** — 자동 등록으로 두고 `/devices`에서 이름 지정하기로 결정(2026-09-14). 확인되면 즉시 매핑.
2. **폴링 주기** — 30분 / 60분. 워치 동기화 자체가 수 시간 단위라 60분도 충분할 수 있다.
3. **2분 간격 심박 전량 보관 여부** — 채택하기로 결정했으나, 워밍업 시간 증가가 체감되면 일별 요약만 상시 +
   분 단위는 최근 N일만 유지하는 방식으로 후퇴 가능.
4. **공식 API 전환** — 현재 라이브러리는 **비공식 Connect 접근**이다(공식 앱과 같은 OAuth를 흉내내는 방식).
   약관·차단 리스크와 계정별 토큰 관리 부담이 있다. 실증을 넘어 서비스화한다면 Garmin 공식
   Health/Wellness API(파트너 계약 + 웹훅 push) 검토가 필요하다 — 계약 조건은 미확인.
   - ⚠️ **2026-09-14 확인: 인증을 담당하는 `garth` 가 deprecated 되었다** (import 시
     `Garth is deprecated and no longer maintained` 경고 — https://github.com/matin/garth/discussions/222).
     당장 동작은 하지만 Garmin 이 인증 방식을 바꾸면 고쳐줄 주체가 없다. 공식 API 검토의 근거가 하나 늘었다.

5. **garth 버전** — `garminconnect` 0.2.38 은 `garth>=0.5.17,<0.6.0` 을 요구한다. 이 PC 에는 **0.8.0** 이
   설치돼 있다(2026-09-14). 동작 여부는 미확인이므로, PoC 실패 시 원인을 토큰 문제와 혼동하지 않도록
   **선언된 범위로 핀 고정**한 상태에서 먼저 돌린다: `pip install "garth>=0.5.17,<0.6.0"`
