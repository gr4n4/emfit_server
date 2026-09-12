# CLAUDE.md — Emfit 돌봄 모니터링 서버

이 파일은 Claude Code 가 이 저장소에서 작업할 때 먼저 읽는 안내다.
사람이 읽는 문서는 [MANUAL.md](MANUAL.md)(운영 매뉴얼), [CHANGELOG.md](CHANGELOG.md)(기술 변경 이력),
[VERSION_HISTORY.md](VERSION_HISTORY.md)(보고용 요약), [docs/](docs/)(배포 가이드·설계 메모)에 있다.

---

## 1. 무엇을 하는 코드인가

침대·레이더·압력 센서가 HTTP POST 로 보내는 돌봄 대상자 생체·재실 데이터를 받아서
append-only JSONL 로 쌓고, 실시간 관제 대시보드 · 일별 CSV 리포트 · 디스코드 알림을 제공하는
FastAPI 서버. A시설 등에서 실증 운영 중이다.

기기 종류 4가지 — 각각 수신 경로와 로그 파일이 분리돼 있다:

| 종류 | 수신 경로 | 로그 파일 | 파서 |
|---|---|---|---|
| EMFIT QS (침대 센서) | `POST /` | `emfit_data.jsonl` | analyzer 내장 |
| AI Radar (라닉스 RMR602A) | `POST /radar` | `radar_data.jsonl` | [radar_parser.py](radar_parser.py) |
| McKare VSR22 / AI 110 | `POST /mckare`, `/mckare` 이미지 | `mckare_data.jsonl`, `mckare_images/` | [mckare_parser.py](mckare_parser.py) |
| ESP32 압력 사용감지 (돌봄기기 부착) | `POST /jy01` | `fsr_data.jsonl` | [fsr_parser.py](fsr_parser.py) |

**다루는 데이터는 환자 건강정보다.** 로그·리포트·토큰 파일을 커밋하거나 외부로 내보내지 않는다(§5-1).

---

## 2. 실행과 배포 — 이 폴더는 "원본 보관소", 운영은 젯슨

| | 위치 |
|---|---|
| 코드 원본 (윈도우, 이 저장소) | `C:\path\to\monitoring_server` |
| 운영 서버 | Jetson Orin Nano `operator@jetson-host` : `/opt/monitoring_server/` |
| 서비스 | `emfit.service` (uvicorn, 포트 8080) · `emfit-discord-bot.service` (봇) |
| 외부 접속 | `http://monitoring.example.com` (공유기 80 → 8080 포워딩) |
| GitHub | `gr4n4/emfit_server` (`main`) |

배포는 **"복사 → 앱 안 멈추고 검사 → 통과할 때만 재시작"** 순서를 지킨다. 상세 절차는
[docs/배포_가이드_3.5.0.md](docs/배포_가이드_3.5.0.md) 가 가장 최신 예시다. 요약:

```powershell
# 1) 윈도우에서 전송 (같이 고친 파일은 반드시 한 번에 — import 짝이 깨지면 앱이 안 켜진다)
scp -O app.py analyzer.py operator@jetson-host:/opt/monitoring_server/
```
```bash
# 2) 젯슨에서 재시작 전 검사 — 이 시점에도 기존 앱은 그대로 돌고 있다
cd ~/emfit_server
grep '^VERSION' app.py
python3 -c "import ast; [ast.parse(open(f,encoding='utf-8').read()) for f in ['app.py','analyzer.py']]; print('문법 OK')"
# 3) 통과하면 재시작 → 확인
sudo systemctl restart emfit && sleep 5 && systemctl is-active emfit
# 4) 회귀 확인: 기존 기기 카드 수·통신 시각 갱신 여부까지 본다
```
되돌리기용으로 배포 전 `~/backup/pre_<버전>/` 에 `app.py`·`analyzer.py`·`assignments.json` 을 복사해 둔다.

작업 시 알아둘 점:
- 재시작하면 **수백 MB 로그 전체 재파싱에 30초~수 분** 걸린다. 그동안 `_maintenance_gate`
  ([app.py:597](app.py#L597))가 "점검 중" 화면을 내보내지만, **센서 수신 경로는 계속 받으므로 데이터 유실은 없다.**
- `python app.py` 로 직접 띄우면 **포트 80** ([app.py:6292](app.py#L6292))이다. 운영은 systemd 가 uvicorn 8080 으로 띄운다. 혼동 주의.
- 디스코드 봇은 별도 프로세스다. 로그를 다시 파싱하지 않고 `GET /internal/discord/status`
  (localhost 전용, [app.py:4733](app.py#L4733))로 대시보드의 메모리 상태를 받아 쓴다.
- **시설(채널) 슬래시 명령어는 봇 시작 시 생성된다** — `/admin/discord` 에서 채널을 추가·삭제했으면 봇을 재시작해야 반영된다.

---

## 3. 코드 지도

### [app.py](app.py) — 6,293줄 단일 파일 (FastAPI 앱 + 대시보드 HTML 인라인)
| 라인 | 내용 |
|---|---|
| 1~120 | 인코딩 설정, `VERSION`, 파일 경로 상수, 쿠키·인증 상수 |
| 250~460 | 관리자 인증(`_is_admin_authenticated`), 워밍업 스레드(`_warmup_then_ready`), 디스코드 알림 설정 |
| 460~600 | 디스코드 끊김 판정·전송 (`_discord_device_snapshot`, `_discord_check_once`, `_discord_monitor_loop`) |
| 597~710 | 점검 중 게이트 미들웨어, 인증 실패 HTML 핸들러 |
| 1690~2500 | 대시보드·뷰·카드 API·시계열 API·상세페이지 |
| 3400~3780 | 원본 데이터 화면(`/dashboard/raw`), 리포트 화면·CSV·ZIP |
| 3890~4450 | 기기 관리(`/devices`, 이전·이름수정), 피드백 |
| 4440~4770 | 디스코드 설정 화면·저장·채널 배정·`/internal/discord/status` |
| 4770~5250 | 토큰 관리(개인 `/d/{token}`, 그룹 `/v/{token}`), 로그인·로그아웃 |
| 5230~5600 | **센서 수신 경로** — `/`(EMFIT), `/radar`, `/mckare`, 이미지, `/jy01`(FSR) |
| 5600~6030 | FSR 게이트웨이 명령 큐(`gw_commands.json`), `/fsr-tune` 실시간 조정 화면 |
| 6031~6300 | FSR 노드 원격 조정(`/fsr-nodes`) — ⚠️ 6031~6071 은 **과거 패치 안내 주석 잔재**이지 실행 코드가 아니다 |

### [analyzer.py](analyzer.py) — 1,245줄, 파싱·캐시·리포트
- `ingest_realtime_record()` ([analyzer.py:535](analyzer.py#L535)) — 수신 즉시 현재 상태 캐시 갱신.
  대시보드를 열지 않아도 디스코드 알림이 최신 상태를 보게 하는 핵심.
- `warmup()` / `get_latest_states()` / `get_report_df()` / `list_available_assignments()` — 재시작 파싱, 카드 상태, CSV.
- `resolve_assignment(sn, dt)` — 측정 시각을 배정 이력과 대조해 "그때 그 기기를 쓰던 사람"을 붙인다.
- `GROUPS` — 대상자 그룹 목록. 여기에만 추가하면 폼 선택지·검증에 자동 반영된다.

### 그 외
[nrcarec_alert.py](nrcarec_alert.py) NRCarec 외부 경보 전송 · [discord_bot.py](discord_bot.py) 슬래시 명령어 봇 ·
`_check_heartbeat_interval.py` 하트비트 주기 점검용 일회성 스크립트(미커밋).

---

## 4. 데이터·상태 모델

- **로그 파일은 append-only, 종류별로 분리**한다. 한쪽이 커져도 다른 쪽 조회 속도에 영향이 없어야 한다.
  기기 종류를 늘리면 `DATA_FILES` ([app.py:37](app.py#L37))에 추가.
- **이미지는 JSONL 에 넣지 않는다.** 파일은 날짜별 폴더에, JSONL 에는 메타데이터만.
- **모든 데이터·설정 파일 경로는 CWD 기준 상대 경로**다 (`analyzer.py` 는 `glob.glob("*.jsonl")` 도 쓴다).
  → 반드시 프로젝트 폴더에서 실행해야 한다. 코드에 절대 경로를 새로 박지 않는다.
- **`device_info.json` = 지금 누가 쓰는가 (기기당 1줄)** / **`assignments.json` = 과거에 누가 언제부터 언제까지 썼는가 (기간별 여러 줄)**.
  데이터는 SN 으로만 들어오므로 이 둘을 분리해야 기기를 옮겨도 과거 기록이 안 섞인다.
- **기기 종류(`kind`)를 SN 모양으로 추측하지 않는다.** ESP32 MAC 과 레이더 SN 이 둘 다 12자리 16진수라
  구분이 불가능하다. 배정 정보에 `kind` 를 명시해 저장한다.

---

## 5. 반드시 지킬 규칙

### 5-1. 커밋 금지
[.gitignore](.gitignore) 에 적힌 것은 **환자 건강 데이터이거나 비밀 정보**다 — `*_data.jsonl`,
`*_리포트.csv`, `device_tokens.json`, `view_tokens.json`, `admin_password.txt`, `mckare_apikey.txt`,
`discord_config.json`, `discord_bot_token.txt`, `*firebase-adminsdk*.json`, `mckare_images/`, `logs/`.
새 비밀·데이터 파일을 만들면 같은 커밋에서 `.gitignore` 에 먼저 추가한다.

### 5-2. 버전 표기는 항상 한 세트
코드를 고치면 `VERSION` ([app.py:20](app.py#L20))과 [CHANGELOG.md](CHANGELOG.md) 를 **같이** 올린다.
SemVer — MAJOR: 기존 사용 방식이 깨짐 / MINOR: 기능 추가 / PATCH: 버그·자잘한 UI.
CHANGELOG 는 "무엇을 고쳤는지"와 함께 **왜 그렇게 판단했는지(관측된 증상·실측 근거)** 를 남기는 형식을 유지한다.
`VERSION_HISTORY.md` 는 보고용 요약이라 MINOR 이상에서만 손댄다.

### 5-3. 끊김 판정 기준은 기기 종류마다 다르다 (건드릴 때 주의)
| 종류 | 끊김 판정 근거 |
|---|---|
| EMFIT QS · AI Radar | payload 의 `connected` 값을 그대로 신뢰. 하트비트 자체가 **6시간** 넘게 없으면 그 값이 `true` 여도 끊김으로 본다 |
| FSR | `fault`/`disconnect` 계열 이벤트 |
| McKare | 끊김 flag 가 없으므로 **무소식만으로 알림을 만들지 않는다** |

이유: 이 두 종류는 대상자가 침대에 없으면(부재) 하트비트가 원래 뜸하게 온다. "시간 경과"를 기준으로
쓰면 멀쩡한 기기가 끊김으로 잡힌다(병동 실증에서 실측 확인). 반대로 `connected` 만 믿으면
수신 경로가 죽어 `true` 로 멈춘 기기는 영영 알림이 안 간다 — 그래서 6시간 안전장치가 둘 다 필요하다.
**대시보드와 디스코드가 같은 기준을 쓰게 유지한다.** 한쪽만 고치면 화면과 알림이 어긋난다.

### 5-4. 알림은 거짓 양성 쪽으로 절대 새지 않게
- 끊김을 감지해도 바로 보내지 않고 같은 시간만큼 더 지켜본다(디바운스, 총 대기 ≈ 기준×2).
- 확인 대기 상태는 메모리에만 둔다 — 재시작으로 날아가면 확인이 한 번 늦어질 뿐, 거짓 알림은 안 생긴다.
- Webhook **전송 성공 시에만** 끊김/복구 상태를 저장한다.
- 조용한 것만으로 경고하지 않는다 (하루 종일 안 쓰는 기기가 정상일 수 있다).

### 5-5. 사람이 손댄 데이터는 코드가 지우지 않는다
퇴역 기기 정리(`_RETIRED_SNS`, [analyzer.py:59](analyzer.py#L59))는 **기본 등록 상태 그대로일 때만** 제거한다.
이름·위치를 고쳤거나 배정 이력이 갈라졌다면 사람이 의미를 부여한 기록이므로 남기고, 사용자가 직접 정리하게 한다.

### 5-6. 주석은 한국어로, "왜"를 남긴다
기존 코드의 주석 밀도·어조를 따른다. 무엇을 하는지보다 **왜 그 선택인지, 무엇을 피하려는지**를 적는다
(예: "반복문 안에서 바로 만들면 모든 명령어가 마지막 채널만 참조하게 되는 흔한 실수를 피한다").

---

## 6. 알려진 함정 / 제약

- `/jy01`(FSR 수신)에는 **인증이 없다.** 장난성 요청으로 기기 목록이 불어나지 않도록
  자동 등록 상한 20 (`_FSR_AUTO_REGISTER_LIMIT`)이 걸려 있다 — 지우지 말 것.
- 대시보드·리포트에 **인증이 필요한 화면과 없는 수신 경로가 섞여 있다.** 라우트를 추가할 때
  `Depends(require_admin)` 또는 `_require_device_access` 중 무엇이 맞는지 반드시 판단한다.
- 세션 서명키는 부팅 시 1회 생성(`_SESSION_SECRET`)이라 **재시작하면 모두 로그아웃**된다.
- EMFIT-DEMO-04(뇌성마비 대상자)는 Emfit 알고리즘이 수면 단계 분류에 실패해 REM/깊은수면이 대부분 0이다.
  총수면 값만 신뢰 가능 — 리포트 로직에서 이 값을 근거로 계산하지 않는다.
- 로컬에 `emfit_data.jsonl`(86MB)·`radar_data.jsonl`(44MB)가 있다. 전체 grep/읽기를 피하고
  `tail` 이나 필터로 접근한다.
- `app.py.bak_*` 는 패치 스크립트가 남긴 백업이다(gitignore 대상). 편집 대상이 아니다.
- 서버 다운타임 자동 감지가 없다(외부 모니터링 미설정). 대시보드·리포트는 DDNS 주소를 아는 사람이면
  열람 가능한 수준의 인증만 갖추고 있다 — 민감 데이터 취급상 개선 과제로 남아 있다.
