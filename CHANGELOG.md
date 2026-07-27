# 변경 이력 (Changelog)

Emfit 침대 센서 데이터 수집 서버의 버전 이력.
[SemVer](https://semver.org/lang/ko/) 규칙: `MAJOR.MINOR.PATCH`
- **MAJOR**: 기존 사용 방식이 깨지는 변경
- **MINOR**: 기능 추가 (기존 사용법은 유지)
- **PATCH**: 버그 수정·자잘한 UI 수정

---

## [3.3.2] - 2026-07-27
- **로그 파싱 속도 8~14배 개선 (재시작 시 워밍업 시간 단축)**
  - 원인: `add_to_storage` 가 행마다 pandas Timestamp 를 만들고 tz 변환을 2번씩 함.
    398MB(약 73만 줄) 전체 파싱이 실서버에서 ~10분(메모리 부족 시엔 훨씬 더) 걸렸음
  - 수정: 시간 변환을 pandas → 표준 datetime 으로 교체 (핫패스). `_to_naive_kst`·`_parse_dt` 도
    datetime 기반으로 통일. 200,000줄 합성 데이터로 8.6배 확인, 결과 190,000건 baseline 과 100% 동일
  - 덤: 13자리 밀리초 타임스탬프가 pandas 범위 밖이라 조용히 버려지던 잠재 버그도 해소
  - 참고: 실행 중 증분 파싱은 원래 빨랐고, 이번 개선은 재시작(콜드 스타트) 전체 파싱에 특히 효과

## [3.3.1] - 2026-07-27
- **같은 초에 여러 레이더 기록이 들어오면 최신 자세가 갱신되지 않던 버그 fix**
  - 원인: `server_received_at` 이 초 단위라 같은 초에 들어온 기록끼리 시각이 동일한데,
    `get_latest_states` 가 `>` 로 비교해 "같은 초의 맨 처음 건"을 붙들고 있었음
    (재실 40건 → 낙상 → 자리비움 순으로 와도 카드는 계속 '재실'로 표시)
  - 수정: `>=` 로 비교 — 같은 초에서는 저장 리스트의 도착순 마지막(=가장 최근 도착)을 최신으로 택함
  - 더미 데이터 종합 검증 스크립트로 확인 (PDF 예시 수신, 잘못된 payload 방어,
    1초 주기 60건 연속 수신, 대시보드·CSV 반영 — 22개 항목 전부 통과)

## [3.3.0] - 2026-07-24
- **AI Radar (RANIX RMR602A) 연동**
  - 전용 수신 경로 `POST /radar` — Emfit(`POST /`)과 분리해 payload 혼동 방지
  - `radar_parser.py` 신규 — BED/FALL 두 형식 자동 판별, 자세 7종 매핑, MAC 정규화.
    Emfit 파서를 건드리지 않도록 별도 모듈로 분리
  - 대시보드를 **EMFIT QS / AI Radar 두 섹션**으로 분리, 낙상 카드는 최상단 정렬
  - 레이더 카드는 ACT 대신 **자세**를 표시 (누움/앉음/배회/걸터앉음/낙상/자리비움/뒤척임)
- **BED/FALL 동시 수신 시 심박·호흡이 화면에서 사라지던 버그 fix**
  - 원인: FALL 이 1초마다, BED 가 5~50초마다 오는데 카드는 "가장 최근 1건"만 보므로
    항상 FALL 이 이겨서 BED 의 심박·호흡·세밀한 자세가 영영 표시되지 않음
  - 수정: `analyzer._merge_radar_states()` 신규 — BED/FALL 최신 기록을 합쳐 하나의 현재 상태로.
    자세는 BED 우선(7종으로 세밀), 심박·호흡은 BED 에서, 낙상은 둘 중 하나라도 감지하면 낙상(안전 우선),
    120초 지난 기록은 무시해 옛날 낙상이 되살아나지 않게 함
- **원본 로그 파일 분리** — Emfit `emfit_data.jsonl` / AI Radar `radar_data.jsonl`
  - 한 파일에 섞으면 레이더(1초 주기, 하루 20MB+)가 커질수록 Emfit 조회까지 같이 느려진다.
    전체 재파싱은 서버 재시작·배정 변경 때마다 일어나므로 그동안 점검 페이지가 길어짐
  - `analyzer` 파싱 캐시를 **파일별 슬롯**으로 전환 — 한쪽에 새 줄이 붙어도 다른 쪽은 다시 읽지 않음.
    조회 시에는 두 파일을 합쳐서 하나의 storage 로 제공 (기기 SN 이 달라 키 충돌 없음)
  - `app.DATA_FILES` 에 파일 목록을 모아둠 — 기기 종류가 늘면 여기만 추가하면 된다
  - 원본 데이터 페이지(`/dashboard/raw`)는 두 파일의 줄 수를 합쳐 세고,
    가장 최근에 갱신된 파일의 마지막 줄을 보여준다
- **CSV 리포트를 기기 종류별로 분리**
  - Emfit 은 기존 파일명(`..._리포트.csv`) 유지, 레이더만 `..._Radar-BED.csv` / `..._Radar-FALL.csv`
    (같은 사람에게 두 기기를 배정하면 파일명이 겹쳐 ZIP 안에서 덮어쓰이던 문제도 해소)
  - 레이더 단일 날짜 다운로드는 BED/FALL 두 파일을 ZIP 으로. `?kind=bed|fall` 로 하나만도 가능
  - 레이더 기록에서 Emfit 전용 빈 컬럼(활동량/심박변이도) 제거, 형식별로 안 쓰는 컬럼도 자동 제외

## [3.2.2] - 2026-06-17
- **관리자가 그룹(A시설) 대시보드에 아예 못 들어가던 회귀 버그 fix**
  - 원인: 3.2.1에서 "admin이면 view 쿠키 무시"를 `_get_view_token_from_request`에 전역으로 걸었더니 `/view`(그룹 대시보드)·`/api/view/cards`까지 막혀 "접근할 수 있는 그룹이 없습니다"가 떴음
  - 수정: 쿠키 무시 로직을 제거해 admin도 그룹 대시보드를 볼 수 있게 되돌림. 3.2.1이 잡으려던 "전체 대시보드에서 그룹 기기 클릭 시 그룹으로 빨려감" 문제는 `/device/{sn}`의 `in_view` 판정으로 옮김 — admin은 **명시적 `?view=`** 로 들어왔을 때만 그룹 컨텍스트로 취급, 그룹 대시보드 카드 링크에 `?view=` 부착
- **그룹 대시보드 안에서 일부 기기 상세가 "접근 권한 없음(403)" 뜨던 버그 fix**
  - 원인: `_require_device_access`가 잔류 device 토큰 쿠키를 먼저 보고, 그 토큰이 가리키는 SN이 아니면 그룹 토큰을 확인하기도 전에 403("other device")으로 차단
  - 수정: device 토큰이 해당 기기를 허용하지 않으면 **곧장 막지 않고 view(그룹) 토큰을 먼저 확인**하도록 순서 변경
- **대시보드 속도 개선 (데이터 누적으로 느려지던 문제)**
  - 원인: `analyzer.get_latest_states()`가 카드 갱신(15초)마다 전체 storage(16만 줄 누적)를 풀스캔
  - 수정: 결과를 파일 mtime 기준으로 캐시 — 새 데이터가 들어왔을 때만 1회 재계산, 그 외에는 즉시 반환. 배정 이력 변경 시 캐시 무효화

## [3.2.1] - 2026-06-04
- **관리자가 그룹(A시설 등) 기기 상세에서 뒤로가기 시 그룹 대시보드로 빠지던 버그 fix**
  - 원인: 그룹 URL(`/v/<token>`)을 한 번 열면 `emfit_view` 쿠키가 365일 박힘 → admin이어도 `_get_view_token_from_request`가 잔류 쿠키를 주워 `in_view=True`가 되어 back_link가 `/view`로 향함
  - 수정: `_get_view_token_from_request` 에서 admin 로그인 상태면 쿠키 fallback 무시 (명시적 `?view=` 미리보기는 그대로 유지). 실사용자(view 토큰만 보유, admin 아님)는 영향 없음

## [3.2.0] - 2026-05-21
- **중간관리자(view 토큰)도 사용자명 수정 가능**
  - 권한 계층: 사업단(admin) → 중간관리자(view) → 실사용자(device) 자연스럽게 정리됨
  - `analyzer.update_active_user(sn, new_user)` — 활성 배정의 user 필드만 갱신 (location/group/start 등 보존). 종료된 옛 배정은 절대 안 건드림 (역사 기록 보존)
  - 새 라우트: `POST /devices/edit_user` — admin OR (view 토큰 + 자기 그룹 SN) 만 허용. device 토큰(실사용자)은 거부
  - 기기 상세 페이지(`/device/{sn}`) h1 옆에 ✏️ 이름 수정 버튼 (활성 배정만, admin·view 모두 노출). 클릭 → 인라인 폼 → 저장 → cache invalidate
  - 경고 문구: "이름 변경 시 이 활성 배정 기간 전체 데이터가 새 이름으로 표시됨 (옛 배정/리포트는 그대로)"

## [3.1.1] - 2026-05-21
- view(그룹) 토큰 사용자가 💬 의견 보내기 누르면 "권한 없다" 뜨던 버그 fix — `/feedback` GET/POST가 view 토큰도 허용
- 기기 상세 페이지(`/device/{sn}`)에서 admin이 view URL로 들어와 카드 클릭 후 "← 대시보드" 누르면 전체 5대(admin 대시보드)로 가던 버그 fix — view 컨텍스트면 `/view` 로 돌아감
- view 사용자의 device 상세 페이지에서 back link가 깨진 `/d/{view_token}` 가리키던 문제 fix

## [3.1.0] - 2026-05-21
- **그룹(view) 대시보드 도입**
  - 새 토큰 종류 `view_tokens.json` — `{token: {name, sns: [...]}}` 형식
  - `/v/{token}` 진입 → `/view` 그룹 대시보드 (그룹에 포함된 기기 카드만 노출)
  - `/api/view/cards` 갱신 엔드포인트
  - `/device/{sn}` 등 기기 상세도 그룹 토큰으로 접근 허용 (포함된 SN만, 다른 SN은 403)
  - `/admin/tokens` 페이지 하단에 그룹 발급 폼 (이름 + 체크박스로 기기 선택) + 기존 그룹 표 + 폐기 버튼
  - 그룹 view 페이지 메뉴: 사용 가이드 + 의견 보내기
- **URL 복사 버튼 fix**
  - `navigator.clipboard.writeText()` 가 HTTPS에서만 동작해서 HTTP 환경에서 silent fail 하던 문제
  - `document.execCommand('copy')` fallback 추가한 `copyText()` 헬퍼로 교체
  - 개별 + 그룹 URL 둘 다 적용

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
