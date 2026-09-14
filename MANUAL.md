# 돌봄기기 통합 관제 서버 매뉴얼

EMFIT QS · AI Radar · McKare · 돌봄기기 사용감지(FSR) · Garmin 워치 다섯 종류 데이터를
수집·관제하고 리포트·알림을 제공하는 서버의 운영 및 사용 문서.

> 기준 버전 **v3.21.0** (2026-09-14). 기술 변경 내역은 [CHANGELOG.md](CHANGELOG.md),
> 개발 여정 요약은 [VERSION_HISTORY.md](VERSION_HISTORY.md), 코드 작업 규칙은 [CLAUDE.md](CLAUDE.md) 참고.

---

## 0. 빠른 링크

기본 주소: `http://monitoring.example.com` — **아래 화면은 모두 관리자 로그인이 필요하다** (§2.1).

| 화면 | 주소 | 용도 |
|---|---|---|
| 통합 관제 대시보드 | `/dashboard` | 기기별 카드 실시간 현황 (15초 자동 갱신) |
| 신규 디자인 (미리보기) | `/dashboard2` | 새 UI 시험용. 기존 화면은 그대로 유지 |
| 리포트 다운로드 | `/reports` | 날짜·기기별 CSV / ZIP |
| 기기 정보 관리 | `/devices` | 이름·위치·그룹 수정, 기기 이전(배정) |
| 사용자 URL 관리 | `/admin/tokens` | 개인용·그룹용 접속 URL 발급 |
| 디스코드 알림 설정 | `/admin/discord` | Webhook·끊김 기준·시설 채널 배정 |
| 의견(피드백) | `/feedback` | 사용자 의견 접수·답글 |
| 사용 가이드 | `/help` | 일반 사용자용 간단 안내 |
| FSR 노드 설정 | `/fsr-nodes` | 사용감지 노드 하트비트·임계값 원격 설정 |
| FSR 실시간 튜닝 | `/fsr-tune` | 사용감지 압력 임계값 실시간 조정 |
| 원본 데이터(디버깅) | `/dashboard/raw` | 수신 로그 파일 마지막 줄 확인 |

---

## 1. 시스템 개요

### 1.1 무엇을 하는 서버인가

센서가 HTTP POST 로 보내는 생체·재실·사용 데이터를 받아 파일에 쌓고,
① 실시간 관제 대시보드 ② 날짜별 CSV 리포트 ③ 디스코드 끊김 알림 ④ NRCarec 낙상 경보를 제공한다.

> **Garmin 워치만 예외다.** 워치는 폰을 거쳐 Garmin 클라우드까지만 가고 서버로 직접 보내지
> 못해서, 젯슨에서 `garmin_poller.py` 가 주기적으로 **당겨온다**. 그래서 수 시간 지연이 정상이다.

### 1.2 데이터 흐름

```
[EMFIT QS]  [AI Radar]  [McKare]  [ESP32 사용감지]        [Garmin 워치]
                                                              │ (폰 경유)
                                                      [Garmin 클라우드]
                                                              ↑ 폴링(30~60분)
     └───────────┴──── HTTP POST ──┴───────────┘
                        │
                        ▼
        [DDNS: monitoring.example.com]
        [집 공유기 TP-Link] 외부80 → 내부8080 포트포워딩
                        │
                        ▼
        [Jetson Orin Nano: JETSON_HOST]
        ├─ systemd `emfit`              → FastAPI(app.py) + 분석(analyzer.py)
        │   ├─ emfit_data.jsonl         ← EMFIT QS
        │   ├─ radar_data.jsonl         ← AI Radar
        │   ├─ mckare_data.jsonl        ← McKare (+ mckare_images/)
        │   ├─ fsr_data.jsonl           ← 사용감지 센서
        │   └─ garmin_data.jsonl        ← Garmin 워치 (폴러가 넣어줌)
        ├─ systemd `emfit-garmin-poller`→ Garmin 클라우드 조회 → POST /garmin
        └─ systemd `emfit-discord-bot`  → 디스코드 슬래시 명령어 봇
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
  [웹 대시보드]   [디스코드 알림]   [NRCarec 앱 낙상 경보]
   / 리포트        (Webhook·봇)      (Firestore + FCM 푸시)
```

### 1.3 기기 종류별 수신 경로

| 종류 | 수신 경로 | 로그 파일 | 전송 주기 | 인증 |
|---|---|---|---|---|
| EMFIT QS (침대 매트) | `POST /` | `emfit_data.jsonl` | 약 30초 | 없음 |
| AI Radar (라닉스 RMR602A) | `POST /radar` | `radar_data.jsonl` | 1~50초 | 없음 |
| McKare (VSR22 / AI 110) | `POST /mckare` | `mckare_data.jsonl`, `mckare_images/` | 기기 설정 | ApiKey (파일 있을 때만) |
| 돌봄기기 사용감지 (ESP32 FSR) | `POST /jy01` | `fsr_data.jsonl` | **주기 없음 — 사용 시작/종료 때만** | 없음 |
| **Garmin 워치** | `POST /garmin` (**localhost 전용**) | `garmin_data.jsonl` | 폴러가 30~60분마다 조회 | 계정별 OAuth 토큰 |

### 1.4 현재 등록된 기기

`device_info.json` 기준 (2026-09-10):

| 기기 SN | 이름 | 위치 | 그룹 | 종류 |
|---|---|---|---|---|
| EMFIT-DEMO-02 | A시설 1 - 돌봄자 | A시설 1 | 일반 | EMFIT QS |
| EMFIT-DEMO-05 | A시설 1 - 돌봄받는자 | A시설 1 | 일반 | EMFIT QS |
| EMFIT-DEMO-01 | A시설 2 - 돌봄자 | A시설 2 | 일반 | EMFIT QS |
| EMFIT-DEMO-03 | A시설 2 - 돌봄받는자 | A시설 2 | 일반 | EMFIT QS |
| EMFIT-DEMO-04 | 사용자-D | 사용자-D님 가정 | 뇌성마비 | EMFIT QS |
| A1B2C3D4E5F8 | AI Radar | - | 일반 | AI Radar |
| fb-A3F2 | 돌봄기기 1 | - | 일반 | 사용감지(FSR) |
| garmin-example-account-03 … | (폴러 가동 후 자동 등록) | - | 일반 | Garmin 워치 |

> **기기 정보는 `/devices` 화면에서 수정한다.** 값은 `device_info.json` 에 저장되며,
> 코드(`analyzer.py` 의 `_DEFAULT_DEVICE_INFO`)는 파일이 아예 없을 때 쓰는 초기값일 뿐이다.
> 과거 사용 이력은 `assignments.json` 에 따로 쌓인다 (§6.6).

---

## 2. 사용자 편 — 대시보드 보는 법

### 2.1 접속과 권한

세 가지 접속 방법이 있다.

| 방법 | 주소 | 보이는 범위 |
|---|---|---|
| 관리자 로그인 | `/login` → `/dashboard` | 전체 기기 + 모든 관리 화면 |
| 그룹(시설) URL | `/v/{토큰}` | 관리자가 묶어준 기기들만 |
| 개인 URL | `/d/{토큰}` | 그 기기 하나만 |

- 관리자 ID 는 `operator` (환경변수 `EMFIT_ADMIN_USER` 로 변경 가능), 비밀번호는 젯슨의
  `admin_password.txt` 파일에 있다. 파일이 없으면 서버가 켜질 때 무작위로 만들어 로그에 한 번 출력한다.
  비밀번호를 바꾸려면 그 파일을 직접 편집한다.
- **서버를 재시작하면 로그인 세션이 모두 풀린다** (세션 서명키를 부팅할 때마다 새로 만들기 때문). 다시 로그인하면 된다.
- 개인·그룹 URL 은 `/admin/tokens` 에서 발급·회수한다. 개인 URL 사용자는 **현재 배정 기간의 데이터만** 받을 수 있다.

### 2.2 화면 구성

대시보드는 기기 종류별로 구역이 나뉘고, 15초마다 자동 갱신된다.

| 구역 | 내용 |
|---|---|
| ❤️ **EMFIT QS** | 심박 · 호흡 · 움직임 등 생체정보 중심 |
| 📡 **AI Radar** | 누움 · 앉음 · 걸터앉음 · 자리비움 · 낙상 등 자세정보 중심 |
| 🔘 **돌봄기기 사용 감지** | 압력 센서 · 사용 중/미사용 · 배터리 잔량 (등록된 기기가 있을 때만 표시) |
| ⌚ **Garmin 워치** | 안정시 심박 · 활동 · 수면 (등록된 계정이 있을 때만 표시) |

- 화면 오른쪽 위 요약: `N / M 연결됨 · EMFIT n대 · Radar n대 · 사용감지 n대`
- **7일 이상 통신이 없는 기기**는 각 구역 아래 `💤 비활성 기기` 로 따로 모인다.
- `/devices` 에서 숨김 처리한 기기는 카드에서만 빠지고, 데이터·리포트에는 그대로 남는다.

### 2.3 카드 구성 요소 (EMFIT QS)

```
┌─────────────────────────┐
│ 사용자-D님 가정       🛌  │ ← 위치 / 상태 아이콘
│ 사용자-D                  │ ← 사용자 이름
│                         │
│  ❤️ HR │ 🫁 RR │ 🏃 ACT │ ← 최근 측정값
│   68   │  12   │   5    │
│                         │
│    🛌 재실               │ ← 판정된 상태
│   측정: 08:13 (1시간 전) │ ← 마지막 측정 시각
│   통신: 연결됨 (방금)    │ ← 장비 연결 상태
│   EMFIT-DEMO-04                │ ← 기기 SN
└─────────────────────────┘
```

AI Radar 카드는 `🏃 ACT` 자리에 `🧭 자세`(누움/앉음/걸터앉음 등)를 보여주고 이름 옆에 `AI Radar` 배지가 붙는다.
사용감지 카드는 생체신호를 재지 않으므로 HR/RR/ACT 칸이 없고, **사용 상태와 배터리**만 표시한다.

### 2.4 상태 아이콘과 판정 기준

**EMFIT QS**

| 아이콘 | 상태 | 판정 기준 |
|---|---|---|
| 🛌 | 재실 | 위 조건에 해당하지 않는 정상 측정 중 |
| 🛏️ | 부재 | ACT < 1 (침대에 없음) **또는** 10분 이상 새 측정값 없음 |
| 🔴 | 끊김 | 장비 하트비트가 `connected=false` |
| ❓ | 상태 없음 | 하트비트 기록이 한 번도 없음 |
| (텍스트) | 설치됨 · 측정 대기 | 통신은 되지만 측정된 사람이 아직 없음 |

**AI Radar**

| 아이콘 | 상태 | 판정 기준 |
|---|---|---|
| 🛌 | 재실 | 정상 감지 중 |
| 🚨 | 낙상 | 낙상 감지 (POS=4) |
| 🚪 | 자리비움 | POS=5 |
| 📡 | 감지 대기 | POS=-1 |
| ⏱️ | 수신 지연 | 10분 이상 새 측정값 없음 |
| 🔴 | 끊김 | `connected=false` |

**돌봄기기 사용감지 (FSR)**

| 아이콘 | 상태 | 판정 기준 |
|---|---|---|
| 🟢 | 사용 중 | 눌림(press) 신호 수신 후 해제까지 |
| ⚪ | 미사용 | 해제(release) + 이상 없음 |
| 🔧 | 센서 확인 필요 | ① 펌웨어가 센서 이상 보고 ② 배터리 5% 이하 ③ 생존신고 보내던 보드가 1시간 침묵 |
| 📻 | 판정 대기 | 사용 여부를 알 수 없는 이벤트(생존신고 등) |

> ⚠️ **조용한 것만으로는 경고하지 않는다.** 사용감지 센서는 하루 종일 안 쓰는 게 정상일 수 있어서,
> 위 세 가지 확실한 근거가 있을 때만 '확인 필요'를 띄운다. 배터리 표시는 15% 이하 🪫 빨강, 30% 이하 주황.

**Garmin 워치**

| 아이콘 | 상태 | 판정 기준 |
|---|---|---|
| 🟢 | 동기화 정상 | 마지막 동기화가 24시간 이내 |
| 🟡 | 동기화 지연 | 24~72시간 — 폰 앱에서 동기화 확인 |
| 🔴 | 미동기화 | 72시간 초과 — 워치 착용·충전 확인 |
| 🔧 | 토큰 갱신 필요 | 계정 인증 실패 — **관리자가 재로그인해야 풀린다** |

> ⚠️ **'동기화: 3시간 전'은 정상이다.** 워치는 폰을 거쳐 클라우드로 올라오므로 수십 분~수 시간
> 지연이 당연하다. 카드에 어느 날짜 값인지도 같이 뜬다(`동기화: 4시간 전 (09-13)`) —
> 워치가 아직 오늘 것을 올리지 않았으면 어제의 온전한 값을 보여준다.
>
> 카드 가운데 칸은 사람에 따라 **`🚶 걸음` / `🦽 밀기` / `🛋 좌식`** 으로 자동으로 바뀐다.
> Garmin 이 걸음과 밀기를 상호 배타로 집계해서, 휠체어를 쓰면 걸음이 아예 안 잡히기 때문이다.

### 2.5 측정 vs 통신 — 두 가지 시각의 차이

- **측정 시각**: 마지막으로 HR/RR/ACT(또는 자세) 값을 받은 시각
- **통신 시각**: 장비가 "나 살아있어요" 하트비트를 마지막으로 보낸 시각

같이 보면 상황 진단이 된다:
- 통신 O, 측정 최신 → 정상 사용 중
- 통신 O, 측정 오래됨 → 장비는 켜졌는데 사람이 침대에 없거나 측정 안 하는 중
- 통신 X → 장비 전원 꺼짐 / 네트워크 끊김

### 2.6 HR/RR/ACT 가 "-" 인 경우

신뢰할 수 없는 값이라 일부러 숨긴다:
- 부재(ACT < 1) / 10분 이상 측정 없음 / 장비 끊김
- AI Radar 는 자리비움·감지 대기·수신 지연·끊김일 때
- 사용감지 센서는 애초에 생체신호를 측정하지 않는다

### 2.7 신규 디자인 미리보기 (`/dashboard2`)

새 UI 를 시험하는 별도 주소다. 상태 판정·데이터 로직은 기존과 같고 화면만 다르며,
**McKare 전용 구역이 여기에만 있다.** A시설 등 실사용은 기존 `/dashboard`·`/view` 를 계속 쓴다.

---

## 3. 사용자 편 — 리포트 다운로드

### 3.1 사용법

`/reports` 접속 (대시보드 하단 `📊 리포트 다운로드` 버튼) →
① 시작일 / 종료일 선택(수집된 범위 내) ② 기기(사용자) 선택 ③ `ZIP 다운로드`

기기 목록은 **배정 기간 단위**로 뜬다. 같은 기기를 여러 사람이 번갈아 썼으면 기간별로 따로 고를 수 있고,
그 기간의 데이터만 나온다 (§6.6).

### 3.2 결과물

```
2026-04-01_to_2026-04-12_사용자-D님 가정_사용자-D.zip
├─ 2026-04-01_사용자-D님 가정_사용자-D_리포트.csv
├─ 2026-04-02_사용자-D님 가정_사용자-D_리포트.csv
└─ ...
```

- 데이터 없는 날짜는 자동으로 빠진다.
- **AI Radar 는 하루에 두 파일**이 나온다 — `..._Radar-BED.csv`(자세 7종 + 생체)와
  `..._Radar-FALL.csv`(자세 3종 + 감지 인원). 측정 성격이 달라 섞지 않는다.
- **Garmin 워치는 `..._Garmin.csv`** 로 나온다. 2분 간격 심박 행 + 하루 한 줄의 일별 요약이 들어 있다.
- CSV 는 BOM 붙은 UTF-8 이라 엑셀에서 한글이 깨지지 않는다.

---

## 4. 사용자 편 — CSV 데이터 이해

### 4.1 공통 컬럼

| 컬럼 | 의미 |
|---|---|
| 날짜 | YYYY-MM-DD |
| 시간(KST) | HH:MM:SS (한국 시간) |
| 사용자 / 위치 | 그 시각의 배정 정보 기준 |
| 유형 | Live / HRV / SleepDetail / Summary / Radar / McKare / FSR |
| 심박수(HR) / 호흡수(RR) | 분당 값 |
| 활동량(ACT) | 활동 지표 (0 = 부재 추정) |
| 심박변이도(RMSSD) | HRV 지표 (HRV 행만) |
| 상태설명 | 측정 종류 설명 |
| 수면점수 · 총수면(분) · REM/깊은/얕은수면(분) · 각성시간(분) | Summary 행만 |

AI Radar 는 자세·낙상·감지인원·Radar모델(bed/fall) 컬럼이, McKare 는 재실코드·체온이,
사용감지는 사용중·배터리 관련 컬럼이 추가로 채워진다. 해당 형식이 쓰지 않는 컬럼은 파일에서 빠진다.

**Garmin 워치**는 안정시심박·최저/최고심박·호흡수·산소포화도·걸음수(또는 밀기)·이동거리·
좌식/활동/고강도(분)·스트레스·바디배터리 컬럼과, **Emfit 과 같은 이름의 수면 컬럼**
(수면점수·총수면(분)·깊은수면(분)·REM수면(분)·얕은수면(분)·각성시간(분))을 채운다.
같은 사람의 침대 센서 수면과 워치 수면을 같은 컬럼에서 바로 대조하려고 일부러 이름을 맞췄다.

### 4.2 유형별 데이터

- **Live**: 실시간 분 단위 측정 (HR/RR/ACT)
- **HRV**: 심박변이도 분석 결과 (주기적으로 산출)
- **SleepDetail**: 수면 완료 후 산출된 분 단위 후처리 데이터 (Live 와 시간 겹치면 Live 는 제외)
- **Summary**: 수면 세션 요약(점수·구간별 시간). CSV 맨 위로 올려 표시
- **Radar / McKare / FSR**: 각 기기가 보낸 자세·재실·사용 이벤트

---

## 5. 알림

### 5.1 디스코드 끊김 알림 (`/admin/discord`)

| 설정 | 설명 |
|---|---|
| 기본 Webhook | 채널 배정이 없는 기기의 알림이 가는 곳 |
| 시설(채널)별 Webhook | `A시설` 처럼 채널을 등록하고 기기를 배정 |
| 끊김 기준 시간 | 기본 **30분**. 보고 주기가 긴 기기는 기기별로 따로 늘릴 수 있다 |
| 테스트 전송 / 기준 재설정 | 화면 버튼으로 즉시 확인 |

동작 방식:
- 서버가 **1분마다** 점검한다. 설정을 바꾸면 재시작 없이 다음 점검부터 반영된다.
- **끊김을 감지해도 바로 보내지 않는다** — 같은 시간만큼 더 지켜보고 그래도 복구가 안 되면 보낸다
  (총 대기 ≈ 기준 × 2). 하트비트가 잠깐 늦었다가 곧 정상화될 때 끊김·복구 알림이 연달아 뜨는 것을 막는다.
- 기기 종류마다 끊김 판정 근거가 다르다:

| 종류 | 판정 근거 |
|---|---|
| EMFIT QS · AI Radar | 하트비트에 실려오는 `connected` 값. 단 하트비트 자체가 **6시간** 넘게 없으면 끊김으로 본다 |
| 사용감지(FSR) | 펌웨어가 보낸 이상(fault/disconnect) 이벤트 |
| McKare | 끊김 신고 수단이 없어 **무소식만으로 알림을 만들지 않는다** |
| Garmin 워치 | **마지막 동기화가 24시간 초과**. 확인 대기는 기본 120분(폴링 주기가 길어서) |

> 이유: EMFIT·Radar 는 사람이 침대에 없으면 하트비트가 원래 뜸하게 온다. "시간이 지났다"로 판정하면
> 멀쩡한 기기가 끊김으로 잡힌다(병동 실증에서 확인). 반대로 `connected` 만 믿으면 수신 경로가 죽어
> `true` 로 멈춘 기기는 영영 알림이 안 간다 — 그래서 6시간 안전장치를 함께 둔다.

알림 메시지 예: `🔴 (EMFIT QS) A시설 1 - 돌봄자 (A시설 1) 연결이 끊겼습니다`

### 배터리 부족 알림

사용감지 센서의 배터리가 설정한 기준 **이하**로 떨어지면 알린다. 기준은 `/admin/discord` 에서
끊김 기준 바로 아래에 있고, 기본값은 **20%** 다.

```
🪫 배터리 부족 — (돌봄기기 사용 감지) 돌봄기기 2 (A시설 1)
남은 배터리: 17% (기준 20%)
→ 배터리 교체가 필요합니다
```

- 교체·충전해서 **기준 + 5%** 이상으로 올라오면 `🔋 배터리 회복` 알림이 가고, 그때부터 다시
  알림을 받을 수 있는 상태가 된다. 사용 중에는 전압이 눌려 값이 오르내리는데, 기준선을
  오르내릴 때마다 알림이 번갈아 가면 정작 진짜 방전이 묻히기 때문이다.
- 끊김 알림과 달리 **확인 대기 시간이 없다.** 끊김은 잠깐 지연됐다 복구되는 일이 흔하지만,
  배터리 잔량은 순간적으로 튀었다 돌아오는 값이 아니다.
- 배터리를 측정하지 않는 보드(`-1` 을 보내는 기기)는 알림 대상이 아니다.
- 디스코드에서 `/배터리` 를 치면 언제든 전체 잔량을 볼 수 있다:

```
🔋 배터리 잔량 (교체 기준: 20% 이하)
전체 4대 · 🪫 교체 필요 2대

🔴 돌봄기기 3(A시설 2) - 4%  ·  3일 전 기준
🪫 돌봄기기 2(A시설 1) - 17%
🔋 돌봄기기 4 - 31%
🟢 돌봄기기 1(우리집) - 62%
```

> ⚠️ **`3일 전 기준` 표시에 주의.** 사용감지 센서는 신호를 보낼 때만 배터리 값이 갱신된다.
> 24시간 넘게 조용했던 기기는 그 값이 언제 것인지 함께 표시하므로, 그 숫자를 지금 잔량으로
> 믿으면 안 된다.

**Garmin 토큰이 만료되면 다른 메시지로 온다**:
`🔧 토큰 갱신 필요 — (Garmin 워치) A시설1 사용자-A님` + `Garmin 재로그인이 필요합니다`.
전원·네트워크 문제가 아니라 **관리자가 재로그인해야만 풀리는** 경우라 제목부터 구분한다.

### 5.2 디스코드 봇 슬래시 명령어

| 명령어 | 응답 |
|---|---|
| `/현황` | 전체 요약 + 끊긴 기기 목록 |
| `/상세보기` | 전체 기기 목록과 상태 |
| `/배터리` | 사용감지 센서 배터리 잔량 — **잔량이 적은 것부터** 정렬 |
| `/A시설` 처럼 **시설 이름** | 그 시설에 배정된 기기만 |

표시 형식: `🟢 김돌봄(우리집, EMFIT QS) - 방금`

> 시설별 명령어는 **봇이 시작할 때** 채널 목록을 읽어 만든다.
> `/admin/discord` 에서 시설을 추가·삭제했으면 `sudo systemctl restart emfit-discord-bot` 을 한 번 해야 반영된다.
> 봇은 대시보드(`emfit` 서비스)의 `/internal/discord/status` 를 조회해 동작하므로, 대시보드가 먼저 떠 있어야 한다
> (동시에 재시작한 경우 최대 1분간 재시도한다).

### 5.3 NRCarec 낙상 경보 ([nrcarec_alert.py](nrcarec_alert.py))

AI Radar 가 **낙상·걸터앉음**을 감지하면 NRCarec 앱으로 보낸다 (Firestore `notification_log` 기록 + FCM 푸시).
- 호실·이름은 그 레이더의 배정 정보에서 읽는다 (`421호 홍길동` 처럼 적어두면 호실을 분리해 쓴다).
- 같은 기기의 같은 경보는 **180초 쿨다운** — 낙상이 10초 이어져도 알림은 한 번.
- 앱에서 끈 알림(걸터앉음 off, 시간대 제한)은 보내지 않는다.
- 서비스 계정 키는 환경변수 `NRCAREC_SERVICE_ACCOUNT` 로 지정한다 (`emfit.service` 에 설정).

---

## 6. 관리자 편 — 서버 운영

### 6.1 SSH 접속

```bash
ssh operator@jetson-host
```

### 6.2 서비스 관리 (systemd)

| 서비스 | 역할 |
|---|---|
| `emfit` | FastAPI 대시보드·수집 서버 (포트 8080) |
| `emfit-garmin-poller` | Garmin 클라우드 조회 → `/garmin` 전송 (systemd timer, 30~60분 주기) |
| `emfit-discord-bot` | 디스코드 슬래시 명령어 봇 |

```bash
sudo systemctl status emfit                 # 상태 확인
sudo systemctl restart emfit                # 재시작 (코드 수정 반영)
sudo systemctl restart emfit-discord-bot    # 봇만 재시작 (시설 명령어 갱신)
systemctl list-timers emfit-garmin-poller   # Garmin 수집 타이머 상태·다음 실행 시각
sudo systemctl start emfit-garmin-poller    # Garmin 수집 즉시 1회 실행
systemctl is-active emfit                   # 한 단어 상태
```

재시작하면 수백 MB 로그를 다시 읽어야 해서 **30초~수 분간 "🛠️ 점검 중" 화면**이 뜬다(5초마다 자동 새로고침).
그동안에도 **센서 수신은 계속되므로 데이터 유실은 없다.** 로그에 `[startup] 파싱 완료 — 서버 준비됨` 이 뜨면 끝.

### 6.3 로그 보기

```bash
sudo journalctl -u emfit -f                        # 실시간
sudo journalctl -u emfit -n 50 --no-pager          # 최근 50줄
sudo journalctl -u emfit-discord-bot -n 50 --no-pager
sudo journalctl -u emfit --since "10 min ago" | grep POST
```

### 6.4 데이터 파일 모니터링

```bash
tail -f ~/emfit_server/emfit_data.jsonl      # EMFIT
tail -f ~/emfit_server/radar_data.jsonl      # AI Radar
tail -f ~/emfit_server/mckare_data.jsonl     # McKare
tail -f ~/emfit_server/fsr_data.jsonl        # 사용감지
tail -f ~/emfit_server/garmin_data.jsonl     # Garmin 워치

tail -f ~/emfit_server/emfit_data.jsonl | grep --line-buffered EMFIT-DEMO-04   # 특정 기기만
du -h ~/emfit_server/*.jsonl                                            # 파일 크기
```

웹에서도 볼 수 있다 — `/dashboard/raw` 에서 파일을 골라 마지막 몇 줄(최대 50줄)만 확인한다.
파일 크기·최종 갱신 시각도 같이 보여준다.

### 6.5 파일 구조

```
/opt/monitoring_server/
├─ app.py                  # FastAPI 서버 (대시보드·리포트·수신 경로·관리 화면 전부)
├─ analyzer.py             # 파싱 / 캐시 / 배정 이력 / 리포트 생성
├─ radar_parser.py         # AI Radar payload 해석 (BED/FALL 두 형식)
├─ mckare_parser.py        # McKare(VSR22) payload 해석
├─ fsr_parser.py           # 사용감지(ESP32) payload 해석
├─ garmin_parser.py        # Garmin 워치 payload 해석
├─ garmin_poller.py        # Garmin 클라우드 수집기 (timer 로 주기 실행)
├─ garmin_poll_state.json  # 계정별 '마지막 전송 심박 시각' (증분 전송용)
├─ discord_bot.py          # 디스코드 봇 (별도 프로세스)
├─ nrcarec_alert.py        # NRCarec 낙상 경보 전송
├─ *_data.jsonl            # 기기별 수집 로그 (append-only)
├─ mckare_images/          # McKare 열화상 이미지 (+ mckare_image_log.jsonl 목록)
├─ device_info.json        # 현재 기기 등록 정보
├─ assignments.json        # 기기 배정 이력
├─ discord_config.json     # 디스코드 Webhook·기준·채널 배정
├─ discord_alert_state.json# 끊김 알림을 이미 보낸 기기
├─ discord_battery_state.json # 배터리 부족 알림을 이미 보낸 기기
├─ device_tokens.json      # 개인 URL 토큰
├─ view_tokens.json        # 그룹 URL 토큰
├─ admin_password.txt      # 관리자 비밀번호
├─ mckare_apikey.txt       # McKare ApiKey (있을 때만 검증)
├─ gw_commands.json        # 사용감지 게이트웨이 명령 큐
├─ feedback.jsonl          # 사용자 의견
├─ MANUAL.md / CHANGELOG.md / CLAUDE.md / docs/
└─ venv/                   # 파이썬 가상환경
```

systemd 정의: `/etc/systemd/system/` 아래
`emfit.service` · `emfit-discord-bot.service` · `emfit-garmin-poller.service` · `emfit-garmin-poller.timer`

**Garmin 수집기 유닛** (젯슨 재설치 시 그대로 다시 만들면 된다):

```ini
# /etc/systemd/system/emfit-garmin-poller.service
[Unit]
Description=Garmin 워치 수집기 (emfit 대시보드)
After=network-online.target emfit.service

[Service]
Type=oneshot
User=operator
WorkingDirectory=/opt/monitoring_server
ExecStart=/opt/monitoring_server/venv/bin/python3 garmin_poller.py --all-accounts --days 3 --save-tokens
```
```ini
# /etc/systemd/system/emfit-garmin-poller.timer
[Unit]
Description=Garmin 수집 30분마다

[Timer]
OnBootSec=10min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

| 선택 | 이유 |
|---|---|
| `--days 3` | 워치 동기화가 하루 넘게 밀리는 일이 잦아서, 오늘·어제만 보면 놓친다 |
| `--save-tokens` | 없으면 30분마다 매번 재인증해 차단 위험. **단 `oauth1_token.json` 까지 덮어쓰므로 토큰 백업이 전제다** |
| `Persistent=true` | 정전으로 꺼져 있던 동안 건너뛴 실행을 부팅 후 한 번 따라잡는다 |
| `venv/bin/python3` 전체 경로 | 젯슨에 conda(`base`)가 깔려 있어 `python3` 만 쓰면 다른 파이썬이 잡힌다 |

### 6.6 기기 등록 정보 vs 배정 이력

| 파일 | 뜻 |
|---|---|
| `device_info.json` | **지금** 이 기기를 누가 쓰는가 (기기당 한 줄) |
| `assignments.json` | 이 기기를 **과거에** 누가, 언제부터 언제까지 썼는가 (기간별 여러 줄) |

데이터는 SN(기기 시리얼)으로만 들어오므로, 측정 시각을 배정 이력과 대조해 '그때 그 기기를 쓰던 사람'을 붙인다.
→ **기기를 다른 사람에게 옮겨도 과거 데이터가 섞이지 않는다.** 기기 이전은 `/devices` 화면의 이전 기능을 쓴다.

### 6.7 코드 수정 → 배포 워크플로우

**원본 위치 (윈도우)**: `C:\path\to\monitoring_server`

핵심 안전장치: **파일을 덮어써도 재시작 전까지는 기존 앱이 그대로 돈다.**
"복사 → (앱 멈추지 않고) 검사 → 통과할 때만 재시작" 순서를 지킨다.

```bash
# 0) 젯슨에서 되돌리기용 백업
mkdir -p ~/backup/pre_$(date +%y%m%d)
cp ~/emfit_server/{app.py,analyzer.py,assignments.json} ~/backup/pre_$(date +%y%m%d)/
```
```powershell
# 1) 윈도우에서 전송 (같이 고친 파일은 반드시 한 번에 — import 짝이 깨지면 앱이 안 켜진다)
cd C:\path\to\monitoring_server
scp -O app.py analyzer.py operator@jetson-host:/opt/monitoring_server/
```
```bash
# 2) 젯슨에서 재시작 전 검사 — 이 시점에도 기존 앱은 살아있다
cd ~/emfit_server
grep '^VERSION' app.py
python3 -c "import ast; [ast.parse(open(f,encoding='utf-8').read()) for f in ['app.py','analyzer.py']]; print('문법 OK')"

# 3) 통과하면 재시작 → 확인
sudo systemctl restart emfit && sleep 5 && systemctl is-active emfit
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/dashboard   # 200/401/503 = 응답 정상

# 4) 회귀 확인 — 기존 기기 카드 수와 통신 시각이 그대로 갱신되는지 대시보드에서 본다
```
검사에서 걸리면 **재시작하지 말고** 백업에서 되돌린다. 앱은 아직 이전 버전으로 잘 돌고 있다.

버전별 상세 절차 예시는 [docs/배포_가이드_3.5.0.md](docs/배포_가이드_3.5.0.md) 참고.

---

## 7. 트러블슈팅

### 7.1 대시보드가 안 뜸

1. `sudo systemctl status emfit` → active 인가? 아니면 `sudo journalctl -u emfit -n 30 --no-pager`
2. "점검 중" 화면이면 워밍업 중이다 — 몇 분 기다린다
3. 내부 IP 로 테스트: `http://JETSON_HOST:8080/dashboard`
   - 뜨면 네트워크/공유기 문제 / 안 뜨면 서비스 문제
4. DDNS 확인: `nslookup monitoring.example.com`
5. 공유기 포트포워딩: 외부 80 → JETSON_HOST:8080 (TCP)

### 7.2 로그인이 안 됨 / 갑자기 로그아웃됨

- 서버를 재시작하면 세션이 모두 풀린다 — 다시 로그인하면 정상이다.
- 비밀번호는 젯슨의 `admin_password.txt` 에 있다. 잊었으면 파일을 직접 열어 확인하거나 새 값으로 편집한다.

### 7.3 데이터가 안 들어옴

1. `tail -f ~/emfit_server/<해당>_data.jsonl` 에 새 줄이 찍히는지
2. `sudo journalctl -u emfit --since "5 min ago" | grep POST` 에 요청이 있는지
3. 없으면 공유기 재부팅 → 서비스 재시작 → 기기 전원·네트워크 확인
4. 대시보드에 🔴 끊김이면 **장비 쪽** 문제다

### 7.4 디스코드 알림이 안 옴 / 잘못 옴

- `/admin/discord` 에서 알림이 켜져 있고 Webhook 이 채워졌는지, 기기가 채널에 배정됐는지 확인
- 감지 후에도 기준 시간만큼 더 지켜보므로 **첫 알림까지 최대 기준×2** 걸린다 (기본 60분)
- McKare 는 설계상 무소식으로 알림을 만들지 않는다 (§5.1)
- 화면의 테스트 전송 버튼으로 Webhook 자체가 살아있는지 먼저 갈라본다

### 7.5 시설별 슬래시 명령어가 안 보임

봇을 재시작한다: `sudo systemctl restart emfit-discord-bot`.
대시보드가 아직 워밍업 중이면 봇이 최대 1분간 재시도하므로, 로그(`journalctl -u emfit-discord-bot`)에
`시설별 명령어 등록 실패` 가 찍혔으면 대시보드가 준비된 뒤 다시 재시작한다.

### 7.6 Garmin 워치 데이터가 안 들어옴

1. 타이머가 살아 있는지: `systemctl list-timers emfit-garmin-poller`
2. 수동으로 한 번 돌려본다: `sudo systemctl start emfit-garmin-poller` 후
   `journalctl -u emfit-garmin-poller -n 30 --no-pager`
3. 로그가 `로그인 실패` 면 **토큰 만료**다 → 윈도우에서 재로그인 후 토큰 폴더를 다시 올려야 한다
4. `새 데이터 없음` 만 반복되면 정상일 수 있다 — 워치가 폰과 동기화되지 않으면 서버가 받을 것도 없다.
   대상자에게 폰의 Garmin Connect 앱을 한 번 열어달라고 요청한다
5. 카드가 🔴 인데 폴러 로그는 정상이면, 워치를 착용하지 않았거나 충전 중일 가능성이 크다

### 7.7 리포트가 너무 느림

최초 파싱 30초~수 분은 정상이고, 그 후엔 증분 파싱이라 즉시 응답한다. 재시작할 때마다 최초 파싱이 다시 일어난다.

### 7.8 서비스가 계속 죽음 / 재시작 반복

- `sudo journalctl -u emfit -n 50 --no-pager` 에서 Traceback 확인
- 흔한 원인: 파서 파일 누락(한 세트로 안 올림), JSON/문법 오류, 패키지 미설치
- venv 확인: `source ~/emfit_server/venv/bin/activate && pip list`

### 7.9 젯슨 전원이 자꾸 나감

어댑터 스펙 확인(USB-C PD 65W 권장) → 저전력 모드 `sudo nvpmodel -m 2`(7W) → UPS/PD 보조배터리 고려

### 7.10 디스크 용량 부족

- `df -h` 로 확인, `du -h ~/emfit_server/*.jsonl` 로 큰 파일 찾기
- **로그 로테이션이 없다.** 커지면 월별 분리를 고려한다. AI Radar 는 1초 주기로 보내면 하루 20MB 이상 쌓인다.
- McKare 열화상 이미지는 `mckare_images/` 에 날짜별로 쌓인다 (측정 로그와 분리되어 있음)

---

## 8. 운영 시나리오

### 8.1 정전 후 복구

전원 복구 → 젯슨 자동 부팅 → systemd 가 두 서비스 자동 시작 → 몇 분 후 정상 서비스.
확인: `sudo systemctl status emfit emfit-discord-bot`

### 8.2 새 기기 추가

| 종류 | 방법 |
|---|---|
| EMFIT QS | 기기가 데이터를 보내기 시작하면 자동 인식 → `/devices` 에서 이름·위치·그룹 지정 |
| AI Radar | 같음. MAC 이 바뀌면 새 기기로 뜨므로 옛 기기는 `/devices`·`assignments.json` 에서 정리 |
| 사용감지(FSR) | 처음 데이터가 오면 자동 등록(`돌봄기기 XXXX`) → `/devices` 에서 이름·설치장소 수정. 무한 증가를 막는 자동 등록 상한 20대 |
| McKare | `POST /mckare` 로 데이터가 오면 등록됨. 전용 대시보드 구역은 `/dashboard2` 에만 있다 |
| **Garmin 워치** | 계정 토큰 폴더를 젯슨의 `~/.garmin_example-account-<번호>` 에 올리면, 다음 수집 주기에 `Garmin example-account-05` 로 자동 등록된다 → `/devices` 에서 이름·위치 지정 |

`/devices` 에서 삭제는 안 된다. 쓰지 않는 기본 등록을 지우려면 `assignments.json` 에서 해당 줄을 지우고 재시작한다.

### 8.3 사용자 URL 발급

`/admin/tokens` → 개인 URL(기기 하나) 또는 그룹 URL(여러 기기 묶음) 발급 → 링크 전달.
회수도 같은 화면에서 한다.

### 8.4 데이터 백업

```bash
mkdir -p ~/backup
for f in emfit radar mckare fsr garmin; do
  cp ~/emfit_server/${f}_data.jsonl ~/backup/${f}_data_$(date +%Y%m%d).jsonl 2>/dev/null
done
cp ~/emfit_server/{device_info.json,assignments.json} ~/backup/
# Garmin 토큰 — oauth1 은 재발급이 불가능한 유일한 자산이라 따로 챙긴다
cp -r ~/.garmin_example-account-* ~/backup/garmin_tokens_$(date +%Y%m%d)/
```
또는 외부 NAS/클라우드로 rsync·scp 주기 전송을 cron 에 설정한다.

### 8.5 서비스 완전 제거 (참고용)

```bash
sudo systemctl stop emfit emfit-discord-bot
sudo systemctl disable emfit emfit-discord-bot
sudo rm /etc/systemd/system/emfit.service /etc/systemd/system/emfit-discord-bot.service
sudo systemctl daemon-reload
```

---

## 9. 알려진 제한사항

- **센서 수신 경로에는 인증이 없다.** 조회 화면은 로그인·토큰으로 막혀 있지만 `POST /`, `/radar`, `/jy01` 은
  누구나 보낼 수 있다(McKare 만 ApiKey 파일이 있을 때 검증). `/jy01` 은 자동 등록 상한 20대로 막아둔 상태다.
- **HTTPS 가 아니다.** 민감한 의료 데이터를 다루므로 적용 권장 ([docs/HTTPS_설치가이드.md](docs/HTTPS_설치가이드.md)).
- **다운타임 감지가 없다.** 서버가 죽으면 알림이 없다 — UptimeRobot 등 외부 모니터링 필요.
  (기기 끊김은 디스코드로 알림이 가지만, 서버 자체가 죽으면 그 알림도 멈춘다.)
- **로그 로테이션이 없다.** 파일이 계속 커지고, 재시작 시 워밍업 시간도 그만큼 늘어난다.
- **EMFIT-DEMO-04(사용자-D, 뇌성마비)** 는 Summary 의 REM/깊은수면 값이 대부분 0 이다. Emfit 알고리즘이 뇌성마비
  대상자의 수면 단계 분류에 실패하는 것으로 추정 — **총수면 값만 신뢰 가능**하다.
- **McKare 는 기존 대시보드(`/dashboard`)에 전용 구역이 없다.** 등록하면 SN 모양(12자리 MAC) 때문에
  AI Radar 구역에 섞여 보인다. 전용 구역은 `/dashboard2` 에 있다.
- **Garmin 은 실시간이 아니다.** 워치 → 폰 → 클라우드 → 서버 폴링(30~60분) 구조라 수 시간 지연이
  정상이다. 낙상·이상 감지 같은 즉시 대응 용도로는 쓸 수 없고, 활동·수면 추세 용도다.
- **Garmin 접근은 비공식 경로다.** 공식 앱과 같은 OAuth 를 흉내내는 라이브러리(`garminconnect` +
  `garth`)를 쓰며, `garth` 는 **유지보수가 중단된 상태**다. Garmin 이 인증 방식을 바꾸면 고쳐줄
  주체가 없다. 서비스화 단계에서는 Garmin 공식 Health API(파트너 계약) 검토가 필요하다.
- **Garmin 토큰은 약 1년 주기로 재로그인이 필요하다.** 만료되면 대상자 폰으로 다시 로그인해야
  하고(2FA 포함), 젯슨에서는 할 수 없다 — 윈도우에서 발급해 토큰 폴더를 올리는 절차를 써야 한다.
  만료 시 디스코드로 `🔧 토큰 갱신 필요` 알림이 간다.
- 세션 서명키가 부팅마다 새로 만들어져 **재시작하면 전원 로그아웃**된다.

---

## 10. 연락처 / 추가 정보

- 코드 원본: `C:\path\to\monitoring_server\` (윈도우) · GitHub `gr4n4/emfit_server`
- 운영 서버: Jetson Orin Nano `operator@jetson-host` : `/opt/monitoring_server/`
- DDNS: `monitoring.example.com` (TP-Link 공유기에 등록)
- 공인 IP: `PUBLIC_IP` (유동, DDNS 가 자동 갱신)
