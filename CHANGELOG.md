# 변경 이력 (Changelog)

Emfit 침대 센서 데이터 수집 서버의 버전 이력.
[SemVer](https://semver.org/lang/ko/) 규칙: `MAJOR.MINOR.PATCH`
- **MAJOR**: 기존 사용 방식이 깨지는 변경
- **MINOR**: 기능 추가 (기존 사용법은 유지)
- **PATCH**: 버그 수정·자잘한 UI 수정

---

## [3.0.2] - 2026-05-20
- 대시보드 하단 버전 표시 색상 진하게 (`#cfd8dc` → `#90a4ae`)

## [3.0.1] - 2026-05-20
- `/dashboard` 하단에 현재 버전(`Emfit Server v{VERSION}`) 표시 추가
- `app.py` 상단에 `VERSION` 상수 도입

## [3.0.0] - 2026-05-19
- **배정 이력 시스템 도입** (`assignments.json`)
  - 기기(SN)별로 "누가·어디서·언제부터~언제까지" 기간 기록
  - `analyzer.resolve_assignment(sn, dt)` — 측정 시각으로 그때 사용자 판정
  - `/devices` 페이지 "🔄 기기 이전" 폼 + `POST /devices/handover`
  - `/reports` 페이지가 기기(SN) 대신 **배정(사람·기간) 단위**로 동작
  - 기기 상세 페이지 + 시계열 API도 배정 단위로 동작
- 사용자 그룹을 `analyzer.GROUPS` 한 곳에서 관리
- 재배포 시 86MB 재파싱 동안 점검 페이지 표시
- A시설 실증 시작 (2026-05-19 00:00부로 4대 재배치)

## [2.3.1] - 2026-05-11
- admin 세션 검사를 device 토큰 검사보다 먼저 수행하도록 fix
  (admin이 테스트로 `/d/{token}` 접속한 뒤 다른 기기 접근이 막히던 버그)

## [2.3.0] - 2026-05-11
- **대시보드 고도화**
  - 글로벌 datetime 범위 발췌 (`start_dt`/`end_dt` 파라미터, 자정 가로지름 가능)
  - 빠른 범위 버튼 (오늘 / 어제 / 지난 밤 / 최근 24시간)
  - 블록 순서 드래그(SortableJS) + ▲/▼ 버튼 재배치, viewer 단위 저장
  - x축 linear scale + epoch 좌표로 라벨 뭉침 fix, 5분 이상 갭 자동 끊김
- 차트별 시간 발췌 박스 제거 (글로벌 datetime이 역할 흡수)

## [2.2.0] - 2026-05-08
- 의견 상태 4단계 (미조치/조치중/조치완료/조치불가) + 관리자 답글 기능
- 상태/답글은 admin만 변경 가능, 모두 열람 가능

## [2.1.0] - 2026-05-08
- **UX 개선**
  - HR/RR/ACT 차트: X축 30분 간격 라벨, 가로 스크롤
  - 수면 Summary 복수 카드 표시 (낮잠/밤잠), 총수면 큰 순 정렬
  - 기기 상세 페이지에 실시간 현황 카드 + 가이드/의견 nav 추가
  - 로그인 페이지를 풀스크린 그라데이션 폼으로 변경

## [2.0.0] - 2026-05-08
- **인증 체계 도입** (기존 접근 방식 breaking)
  - 관리자: ID/PW 세션 로그인 (HMAC 쿠키 30일)
  - 피험자/가족: 개인 URL 토큰 `/d/{token}` — admin이 `/admin/tokens`에서 발급
  - 옛 admin 토큰 시스템 폐지
  - 토큰 발급 페이지에 내부(LAN) + 외부(DDNS) URL 동시 표시

## [1.1.0] - 2026-04-26
- `/devices` 기기 정보 편집 페이지 추가 (`device_info.json` 외부화)
- `/feedback` 의견 작성·열람 페이지 추가 (`feedback.jsonl`에 저장)
- HR/RR/ACT 글씨 크기 확대

## [1.0.0] - 2026-04-21~22
- **젯슨 이전** (윈도우 PC → Jetson Orin Nano)
  - 24/7 가동, 저전력 안정 운영
  - systemd 서비스 `emfit`, 파이썬 venv
  - DDNS `monitoring.example.com` 그대로 유지하면서 백엔드만 교체

## [0.2.0] - 2026-03 말 ~ 2026-04 초 *(추정)*
- `analyzer.py` 분석 로직 추가
- 일별 CSV 리포트 생성 시작 (첫 리포트 파일은 2026-03-23부터)

## [0.1.0] - 2026-03-18
- **초기 수신 서버 가동**
  - FastAPI `app.py` 첫 작성 (POST `/`, port 80, try/except 에러 처리, `host="0.0.0.0"`)
  - `emfit_data.jsonl` JSONL 형식 저장
- **네트워크 인프라 구성**
  - Windows 방화벽 인바운드 룰 (TCP 80 허용, 이름 `Emfit_API`)
  - TP-Link 공유기 포트 포워딩 (외부 80 → 내부 SERVER_LAN_IP:80)
  - TP-Link DDNS 발급: `monitoring.example.com`
  - Emfit 본사(Pekka)에 API endpoint URL 변경 요청

## [0.0.1] - 2026-03-18
- FastAPI 첫 코드 작성 — `/data-endpoint`, port 8000, JSON 받아서 `print`만
- 2026-03-16에 finenurse 측에서 API 컨테이너 생성 (`http://partner.example.com/`)
