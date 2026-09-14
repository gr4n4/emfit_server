# Garmin 스마트워치 통합 설계안 (돌봄기기 통합 대시보드)

> 목적: A시설 실증 대상자가 착용 중인 Garmin 워치 데이터를 기존 대시보드에 **5번째 기기 종류**로 붙인다.
> 상태: **설계만 (코드 미반영).** 2026-09-14 작성, 기준 서버 버전 v3.19.2.
> 원본 자산: `PRIVATE_GARMIN_SOURCE` (다른 연구자가 작업한 것을 이관받음)

---

## 0. 한 줄 요약

기존 4종은 **기기가 서버로 밀어 넣는다**. Garmin은 그게 안 되므로 **서버가 Garmin 클라우드에서 당겨오는
수집기(폴러) 한 개**를 새로 두고, 그 뒤부터는 McKare·FSR을 붙였던 경로(`_process_line` → 파서 →
`add_to_storage`)를 그대로 재사용한다. 화면·리포트·알림·배정 이력은 손댈 필요 없이 종류 하나가 늘어난다.

**연동의 실질적 가치**: 같은 대상자의 **EMFIT(야간 침대) + Garmin(주간 활동)** 이 같은 이름·위치로
한 화면에 나란히 뜬다. 지금은 두 시스템에 쪼개져 있다.

---

## 1. 이관받은 자산 — 무엇을 쓰고 무엇을 버리는가

`garmin` 폴더는 `cyberjunky/python-garminconnect` v0.2.38 포크(`hanbl0502-lab/garminconnect-0.2.38`)에
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
| `token/.garmin_example-account-01~0007` | 계정별 OAuth 토큰 | ✅ **필수** (§7) |

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
example-account-03_..._heart_rate.csv
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
| SN 규칙 | **`garmin-example-account-04`** 형태 |
| kind | `analyzer.KIND_GARMIN = "garmin"` 신설. 배정 정보에 명시 |
| dtype | `"Garmin"` |

> ⚠️ **SN을 12자리 16진수로 만들면 안 된다.** `_is_radar_device()`([app.py:752](../app.py#L752))가 AI Radar로
> 오인한다 (ESP32 MAC과 레이더가 같은 모양이라 이미 겪은 문제 — [app.py:783](../app.py#L783) 주석 참고).
> 종류는 SN 모양으로 추측하지 않고 배정 정보의 `kind` 로 판정한다.

### 저장 컬럼 (`add_to_storage(storage, sn, ts, "Garmin", {...})`)
심박수(HR) · 안정시심박 · 걸음수 · 이동거리(m) · 수면점수 · 총수면(분) · 스트레스 · 바디배터리 · 호흡수(RR) ·
상태설명 — 측정하지 않는 칸(활동량 ACT, RMSSD 등)은 **만들지 않는다** (CSV에 빈 컬럼이 남지 않게).
`add_to_storage()` 의 dtype 분기([analyzer.py:491](../analyzer.py#L491))에 `"Garmin"` 케이스 추가.

### 타임스탬프
Garmin은 GMT ISO 문자열(`2026-09-14T05:00:00.0`)과 epoch ms 가 섞여 온다. `add_to_storage()` 는
epoch(초/밀리초 자동 판별)를 받으므로 파서에서 변환해 넘긴다. KST 변환 로직은 `health_panels.py` 에 이미 있다.

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
│ A시설 1            🟢    │
│ 사용자-A님                     │
│ ❤️ 안정 │ 👣 걸음 │ 😴 수면 │
│   53    │  4,820  │   72    │
│   🟢 동기화 정상             │
│  측정: 09-14 14:20          │
│  동기화: 2시간 전            │
│  garmin-example-account-04       │
└─────────────────────────────┘
```

- **HR/RR/ACT 3칸 형식을 쓰지 않는다.** 돌봄 맥락에서 의미 있는 값은 안정시 심박·걸음수·수면점수다.
  (FSR이 전용 렌더러를 따로 만든 것과 같은 이유. 호흡수도 Garmin이 제공하므로 필요하면 교체 가능)
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
| 계정 | 최근 30일 중 데이터 있는 날 | 닉네임 |
|---|---|---|
| example-account-03 | 20일 | A시설2 박O희님 |
| example-account-04 | 29일 | A시설1 사용자-A님 |
| example-account-05 | 25일 | **미지정 — 누구인지 확인 필요** |
| example-account-06 | 26일 | **미지정 — 누구인지 확인 필요** |
| 0001 · 0002 · 0007 | 0~1일 | 없음 (미사용으로 보임) |

### 자동 등록 방침 (코드 수정 없이 계정 추가)
FSR의 `_ensure_fsr_device()` 패턴을 그대로 따른다:
1. 폴러가 토큰 폴더를 발견 → SN `garmin-example-account-05` 생성
2. 첫 데이터 수신 시 **자동 등록** → 대시보드에 `Garmin example-account-05` 카드로 뜸
3. `/devices` 화면에서 이름·위치 지정 → `A시설1 사용자-A님` 으로 표시

**나중에 계정을 추가할 때 서버 코드를 건드릴 필요가 없다.** 토큰 폴더만 젯슨에 올리면 된다.
데이터가 없는 계정(0001·0002·0007)은 기존 **7일 규칙**으로 자동으로 `💤 비활성 기기`로 내려가므로
화면이 지저분해지지 않는다.

---

## 9. 토큰 운영 (가장 주의할 부분)

### 확인된 상태 (2026-09-14, 파일만 확인 — 네트워크 호출 안 함)
| 계정 | oauth2 발급 | access 만료 | **refresh 만료** |
|---|---|---|---|
| 0003 | 2026-09-10 08:19 | 09-11 13:40 | **2026-10-10 08:19** |
| 0004·0005·0006 | 2026-09-11 08:27 | 09-12 05:47~14:00 | **2026-10-11 08:27** |

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
- 뒤집어 보면 9월 10~11일에 oauth2 가 발급된 기록은 **그날 oauth1 이 살아있었다는 직접 증거**다.
  (그 발급 자체가 oauth1 교환의 결과이므로)
- 갱신이 파일을 덮어쓰므로, **최초 실행 전에 토큰 폴더를 1회 백업**해 둔다. oauth1 은 재발급이 불가능한
  유일한 자산이다.

### 운영 규칙
1. **토큰 원본은 젯슨 한 곳.** `/home/operator/.garmin_example-account-*`, `chmod 600`, **`.gitignore` 에 먼저 추가**(비밀 정보).
2. **같은 토큰을 두 곳에서 동시에 쓰지 않는다.** garth는 갱신할 때 토큰 파일을 **덮어쓴다** — 젯슨 폴러와
   윈도우 `dashboard.py` 를 같은 계정으로 동시 운영하면 한쪽 파일이 낡아 401이 난다.
   윈도우 `token/` 폴더는 손대지 않는 백업으로만 둔다.
3. **복사는 무해하다.** `scp` 자체는 유효성에 영향이 없다. 바뀌는 건 *사용해서 갱신될 때*뿐.
4. **`refresh_token_expires_at` 을 화면에 노출한다.** 갱신할 때마다 미래로 밀리므로, `/devices` 나 카드
   상세에 `토큰 유효: 2026-10-10` 형태로 표시하면 oauth1 만료일을 몰라도 안전 여부를 눈으로 확인할 수 있다.
   이 날짜가 **7일 이내로 다가오면** = 수집이 2~3주 멈췄다는 뜻 → 디스코드 알림.
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
| **1. PoC** (10월 초 전) | 윈도우에서 토큰 1개로 폴러 프로토타입 실행 | **실제 Garmin 응답 JSON 샘플** + 토큰 갱신 검증 | 없음 (emfit 서버 안 건드림) |
| 2. 파서·저장 | `garmin_parser.py` + `_store_garmin_record` + `/garmin` + 폴러 젯슨 배치 | `garmin_data.jsonl` 에 데이터 축적 | 로그 파일 1개 추가 |
| 3. 화면 | 카드 섹션 + 전용 렌더러 + V2 섹션 | 대시보드에 워치 카드 | 대시보드 변경 |
| 4. 알림·리포트 | 디스코드 기준(24h) + 토큰 만료 감지 + `_Garmin.csv` | 완성 | 알림 규칙 추가 |
| 5. 문서 | MANUAL.md 기기 표·수신 경로·트러블슈팅, CHANGELOG, `VERSION` → 3.20.0 | — | — |

> **1단계가 가장 중요하다.** 실제 응답 JSON 구조를 확보하기 전에 파서를 쓰면 추측이 된다.
> McKare 이미지 규격을 몰라 세 가지 방식을 모두 받아둬야 했던 상황을 반복하지 않기 위함.

---

## 12. 미결정 사항

1. **0005·0006이 누구인가** — 자동 등록으로 두고 `/devices` 에서 이름 지정하기로 결정(2026-09-14). 확인되면 즉시 매핑.
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
