# Emfit 케어로봇 서버 매뉴얼

Emfit 침대 센서 데이터 수집·조회 서버 운영 및 사용 문서.

---

## 0. 빠른 링크

- **실시간 관제 대시보드**: http://monitoring.example.com/dashboard
- **리포트 다운로드**: http://monitoring.example.com/reports
- **원본 데이터(디버깅)**: http://monitoring.example.com/dashboard/raw

---

## 1. 시스템 개요

### 1.1 무엇을 하는 서버인가

Emfit QS 센서에서 보내는 심박수·호흡수·활동량·수면 데이터를 수집해서 저장하고, 실시간 관제 화면과 날짜별 CSV 리포트를 제공합니다.

### 1.2 데이터 흐름

```
[Emfit 침대 센서] ──HTTP POST──▶ [DDNS(monitoring.example.com)]
                                           │
                                           ▼
                               [집 공유기 TP-Link]
                               (외부80 → 내부8080 포트포워딩)
                                           │
                                           ▼
                          [Jetson Orin Nano: JETSON_HOST]
                          ├─ systemd 서비스 `emfit`
                          ├─ FastAPI (app.py)
                          ├─ 분석 로직 (analyzer.py)
                          └─ 로그 파일 (emfit_data.jsonl)
                                           │
                                           ▼
                         [웹 브라우저: 대시보드 / 리포트]
```

### 1.3 현재 등록된 기기

| 기기 SN | 이름 | 위치 | 그룹 |
|---|---|---|---|
| EMFIT-DEMO-01 | 돌봄A | - | 일반 |
| EMFIT-DEMO-02 | 돌봄B | 테스트 공간 | 일반 |
| EMFIT-DEMO-03 | 사용자-C | A시설 | 일반 |
| EMFIT-DEMO-04 | 사용자-D | 사용자-D님 가정 | 뇌성마비 |
| EMFIT-DEMO-05 | 사용자E | 301호 | 일반 |

> 기기 정보 수정은 [analyzer.py](analyzer.py) 의 `DEVICE_INFO` 딕셔너리에서.

---

## 2. 사용자 편 — 대시보드 보는 법

### 2.1 대시보드 접속

브라우저에서 http://monitoring.example.com/dashboard

30초마다 자동 새로고침됩니다.

### 2.2 카드 구성 요소

```
┌─────────────────────────┐
│ 사용자-D님 가정       🛌  │ ← 위치 / 상태 아이콘
│ ♿ 사용자-D               │ ← 사용자 이름 (♿는 뇌성마비 그룹)
│                         │
│  ❤️ HR │ 🫁 RR │ 🏃 ACT │ ← 최근 측정값
│   68   │  12   │   5    │
│                         │
│    🛌 수면/안정          │ ← 판정된 상태
│   측정: 08:13 (1시간 전) │ ← 마지막 측정 시각
│   통신: 연결됨 (방금)    │ ← 장비 연결 상태
│   EMFIT-DEMO-04                │ ← 기기 SN
└─────────────────────────┘
```

### 2.3 상태 아이콘과 의미

| 아이콘 | 의미 | 판정 기준 |
|---|---|---|
| 🚶 | 활동 | ACT ≥ 80 |
| 🪑 | 휴식 | 1 ≤ ACT < 80 |
| 🛌 | 수면/안정 | SleepDetail 타입 또는 ACT < 10 |
| 🛏️ | 부재 | ACT < 1 (사람이 침대에 없음) |
| ⏸ | 측정 대기 | 10분 이상 새 측정값 없음 |
| 🔴 | 끊김 | 장비 하트비트 `connected=false` |
| ❓ | 상태 없음 | 하트비트 기록이 한 번도 없음 |

### 2.4 측정 vs 통신 — 두 가지 시각의 차이

- **측정 시각**: 마지막으로 HR/RR/ACT 값을 받은 시각
- **통신 시각**: 장비가 "나 살아있어요" 하트비트를 마지막으로 보낸 시각

같이 보면 상황 진단 가능:
- 통신 O, 측정 최신 → 정상 사용 중
- 통신 O, 측정 오래됨 → 장비는 켜졌는데 사람이 침대에 없거나 측정 안 하는 중
- 통신 X → 장비 전원 꺼짐/네트워크 끊김

### 2.5 HR/RR/ACT 가 "-" 인 경우

다음 중 하나면 수치 대신 `-` 를 보여줍니다 (신뢰할 수 없는 값이라서):
- 부재 상태 (ACT < 1)
- 10분 이상 측정 없음
- 장비 끊김

---

## 3. 사용자 편 — 리포트 다운로드

### 3.1 접속

대시보드 하단 `📊 리포트 다운로드` 버튼 또는 http://monitoring.example.com/reports

### 3.2 사용법

1. **시작일 / 종료일** 선택 (수집된 범위 내에서만 가능)
2. **기기(사용자)** 선택
3. `ZIP 다운로드` 클릭

결과물: `<시작일>_to_<종료일>_<위치>_<이름>.zip`

### 3.3 ZIP 파일 구조

```
2026-04-01_to_2026-04-12_사용자-D님 가정_사용자-D.zip
├─ 2026-04-01_사용자-D님 가정_사용자-D_리포트.csv
├─ 2026-04-02_사용자-D님 가정_사용자-D_리포트.csv
├─ ...
└─ 2026-04-12_사용자-D님 가정_사용자-D_리포트.csv
```

데이터 없는 날짜는 자동 스킵됩니다.

---

## 4. 사용자 편 — CSV 데이터 이해

### 4.1 컬럼 설명

| 컬럼 | 의미 |
|---|---|
| 날짜 | YYYY-MM-DD |
| 시간(KST) | HH:MM:SS (한국 시간) |
| 사용자 | DEVICE_INFO 의 name |
| 위치 | DEVICE_INFO 의 location |
| 유형 | Live / HRV / SleepDetail / Summary |
| 심박수(HR) | 분당 심박수 |
| 호흡수(RR) | 분당 호흡수 |
| 활동량(ACT) | Emfit 활동 지표 (0 = 부재 추정) |
| 심박변이도(RMSSD) | HRV 지표 (HRV 행만 값 있음) |
| 상태설명 | 측정 종류 설명 |
| 수면점수 | 0~100 (Summary 행만) |
| 총수면(분), REM/깊은/얕은수면(분), 각성시간(분) | Summary 행의 수면 구간별 시간 |

### 4.2 유형별 데이터

- **Live**: Emfit이 실시간으로 보내는 분 단위 측정 (HR/RR/ACT)
- **HRV**: 심박변이도 분석 결과 (주기적으로 산출)
- **SleepDetail**: 수면 완료 후 산출된 분 단위 후처리 데이터 (Live와 시간 겹치면 Live는 제외됨)
- **Summary**: 수면 세션 요약 (점수, 구간별 시간). CSV 맨 위로 올려 표시

---

## 5. 관리자 편 — 서버 운영

### 5.1 SSH 접속

```bash
ssh operator@jetson-host
```

비밀번호 입력 → 젯슨 터미널 접속.

### 5.2 서비스 상태 관리 (systemd)

| 명령 | 용도 |
|---|---|
| `sudo systemctl status emfit` | 상태 확인 |
| `sudo systemctl restart emfit` | 재시작 (코드 수정 반영) |
| `sudo systemctl stop emfit` | 정지 |
| `sudo systemctl start emfit` | 시작 |
| `systemctl is-active emfit` | 한 단어 상태 |

재시작 후 **백그라운드 캐시 워밍업에 약 30초~3분** 소요. 대시보드 느리면 그동안일 확률 높음.

### 5.3 로그 보기

```bash
# 실시간 스트리밍
sudo journalctl -u emfit -f

# 최근 50줄
sudo journalctl -u emfit -n 50 --no-pager

# 분석기 로그만
sudo journalctl -u emfit -n 100 --no-pager | grep analyzer

# 최근 POST 요청만
sudo journalctl -u emfit --since "10 min ago" | grep POST
```

### 5.4 데이터 파일 모니터링

```bash
# 실시간 새 데이터 확인
tail -f ~/emfit_server/emfit_data.jsonl

# 특정 기기만 필터
tail -f ~/emfit_server/emfit_data.jsonl | grep --line-buffered EMFIT-DEMO-04

# 마지막 줄 JSON 예쁘게
tail -1 ~/emfit_server/emfit_data.jsonl | python3 -m json.tool | head -40

# 파일 크기
du -h ~/emfit_server/emfit_data.jsonl
```

### 5.5 파일 구조

```
/opt/monitoring_server/
├─ app.py                    # FastAPI 서버 (엔드포인트: /dashboard, /reports, /report, /report_range, /)
├─ analyzer.py               # 데이터 파싱 / 캐시 / 리포트 생성
├─ emfit_data.jsonl          # 수집 로그 (append-only)
├─ MANUAL.md                 # 이 문서
├─ venv/                     # 파이썬 가상환경
│   └─ bin/uvicorn           # 실제 실행 바이너리
└─ (과거 CSV 리포트들)
```

systemd 서비스 정의: `/etc/systemd/system/emfit.service`

### 5.6 코드 수정 → 배포 워크플로우

**로컬(윈도우)에서 수정**:
```
C:\path\to\monitoring_server\app.py  ← 여기가 "원본"
```

**젯슨으로 전송**:
```powershell
cd C:\path\to\monitoring_server
scp app.py analyzer.py operator@jetson-host:/opt/monitoring_server/
```

**서비스 재시작**:
```bash
sudo systemctl restart emfit
```

**파싱 완료까지 대기** (로그에 `[analyzer] 전체 파싱 완료` 뜨면 끝).

---

## 6. 트러블슈팅

### 6.1 대시보드가 안 뜸

1. `sudo systemctl status emfit` → active 인가?
   - 아니면 `sudo journalctl -u emfit -n 30 --no-pager` 에서 에러 확인
2. 내부 IP 로는 뜨는지 테스트: http://JETSON_HOST:8080/dashboard
   - 뜨면 네트워크/공유기 문제
   - 안 뜨면 서비스 문제
3. DDNS 해석 확인: `nslookup monitoring.example.com`
4. 공유기 포트포워딩 확인: 외부 80 → JETSON_HOST:8080 (TCP)

### 6.2 데이터가 안 들어옴

1. `tail -f ~/emfit_server/emfit_data.jsonl` 에 새 줄 찍히는지
2. `sudo journalctl -u emfit --since "5 min ago" | grep POST` 에 요청 있는지
3. 없다면:
   - 공유기 재부팅 시도
   - 젯슨 서비스 재시작
   - Emfit 장비 자체 네트워크/전원 확인
4. 대시보드에서 🔴 끊김 표시면 **장비 쪽** 문제

### 6.3 리포트가 너무 느림

- 최초 파싱은 30초~3분 정도 소요 (정상)
- 그 후엔 증분 파싱이라 즉시 응답
- 서비스 재시작할 때마다 최초 파싱이 다시 일어남

### 6.4 서비스가 계속 죽음 / 재시작 반복

- `sudo journalctl -u emfit -n 50 --no-pager` 에서 Traceback 확인
- 흔한 원인:
  - `analyzer.py` 의 `DEVICE_INFO` 딕셔너리 문법 오류 (콤마 빠짐 등)
  - pandas 등 필요한 패키지 미설치
- venv 확인: `source ~/emfit_server/venv/bin/activate && pip list`

### 6.5 젯슨 전원이 자꾸 나감

- 어댑터 스펙 확인 (USB-C PD 65W 권장)
- 저전력 모드로 변경: `sudo nvpmodel -m 2` (7W 모드)
- UPS 또는 PD 패스스루 보조배터리 고려

### 6.6 디스크 용량 부족

- `df -h` 로 확인
- `emfit_data.jsonl` 이 커지면 로테이션 고려 (예: 월별 분리)

---

## 7. 운영 시나리오

### 7.1 정전 후 복구

전원 복구 → 젯슨 자동 부팅 → `systemd` 가 `emfit` 서비스 자동 시작 → 3분 후 정상 서비스.

확인:
```bash
sudo systemctl status emfit
```

### 7.2 새 Emfit 기기 추가

1. `analyzer.py` 의 `DEVICE_INFO` 에 항목 추가:
   ```python
   "새SN": {"name": "이름", "location": "위치", "group": "그룹"},
   ```
2. 윈도우에서 scp → 젯슨 → 서비스 재시작
3. 새 기기가 데이터 보내기 시작하면 자동 인식

### 7.3 데이터 백업

```bash
# 젯슨에서
cp ~/emfit_server/emfit_data.jsonl ~/backup/emfit_data_$(date +%Y%m%d).jsonl
```

또는 외부 NAS / 클라우드로 rsync/scp 주기적 전송 cron 설정.

### 7.4 서비스 완전 제거 (참고용)

```bash
sudo systemctl stop emfit
sudo systemctl disable emfit
sudo rm /etc/systemd/system/emfit.service
sudo systemctl daemon-reload
```

---

## 8. 알려진 제한사항

- **인증 없음**: DDNS 주소 아는 사람 누구나 대시보드/리포트 열람 가능. 민감한 의료 데이터 다루므로 **추후 인증 추가 권장**.
- **EMFIT-DEMO-04(사용자-D, 뇌성마비)** Summary 의 REM/깊은수면/실제수면 값이 대부분 0. Emfit 알고리즘이 뇌성마비 대상자 수면 단계 분류에 실패하는 것으로 추정. 총수면 값만 신뢰 가능.
- **다운타임 감지 없음**: 서버가 다운되도 자동 알림 없음. UptimeRobot 등 외부 모니터링 설정 필요.

---

## 9. 연락처 / 추가 정보

- 코드 원본: `C:\path\to\monitoring_server\` (윈도우)
- 운영 서버: Jetson Orin Nano `operator@jetson-host`
- DDNS: `monitoring.example.com` (TP-Link 공유기에 등록)
- 공인 IP: `PUBLIC_IP` (유동, DDNS가 자동 갱신)
