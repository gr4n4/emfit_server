import sys
if hasattr(sys.stdout, "reconfigure"):
    # Windows 콘솔 기본 코드페이지(cp949)는 한글 로그의 em dash(—) 등을 인코딩 못 해
    # print() 가 있는 모듈을 import 하는 순간 UnicodeEncodeError 로 서버가 죽는다.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, Request, Query, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse, RedirectResponse, JSONResponse
import json, os, uvicorn, threading, io, zipfile, html, secrets, hmac, hashlib, time, re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import pandas as pd
import requests
import analyzer
from nrcarec_alert import send_alert, should_send

# SemVer (MAJOR.MINOR.PATCH) — 변경 시 CHANGELOG.md 같이 업데이트.
# MAJOR: 기존 사용 방식이 깨지는 변경 / MINOR: 기능 추가 / PATCH: 버그·자잘한 수정.
VERSION = "3.18.2"

app = FastAPI()
LOG_FILE = "emfit_data.jsonl"
RADAR_LOG_FILE = "radar_data.jsonl"  # AI Radar 는 별도 파일에 쌓는다
MCKARE_LOG_FILE = "mckare_data.jsonl"  # McKare(VSR22) 도 별도 파일
MCKARE_APIKEY_FILE = "mckare_apikey.txt"  # 있으면 그 값으로 ApiKey 검증, 없으면 미적용
# McKare AI 110 열화상 이미지 — ⚠️ 이미지는 jsonl 에 넣지 않는다.
# 한 장 10KB 라도 1분마다면 하루 14MB, 1년 5GB 다. 측정값 로그에 섞으면
# 대시보드가 읽어야 할 파일이 그만큼 무거워져 조회가 통째로 느려진다.
# 파일은 날짜별 폴더에 두고, jsonl 에는 '어디에 뭐가 있다'는 목록만 남긴다.
MCKARE_IMAGE_DIR = "mckare_images"
MCKARE_IMAGE_LOG = "mckare_image_log.jsonl"   # 이미지 목록(메타데이터)만
MCKARE_IMAGE_MAX_BYTES = 10 * 1024 * 1024     # 한 장 상한 — 이상하게 큰 요청 차단
FSR_LOG_FILE = "fsr_data.jsonl"  # ESP32 압력 사용감지 센서(돌봄기기 부착) 이벤트
# 대시보드·리포트가 읽어야 할 로그 파일 전체. 기기 종류가 늘면 여기에 추가.
# 원본을 나눠두면 한쪽 데이터가 커져도 다른 쪽 조회 속도에 영향을 주지 않는다.
DATA_FILES = [LOG_FILE, RADAR_LOG_FILE, MCKARE_LOG_FILE, FSR_LOG_FILE]
FEEDBACK_FILE = "feedback.jsonl"
TOKENS_FILE = "device_tokens.json"
VIEW_TOKENS_FILE = "view_tokens.json"  # 그룹(여러 기기 묶음) 보기 토큰
ADMIN_PW_FILE = "admin_password.txt"
PREFERENCES_FILE = "preferences.json"  # viewer 단위 UI 환경설정 (블록 순서 등)
DISCORD_CONFIG_FILE = "discord_config.json"  # Webhook URL·끊김 임계값 (/admin/discord 에서 편집)
ADMIN_COOKIE = "emfit_admin"  # device 토큰 쿠키 이름 (변수 이름은 옛 잔재)
SESSION_COOKIE = "emfit_session"  # 관리자 로그인 세션 쿠키
VIEW_COOKIE = "emfit_view"  # 그룹(view) 토큰 쿠키
# 관리자 ID — 비번은 ADMIN_PW_FILE에서 읽음. ID 변경 원하면 환경변수로.
ADMIN_USERNAME = os.environ.get("EMFIT_ADMIN_USER", "operator")
# 세션 쿠키 서명용 비밀키 — 서버 부팅 시 1회 생성. 재시작하면 모두 로그아웃됨.
_SESSION_SECRET = secrets.token_bytes(32)
# 외부 접속 URL (DDNS). 내부 base는 사용자가 들어온 host에서 자동 추출.
EXTERNAL_BASE = os.environ.get("EMFIT_EXTERNAL_BASE", "http://monitoring.example.com")
_tokens_lock = threading.Lock()
_view_tokens_lock = threading.Lock()
_feedback_lock = threading.Lock()
_prefs_lock = threading.Lock()

# 상세페이지 블록의 기본 순서.
# '얼마나 오래'(재실·자세별·사용 시간)를 맨 위에 둔다 — 화면을 열자마자 제일 먼저
# 알고 싶은 게 그 값이고, 그래프는 그 뒤를 뒷받침하는 근거이기 때문.
# ※ 이미 순서를 바꿔 저장한 사용자는 그 순서가 유지되고, 새 블록만 뒤에 붙는다.
#    (사람이 정한 배치를 코드가 되돌리지 않는다. 원하면 화면에서 다시 끌어 올리면 됨)
DEFAULT_BLOCK_ORDER = ["occupancy", "postures", "presence", "usage", "daily",
                       "summary", "hr", "rr", "act", "posture", "temp"]

# 기기 종류마다 상세페이지에 넣을 블록이 다르다.
# 안 재는 값을 빈 그래프로 그려두면 "고장인가?" 하고 헷갈리므로 아예 만들지 않는다.
#   emfit  : 수면요약 + 심박 + 호흡 + 활동량 (기존 그대로)
#   radar  : 심박 + 호흡 + 자세  (활동량·수면요약은 레이더가 측정하지 않음)
#   mckare : 심박 + 호흡 + 체온  (체온은 McKare 만 잰다)
#   fsr    : 사용구간 + 일별 사용시간 (생체신호를 아예 재지 않음)
#   occupancy/postures/presence : '얼마나 오래' 통계 (재실 시간·자세별 시간·구간 재실)
DEVICE_BLOCKS = {
    "emfit":  ["occupancy", "summary", "hr", "rr", "act"],
    "radar":  ["postures", "hr", "rr", "posture"],
    "mckare": ["presence", "hr", "rr", "temp"],
    "fsr":    ["usage", "daily"],
}
# 블록 순서 환경설정에서 허용하는 전체 블록 목록 (viewer 단위라 기기 종류와 무관하게 저장됨).
# 화면에 없는 블록 id 는 프런트에서 그냥 무시되므로 한 목록으로 관리해도 안전하다.
ALL_BLOCK_IDS = ["occupancy", "postures", "presence", "summary",
                 "hr", "rr", "act", "posture", "temp", "usage", "daily"]

# 일별 사용시간 그래프에 보여줄 최근 날짜 수
FSR_DAILY_DAYS = 14

# Emfit 데이터는 모두 KST 기준으로 저장됨 (analyzer가 UTC → Asia/Seoul 변환).
# 프런트는 datetime-local로 KST 시각을 입력하고, 그대로 KST aware datetime으로 해석.
KST = timezone(timedelta(hours=9))


def _has_data():
    """수집된 로그 파일이 하나라도 있는지."""
    return any(os.path.exists(p) for p in DATA_FILES)


def _parse_kst_dt(s):
    """프런트 datetime-local 문자열('YYYY-MM-DDTHH:MM' 또는 '...:SS') → KST aware datetime.
    실패 시 None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


FEEDBACK_STATUSES = {
    "pending": ("미조치", "#90a4ae", "🔵"),
    "in_progress": ("조치중", "#f39c12", "🟡"),
    "done": ("조치완료", "#27ae60", "✅"),
    "cant": ("조치불가", "#e57373", "❌"),
}


def _load_feedback_items():
    """전체 의견 목록 로드. 빠진 필드는 기본값으로."""
    items = []
    if not os.path.exists(FEEDBACK_FILE):
        return items
    try:
        with open(FEEDBACK_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                obj.setdefault("status", "pending")
                obj.setdefault("replies", [])
                items.append(obj)
    except Exception:
        pass
    return items


def _save_feedback_items(items):
    """전체 의견 목록을 파일로 다시 작성."""
    with _feedback_lock:
        tmp = FEEDBACK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, FEEDBACK_FILE)


def _load_tokens():
    """device_tokens.json 로드. 형식: {"<token>": "<sn>" 또는 "*", ...}. 관리자 토큰은 SN 자리에 "*"."""
    if not os.path.exists(TOKENS_FILE):
        return {}
    try:
        with open(TOKENS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_tokens(tokens):
    with _tokens_lock:
        tmp = TOKENS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tokens, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TOKENS_FILE)


def _load_view_tokens():
    """view_tokens.json 로드. 형식: {"<token>": {"name": "A시설", "sns": ["EMFIT-DEMO-01", ...]}}."""
    if not os.path.exists(VIEW_TOKENS_FILE):
        return {}
    try:
        with open(VIEW_TOKENS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_view_tokens(views):
    with _view_tokens_lock:
        tmp = VIEW_TOKENS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(views, f, ensure_ascii=False, indent=2)
        os.replace(tmp, VIEW_TOKENS_FILE)


def _load_preferences():
    """preferences.json 로드. 형식: {"<viewer_id>": {"block_order": [...]}, ...}.
    viewer_id 는 admin이면 "admin", 아니면 device 토큰 값."""
    if not os.path.exists(PREFERENCES_FILE):
        return {}
    try:
        with open(PREFERENCES_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_preferences(prefs):
    with _prefs_lock:
        tmp = PREFERENCES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PREFERENCES_FILE)


# (admin 토큰 부트스트랩 제거됨 — 이제 admin은 ID/비번으로 인증)


def _load_admin_password():
    """토큰 발급 페이지용 비밀번호 파일에서 읽음. 없거나 빈 줄이면 None."""
    if not os.path.exists(ADMIN_PW_FILE):
        return None
    try:
        with open(ADMIN_PW_FILE, encoding="utf-8") as f:
            pw = f.read().strip()
        return pw or None
    except Exception:
        return None


def _bootstrap_admin_password():
    """비번 파일이 없으면 랜덤 생성. 사용자가 ssh로 직접 편집해서 원하는 비번으로 바꿀 수 있음."""
    if _load_admin_password() is not None:
        return
    pw = secrets.token_urlsafe(9)
    try:
        with open(ADMIN_PW_FILE, "w", encoding="utf-8") as f:
            f.write(pw + "\n")
        try:
            os.chmod(ADMIN_PW_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[emfit] 비밀번호 파일 생성 실패: {e}")
        return
    print("=" * 60)
    print("[emfit] 토큰 발급 페이지 비밀번호가 새로 생성되었습니다:")
    print(f"  {pw}")
    print(f"  파일: {ADMIN_PW_FILE}  (직접 편집해서 원하는 비번으로 변경 가능)")
    print("=" * 60)


_bootstrap_admin_password()


def _check_basic_credentials(username, password):
    """ID와 비번이 모두 일치하면 True. timing-safe 비교."""
    pw = _load_admin_password()
    if pw is None:
        return False
    return (
        secrets.compare_digest(username or "", ADMIN_USERNAME)
        and secrets.compare_digest(password or "", pw)
    )


def _make_session_cookie():
    """현재 ID/비번 기반 HMAC. 비번 바뀌거나 서버 재시작 시 자동 무효화."""
    pw = _load_admin_password() or ""
    msg = (ADMIN_USERNAME + ":" + pw).encode("utf-8")
    return hmac.new(_SESSION_SECRET, msg, hashlib.sha256).hexdigest()


def _is_admin_authenticated(request: Request):
    """세션 쿠키가 유효한지 확인 (예외 없음)."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    expected = _make_session_cookie()
    return hmac.compare_digest(cookie, expected)


def require_admin(request: Request):
    """모든 admin 페이지 — 세션 쿠키 통과해야 접근 가능. 없으면 로그인 페이지로."""
    if not _is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="login required")
    return ADMIN_USERNAME


def _get_token_from_request(request: Request):
    """쿼리 파라미터 ?token= 또는 쿠키에서 device 토큰 추출."""
    t = request.query_params.get("token")
    if t:
        return t
    return request.cookies.get(ADMIN_COOKIE)


def _get_view_token_from_request(request: Request):
    """쿼리 파라미터 ?view= 또는 쿠키에서 view(그룹) 토큰 추출.

    admin도 그룹 대시보드(/view)를 봐야 하므로 쿠키를 막지 않는다.
    'admin이 전체 대시보드에서 그룹 기기를 눌렀을 때 그룹으로 빨려가는' 문제는
    여기서가 아니라 /device/{sn} 의 in_view 판정에서 '명시적 ?view=' 로만
    그룹 컨텍스트를 인정하는 방식으로 따로 처리한다."""
    t = request.query_params.get("view")
    if t:
        return t
    return request.cookies.get(VIEW_COOKIE)


def _resolve_view(request: Request):
    """현재 view 토큰이 가리키는 그룹 정보. 유효하면 {token, name, sns} 반환, 아니면 None."""
    t = _get_view_token_from_request(request)
    if not t:
        return None
    views = _load_view_tokens()
    v = views.get(t)
    if not isinstance(v, dict):
        return None
    sns = v.get("sns") or []
    if not isinstance(sns, list) or not sns:
        return None
    return {"token": t, "name": str(v.get("name") or ""), "sns": list(sns)}

# ── 점검(워밍업) 게이트 ───────────────────────────────────────────────
# 재배포·재시작 직후 대용량 로그를 파싱하는 동안 깔끔한 '점검 중' 페이지를 보여준다.
# 파싱은 백그라운드 스레드에서 돌고, 끝나면 _SERVER_READY 가 True 로 바뀐다.
_SERVER_READY = False


def _maintenance_page_html():
    return """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="5">
    <title>점검 중 · 돌봄기기 통합 대시보드</title>
    <style>
        body { font-family:'Malgun Gothic',sans-serif; margin:0; min-height:100vh;
               display:flex; align-items:center; justify-content:center;
               background:linear-gradient(135deg,#1a73e8,#5b3fd6); }
        .card { background:white; padding:48px 36px; border-radius:20px; text-align:center;
                box-shadow:0 12px 32px rgba(0,0,0,0.2); max-width:380px; }
        .spinner { width:54px; height:54px; margin:0 auto 22px; border:6px solid #e3e8f0;
                   border-top-color:#1a73e8; border-radius:50%; animation:spin 1s linear infinite; }
        @keyframes spin { to { transform:rotate(360deg); } }
        h1 { color:#1a237e; margin:0 0 10px; font-size:1.4em; }
        p { color:#607d8b; line-height:1.6; margin:6px 0; }
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner"></div>
        <h1>🛠️ 점검 중입니다</h1>
        <p>데이터를 준비하고 있어요.<br>잠시만 기다려 주세요.</p>
        <p style="font-size:0.85em; color:#90a4ae;">이 화면은 5초마다 자동으로 새로고침됩니다.</p>
    </div>
</body>
</html>"""


def _warmup_then_ready():
    """백그라운드에서 로그 파싱(워밍업)을 끝낸 뒤 서버를 '준비됨' 상태로 전환."""
    global _SERVER_READY
    try:
        if _has_data():
            analyzer.warmup(DATA_FILES)
    except Exception as e:
        print(f"[startup] 워밍업 실패: {e}", flush=True)
    finally:
        _SERVER_READY = True
        print("[startup] 파싱 완료 — 서버 준비됨", flush=True)


# 서비스 시작 시 백그라운드에서 캐시 워밍업 (첫 요청 느림 방지)
threading.Thread(target=_warmup_then_ready, daemon=True).start()


# ── 디스코드 연결 끊김 알림 ──────────────────────────────────────
# Webhook URL·임계값은 파일로 저장해서 /admin/discord 화면에서 바로 바꿀 수 있고,
# 서버 재시작 없이 다음 점검 주기(1분 이내)부터 반영된다.
DISCORD_CHECK_INTERVAL_SEC = 60
DISCORD_ALERT_STATE_FILE = "discord_alert_state.json"
_DEFAULT_DISCORD_CONFIG = {
    "webhook_url": "",          # 기본 채널 — 기기에 아래 channels 매핑이 없으면 여기로 감
    "threshold_minutes": 30,
    "enabled": False,
    "device_overrides": {},     # {sn: 임계값(분)}
    "channels": {},             # {채널이름: Webhook URL}  — 시설별 채널 등록
    "device_channels": {},      # {sn: 채널이름}  — 없으면 기본 채널(webhook_url)로 감
}
_discord_config_lock = threading.Lock()


def _load_discord_alerted():
    """파일로 저장해둔다 — 메모리에만 두면 서버 재시작(배포·크래시 등)마다
    '이미 끊겨있던 기기'를 새로 끊긴 걸로 착각해 알림을 또 보낸다."""
    if not os.path.exists(DISCORD_ALERT_STATE_FILE):
        return {}
    try:
        with open(DISCORD_ALERT_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {sn: True for sn, v in data.items() if v}
    except Exception:
        return {}


def _save_discord_alerted():
    try:
        with open(DISCORD_ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_discord_alerted, f, ensure_ascii=False)
    except Exception as e:
        print(f"[discord] 알림 상태 저장 실패: {e}", flush=True)


# {sn: True}  끊김 알림을 이미 보낸 기기. 복구되면 지워서 다음에 또 끊기면 다시 보낸다.
_discord_alerted = _load_discord_alerted()


def _load_discord_config():
    with _discord_config_lock:
        if not os.path.exists(DISCORD_CONFIG_FILE):
            return dict(_DEFAULT_DISCORD_CONFIG)
        try:
            with open(DISCORD_CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            return {**_DEFAULT_DISCORD_CONFIG, **cfg}
        except Exception:
            return dict(_DEFAULT_DISCORD_CONFIG)


def _save_discord_config(cfg):
    with _discord_config_lock:
        with open(DISCORD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def _send_discord_message(webhook_url, content):
    """알림 실패가 서버를 죽이면 안 되므로 예외는 여기서 삼킨다."""
    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        return 200 <= resp.status_code < 300
    except Exception as e:
        print(f"[discord] 알림 전송 실패: {e}", flush=True)
        return False


def _discord_threshold_minutes(cfg, sn):
    """기기별 임계값(device_overrides)이 있으면 그걸, 없으면 전역 기본값을 쓴다.
    보고 주기가 원래 긴 기기(예: 30분 간격 게이트웨이)를 전역 기준(30분)에 맞춰
    끊김/복구로 계속 플랩하는 걸 막기 위함 — 그 기기만 여유를 더 줄 수 있게."""
    overrides = cfg.get("device_overrides") or {}
    val = overrides.get(sn)
    if isinstance(val, (int, float)) and val > 0:
        return int(val)
    return max(1, int(cfg.get("threshold_minutes") or 30))


# 기기 종류 표시 이름 — 대시보드(_V2_SECTIONS)와 같은 문구를 재사용해서 알림 메시지에 붙인다.
# 병동처럼 여러 기기가 있고 사용자명이 겹치는 곳에서, 어느 종류 기기인지 메시지만 보고 구분하기 위함.
_DISCORD_KIND_LABELS = {"emfit": "EMFIT QS", "radar": "AI Radar", "mckare": "McKare", "fsr": "돌봄기기 사용 감지"}


def _nrcarec_patient_context(sn):
    """Radar SN 배정 정보에서 NRCarec 알림의 호실/이름을 만든다."""
    info = analyzer.DEVICE_INFO.get(sn) or {}
    location = str(info.get("location") or "").strip()
    name = str(info.get("name") or "").strip()
    if location == "-":
        location = ""

    room_match = re.search(r"(\d+)\s*호?", location) or re.search(r"(\d+)\s*호?", name)
    room = room_match.group(1) if room_match else ""
    if room:
        name = re.sub(rf"^\s*{re.escape(room)}\s*호?\s*", "", name).strip()
    return room, name or "사용자"


def _discord_device_snapshot(cfg):
    """숨기지 않은 기기 전체의 연결 상태 스냅샷.
    /admin/discord 설정 화면, 끊김 감시 루프, 디스코드 봇 API가 모두 이 함수 하나를 써서
    '연결됨' 판정 기준이 세 곳에서 어긋나지 않게 한다."""
    now_ts = datetime.now(timezone.utc).timestamp()
    statuses = analyzer.get_device_statuses()
    out = []
    for sn in sorted(analyzer.DEVICE_INFO):
        info = analyzer.DEVICE_INFO[sn]
        if info.get("hidden"):
            continue
        ds = statuses.get(sn)
        last_seen = ds.get("last_seen_ts") if isinstance(ds, dict) else None
        name = str(info.get("name") or sn)
        location = str(info.get("location") or "").strip()
        if location == "-":
            location = ""
        threshold_minutes = _discord_threshold_minutes(cfg, sn)
        kind = _v2_kind(sn, None, ds)

        if isinstance(last_seen, (int, float)):
            age_sec = now_ts - last_seen
            last_seen_text = _format_ago(int(age_sec))
        else:
            age_sec, last_seen_text = None, "통신 이력 없음"

        raw_connected = ds.get("connected") if isinstance(ds, dict) else None
        raw_fault = bool(ds.get("fault")) if isinstance(ds, dict) else False
        if raw_connected is not None:
            # Discord alerts are based only on device-reported state, not elapsed
            # communication time. A quiet device can mean absence or idle use.
            connected = bool(raw_connected)
        elif kind == "fsr" and raw_fault:
            connected = False
        else:
            connected = None

        out.append({
            "sn": sn, "name": name, "location": location,
            "connected": connected, "age_sec": age_sec, "last_seen_text": last_seen_text,
            "threshold_minutes": threshold_minutes,
            "kind": kind, "kind_label": _DISCORD_KIND_LABELS.get(kind, kind),
        })
    return out


def _discord_webhook_for(cfg, sn):
    """기기가 특정 채널(시설)에 배정돼 있으면 그 채널로, 아니면 기본 채널로."""
    ch_name = (cfg.get("device_channels") or {}).get(sn)
    if ch_name:
        url = (cfg.get("channels") or {}).get(ch_name)
        if url:
            return url
    return cfg.get("webhook_url") or ""


# {sn: 처음 '끊긴 것 같다'고 감지한 시각(epoch)} — 아직 알림은 안 보낸, 확인 대기 중인 기기.
# 재시작으로 이게 날아가도 최악의 경우 확인이 한 번 더 늦어질 뿐(다시 처음부터 재는 것뿐)이라
# _discord_alerted 와 달리 파일로 저장하지 않는다 — 잘못된 방향(거짓 알림)으로 새지 않는 쪽.
_discord_pending = {}


def _discord_check_once():
    cfg = _load_discord_config()
    if not cfg.get("enabled"):
        return
    now_ts = datetime.now(timezone.utc).timestamp()

    for d in _discord_device_snapshot(cfg):
        if d["connected"] is None:
            continue  # 한 번도 통신한 적 없는 기기 — 판단 근거가 없으니 건너뜀
        sn = d["sn"]
        webhook_url = _discord_webhook_for(cfg, sn)
        if not webhook_url:
            continue
        was_alerted = _discord_alerted.get(sn, False)
        label = f"({d['kind_label']}) {d['name']} ({d['location']})" if d["location"] else f"({d['kind_label']}) {d['name']}"

        if d["connected"]:
            _discord_pending.pop(sn, None)  # 확인 대기 중이었다면 조용히 취소 — 알림 자체가 없었으니 복구 알림도 없음
            if was_alerted:
                if _send_discord_message(webhook_url, f"🟢 **연결 복구** — {label}"):
                    _discord_alerted.pop(sn, None)
                    _save_discord_alerted()
            continue

        if was_alerted:
            continue  # 이미 끊김 알림 보낸 상태 — 복구될 때까지 조용히 대기

        # 여기부터는 '지금 이 순간 끊긴 것처럼 보이는' 기기.
        # 곧바로 알림을 보내지 않고, 같은 시간(threshold_minutes)만큼 더 지켜봐서
        # 그사이 복구되면(하트비트 지연 등 일시적 현상) 알림 자체를 안 보낸다 — 양치기 소년 방지.
        confirm_sec = d["threshold_minutes"] * 60
        pending_since = _discord_pending.get(sn)
        if pending_since is None:
            _discord_pending[sn] = now_ts
        elif now_ts - pending_since >= confirm_sec:
            if _send_discord_message(
                webhook_url,
                f"🔴 **연결 끊김** — {label}\n마지막 통신: {d['last_seen_text']}",
            ):
                _discord_alerted[sn] = True
                _save_discord_alerted()
                _discord_pending.pop(sn, None)


def _discord_monitor_loop():
    while True:
        try:
            _discord_check_once()
        except Exception as e:
            print(f"[discord] 점검 중 오류: {e}", flush=True)
        time.sleep(DISCORD_CHECK_INTERVAL_SEC)


threading.Thread(target=_discord_monitor_loop, daemon=True).start()


@app.middleware("http")
async def _maintenance_gate(request: Request, call_next):
    """워밍업이 안 끝났으면 점검 페이지(503)로 응답한다.
    단, Emfit(POST /)·AI Radar(POST /radar)·McKare(POST /mckare)·FSR(POST /jy01)의
    데이터 수신은 점검 중에도 받아 데이터 유실을 막는다."""
    if not _SERVER_READY:
        # McKare 표준 경로는 여러 개라 아래 목록과 합쳐서 판단한다.
        receive_paths = ({"/", "/radar", "/jy01"}
                         | set(_MCKARE_PATHS) | set(_MCKARE_IMAGE_PATHS))
        if not (request.method == "POST" and request.url.path in receive_paths):
            return HTMLResponse(_maintenance_page_html(), status_code=503,
                                headers={"Retry-After": "5"})
    return await call_next(request)


from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException


def _login_page_html(error="", next_url="/dashboard"):
    """예쁜 로그인 폼."""
    safe_next = html.escape(next_url)
    error_html = (
        f'<div style="background:#ffebee; border-left:3px solid #e57373; color:#b71c1c; padding:10px 14px; border-radius:6px; margin-bottom:14px; font-size:0.9em;">⚠️ {html.escape(error)}</div>'
        if error else ""
    )
    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>관리자 로그인 · 돌봄기기 통합 대시보드</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{ box-sizing: border-box; }}
            body {{
                font-family: 'Malgun Gothic', sans-serif; margin: 0;
                min-height: 100vh; display: flex; align-items: center; justify-content: center;
                background: linear-gradient(135deg, #1a73e8 0%, #5e35b1 100%);
                padding: 20px;
            }}
            .box {{
                width: 100%; max-width: 380px;
                background: white; padding: 36px 32px; border-radius: 16px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.2);
            }}
            .logo {{ text-align: center; font-size: 2.5em; margin-bottom: 8px; }}
            h1 {{ text-align: center; color: #1a237e; margin: 0 0 6px 0; font-size: 1.4em; }}
            .sub {{ text-align: center; color: #90a4ae; font-size: 0.85em; margin: 0 0 24px 0; }}
            label {{ display: block; font-size: 0.85em; color: #546e7a; margin-bottom: 6px; font-weight: bold; }}
            input[type=text], input[type=password] {{
                width: 100%; padding: 12px 14px; border: 1px solid #cfd8dc; border-radius: 8px;
                font-size: 1em; margin-bottom: 14px; transition: border-color 0.15s;
            }}
            input:focus {{ outline: none; border-color: #1a73e8; }}
            button {{
                width: 100%; padding: 12px; border: none; border-radius: 8px;
                background: #1a73e8; color: white; font-size: 1em; font-weight: bold;
                cursor: pointer; transition: background 0.15s; margin-top: 6px;
            }}
            button:hover {{ background: #1557b0; }}
            .footer {{ text-align: center; color: #b0bec5; font-size: 0.75em; margin-top: 20px; }}
        </style>
    </head>
    <body>
        <div class="box">
            <div class="logo">📡</div>
            <h1>돌봄기기 통합 대시보드</h1>
            <p class="sub">관리자 로그인</p>
            {error_html}
            <form method="post" action="/login" autocomplete="on">
                <input type="hidden" name="next" value="{safe_next}">
                <label for="username">아이디</label>
                <input type="text" id="username" name="username" autocomplete="username" required autofocus>
                <label for="password">비밀번호</label>
                <input type="password" id="password" name="password" autocomplete="current-password" required>
                <button type="submit">로그인</button>
            </form>
            <p class="footer">권한 없으신가요? 관리자에게 문의해주세요.</p>
        </div>
    </body>
    </html>
    """


def _other_device_page_html():
    """피험자가 자기 SN 외 다른 기기에 접근하려 할 때 표시."""
    return """
    <html>
    <head>
        <meta charset="utf-8">
        <title>접근 불가 · 돌봄기기 통합 대시보드</title>
        <style>
            body{ font-family:'Malgun Gothic',sans-serif; padding:40px; background:#f0f2f5; margin:0; }
            .box{ max-width:480px; margin:60px auto; background:white; padding:36px; border-radius:14px;
                  box-shadow:0 10px 30px rgba(0,0,0,0.08); text-align:center; }
            h1{ color:#e57373; margin:8px 0; }
            p{ color:#546e7a; line-height:1.6; }
        </style>
    </head>
    <body>
        <div class="box">
            <div style="font-size:3em;">🔒</div>
            <h1>다른 기기 페이지</h1>
            <p>전달받으신 URL은 다른 기기를 볼 수 없습니다.<br>본인 URL로 다시 접속해주세요.</p>
        </div>
    </body>
    </html>
    """


@app.exception_handler(StarletteHTTPException)
async def _auth_html_handler(request: Request, exc: StarletteHTTPException):
    """401 → 로그인 폼, 403 → 다른 기기 안내, 그 외 → 기본 텍스트."""
    if exc.status_code == 401:
        # 원래 가려던 URL을 next= 로 넘겨, 로그인 후 그 페이지로 돌려보냄.
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        # next_url을 폼에 넣을 거라 따옴표 잘 처리됨 (escape는 _login_page_html 안에서)
        return HTMLResponse(_login_page_html(next_url=next_url), status_code=401)
    if exc.status_code == 403:
        return HTMLResponse(_other_device_page_html(), status_code=403)
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

def _state_dt(state):
    """측정 레코드의 '날짜 + 시간(KST)' → KST 인식 datetime.

    ⚠️ 반드시 tz 를 붙여야 한다. 예전에는 naive 로 만들어 서버 로컬 시각과 직접 뺐는데,
    서버 시간대가 KST 가 아니면 계산이 통째로 어긋났다. 특히 UTC 서버에서는
    5시간 전에 끊긴 기기가 '재실 · 방금' 으로 표시돼, 죽은 장비를 정상으로 오인하게 된다.
    실패 시 예외를 그대로 올려 호출부의 try/except 가 '측정 시각 알 수 없음' 으로 처리한다."""
    return datetime.strptime(f"{state['날짜']} {state['시간(KST)']}",
                             "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)


def _format_ago(delta_sec):
    if delta_sec < 60:
        return "방금"
    m = delta_sec // 60
    if m < 60:
        return f"{m}분 전"
    if m < 24 * 60:
        return f"{m // 60}시간 전"
    return f"{m // (24 * 60)}일 전"


def _is_active(ds, now_ts):
    """7일 이내 통신 이력이 있으면 활성 기기."""
    if ds is None:
        return False
    last_seen = ds.get("last_seen_ts")
    if not isinstance(last_seen, (int, float)):
        return False
    return (now_ts - last_seen) <= 7 * 24 * 3600


def _is_radar_device(sn, state=None, ds=None):
    """Radar 분석 기록, 상태 출처 또는 12자리 MAC으로 AI Radar를 판별."""
    if isinstance(state, dict) and state.get("유형") == "Radar":
        return True
    if isinstance(ds, dict) and ds.get("source") == "ai_radar":
        return True
    compact = str(sn or "").replace(":", "").replace("-", "").upper()
    return len(compact) == 12 and all(c in "0123456789ABCDEF" for c in compact)


# ── 사용감지 센서 상태 판정 기준 ─────────────────────────────────────
# 원칙: '센서 확인 필요'는 **근거가 있을 때만** 띄운다.
#
# 조용한 것은 근거가 아니다. 하루 종일 기기를 안 쓸 수도 있고, 서버는
# '신호가 없다'만 알 뿐 그게 고장 때문인지 미사용 때문인지 구분하지 못한다.
# 침묵으로 경고를 띄우면 거짓 경보가 반복되어 진짜 고장도 무시하게 된다.
#
# 그래서 아래 세 가지 '확실한 근거'로만 판정한다:
#   1. 펌웨어가 센서 이상을 직접 알림 (event: error/disconnect/... → fault)
#   2. 배터리 잔량이 바닥 (아래 임계 이하)
#   3. 생존신고를 보내는 보드인데 그마저 끊김 (보내는 보드일 때만 적용)
#
# 3번은 보드가 실제로 생존신고를 보낸 적이 있어야만 작동한다(keepalive_seen).
# 이벤트만 보내는 보드에는 시간 기준을 아예 적용하지 않는다.
#
# 오래 죽어 있는 기기는 기존 7일 규칙이 '비활성 기기'로 따로 걸러준다.
FSR_KEEPALIVE_STALE_SEC = 3600      # 생존신고 보내는 보드 기준: 1시간 침묵이면 이상
FSR_BATT_CRITICAL = 5               # 이 이하면 '확인 필요' (사실상 방전)
FSR_BATT_LOW, FSR_BATT_WARN = 15, 30   # 카드 색 경고 기준(주의 표시용, 상태는 안 바꿈)


def _is_fsr_device(sn, state=None, ds=None):
    """사용감지 센서 판별. ⚠️ _is_radar_device 보다 먼저 확인해야 한다.

    ESP32 의 MAC(예: ECE334450058)도 12자리 16진수라 레이더와 생김새가 같다.
    그래서 SN 모양이 아니라 등록 정보의 kind 를 먼저 본다 —
    이래야 데이터가 아직 안 들어온 기기도 올바른 섹션에 뜬다."""
    if (analyzer.DEVICE_INFO.get(sn) or {}).get("kind") == analyzer.KIND_FSR:
        return True
    if isinstance(state, dict) and state.get("유형") == "FSR":
        return True
    return isinstance(ds, dict) and ds.get("source") == "fsr"


_FSR_CHECK = ("센서 확인 필요", "🔧", "#ffcdd2", "#e57373", "#b71c1c")


def _fsr_status(state, ds, now_ts):
    """돌봄기기 사용 상태 판정 → (라벨, 아이콘, 배경, 테두리, 글자색, 사유).

    사유는 '센서 확인 필요'일 때만 채워지고, 카드에 왜 그런지 한 줄로 보여준다.
    경과 판정은 문자열 시각이 아니라 epoch(last_seen_ts)로 해서
    서버 시간대가 KST 가 아니어도 어긋나지 않게 한다."""
    st = state if isinstance(state, dict) else {}
    d = ds if isinstance(ds, dict) else {}

    # 근거 1 — 펌웨어가 센서 이상을 직접 알려준 경우 (선 빠짐 등)
    if st.get("센서이상") or d.get("fault"):
        return _FSR_CHECK + ("압력 센서 연결 확인",)

    # 근거 2 — 배터리가 바닥.
    # 범위 밖(음수 등)은 '측정 불가'라는 뜻이지 방전이 아니다. 배터리 측정 회로가 없는
    # 보드는 -1 을 보내는데, 이걸 0% 로 읽으면 멀쩡한 센서가 '확인 필요'로 뜬다.
    batt = st.get("배터리(%)")
    if isinstance(batt, (int, float)) and 0 <= batt <= FSR_BATT_CRITICAL:
        return _FSR_CHECK + (f"배터리 소진 ({int(batt)}%)",)

    # 근거 3 — 생존신고를 보내는 보드인데 그마저 끊긴 경우.
    # 생존신고를 안 보내는 보드에는 적용하지 않는다 (조용함 ≠ 고장).
    if d.get("keepalive_seen"):
        last_seen = d.get("last_seen_ts")
        if isinstance(last_seen, (int, float)) and (now_ts - last_seen) > FSR_KEEPALIVE_STALE_SEC:
            return _FSR_CHECK + ("전원·통신 확인",)

    in_use = st.get("사용중")
    if in_use is True:
        return "사용 중", "🟢", "#e8f5e9", "#66bb6a", "#1b5e20", ""
    if in_use is False:
        return "미사용", "⚪", "#eceff1", "#b0bec5", "#455a64", ""
    # 사용 여부를 알 수 없는 이벤트(생존신고, 새 펌웨어 이벤트 등)
    label = str(st.get("이벤트설명") or "판정 대기")
    return label, "📻", "#e3f2fd", "#64b5f6", "#0d47a1", ""


def _fsr_battery_html(state):
    """배터리 잔량 표시. 값이 없거나 범위 밖(측정 불가)이면 빈 문자열."""
    pct = (state or {}).get("배터리(%)")
    if not isinstance(pct, (int, float)) or not (0 <= pct <= 100):
        return ""
    pct = int(pct)
    if pct <= FSR_BATT_LOW:
        icon, color = "🪫", "#c62828"
    elif pct <= FSR_BATT_WARN:
        icon, color = "🔋", "#ef6c00"
    else:
        icon, color = "🔋", "#2e7d32"
    mv = (state or {}).get("배터리(mV)")
    mv_txt = f" · {int(mv)}mV" if isinstance(mv, (int, float)) else ""
    return (f'<div style="text-align:center; margin-top:6px; font-size:0.8em; '
            f'font-weight:bold; color:{color};">{icon} 배터리 {pct}%{mv_txt}</div>')


def _render_fsr_card(sn, info, state, ds, now, link_suffix=""):
    """돌봄기기 사용감지 카드 — 사용자·설치장소·사용상태·배터리 네 가지만 보여준다.
    (생체신호를 재지 않으므로 HR/RR/ACT 칸을 만들지 않는다)"""
    location_text = html.escape(str(info['location'] if info['location'] and info['location'] != '-' else '미지정'))
    name = html.escape(str(info['name']))
    now_ts = now.timestamp()

    if state is None:
        return f"""
        <a href="/device/{sn}{link_suffix}" style="display:block; text-decoration:none; color:inherit;">
        <div style="background:#fff8e1; padding:16px; border-radius:14px; border:2px solid #ffd54f;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                    <div style="font-size:0.85em; color:#5d4037;">{location_text}</div>
                    <div style="font-size:1.3em; font-weight:bold; color:#263238;">{name}</div>
                </div>
                <div style="font-size:1.8em;">❓</div>
            </div>
            <div style="text-align:center; margin-top:20px; color:#5d4037; font-weight:bold;">수신 기록 없음</div>
            <div style="text-align:center; margin-top:6px; color:#90a4ae; font-size:0.65em;">{sn}</div>
        </div>
        </a>
        """

    status_label, status_icon, bg, border, text_color, reason = _fsr_status(state, ds, now_ts)
    reason_html = (
        f'<div style="text-align:center; margin-top:4px; font-size:0.82em; '
        f'font-weight:bold; color:{text_color};">→ {html.escape(reason)}</div>'
        if reason else ""
    )

    last_seen = ds.get("last_seen_ts") if isinstance(ds, dict) else None
    if isinstance(last_seen, (int, float)):
        last_txt = _format_ago(max(0, int(now_ts - last_seen)))
    else:
        last_txt = "?"

    # 직전에 얼마나 오래 쓰였는지 (사용 종료 이벤트에 실려 온다)
    dur = state.get("사용시간(ms)")
    dur_html = ""
    if isinstance(dur, (int, float)) and dur > 0:
        secs = dur / 1000
        if secs < 60:
            dur_txt = f"{secs:.0f}초"
        elif secs < 3600:
            dur_txt = f"{int(secs // 60)}분"
        else:
            dur_txt = f"{int(secs // 3600)}시간 {int((secs % 3600) // 60)}분"
        dur_html = (f'<div style="text-align:center; margin-top:8px; font-size:0.8em; '
                    f'color:#546e7a;">직전 사용 시간 <b>{dur_txt}</b></div>')

    return f"""
    <a href="/device/{sn}{link_suffix}" style="display:block; text-decoration:none; color:inherit;">
    <div style="background:{bg}; padding:16px; border-radius:14px; border:2px solid {border}; transition:transform 0.1s;" onmouseover="this.style.transform='translateY(-2px)'" onmouseout="this.style.transform='translateY(0)'">
        <div style="display:flex; justify-content:space-between; align-items:flex-start;">
            <div>
                <div style="font-size:0.85em; color:#455a64;">{location_text}</div>
                <div style="font-size:1.3em; font-weight:bold; color:#1a237e;">{name}<span style="display:inline-block; margin-left:6px; padding:2px 7px; border-radius:10px; background:#00695c; color:white; font-size:0.55em; vertical-align:middle;">사용감지</span></div>
            </div>
            <div style="font-size:1.8em;">{status_icon}</div>
        </div>
        <div style="margin-top:14px; padding:20px 4px; background:rgba(255,255,255,0.85); border-radius:10px; text-align:center;">
            <div style="font-size:0.72em; color:#00695c; font-weight:bold;">기기 사용 상태</div>
            <div style="font-weight:bold; font-size:2em; color:{text_color}; line-height:1.2; margin-top:2px;">{html.escape(status_label)}</div>
        </div>
        {reason_html}
        {dur_html}
        {_fsr_battery_html(state)}
        <div style="text-align:center; margin-top:6px; font-size:0.8em; color:#455a64;">
            마지막 신호: {last_txt}
        </div>
        <div style="text-align:center; margin-top:6px; color:#90a4ae; font-size:0.65em;">{sn}</div>
    </div>
    </a>
    """


def _card_sort_key(sn, state, ds, now):
    """이상 상태일수록 위로. 같은 카테고리에서는 SN 순.

    사용감지 센서는 생체값이 없어 아래 ACT 기준이 통하지 않으므로 따로 판정한다.
    순서: 센서 확인 필요 → 사용 중 → 미사용 → 판정 대기
    (대시보드 전체 원칙과 같다 — 손봐야 할 것이 위로, 조용한 것이 아래로)"""
    if _is_fsr_device(sn, state, ds):
        label = _fsr_status(state, ds, now.timestamp())[0] if state is not None else None
        return ({"센서 확인 필요": -1, "사용 중": 2, "미사용": 3}.get(label, 1), sn)
    if isinstance(state, dict) and state.get("낙상"):
        return (-1, sn)
    if ds is not None and not ds.get("connected"):
        return (0, sn)
    if state is None:
        return (1, sn)
    try:
        last_dt = _state_dt(state)
        mins_ago = (now - last_dt).total_seconds() / 60
    except Exception:
        return (2, sn)
    if mins_ago > 10:
        return (2, sn)
    act = state.get("활동량(ACT)")
    if isinstance(act, (int, float)) and act < 1:
        return (3, sn)
    return (4, sn)

def _render_inactive_card(sn, info, ds, now, link_suffix=""):
    """비활성 기기용 minimal 카드. 측정값은 안 보여주고 위치/이름/마지막 통신만."""
    location_text = html.escape(str(info['location'] if info['location'] and info['location'] != '-' else '미지정'))
    name_safe = html.escape(str(info['name']))
    if ds is None:
        last_text = "통신 이력 없음"
    else:
        last_seen_ts = ds.get("last_seen_ts")
        if isinstance(last_seen_ts, (int, float)):
            last_text = f"마지막 통신: {_format_ago(max(0, int(now.timestamp() - last_seen_ts)))}"
        else:
            last_text = "통신 끊김"
    return f"""
    <a href="/device/{sn}{link_suffix}" style="display:block; text-decoration:none; color:inherit;">
    <div style="background:#fafafa; padding:12px 14px; border-radius:10px; border:1px solid #e0e0e0; transition:transform 0.1s;" onmouseover="this.style.transform='translateY(-2px)'" onmouseout="this.style.transform='translateY(0)'">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
                <div style="font-size:0.75em; color:#78909c;">{location_text}</div>
                <div style="font-size:1em; font-weight:bold; color:#546e7a;">{name_safe}</div>
            </div>
            <div style="font-size:1.3em; opacity:0.6;">💤</div>
        </div>
        <div style="margin-top:8px; font-size:0.78em; color:#90a4ae;">{last_text}</div>
        <div style="margin-top:4px; color:#b0bec5; font-size:0.6em;">{sn}</div>
    </div>
    </a>
    """


def _render_card(sn, info, state, ds, now, link_suffix=""):
    location_text = html.escape(str(info['location'] if info['location'] and info['location'] != '-' else '미지정'))
    name_safe = html.escape(str(info['name']))
    is_radar = _is_radar_device(sn, state, ds)
    source_badge = (
        '<span style="display:inline-block; margin-left:6px; padding:2px 7px; border-radius:10px; '
        'background:#6a1b9a; color:white; font-size:0.55em; vertical-align:middle;">AI Radar</span>'
        if is_radar else ""
    )

    if ds is None:
        conn_icon, conn_text, conn_color = "❓", "상태 없음", "#78909c"
        connected = None
    elif ds.get("connected"):
        last_seen_ts = ds.get("last_seen_ts")
        seen_ago = None
        if isinstance(last_seen_ts, (int, float)):
            seen_ago = _format_ago(max(0, int(now.timestamp() - last_seen_ts)))
        conn_icon, conn_text, conn_color = "🟢", f"연결됨 ({seen_ago})" if seen_ago else "연결됨", "#1b5e20"
        connected = True
    else:
        last_seen_ts = ds.get("last_seen_ts")
        seen_ago = None
        if isinstance(last_seen_ts, (int, float)):
            seen_ago = _format_ago(max(0, int(now.timestamp() - last_seen_ts)))
        conn_icon, conn_text, conn_color = "🔴", f"끊김 ({seen_ago})" if seen_ago else "끊김", "#b71c1c"
        connected = False

    if state is None:
        return f"""
        <a href="/device/{sn}{link_suffix}" style="display:block; text-decoration:none; color:inherit;">
        <div style="background:#f5f7f9; padding:16px; border-radius:14px; border:2px solid #cfd8dc; transition:transform 0.1s;" onmouseover="this.style.transform='translateY(-2px)'" onmouseout="this.style.transform='translateY(0)'">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                    <div style="font-size:0.85em; color:#546e7a;">{location_text}</div>
                    <div style="font-size:1.3em; font-weight:bold; color:#263238;">{name_safe}{source_badge}</div>
                </div>
                <div style="font-size:1.8em;">{conn_icon}</div>
            </div>
            <div style="text-align:center; margin-top:20px; color:#546e7a; font-size:1em; font-weight:bold;">설치됨 · 측정 대기</div>
            <div style="text-align:center; margin-top:2px; color:#90a4ae; font-size:0.75em;">아직 측정된 사람이 없습니다</div>
            <div style="text-align:center; margin-top:6px; color:{conn_color}; font-size:0.9em; font-weight:bold;">{conn_text}</div>
            <div style="text-align:center; margin-top:6px; color:#90a4ae; font-size:0.65em;">{sn}</div>
        </div>
        </a>
        """

    try:
        last_dt = _state_dt(state)
        delta_sec = max(0, int((now - last_dt).total_seconds()))
        mins_ago = delta_sec // 60
        if delta_sec >= 3600:
            time_part = last_dt.strftime("%m-%d %H:%M")
        else:
            time_part = last_dt.strftime("%H:%M")
        time_str = f"{time_part} ({_format_ago(delta_sec)})"
    except Exception:
        mins_ago = 10 ** 9
        time_str = "?"

    hr = state.get("심박수(HR)")
    rr = state.get("호흡수(RR)")
    act = state.get("활동량(ACT)")
    act_num = act if isinstance(act, (int, float)) else None

    if is_radar:
        pos = state.get("자세(POS)")
        posture = str(state.get("자세") or "-")
        metric_label = "🧭 자세"
        metric_value = posture
        metric_font_size = "1.25em"

        if connected is False:
            status_icon, status_label, bg, border, text_color = "🔴", "끊김", "#ffcdd2", "#e57373", "#b71c1c"
        elif mins_ago > 10:
            status_icon, status_label, bg, border, text_color = "⏱️", "수신 지연", "#fff3e0", "#ffb74d", "#e65100"
        elif state.get("낙상") or pos == 4:
            status_icon, status_label, bg, border, text_color = "🚨", "낙상", "#ffebee", "#e53935", "#b71c1c"
        elif pos == 5:
            status_icon, status_label, bg, border, text_color = "🚪", "자리비움", "#efebe9", "#bcaaa4", "#4e342e"
        elif pos == -1:
            status_icon, status_label, bg, border, text_color = "📡", "감지 대기", "#eceff1", "#b0bec5", "#455a64"
        else:
            status_icon, status_label, bg, border, text_color = "🛌", "재실", "#e8eaf6", "#7986cb", "#283593"

        hide_vitals = status_label in ("끊김", "수신 지연", "자리비움", "감지 대기")
        hr_str = "-" if hide_vitals else (f"{hr:.0f}" if isinstance(hr, (int, float)) else "-")
        rr_str = "-" if hide_vitals else (f"{rr:.0f}" if isinstance(rr, (int, float)) else "-")
        if status_label in ("끊김", "수신 지연"):
            metric_value = "-"
    else:
        metric_label = "🏃 ACT"
        metric_value = f"{act_num:.0f}" if act_num is not None else "-"
        metric_font_size = "2.4em"

        # Emfit 상태는 기존 방식 유지: 끊김 / 부재 / 재실
        absent = act_num is not None and act_num < 1
        if connected is False:
            status_icon, status_label, bg, border, text_color = "🔴", "끊김", "#ffcdd2", "#e57373", "#b71c1c"
        elif mins_ago > 10 or absent:
            status_icon, status_label, bg, border, text_color = "🛏️", "부재", "#efebe9", "#bcaaa4", "#4e342e"
        else:
            status_icon, status_label, bg, border, text_color = "🛌", "재실", "#e3f2fd", "#64b5f6", "#0d47a1"

        if status_label != "재실":
            hr_str = "-"
            rr_str = "-"
            metric_value = "-"
        else:
            hr_str = f"{hr:.0f}" if isinstance(hr, (int, float)) else "-"
            rr_str = f"{rr:.0f}" if isinstance(rr, (int, float)) else "-"

    return f"""
    <a href="/device/{sn}{link_suffix}" style="display:block; text-decoration:none; color:inherit;">
    <div style="background:{bg}; padding:16px; border-radius:14px; border:2px solid {border}; transition:transform 0.1s;" onmouseover="this.style.transform='translateY(-2px)'" onmouseout="this.style.transform='translateY(0)'">
        <div style="display:flex; justify-content:space-between; align-items:flex-start;">
            <div>
                <div style="font-size:0.85em; color:#455a64;">{location_text}</div>
                <div style="font-size:1.3em; font-weight:bold; color:#1a237e;">{name_safe}{source_badge}</div>
            </div>
            <div style="font-size:1.8em;">{status_icon}</div>
        </div>
        <div style="display:flex; justify-content:space-between; margin-top:14px; padding:14px 4px; background:rgba(255,255,255,0.85); border-radius:10px;">
            <div style="text-align:center; flex:1;">
                <div style="font-size:0.7em; color:#c0392b; font-weight:bold;">❤️ HR</div>
                <div style="font-weight:bold; font-size:2.4em; color:#212121; line-height:1.1;">{hr_str}</div>
            </div>
            <div style="text-align:center; flex:1; border-left:1px solid #ddd; border-right:1px solid #ddd;">
                <div style="font-size:0.7em; color:#1976d2; font-weight:bold;">🫁 RR</div>
                <div style="font-weight:bold; font-size:2.4em; color:#212121; line-height:1.1;">{rr_str}</div>
            </div>
            <div style="text-align:center; flex:1;">
                <div style="font-size:0.7em; color:#e67e22; font-weight:bold;">{metric_label}</div>
                <div style="font-weight:bold; font-size:{metric_font_size}; color:#212121; line-height:1.1; min-height:1.1em; display:flex; align-items:center; justify-content:center;">{metric_value}</div>
            </div>
        </div>
        <div style="text-align:center; margin-top:12px; font-size:1.05em; font-weight:bold; color:{text_color};">
            {status_icon} {status_label}
        </div>
        <div style="text-align:center; margin-top:6px; font-size:0.8em; color:#455a64;">
            측정: {time_str}
        </div>
        <div style="text-align:center; margin-top:2px; font-size:0.8em; font-weight:bold; color:{conn_color};">
            통신: {conn_text}
        </div>
        <div style="text-align:center; margin-top:6px; color:#90a4ae; font-size:0.65em;">{sn}</div>
    </div>
    </a>
    """

def _render_device_section(title, icon, description, active_sns, inactive_sns,
                           latest, statuses, now, link_suffix="", accent="#1a73e8",
                           render=None):
    """기기 종류별로 활성·비활성 카드를 한 구역에 묶어 표시.
    render 를 주면 그 함수로 활성 카드를 그린다 (FSR 처럼 표시 항목이 다른 기기용)."""
    render = render or _render_card
    active_cards = "\n".join(
        render(sn, analyzer.DEVICE_INFO[sn], latest.get(sn), statuses.get(sn), now, link_suffix)
        for sn in active_sns
    )
    if not active_cards:
        active_cards = (
            '<p style="grid-column:1/-1; text-align:center; color:#90a4ae; '
            'padding:28px 10px; margin:0;">최근 7일간 활성 기기가 없습니다.</p>'
        )

    inactive_html = ""
    if inactive_sns:
        inactive_cards = "\n".join(
            _render_inactive_card(sn, analyzer.DEVICE_INFO[sn], statuses.get(sn), now, link_suffix)
            for sn in inactive_sns
        )
        inactive_html = f"""
            <div style="margin-top:18px; color:#90a4ae; font-size:0.88em;">
                💤 비활성 기기 ({len(inactive_sns)}대)
                <span style="font-size:0.9em; color:#b0bec5;">— 7일 이상 통신 없음</span>
            </div>
            <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:10px; margin-top:10px;">
                {inactive_cards}
            </div>
        """

    total_count = len(active_sns) + len(inactive_sns)
    return f"""
        <section style="margin-top:26px; padding:20px; background:#ffffff; border-radius:16px; border-top:5px solid {accent}; box-shadow:0 2px 8px rgba(0,0,0,0.05);">
            <div style="display:flex; justify-content:space-between; align-items:flex-end; flex-wrap:wrap; gap:8px; margin-bottom:15px;">
                <div>
                    <h2 style="margin:0; color:#263238; font-size:1.35em;">{icon} {title}</h2>
                    <div style="margin-top:5px; color:#78909c; font-size:0.85em;">{description}</div>
                </div>
                <div style="color:#90a4ae; font-size:0.85em;">총 {total_count}대 · 활성 {len(active_sns)}대</div>
            </div>
            <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); gap:15px;">
                {active_cards}
            </div>
            {inactive_html}
        </section>
    """

def _build_cards_payload(token="", sn_filter=None, view_token=""):
    """대시보드 카드 영역 + 헤더 요약을 HTML 조각으로 빌드.
    /dashboard·/api/cards (전체) 와 /view·/api/view/cards (그룹 필터) 양쪽에서 재사용.
    sn_filter (list of SNs) 주면 그 기기들만 포함.
    view_token 주면 카드 링크에 ?view= 를 붙여 그룹 컨텍스트를 명시한다
    (그래야 admin이 그룹 대시보드에서 누른 기기의 '뒤로가기'가 /view 로 돌아감)."""
    link_suffix = f"?view={view_token}" if view_token else ""
    now = datetime.now(KST)   # 서버 시간대와 무관하게 KST 기준으로 비교
    now_ts = now.timestamp()

    latest = {}
    if _has_data():
        try:
            latest = analyzer.get_latest_states(DATA_FILES)
        except Exception:
            latest = {}

    statuses = analyzer.get_device_statuses()

    sn_set = set(sn_filter) if sn_filter else None
    # 숨김 처리한 기기는 카드에서 뺀다 — 데이터·리포트·기기관리에는 그대로 남는다
    visible_sns = [sn for sn, i in analyzer.DEVICE_INFO.items()
                   if not i.get("hidden") and (sn_set is None or sn in sn_set)]

    active_sns, inactive_sns = [], []
    connected_count = 0
    for sn in visible_sns:
        ds = statuses.get(sn)
        if _is_active(ds, now_ts):
            active_sns.append(sn)
            if ds and ds.get("connected"):
                connected_count += 1
        else:
            inactive_sns.append(sn)

    active_sns.sort(key=lambda s: _card_sort_key(s, latest.get(s), statuses.get(s), now))
    inactive_sns.sort()

    # 기기 종류 분류 — FSR 을 먼저 걸러낸다. deviceId 가 12자리 16진수면
    # _is_radar_device 가 레이더로 오인할 수 있어서 순서가 중요하다.
    def _kind(sn):
        state, ds = latest.get(sn), statuses.get(sn)
        if _is_fsr_device(sn, state, ds):
            return "fsr"
        return "radar" if _is_radar_device(sn, state, ds) else "emfit"

    emfit_active = [sn for sn in active_sns if _kind(sn) == "emfit"]
    radar_active = [sn for sn in active_sns if _kind(sn) == "radar"]
    fsr_active = [sn for sn in active_sns if _kind(sn) == "fsr"]
    emfit_inactive = [sn for sn in inactive_sns if _kind(sn) == "emfit"]
    radar_inactive = [sn for sn in inactive_sns if _kind(sn) == "radar"]
    fsr_inactive = [sn for sn in inactive_sns if _kind(sn) == "fsr"]

    emfit_section = ""
    if sn_filter is None or emfit_active or emfit_inactive:
        emfit_section = _render_device_section(
            "EMFIT QS", "❤️", "심박 · 호흡 · 움직임 등 생체정보 중심",
            emfit_active, emfit_inactive, latest, statuses, now, link_suffix, "#1a73e8",
        )

    radar_section = ""
    if sn_filter is None or radar_active or radar_inactive:
        radar_section = _render_device_section(
            "AI Radar", "📡", "누움 · 앉음 · 걸터앉음 · 자리비움 · 낙상 등 자세정보 중심",
            radar_active, radar_inactive, latest, statuses, now, link_suffix, "#7e57c2",
        )

    fsr_section = ""
    if fsr_active or fsr_inactive:
        fsr_section = _render_device_section(
            "돌봄기기 사용 감지", "🔘", "압력 센서 · 사용 중/미사용 · 배터리 잔량",
            fsr_active, fsr_inactive, latest, statuses, now, link_suffix, "#00897b",
            render=_render_fsr_card,
        )

    total = len(visible_sns)
    emfit_count = len(emfit_active) + len(emfit_inactive)
    radar_count = len(radar_active) + len(radar_inactive)
    fsr_count = len(fsr_active) + len(fsr_inactive)
    summary_parts = [f'<b style="color:#2e7d32;">{connected_count}</b> / {total} 연결됨']
    if sn_filter is None or emfit_count:
        summary_parts.append(f'<span style="color:#546e7a;">EMFIT {emfit_count}대</span>')
    if sn_filter is None or radar_count:
        summary_parts.append(f'<span style="color:#6a1b9a;">Radar {radar_count}대</span>')
    if fsr_count:
        summary_parts.append(f'<span style="color:#00695c;">사용감지 {fsr_count}대</span>')
    summary_html = ' · '.join(summary_parts)

    return {
        "emfit_section": emfit_section,
        "radar_section": radar_section,
        "fsr_section": fsr_section,
        "summary": summary_html,
        "now": now.strftime('%Y-%m-%d %H:%M:%S'),
    }


# ══════════════════════════════════════════════════════════════════════════
#  대시보드 V2 (신규 디자인) — 기존 /dashboard·/view 는 그대로 두고 별도 주소로.
#  기존 상태 판정·데이터 로직은 재사용하고, 화면(HTML/CSS)만 새로 그린다.
#  A시설는 기존 /view 를 계속 쓰므로 영향 없음.
# ══════════════════════════════════════════════════════════════════════════

# 자세 라벨 → (SVG 심볼 id, 위험도 색 변수)
_V2_POSTURE = {
    "누움": ("s-lie", "--p-lie"), "오래누움": ("s-longlie", "--p-lie"),
    "뒤척임": ("s-turn", "--p-turn"), "앉음": ("s-sit", "--p-sit"),
    "배회": ("s-walk", "--p-wander"), "걸터앉음": ("s-edge", "--p-edge"),
    "낙상": ("s-fall", "--p-fall"), "자리비움": ("s-absent", "--p-absent"),
    "감지 대기": ("s-absent", "--p-absent"), "재실": ("s-lie", "--p-lie"),
}


def _v2_tint(status_label):
    """상태 라벨 → 카드 틴트 class."""
    return {
        "재실": "st-live", "부재": "st-absent", "자리비움": "st-absent",
        "감지 대기": "st-absent", "수신 지연": "st-off", "끊김": "st-off",
        "낙상": "st-danger",
    }.get(status_label, "st-live")


def _render_fsr_card_v2(sn, info, state, ds, now, link_suffix=""):
    """신규 디자인 사용감지 카드 — 생체 셀 대신 사용 상태 + 배터리."""
    loc = html.escape(str(info['location'] if info['location'] and info['location'] != '-' else '미지정'))
    name = html.escape(str(info['name']))
    now_ts = now.timestamp()
    last_seen = ds.get("last_seen_ts") if isinstance(ds, dict) else None
    last_txt = (f"마지막 신호 {_format_ago(max(0, int(now_ts - last_seen)))}"
                if isinstance(last_seen, (int, float)) else "신호 없음")

    if state is None:
        return f"""
        <a href="/device/{sn}{link_suffix}" class="v2card st-inact">
          <div class="v2-top"><div><div class="v2-loc">{loc}</div><div class="v2-who">{name}</div></div>
            <span class="v2-tag"><svg><use href="#i-clock"/></svg>데이터 없음</span></div>
          <div class="v2-empty">수신 기록 없음</div>
          <div class="v2-foot"><span>{sn}</span><span class="conn off">{last_txt}</span></div>
        </a>"""

    status_label, _icon, _bg, _bd, _fg, reason = _fsr_status(state, ds, now_ts)
    tint = {"사용 중": "st-live", "미사용": "st-absent",
            "센서 확인 필요": "st-danger"}.get(status_label, "st-live")
    tag_icon = {"사용 중": "i-act", "미사용": "i-bed",
                "센서 확인 필요": "i-alert"}.get(status_label, "i-pulse")
    hero_color = {"사용 중": "#00897b", "미사용": "#90a4ae",
                  "센서 확인 필요": "#e57373"}.get(status_label, "#00897b")
    hero_sub = html.escape(reason) if reason else "돌봄기기 사용 감지"

    pct = state.get("배터리(%)")
    batt_cell = ""
    if isinstance(pct, (int, float)):
        muted = " muted" if int(pct) > FSR_BATT_WARN else ""
        batt_cell = (f'<div class="v2-vital v-temp{muted}"><div class="vic"><svg class="pic"><use href="#i-cog"/></svg></div>'
                     f'<div class="vnum num">{int(pct)}</div><div class="vunit">배터리(%)</div></div>')

    return f"""
    <a href="/device/{sn}{link_suffix}" class="v2card {tint}">
      <div class="v2-top"><div><div class="v2-loc">{loc}</div><div class="v2-who">{name}</div></div>
        <span class="v2-tag"><svg class="pic"><use href="#{tag_icon}"/></svg>{html.escape(status_label)}</span></div>
      <div class="v2-hero"><div class="v2-big" style="background:{hero_color}"><svg class="pic"><use href="#i-act"/></svg></div>
        <div><div class="v2-hlabel">{html.escape(status_label)}</div><div class="v2-hsub">{hero_sub}</div></div></div>
      <div class="v2-vitals">{batt_cell}</div>
      <div class="v2-foot"><span>{last_txt}</span><span>{sn}</span></div>
    </a>"""


def _render_card_v2(sn, info, state, ds, now, link_suffix=""):
    """신규 디자인 카드 1개. 상태 판정은 기존 _render_card 와 동일 규칙."""
    if _is_fsr_device(sn, state, ds):
        return _render_fsr_card_v2(sn, info, state, ds, now, link_suffix)
    is_radar = _is_radar_device(sn, state, ds)
    loc = html.escape(str(info['location'] if info['location'] and info['location'] != '-' else '미지정'))
    name = html.escape(str(info['name']))

    # 연결 상태
    if ds is None:
        connected = None
    elif ds.get("connected"):
        connected = True
    else:
        connected = False
    seen_ago = None
    if ds and isinstance(ds.get("last_seen_ts"), (int, float)):
        seen_ago = _format_ago(max(0, int(now.timestamp() - ds["last_seen_ts"])))
    conn_ok = connected is True
    conn_txt = ("연결됨" + (f" ({seen_ago})" if seen_ago else "")) if conn_ok \
        else ("끊김" + (f" ({seen_ago})" if seen_ago else "")) if connected is False else "상태 없음"

    # 측정 데이터 없음
    if state is None:
        return f"""
        <a href="/device/{sn}{link_suffix}" class="v2card {'st-off' if connected is False else 'st-inact'}">
          <div class="v2-top"><div><div class="v2-loc">{loc}</div><div class="v2-who">{name}</div></div>
            <span class="v2-tag"><svg><use href="#i-clock"/></svg>데이터 없음</span></div>
          <div class="v2-empty">측정 데이터 없음</div>
          <div class="v2-foot"><span>{sn}</span><span class="conn {'ok' if conn_ok else 'off'}">{conn_txt}</span></div>
        </a>"""

    # 측정 시각
    try:
        last_dt = _state_dt(state)
        delta_sec = max(0, int((now - last_dt).total_seconds()))
        mins_ago = delta_sec // 60
        tp = last_dt.strftime("%m-%d %H:%M") if delta_sec >= 3600 else last_dt.strftime("%H:%M")
        time_str = f"{tp} ({_format_ago(delta_sec)})"
    except Exception:
        mins_ago, time_str = 10 ** 9, "?"

    hr, rr = state.get("심박수(HR)"), state.get("호흡수(RR)")
    act = state.get("활동량(ACT)")
    act_num = act if isinstance(act, (int, float)) else None

    # 상태 판정 (기존 규칙 그대로)
    if is_radar:
        pos = state.get("자세(POS)")
        posture = str(state.get("자세") or "-")
        if connected is False:
            status = "끊김"
        elif mins_ago > 10:
            status = "수신 지연"
        elif state.get("낙상") or pos == 4:
            status = "낙상"
        elif pos == 5:
            status = "자리비움"
        elif pos == -1:
            status = "감지 대기"
        else:
            status = "재실"
    else:
        absent = act_num is not None and act_num < 1
        if connected is False:
            status = "끊김"
        elif mins_ago > 10 or absent:
            status = "부재"
        else:
            status = "재실"

    tint = _v2_tint(status)
    show_vitals = status == "재실"
    hr_s = f"{hr:.0f}" if (show_vitals and isinstance(hr, (int, float))) else "–"
    rr_s = f"{rr:.0f}" if (show_vitals and isinstance(rr, (int, float))) else "–"
    act_s = f"{act_num:.0f}" if (show_vitals and act_num is not None) else "–"
    mut = "" if show_vitals else " muted"

    # 상태 태그 아이콘
    tag_icon = {"낙상": "i-alert", "끊김": "i-wifi-off", "수신 지연": "i-clock",
                "부재": "i-bed", "자리비움": "s-absent", "감지 대기": "i-radar",
                "재실": "i-pulse"}.get(status, "i-pulse")

    # 레이더는 자세 픽토그램 히어로
    hero = ""
    if is_radar:
        sym, color = _V2_POSTURE.get(posture if status not in ("낙상",) else "낙상",
                                     ("s-lie", "--p-lie"))
        if status == "낙상":
            sym, color = "s-fall", "--p-fall"
        disp = "낙상 감지" if status == "낙상" else posture
        sub = (f'<span class="v2-fall"><svg><use href="#i-alert"/></svg>낙상</span>'
               if status == "낙상" else f'{status}')
        hero = f"""<div class="v2-hero"><div class="v2-big" style="background:var({color})"><svg class="pic"><use href="#{sym}"/></svg></div>
          <div><div class="v2-hlabel">{disp}</div><div class="v2-hsub">{sub}</div></div></div>"""

    # 생체 셀 (레이더는 심박·호흡만, Emfit·McKare는 움직임/체온까지)
    temp = state.get("체온")
    vit_cells = f"""
      <div class="v2-vital v-hr{mut}"><div class="vic"><svg class="pic"><use href="#i-heart"/></svg></div><div class="vnum num">{hr_s}</div><div class="vunit">심박(HR)</div></div>
      <div class="v2-vital v-rr{mut}"><div class="vic"><svg class="pic"><use href="#i-lung"/></svg></div><div class="vnum num">{rr_s}</div><div class="vunit">호흡(RR)</div></div>"""
    if not is_radar:
        vit_cells += f"""
      <div class="v2-vital v-act{mut}"><div class="vic"><svg class="pic"><use href="#i-act"/></svg></div><div class="vnum num">{act_s}</div><div class="vunit">움직임(ACT)</div></div>"""
    if isinstance(temp, (int, float)) and show_vitals:
        vit_cells += f"""
      <div class="v2-vital v-temp"><div class="vic"><svg class="pic"><use href="#i-temp"/></svg></div><div class="vnum num">{temp:.1f}</div><div class="vunit">체온(℃)</div></div>"""

    return f"""
    <a href="/device/{sn}{link_suffix}" class="v2card {tint}">
      <div class="v2-top"><div><div class="v2-loc">{loc}</div><div class="v2-who">{name}</div></div>
        <span class="v2-tag"><svg class="pic"><use href="#{tag_icon}"/></svg>{status}</span></div>
      {hero}
      <div class="v2-vitals">{vit_cells}</div>
      <div class="v2-foot"><span>측정 {time_str}</span><span class="conn {'ok' if conn_ok else 'off'}">{conn_txt}</span></div>
    </a>"""


def _render_inactive_card_v2(sn, info, ds, now, link_suffix=""):
    """비활성(7일+) 미니 카드."""
    loc = html.escape(str(info['location'] if info['location'] and info['location'] != '-' else '미지정'))
    name = html.escape(str(info['name']))
    last = "통신 이력 없음"
    if ds and isinstance(ds.get("last_seen_ts"), (int, float)):
        last = f"마지막 통신 {_format_ago(max(0, int(now.timestamp() - ds['last_seen_ts'])))}"
    return f"""<a href="/device/{sn}{link_suffix}" class="v2card st-inact v2-mini">
      <div class="v2-top"><div><div class="v2-loc">{loc}</div><div class="v2-who">{name}</div></div>
        <span class="v2-tag"><svg><use href="#i-clock"/></svg>비활성</span></div>
      <div class="v2-empty">측정 데이터 없음</div>
      <div class="v2-foot"><span>{last}</span><span>{sn}</span></div></a>"""


def _v2_kind(sn, state, ds):
    if isinstance(state, dict) and state.get("유형") == "McKare":
        return "mckare"
    if isinstance(ds, dict) and ds.get("source") == "mckare":
        return "mckare"
    # FSR 을 레이더보다 먼저 — deviceId 가 12자리 16진수면 레이더로 오인될 수 있다.
    if _is_fsr_device(sn, state, ds):
        return "fsr"
    if _is_radar_device(sn, state, ds):
        return "radar"
    return "emfit"


_V2_SECTIONS = [
    ("emfit", "EMFIT QS", "침대 매트 · 심박 · 호흡 · 움직임", "#e05575", "i-heart"),
    ("radar", "AI Radar", "침대 위 레이더 · 자세 · 낙상 감지", "#7c5cd6", "i-radar"),
    ("mckare", "McKare", "천장·벽 레이더 · 구간 재실 · 체온", "#1fa39c", "i-temp"),
    ("fsr", "돌봄기기 사용 감지", "압력 센서 · 사용 중/미사용 · 배터리 잔량", "#00897b", "i-act"),
]


def _build_cards_payload_v2(sn_filter=None, view_token=""):
    """신규 대시보드 카드 섹션 HTML + 요약."""
    link_suffix = f"?view={view_token}" if view_token else ""
    now = datetime.now(KST)   # 서버 시간대와 무관하게 KST 기준으로 비교
    now_ts = now.timestamp()
    latest = {}
    if _has_data():
        try:
            latest = analyzer.get_latest_states(DATA_FILES)
        except Exception:
            latest = {}
    statuses = analyzer.get_device_statuses()
    sn_set = set(sn_filter) if sn_filter else None
    visible = [sn for sn, i in analyzer.DEVICE_INFO.items()
               if not i.get("hidden") and (sn_set is None or sn in sn_set)]

    active, inactive, connected_count = [], [], 0
    for sn in visible:
        ds = statuses.get(sn)
        if _is_active(ds, now_ts):
            active.append(sn)
            if ds and ds.get("connected"):
                connected_count += 1
        else:
            inactive.append(sn)
    active.sort(key=lambda s: _card_sort_key(s, latest.get(s), statuses.get(s), now))
    inactive.sort()

    groups = {key: ([], []) for key, *_ in _V2_SECTIONS}
    for sn in active:
        groups[_v2_kind(sn, latest.get(sn), statuses.get(sn))][0].append(sn)
    for sn in inactive:
        groups[_v2_kind(sn, latest.get(sn), statuses.get(sn))][1].append(sn)

    sections_html = ""
    for key, title, sub, accent, icon in _V2_SECTIONS:
        act_l, inact_l = groups[key]
        if sn_filter is not None and not act_l and not inact_l:
            continue
        if not act_l and not inact_l:
            continue
        render_fn = _render_fsr_card_v2 if key == "fsr" else _render_card_v2
        cards = "\n".join(
            render_fn(sn, analyzer.DEVICE_INFO[sn], latest.get(sn), statuses.get(sn), now, link_suffix)
            for sn in act_l) or '<p class="v2-none">활성 기기가 없습니다.</p>'
        inact_html = ""
        if inact_l:
            ic = "\n".join(
                _render_inactive_card_v2(sn, analyzer.DEVICE_INFO[sn], statuses.get(sn), now, link_suffix)
                for sn in inact_l)
            inact_html = (f'<div class="v2-inact-h">비활성 {len(inact_l)}대 · 7일 이상 통신 없음</div>'
                          f'<div class="v2-grid mini">{ic}</div>')
        sections_html += f"""
        <section class="v2-sec">
          <div class="v2-sechead"><div class="v2-badge" style="background:{accent}"><svg class="pic"><use href="#{icon}"/></svg></div>
            <div><h2>{title}</h2><p>{sub}</p></div>
            <div class="v2-count">총 {len(act_l)+len(inact_l)}대 · 활성 {len(act_l)}</div></div>
          <div class="v2-grid">{cards}</div>{inact_html}
        </section>"""

    total = len(visible)
    summary = f'<b style="color:var(--ok)">{connected_count}</b> / {total} 연결됨'
    return {"sections": sections_html, "summary": summary,
            "now": now.strftime('%Y-%m-%d %H:%M:%S')}


# ── V2 정적 자원: CSS + SVG 심볼 (문자열 상수, f-string 아님) ──
_V2_STYLE = """
:root{--bg:#eef1f6;--surface:#fff;--surface-2:#f5f8fc;--border:#e3e8f1;--ink:#1b2434;--ink-2:#59637a;--ink-3:#8b94a8;
--accent:#3b5bdb;--accent-soft:#eaeefb;--ok:#1f9d6b;--off:#79839a;
--hr:#e0556f;--rr:#3b82d6;--act:#e08a3c;--temp:#e2703f;
--p-lie:#3f7fd6;--p-turn:#3fa88f;--p-sit:#e2b52f;--p-wander:#e8933c;--p-edge:#df6b3b;--p-fall:#d6455a;--p-absent:#8b98ad;
--st-live-bg:#e9f1fd;--st-live-bd:#bcd6f7;--st-live-fg:#1a5fd0;
--st-absent-bg:#f1ebe2;--st-absent-bd:#dccdb7;--st-absent-fg:#8a6d43;
--st-off-bg:#fdeaee;--st-off-bd:#f3bac5;--st-off-fg:#c23a52;
--st-inact-bg:#fdf5dd;--st-inact-bd:#eedd9c;--st-inact-fg:#a2841f;
--st-danger-bg:#fbdce1;--st-danger-bd:#ec8998;--st-danger-fg:#c62f45;
--r:14px;--font:'Pretendard',-apple-system,BlinkMacSystemFont,'Malgun Gothic','Apple SD Gothic Neo',sans-serif;
--shadow:0 1px 2px rgba(20,30,50,.05),0 6px 20px rgba(20,30,50,.07);}
:root[data-theme="dark"]{--bg:#0e1218;--surface:#161c26;--surface-2:#1c2430;--border:#2a3341;--ink:#e9edf4;--ink-2:#a6b0c2;--ink-3:#6e7889;
--accent:#6d84f0;--accent-soft:#1e2740;--ok:#3ac088;--off:#8792a4;
--hr:#f0728a;--rr:#5c9be8;--act:#eaa05a;--temp:#ef8a5f;
--p-lie:#5a93e6;--p-turn:#4fbca3;--p-sit:#eac04a;--p-wander:#f0a453;--p-edge:#ec8256;--p-fall:#ec6274;--p-absent:#7d879a;
--st-live-bg:#15233b;--st-live-bd:#2f4a72;--st-live-fg:#7ba6f0;
--st-absent-bg:#25211a;--st-absent-bd:#463d2c;--st-absent-fg:#c9a978;
--st-off-bg:#2c1620;--st-off-bd:#5a2e3a;--st-off-fg:#ef8798;
--st-inact-bg:#272108;--st-inact-bd:#4a3f16;--st-inact-fg:#dcc067;
--st-danger-bg:#331419;--st-danger-bd:#6e2e39;--st-danger-fg:#ef8798;
--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.3);}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
svg{display:block}.pic{fill:currentColor}.num{font-variant-numeric:tabular-nums}
.v2app{display:grid;grid-template-columns:232px 1fr;min-height:100vh}
.v2side{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.v2brand{padding:20px 18px;display:flex;gap:11px;align-items:center;border-bottom:1px solid var(--border)}
.v2brand .mk{width:36px;height:36px;border-radius:9px;flex:none;background:linear-gradient(140deg,var(--accent),var(--p-turn));color:#fff;display:grid;place-items:center}
.v2brand .mk svg{width:21px;height:21px}
.v2brand b{font-size:14px;font-weight:700;letter-spacing:-.2px;line-height:1.3}.v2brand span{display:block;font-size:11px;color:var(--ink-3);font-weight:500}
.v2nav{padding:10px;display:flex;flex-direction:column;gap:2px}
.v2nl{font-size:10.5px;font-weight:700;letter-spacing:.09em;color:var(--ink-3);padding:14px 10px 6px;text-transform:uppercase}
.v2ni{display:flex;align-items:center;gap:11px;padding:10px 11px;border-radius:9px;color:var(--ink-2);font-weight:500;font-size:13.5px;cursor:pointer;text-decoration:none}
.v2ni svg{width:19px;height:19px;color:var(--ink-3)}
.v2ni:hover{background:var(--surface-2);color:var(--ink)}
.v2ni.on{background:var(--accent-soft);color:var(--accent);font-weight:600}.v2ni.on svg{color:var(--accent)}
.v2foot{margin-top:auto;padding:14px 18px;border-top:1px solid var(--border);font-size:11px;color:var(--ink-3);display:flex;justify-content:space-between}
.v2main{min-width:0}
.v2top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--border);padding:16px 26px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.v2top h1{margin:0;font-size:17px;font-weight:700;letter-spacing:-.3px}.v2top .sub{color:var(--ink-3);font-size:12px;margin-top:2px}
.v2sp{flex:1}.v2chip{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;border-radius:999px;background:var(--surface);border:1px solid var(--border);font-size:12.5px;font-weight:600}
.v2clock{font-variant-numeric:tabular-nums;color:var(--ink-2);font-size:12.5px;font-weight:600}
.v2wrap{padding:22px 26px 60px;max-width:1240px}
.v2leg{display:flex;gap:8px 16px;flex-wrap:wrap;align-items:center;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;margin-bottom:22px;font-size:12.5px}
.v2leg .t{font-weight:700;color:var(--ink)}.v2leg .l{display:flex;align-items:center;gap:7px;font-weight:600;color:var(--ink-2)}
.v2leg .cs{width:22px;height:14px;border-radius:4px;border:1px solid var(--border);flex:none}
.v2sec{margin-bottom:30px}
.v2sechead{display:flex;align-items:center;gap:11px;margin:0 2px 13px}
.v2-badge{width:32px;height:32px;border-radius:8px;display:grid;place-items:center;flex:none;color:#fff}.v2-badge svg{width:19px;height:19px}
.v2sechead h2{margin:0;font-size:15px;font-weight:700}.v2sechead p{margin:1px 0 0;font-size:12px;color:var(--ink-3)}
.v2count{margin-left:auto;font-size:12px;color:var(--ink-3);font-weight:600}
.v2-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(266px,1fr));gap:14px}
.v2-grid.mini{grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;margin-top:12px}
.v2-inact-h{margin-top:18px;font-size:12.5px;color:var(--ink-3);font-weight:600}
.v2card{display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--shadow);padding:15px 16px 16px;transition:transform .12s}
.v2card:hover{transform:translateY(-2px)}
.v2card.st-live{background:var(--st-live-bg);border-color:var(--st-live-bd)}
.v2card.st-absent{background:var(--st-absent-bg);border-color:var(--st-absent-bd)}
.v2card.st-off{background:var(--st-off-bg);border-color:var(--st-off-bd)}
.v2card.st-inact{background:var(--st-inact-bg);border-color:var(--st-inact-bd)}
.v2card.st-danger{background:var(--st-danger-bg);border-color:var(--st-danger-bd);box-shadow:0 0 0 1.5px var(--st-danger-bd),0 8px 26px rgba(214,69,90,.22)}
.v2-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.v2-loc{font-size:11.5px;color:var(--ink-3);font-weight:600}.v2-who{font-size:16px;font-weight:700;letter-spacing:-.3px;margin-top:1px}
.v2-tag{display:inline-flex;align-items:center;gap:5px;padding:5px 10px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}
.v2-tag svg{width:15px;height:15px}
.st-live .v2-tag{background:color-mix(in srgb,var(--st-live-fg) 15%,transparent);color:var(--st-live-fg)}
.st-absent .v2-tag{background:color-mix(in srgb,var(--st-absent-fg) 16%,transparent);color:var(--st-absent-fg)}
.st-off .v2-tag{background:color-mix(in srgb,var(--st-off-fg) 14%,transparent);color:var(--st-off-fg)}
.st-inact .v2-tag{background:color-mix(in srgb,var(--st-inact-fg) 18%,transparent);color:var(--st-inact-fg)}
.st-danger .v2-tag{background:var(--st-danger-fg);color:#fff}
.v2-empty{text-align:center;color:var(--ink-3);font-weight:600;padding:22px 0 16px}
.v2-hero{display:flex;align-items:center;gap:14px;margin:14px 0 4px;padding:13px 14px;background:color-mix(in srgb,var(--surface) 55%,transparent);border-radius:12px}
.v2-big{width:58px;height:58px;flex:none;display:grid;place-items:center;border-radius:13px;color:#fff}.v2-big svg{width:38px;height:38px}
.v2-hlabel{font-size:19px;font-weight:700;letter-spacing:-.4px}.v2-hsub{font-size:12px;color:var(--ink-3);margin-top:3px;display:flex;align-items:center;gap:6px}
.v2-fall{display:inline-flex;align-items:center;gap:4px;background:var(--st-danger-bg);color:var(--st-danger-fg);font-weight:700;font-size:11px;padding:2px 7px;border-radius:999px}.v2-fall svg{width:12px;height:12px}
.v2-vitals{display:flex;margin-top:13px;border:1px solid color-mix(in srgb,var(--ink) 8%,transparent);border-radius:11px;overflow:hidden;background:color-mix(in srgb,var(--surface) 62%,transparent)}
.v2-vital{flex:1;padding:11px 4px;text-align:center;border-right:1px solid color-mix(in srgb,var(--ink) 8%,transparent)}.v2-vital:last-child{border-right:0}
.v2-vital .vic{display:flex;justify-content:center;margin-bottom:4px}.v2-vital .vic svg{width:19px;height:19px}
.v2-vital .vnum{font-size:22px;font-weight:700;letter-spacing:-.5px;line-height:1}.v2-vital .vunit{font-size:10.5px;color:var(--ink-3);font-weight:600;margin-top:3px}
.v-hr .vic,.v-hr .vnum{color:var(--hr)}.v-rr .vic,.v-rr .vnum{color:var(--rr)}.v-act .vic,.v-act .vnum{color:var(--act)}.v-temp .vic,.v-temp .vnum{color:var(--temp)}
.v2-vital.muted .vic,.v2-vital.muted .vnum{color:var(--ink-3)!important}
.v2-foot{display:flex;justify-content:space-between;align-items:center;margin-top:13px;font-size:11.5px;color:var(--ink-3)}
.v2-foot .conn{font-weight:600}.conn.ok{color:var(--ok)}.conn.off{color:var(--off)}
.v2-none{grid-column:1/-1;text-align:center;color:var(--ink-3);padding:30px}
@media(max-width:820px){.v2app{grid-template-columns:1fr}.v2side{position:fixed;left:-232px;z-index:20}.v2wrap,.v2top{padding-left:16px;padding-right:16px}}
"""

_V2_DEFS = """
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-heart" viewBox="0 0 24 24"><path d="M12 20.7c-.35 0-.7-.13-.97-.4C6.5 16.05 3.4 13.25 3.4 9.7 3.4 7.05 5.4 5.05 7.9 5.05c1.45 0 2.83.68 3.73 1.82.14.18.42.18.56 0C13.1 5.73 14.47 5.05 15.9 5.05c2.5 0 4.5 2 4.5 4.65 0 3.55-3.1 6.35-7.63 10.6-.27.27-.62.4-.97.4z"/></symbol>
<symbol id="i-lung" viewBox="0 0 24 24"><path d="M11 4a1 1 0 0 1 2 0v6.2l2.5-.9c1.55-.56 3.2.52 3.35 2.15l.45 5.2c.18 1.9-1.45 3.45-3.32 3.12-1.4-.24-2.42-1.36-2.6-2.75L13 13.4a2 2 0 0 0-2 0l-.38 3.62c-.18 1.4-1.2 2.5-2.6 2.75-1.87.33-3.5-1.22-3.32-3.12l.45-5.2c.15-1.63 1.8-2.7 3.35-2.15L11 10.2z"/></symbol>
<symbol id="i-act" viewBox="0 0 24 24"><rect x="3.4" y="13.2" width="3.5" height="7.4" rx="1.3"/><rect x="10.25" y="8.6" width="3.5" height="12" rx="1.3"/><rect x="17.1" y="4.4" width="3.5" height="16.2" rx="1.3"/></symbol>
<symbol id="i-temp" viewBox="0 0 24 24"><path d="M12 2.6a3.1 3.1 0 0 0-3.1 3.1v7.03a4.6 4.6 0 1 0 6.2 0V5.7A3.1 3.1 0 0 0 12 2.6zm0 2a1.1 1.1 0 0 1 1.1 1.1v7.9l.5.4a2.6 2.6 0 1 1-3.2 0l.5-.4v-7.9A1.1 1.1 0 0 1 12 4.6z"/><circle cx="12" cy="16.6" r="1.7"/><rect x="11.2" y="8" width="1.6" height="8" rx=".8"/></symbol>
<symbol id="i-bed" viewBox="0 0 24 24"><circle cx="6.6" cy="9.3" r="2.1"/><path d="M10 12.6A2.6 2.6 0 0 1 12.6 10H20a1 1 0 0 1 1 1v3.2H10z"/><path d="M3 9.6a1 1 0 0 1 2 0v4.6H3z"/><rect x="2.6" y="15.6" width="18.8" height="2.1" rx="1"/></symbol>
<symbol id="i-radar" viewBox="0 0 24 24"><path d="M12 12L5 5" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M4 12a8 8 0 1 1 8 8" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M7.5 12a4.5 4.5 0 1 1 4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="12" r="1.1"/></symbol>
<symbol id="i-wifi" viewBox="0 0 24 24"><path d="M4.5 11a11 11 0 0 1 15 0M7.8 14.3a6.3 6.3 0 0 1 8.4 0M11 17.6a1.5 1.5 0 0 1 2 0" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></symbol>
<symbol id="i-wifi-off" viewBox="0 0 24 24"><path d="M4.5 11a11 11 0 0 1 12-2.2M16.2 14.3a6.3 6.3 0 0 0-4.2-1.8M11 17.6a1.5 1.5 0 0 1 2 0M3 3l18 18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="M12 4.5 2.8 20h18.4z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12 10v4.3M12 17.4v.01" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M12 7v5l3 2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></symbol>
<symbol id="i-pulse" viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 10 10M12 6v6l4 2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></symbol>
<symbol id="i-grid" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/></g></symbol>
<symbol id="i-place" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M12 21s7-5.5 7-11a7 7 0 1 0-14 0c0 5.5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/></g></symbol>
<symbol id="i-cog" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M22 12h-3M5 12H2M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7 5.6 5.6"/></g></symbol>
<symbol id="i-report" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6zM14 3v4h4M9 13h6M9 17h6M9 9h2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></symbol>
<symbol id="i-book" viewBox="0 0 24 24"><path d="M5 4h9a2 2 0 0 1 2 2v14a2 2 0 0 0-2-2H5zM19 4h-1a2 2 0 0 0-2 2v14a2 2 0 0 1 2-2h1z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></symbol>
<symbol id="s-absent" viewBox="0 0 24 24"><path d="M19.775 22.625L17.15 20H4v-2.8q0-.85.438-1.562T5.6 14.55q1.125-.575 2.288-.925t2.362-.525L1.375 4.225L2.8 2.8l18.4 18.4zM18.4 14.55q.725.35 1.15 1.062T20 17.15l-3.35-3.35q.45.175.888.35t.862.4m-4.2-3.2L8.65 5.8q.575-.85 1.45-1.325T12 4q1.65 0 2.825 1.175T16 8q0 1.025-.475 1.9T14.2 11.35"/></symbol>
<symbol id="s-lie" viewBox="0 0 24 24"><path d="M9 14V7h9q1.65 0 2.825 1.175T22 11v3zm-7 3v-2h20v2zm.875-3.875Q2 12.25 2 11t.875-2.125T5 8t2.125.875T8 11t-.875 2.125T5 14t-2.125-.875"/></symbol>
<symbol id="s-longlie" viewBox="0 0 24 24"><path d="M9 14V7h9q1.65 0 2.825 1.175T22 11v3zm-7 3v-2h20v2zm.875-3.875Q2 12.25 2 11t.875-2.125T5 8t2.125.875T8 11t-.875 2.125T5 14t-2.125-.875"/><text x="5.2" y="5.6" font-family="Arial,sans-serif" font-weight="900" font-size="5" fill="currentColor">z</text><text x="8.8" y="3.1" font-family="Arial,sans-serif" font-weight="900" font-size="3.4" fill="currentColor">z</text></symbol>
<symbol id="s-sit" viewBox="0 0 24 24"><circle cx="7.7" cy="4.2" r="2.7"/><path d="M5 14.8V9q0-1.15 1.2-1.45h1.9l2.5 3.4q.5.65.5 1.5V14.8z"/><path d="M11.3 11.5h4.3A3.7 3.7 0 0 1 19.3 15.2H11.3z"/><rect x="2.4" y="15" width="17.2" height="2.4" rx="0.8"/><rect x="2.6" y="17.4" width="1.9" height="3.1" rx="0.6"/><rect x="17.5" y="17.4" width="1.9" height="2.6" rx="0.6"/></symbol>
<symbol id="s-edge" viewBox="0 0 15 15"><path d="M14.5 9c.28 0 .5.22.5.5V14h-2v-3h-2V9zm-8 0v2H2v3H0V9.5c0-.28.22-.5.5-.5zM6 5.75l-1 3H3.75l1.03-3.52c.15-.48.41-.92.76-1.27l.25-.25C6.25 3.25 6.86 3 7.5 3s1.25.25 1.71.71l.25.25c.35.35.61.79.76 1.27l1.03 3.52H11a1.5 1.5 0 0 0-1.25-1.38l-.22-.04L9 5.75v2l.66.11l.15.04l.1.03l.1.06l.08.05l.1.08c.01.02.02.03.04.04l.05.07l.07.09l.06.11l.03.07c.03.08.04.15.05.23l.01.07v3.45c0 .41-.34.75-.75.75S9 12.66 9 12.25V9h-.5v3.25c0 .41-.34.75-.75.75S7 12.66 7 12.25V9l-.12-.01a.8.8 0 0 1-.26-.07l-.14-.06l-.09-.07l-.08-.06l-.03-.04l-.05-.05l-.06-.08v-.01c-.05-.06-.08-.13-.11-.2l-.02-.07c-.01-.05-.03-.1-.03-.16L6 8.06zM7.5 2.5a1.25 1.25 0 1 0 0-2.5a1.25 1.25 0 0 0 0 2.5"/></symbol>
<symbol id="s-walk" viewBox="0 0 24 24"><path d="M13.5 5.5c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zM9.8 8.9L7 23h2.1l1.8-8 2.1 2v6h2v-7.5l-2.1-2 .6-3C14.8 12 16.8 13 19 13v-2c-1.9 0-3.5-1-4.3-2.4l-1-1.6c-.4-.6-1-1-1.7-1-.3 0-.5.1-.8.1L6 8.3V13h2V9.6l1.8-.7"/></symbol>
<symbol id="s-fall" viewBox="0 0 24 24"><path d="m13.5 22.5l-2-.4l.8-4.3l-3.6-2.7l-1.3-5.7l-2.2 1.9l.8 3.8l-2 .4l-1-4.9l4.45-3.975q.575-.5 1.363-.412t1.512.387q.8.35 1.663.5t1.737.025t1.613-.575t1.412-1L18 7.1q-.75.575-1.55 1.075t-1.725.775q-.825.225-1.662.238T11.4 9l.7 3.1l3.7-.7l5.2 3.7l-1.2 1.6l-4.3-3l-3.6.7l2.7 2zM6.588 4.913Q6 4.325 6 3.5t.588-1.412T8 1.5t1.413.588T10 3.5t-.587 1.413T8 5.5t-1.412-.587"/></symbol>
<symbol id="s-turn" viewBox="0 0 15 15"><path d="M1.83 8.16a1.83 1.83 0 0 1 0-3.66c1 0 1.82.82 1.82 1.83s-.82 1.83-1.82 1.83M15 10.5H6.5V5.21H12c1.66 0 3 1.34 3 3zM4.21 8.7V5.98c0-.5.4-.9.9-.9c.49 0 .9.4.9.9V9.6c0 .49-.41.9-.9.9H.94c-.5 0-.9-.41-.9-.9c0-.5.4-.9.9-.9z"/><rect x="0.4" y="11.6" width="14.2" height="1.5" rx="0.55"/></symbol>
</defs></svg>
"""


@app.get("/dashboard2", response_class=HTMLResponse)
def view_dashboard2(request: Request, _: str = Depends(require_admin)):
    """신규 디자인 대시보드 (미리보기). 기존 /dashboard 는 그대로."""
    p = _build_cards_payload_v2()
    return HTMLResponse(_v2_page(p))


@app.get("/api/cards2")
def api_cards2(request: Request, _: str = Depends(require_admin)):
    return _build_cards_payload_v2()


def _v2_page(p):
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<title>통합 관제 (신규) · 돌봄로봇 사업단</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_V2_STYLE}</style></head><body>{_V2_DEFS}
<div class="v2app">
  <aside class="v2side">
    <div class="v2brand"><div class="mk"><svg><use href="#i-radar"/></svg></div>
      <div><b>돌봄로봇 사업단</b><span>통합 관제 시스템</span></div></div>
    <nav class="v2nav">
      <div class="v2nl">모니터링</div>
      <a class="v2ni on"><svg><use href="#i-grid"/></svg>통합 현황</a>
      <a class="v2ni" href="/view"><svg><use href="#i-place"/></svg>장소별 보기</a>
      <div class="v2nl">관리</div>
      <a class="v2ni" href="/devices"><svg><use href="#i-cog"/></svg>장비 관리</a>
      <a class="v2ni" href="/admin/discord"><svg><use href="#i-cog"/></svg>디스코드 알림</a>
      <div class="v2nl">자료</div>
      <a class="v2ni" href="/reports"><svg><use href="#i-report"/></svg>리포트</a>
      <a class="v2ni" href="/help"><svg><use href="#i-book"/></svg>사용 가이드</a>
    </nav>
    <div class="v2foot"><span>v{VERSION} · 신규 미리보기</span></div>
  </aside>
  <main class="v2main">
    <div class="v2top">
      <div><h1>돌봄기기 통합 대시보드</h1><div class="sub">EMFIT QS · AI Radar · McKare · 사용감지 통합 관제</div></div>
      <div class="v2sp"></div>
      <span class="v2chip" id="v2sum">{p['summary']}</span>
      <span class="v2clock" id="v2clk">{p['now']}</span>
    </div>
    <div class="v2wrap">
      <div class="v2leg"><span class="t">카드 색 = 상태</span>
        <span class="l"><span class="cs" style="background:var(--st-live-bg);border-color:var(--st-live-bd)"></span>측정 중(재실)</span>
        <span class="l"><span class="cs" style="background:var(--st-absent-bg);border-color:var(--st-absent-bd)"></span>부재</span>
        <span class="l"><span class="cs" style="background:var(--st-off-bg);border-color:var(--st-off-bd)"></span>연결 끊김</span>
        <span class="l"><span class="cs" style="background:var(--st-inact-bg);border-color:var(--st-inact-bd)"></span>비활성(7일+)</span>
        <span class="l"><span class="cs" style="background:var(--st-danger-bg);border-color:var(--st-danger-fg)"></span>낙상(긴급)</span>
      </div>
      <div id="v2secs">{p['sections']}</div>
    </div>
  </main>
</div>
<script>
async function v2refresh(){{try{{const r=await fetch('/api/cards2');if(!r.ok)return;const d=await r.json();
document.getElementById('v2secs').innerHTML=d.sections;document.getElementById('v2sum').innerHTML=d.summary;
document.getElementById('v2clk').textContent=d.now;}}catch(e){{}}}}
setInterval(v2refresh,15000);document.addEventListener('visibilitychange',()=>{{if(!document.hidden)v2refresh();}});
</script></body></html>"""

# 관제 대시보드 (기기별 카드 그리드) — 관리자 전용
@app.get("/dashboard", response_class=HTMLResponse)
def view_dashboard(request: Request, _: str = Depends(require_admin)):
    admin_token = _get_token_from_request(request) or ""
    p = _build_cards_payload(admin_token)
    return f"""
    <html>
        <head>
            <title>통합 관제 화면</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: 'Malgun Gothic', sans-serif; padding:20px; background:#f0f2f5; margin:0; }}
                a {{ -webkit-tap-highlight-color: transparent; }}
                @media (max-width: 600px) {{
                    body {{ padding: 10px; }}
                    h1 {{ font-size: 1.3em !important; }}
                    .nav-buttons a {{
                        display:block !important; margin: 6px 0 !important;
                    }}
                }}
            </style>
        </head>
        <body>
            <div style="max-width:1400px; margin:auto;">
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                    <h1 style="color:#1a73e8; margin:0;">📡 통합 실시간 관제</h1>
                    <div style="color:#7f8c8d; font-size:0.9em;">
                        <span id="clock">{p['now']}</span> · <span id="header-summary">{p['summary']}</span>
                    </div>
                </div>
                <div id="emfit-section">
                    {p['emfit_section']}
                </div>
                <div id="radar-section">
                    {p['radar_section']}
                </div>
                <div id="fsr-section">
                    {p['fsr_section']}
                </div>

                <p class="nav-buttons" style="text-align:center; margin-top:30px;">
                    <a href="/reports" style="display:inline-block; padding:10px 20px; background:#1a73e8; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">📊 리포트 다운로드</a>
                    <a href="/devices" style="display:inline-block; padding:10px 20px; background:#8e44ad; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">⚙️ 기기 정보</a>
                    <a href="/admin/tokens" style="display:inline-block; padding:10px 20px; background:#16a085; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🔑 사용자 URL 관리</a>
                    <a href="/admin/discord" style="display:inline-block; padding:10px 20px; background:#5865F2; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🔔 디스코드 알림</a>
                    <a href="/feedback" style="display:inline-block; padding:10px 20px; background:#e67e22; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">💬 의견 보기</a>
                    <a href="/help" style="display:inline-block; padding:10px 20px; background:#27ae60; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">📘 사용 가이드</a>
                    <a href="/fsr-nodes" style="display:inline-block; padding:10px 20px; background:#d35400; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🛏️ FSR 노드 설정</a>
                    <a href="/fsr-tune" style="display:inline-block; padding:10px 20px; background:#c0392b; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🎚️ FSR 실시간 튜닝</a>
                    <a href="/dashboard/raw" style="display:inline-block; padding:10px 20px; background:#7f8c8d; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🔎 원본 데이터</a>
                    <a href="/logout" style="display:inline-block; padding:10px 20px; background:#b0bec5; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🚪 로그아웃</a>
                </p>

                <p style="text-align:center; color:#bdc3c7; font-size:0.8em; margin-top:10px;">
                    15초마다 자동 갱신 · 카드를 누르면 기기별 상세 그래프
                </p>
                <p style="text-align:center; color:#90a4ae; font-size:0.75em; margin-top:20px;">
                    돌봄기기 통합 대시보드 v{VERSION}
                </p>
            </div>
            <script>
                async function refreshCards() {{
                    try {{
                        const r = await fetch('/api/cards');
                        if (!r.ok) return;
                        const d = await r.json();
                        document.getElementById('emfit-section').innerHTML = d.emfit_section;
                        document.getElementById('radar-section').innerHTML = d.radar_section;
                        document.getElementById('fsr-section').innerHTML = d.fsr_section;
                        document.getElementById('header-summary').innerHTML = d.summary;
                        document.getElementById('clock').textContent = d.now;
                    }} catch (e) {{}}
                }}
                setInterval(refreshCards, 15000);
                document.addEventListener('visibilitychange', () => {{
                    if (!document.hidden) refreshCards();
                }});
            </script>
        </body>
    </html>
    """


@app.get("/api/cards")
def api_cards(request: Request, _: str = Depends(require_admin)):
    return _build_cards_payload(_get_token_from_request(request) or "")


# 그룹(view) 대시보드 — 공무원/외부 관람자가 admin이 묶어준 기기만 모아 봄
@app.get("/v/{token}", response_class=HTMLResponse)
def view_group_entry(token: str):
    """발급된 그룹 URL 진입 → 쿠키 저장 후 /view 로 리다이렉트."""
    views = _load_view_tokens()
    v = views.get(token)
    if not isinstance(v, dict) or not v.get("sns"):
        raise HTTPException(status_code=401, detail="invalid view token")
    resp = RedirectResponse("/view", status_code=303)
    resp.set_cookie(VIEW_COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@app.get("/view", response_class=HTMLResponse)
def view_group_dashboard(request: Request):
    """그룹 토큰 보유자 전용 대시보드 — 본인 그룹 SN만 카드 노출."""
    view = _resolve_view(request)
    if view is None:
        return HTMLResponse(
            "<p style='font-family:sans-serif; padding:40px; text-align:center;'>"
            "접근할 수 있는 그룹이 없습니다. 관리자에게 받은 URL로 다시 접속해주세요.</p>",
            status_code=401,
        )
    name_safe = html.escape(view["name"])
    p = _build_cards_payload(token=view["token"], sn_filter=view["sns"], view_token=view["token"])
    return f"""
    <html>
        <head>
            <title>{name_safe} · 통합 관제</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: 'Malgun Gothic', sans-serif; padding:20px; background:#f0f2f5; margin:0; }}
                a {{ -webkit-tap-highlight-color: transparent; }}
                @media (max-width: 600px) {{
                    body {{ padding: 10px; }}
                    h1 {{ font-size: 1.3em !important; }}
                    .nav-buttons a {{
                        display:block !important; margin: 6px 0 !important;
                    }}
                }}
            </style>
        </head>
        <body>
            <div style="max-width:1400px; margin:auto;">
                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                    <h1 style="color:#1a73e8; margin:0;">📡 {name_safe}</h1>
                    <div style="color:#7f8c8d; font-size:0.9em;">
                        <span id="clock">{p['now']}</span> · <span id="header-summary">{p['summary']}</span>
                    </div>
                </div>
                <div id="emfit-section">
                    {p['emfit_section']}
                </div>
                <div id="radar-section">
                    {p['radar_section']}
                </div>
                <div id="fsr-section">
                    {p['fsr_section']}
                </div>

                <p class="nav-buttons" style="text-align:center; margin-top:30px;">
                    <a href="/help" style="display:inline-block; padding:10px 20px; background:#27ae60; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">📘 사용 가이드</a>
                    <a href="/feedback" style="display:inline-block; padding:10px 20px; background:#e67e22; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">💬 의견 보내기</a>
                </p>

                <p style="text-align:center; color:#bdc3c7; font-size:0.8em; margin-top:10px;">
                    15초마다 자동 갱신 · 카드를 누르면 기기별 상세 그래프
                </p>
                <p style="text-align:center; color:#90a4ae; font-size:0.75em; margin-top:20px;">
                    돌봄기기 통합 대시보드 v{VERSION}
                </p>
            </div>
            <script>
                async function refreshCards() {{
                    try {{
                        const r = await fetch('/api/view/cards');
                        if (!r.ok) return;
                        const d = await r.json();
                        document.getElementById('emfit-section').innerHTML = d.emfit_section;
                        document.getElementById('radar-section').innerHTML = d.radar_section;
                        document.getElementById('fsr-section').innerHTML = d.fsr_section;
                        document.getElementById('header-summary').innerHTML = d.summary;
                        document.getElementById('clock').textContent = d.now;
                    }} catch (e) {{}}
                }}
                setInterval(refreshCards, 15000);
                document.addEventListener('visibilitychange', () => {{
                    if (!document.hidden) refreshCards();
                }});
            </script>
        </body>
    </html>
    """


@app.get("/api/view/cards")
def api_view_cards(request: Request):
    """그룹 대시보드 카드 갱신용."""
    view = _resolve_view(request)
    if view is None:
        raise HTTPException(status_code=401, detail="login required")
    return _build_cards_payload(token=view["token"], sn_filter=view["sns"], view_token=view["token"])


def _build_single_card(sn, token=""):
    """단일 기기 카드 HTML — /device/{sn} 페이지의 실시간 현황 영역에 사용."""
    info = analyzer.DEVICE_INFO.get(sn)
    if info is None:
        return ""
    now = datetime.now(KST)   # 서버 시간대와 무관하게 KST 기준으로 비교
    now_ts = now.timestamp()
    latest = {}
    if _has_data():
        try:
            latest = analyzer.get_latest_states(DATA_FILES)
        except Exception:
            pass
    statuses = analyzer.get_device_statuses()
    state = latest.get(sn)
    ds = statuses.get(sn)
    if not _is_active(ds, now_ts):
        return _render_inactive_card(sn, info, ds, now)
    if _is_fsr_device(sn, state, ds):
        return _render_fsr_card(sn, info, state, ds, now)
    return _render_card(sn, info, state, ds, now)


@app.get("/api/device/{sn}/card", response_class=HTMLResponse)
def api_device_card(sn: str, request: Request):
    _require_device_access(request, sn)
    token = _get_token_from_request(request) or ""
    return HTMLResponse(_build_single_card(sn, token))


def _require_device_access(request: Request, sn: str):
    """admin → device 토큰 → view(그룹) 토큰 순으로 접근 검사.
    어디에도 안 맞으면 401/403."""
    # admin 먼저 — admin이 테스트로 /d/{token} 접속해 쿠키가 남아있어도 다른 기기 접근 가능해야 함.
    if _is_admin_authenticated(request):
        return None
    t = _get_token_from_request(request)
    mapped = None
    if t:
        mapped = _load_tokens().get(t)
        if mapped == sn or mapped == "*":
            return t  # device 토큰 통과
    # device 토큰이 이 기기를 허용하지 않더라도 곧장 막지 말고 view(그룹) 토큰을 먼저 확인한다.
    # (잔류 device 토큰 쿠키가 그룹 접근을 가로채던 버그 fix)
    view = _resolve_view(request)
    if view is not None and sn in view["sns"]:
        return view["token"]
    # 어떤 토큰으로도 이 기기 접근이 허용되지 않음.
    if mapped is not None or (view is not None):
        raise HTTPException(status_code=403, detail="other device")  # 토큰은 있으나 이 기기는 권한 밖
    raise HTTPException(status_code=401, detail="login required")


def _get_viewer_id(request: Request):
    """현재 viewer 식별자. UI 환경설정(블록 순서 등) 저장 키.
    admin 세션 → 'admin', device 토큰 → 토큰 값, view 토큰 → 'view:' + 토큰."""
    if _is_admin_authenticated(request):
        return "admin"
    t = _get_token_from_request(request)
    if t:
        return t
    view = _resolve_view(request)
    if view is not None:
        return "view:" + view["token"]
    return None


@app.get("/api/preferences/order")
def api_get_block_order(request: Request):
    """현재 viewer의 블록 순서 반환. 저장된 게 없으면 기본 순서."""
    vid = _get_viewer_id(request)
    if vid is None:
        raise HTTPException(status_code=401, detail="login required")
    prefs = _load_preferences()
    order = (prefs.get(vid) or {}).get("block_order") or DEFAULT_BLOCK_ORDER
    # 알려진 블록만 남기고 누락분 뒤에 채워서 신뢰 가능한 순서 보장.
    # 화면에 없는 블록 id 가 섞여 있어도 프런트가 무시하므로 문제되지 않는다.
    known = set(ALL_BLOCK_IDS)
    cleaned = [b for b in order if b in known]
    for b in ALL_BLOCK_IDS:
        if b not in cleaned:
            cleaned.append(b)
    return {"order": cleaned}


@app.post("/api/preferences/order")
async def api_set_block_order(request: Request):
    """블록 순서 저장. body: {"order": ["summary", "hr", ...]}."""
    vid = _get_viewer_id(request)
    if vid is None:
        raise HTTPException(status_code=401, detail="login required")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    order = body.get("order")
    if not isinstance(order, list) or not order:
        raise HTTPException(status_code=400, detail="order must be non-empty list")
    known = set(ALL_BLOCK_IDS)
    cleaned = [b for b in order if isinstance(b, str) and b in known]
    if not cleaned:
        raise HTTPException(status_code=400, detail="no valid blocks in order")
    # 누락된 블록은 뒤에 채움 (다른 기기 종류의 블록도 순서를 잃지 않도록)
    for b in ALL_BLOCK_IDS:
        if b not in cleaned:
            cleaned.append(b)
    prefs = _load_preferences()
    user_prefs = prefs.get(vid) or {}
    user_prefs["block_order"] = cleaned
    prefs[vid] = user_prefs
    _save_preferences(prefs)
    return {"order": cleaned}


def _active_assignment(sn):
    """sn 의 현재 활성 배정(end=None) 반환. 없으면 None.
    상세페이지를 '현재 사용자 기간'으로 한정하는 데 쓴다."""
    for a in analyzer.list_assignments():
        if a.get("sn") == sn and not a.get("end"):
            return a
    return None


def _resolve_device_stint(sn, assignment, is_admin):
    """상세페이지에서 볼 배정을 결정. admin 이 ?assignment= 로 그 기기의
    유효한 배정을 지정하면 그것, 아니면 활성 배정. 토큰 사용자는 항상 활성 배정."""
    if assignment and is_admin:
        st = _find_assignment(assignment)
        if st and st.get("sn") == sn:
            return st
    return _active_assignment(sn)


def _fsr_intervals(events, win_start, win_end, now_ts):
    """사용감지 이벤트 목록 → '언제부터 언제까지 썼는지' 구간 목록.

    events: [{"epoch": int, "in_use": True/False/None, "used_sec": float|None}, ...] 시간순.
    win_start/win_end: 보고 있는 시간 범위(epoch). 구간은 이 안으로 잘린다.

    까다로운 경우를 다룬다:
      · 사용 시작만 있고 종료가 없음 → 아직 쓰는 중. now(또는 범위 끝)까지로 본다.
      · 종료만 있고 시작이 없음     → 범위 이전부터 쓰고 있었다는 뜻.
                                     보드가 준 사용시간(used_sec)으로 시작 시각을 역산한다.
      · 생존신고 등 상태를 모르는 이벤트는 무시 (in_use is None)."""
    intervals = []
    open_at = None
    last_end = win_start          # 이미 집계한 구간의 끝 — 겹쳐서 이중 계산되지 않게
    for ev in events:
        in_use = ev.get("in_use")
        if in_use is None:
            continue
        ep = ev["epoch"]
        if in_use:
            if open_at is None:
                open_at = ep
            # 이미 열려 있으면 중복 press — 무시 (먼저 것을 시작으로 유지)
        else:
            if open_at is not None:
                start = open_at
            else:
                # 시작을 못 본 종료 — 범위 이전부터 썼거나, 시작 신호가 유실된 경우.
                # 보드가 준 사용시간으로 역산하고, 없으면 알 수 없으므로
                # 직전 구간이 끝난 시점부터로 본다(가장 보수적이고 겹치지 않는 선택).
                used = ev.get("used_sec")
                start = (ep - used) if isinstance(used, (int, float)) and used > 0 else last_end
            s = max(start, win_start, last_end)
            if ep > s:
                intervals.append({"start": int(s), "end": int(ep), "ongoing": False})
                last_end = ep
            open_at = None

    if open_at is not None:                      # 아직 사용 중
        end = min(win_end, now_ts)
        s = max(open_at, win_start)
        if end > s:
            intervals.append({"start": int(s), "end": int(end), "ongoing": True})
    return intervals


# 기기마다 측정 간격이 달라, 이 시간보다 크게 벌어지면 '그동안 기록이 없다'로 보고 구간을 끊는다.
# 너무 크게 잡으면 꺼져 있던 시간까지 재실로 세고, 너무 작으면 구간이 잘게 쪼개진다.
_BAND_GAP_SEC = {"emfit": 300, "radar": 120, "mckare": 300}


def _sampled_bands(samples, max_gap_sec, win_end):
    """[(epoch, 라벨)] 시간순 → 같은 라벨이 이어지는 구간 목록.

    Emfit·레이더·McKare 는 이벤트가 아니라 '주기적으로 찍히는 값'이라
    각 표본이 다음 표본까지의 시간을 대표한다고 보고 길이를 매긴다.
    마지막 표본이나 간격이 벌어진 표본은 max_gap_sec 까지만 인정한다."""
    bands = []
    n = len(samples)
    for i, (t, lab) in enumerate(samples):
        if lab is None:
            continue
        nxt = samples[i + 1][0] if i + 1 < n else win_end
        end = min(nxt, t + max_gap_sec)
        if end <= t:
            continue
        if bands and bands[-1]["label"] == lab and bands[-1]["end"] >= t - 1:
            bands[-1]["end"] = max(bands[-1]["end"], end)   # 이어지는 같은 상태는 합친다
        else:
            bands.append({"label": lab, "start": int(t), "end": int(end)})
    return bands


def _band_totals(bands):
    """구간 목록 → {라벨: 총 초}, 그리고 라벨별 등장 횟수."""
    secs, cnt = {}, {}
    for b in bands:
        secs[b["label"]] = secs.get(b["label"], 0) + (b["end"] - b["start"])
        cnt[b["label"]] = cnt.get(b["label"], 0) + 1
    return secs, cnt


def _fsr_daily_totals(sn, aid, dates, tz=KST):
    """날짜별 총 사용시간(초). dates 는 'YYYY-MM-DD' 목록(최신순)."""
    out = []
    # 아직 사용 중인 세션은 '지금'까지만 센다. 자정까지로 계산하면 오늘 사용시간이 부풀어 오른다.
    now_ts = datetime.now().timestamp()
    for d in dates:
        try:
            df = analyzer.get_report_df(DATA_FILES, d, sn, assignment_id=aid)
        except Exception:
            continue
        if df is None or df.empty or "사용중" not in df.columns:
            continue
        try:
            day0 = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=tz)
        except Exception:
            continue
        day_start = day0.timestamp()
        day_end = day_start + 24 * 3600
        events = []
        for _, row in df.iterrows():
            if row.get("유형") != "FSR":
                continue
            t = row.get("시간(KST)")
            if not isinstance(t, str) or len(t) < 8:
                continue
            try:
                ep = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
            except Exception:
                continue
            iu = row.get("사용중")
            used = row.get("사용시간(ms)")
            events.append({
                "epoch": ep,
                "in_use": bool(iu) if isinstance(iu, (bool, int, float)) and pd.notna(iu) else None,
                "used_sec": float(used) / 1000 if isinstance(used, (int, float)) and pd.notna(used) else None,
            })
        events.sort(key=lambda e: e["epoch"])
        total = sum(i["end"] - i["start"]
                    for i in _fsr_intervals(events, day_start, day_end, now_ts))
        out.append({"date": d, "seconds": int(total)})
    out.reverse()                                 # 오래된 → 최신 (그래프 x축 순서)
    return out


@app.get("/api/device/{sn}/timeseries")
def api_device_timeseries(
    sn: str,
    request: Request,
    date: str = Query(None),
    start_dt: str = Query(None),
    end_dt: str = Query(None),
    assignment: str = Query(None),
):
    """기기 시계열.
    - start_dt + end_dt 둘 다 제공 시 → 그 KST 범위 (자정 가로지름 가능)
    - date만 → 그 날 00:00 ~ 23:59 (단일 날짜 호환)
    - 모두 없으면 가장 최근 데이터 있는 날 단일 모드.
    각 point에 epoch (unix sec) 포함 — 프런트에서 timestamp 기반 x축에 사용."""
    token = _require_device_access(request, sn)
    info = analyzer.DEVICE_INFO.get(sn)
    if info is None:
        return PlainTextResponse("기기 없음", status_code=404)

    # 볼 배정 결정 — admin 은 ?assignment= 로 옛 배정도 조회, 토큰 사용자는 활성 배정만
    target = _resolve_device_stint(sn, assignment, token is None)
    target_id = target["id"] if target else None
    disp_name = target["user"] if target else info["name"]
    disp_loc = target["location"] if target else info["location"]
    lo = target["start"][:10] if target and target.get("start") else None
    hi = target["end"][:10] if target and target.get("end") else None

    available_for_sn = []
    if _has_data():
        try:
            available = analyzer.list_available(DATA_FILES)
            available_for_sn = [d for s, d in available if s == sn
                                and (not lo or d >= lo) and (not hi or d <= hi)]
        except Exception:
            pass

    sdt_obj = _parse_kst_dt(start_dt) if start_dt else None
    edt_obj = _parse_kst_dt(end_dt) if end_dt else None

    if sdt_obj and edt_obj and sdt_obj < edt_obj:
        # 범위 모드
        cur = sdt_obj.date()
        last = edt_obj.date()
        dates_to_load = []
        while cur <= last:
            dates_to_load.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        date = sdt_obj.strftime("%Y-%m-%d")
    else:
        # 단일 date 모드 (기본은 가장 최근 데이터 날짜)
        if not date:
            date = available_for_sn[0] if available_for_sn else datetime.now().strftime("%Y-%m-%d")
        dates_to_load = [date]
        try:
            d_obj = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=KST)
        except Exception:
            d_obj = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        sdt_obj = d_obj
        edt_obj = d_obj.replace(hour=23, minute=59, second=59)

    points = []
    summaries = []
    fsr_events = []
    if _has_data():
        for d in dates_to_load:
            try:
                df = analyzer.get_report_df(DATA_FILES, d, sn, assignment_id=target_id)
            except Exception:
                df = None
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                rtype = row.get("유형")
                t = row.get("시간(KST)")
                if not isinstance(t, str) or len(t) < 8:
                    continue
                try:
                    full_dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                except Exception:
                    continue
                # 범위 필터 — 단일 모드에선 그 날 00~23:59:59 cap
                if full_dt < sdt_obj or full_dt > edt_obj:
                    continue
                epoch = int(full_dt.timestamp())

                if rtype == "Summary":
                    s = {}
                    for k in ["수면점수", "총수면(분)", "얕은수면(분)", "REM수면(분)", "깊은수면(분)", "각성시간(분)"]:
                        if k in row.index:
                            v = row.get(k)
                            if pd.notna(v):
                                s[k] = float(v) if isinstance(v, (int, float)) else v
                    if s:
                        s["__end__"] = t[:5]
                        s["__date__"] = d
                        s["__epoch__"] = epoch
                        summaries.append(s)
                    continue

                if rtype == "FSR":
                    # 사용감지는 값이 아니라 '이벤트'라서 꺾은선 점으로 만들지 않는다.
                    # 아래에서 사용 구간(띠)으로 변환한다.
                    iu = row.get("사용중")
                    used = row.get("사용시간(ms)")
                    batt = row.get("배터리(%)")
                    fsr_events.append({
                        "epoch": epoch,
                        "in_use": bool(iu) if isinstance(iu, (bool, int, float)) and pd.notna(iu) else None,
                        "used_sec": float(used) / 1000 if isinstance(used, (int, float)) and pd.notna(used) else None,
                        "event": row.get("이벤트"),
                        "battery": float(batt) if isinstance(batt, (int, float)) and pd.notna(batt) else None,
                    })
                    continue

                pt = {"time": t, "date": d, "epoch": epoch, "type": rtype}
                for k, jk in [("심박수(HR)", "hr"), ("호흡수(RR)", "rr"), ("활동량(ACT)", "act"),
                              ("체온", "temp")]:
                    v = row.get(k)
                    pt[jk] = float(v) if isinstance(v, (int, float)) and pd.notna(v) else None
                # 레이더 자세 — 숫자 코드와 한글 라벨 둘 다 (그래프는 코드, 툴팁은 라벨)
                pos = row.get("자세(POS)")
                pt["pos"] = int(pos) if isinstance(pos, (int, float)) and pd.notna(pos) else None
                pt["posture"] = str(row.get("자세")) if row.get("자세") is not None else None
                # McKare 재실/부재 라벨 (구간 재실 시간 계산용)
                pres = row.get("재실")
                pt["presence"] = str(pres) if isinstance(pres, str) and pres else None
                points.append(pt)

    # 시간 순 정렬 (multi-day 합쳤을 때 필수)
    points.sort(key=lambda p: p.get("epoch", 0))

    # summary 정렬: 총수면(분) 큰 순 → 종료 epoch 이른 순
    def _summary_key(s):
        total = s.get("총수면(분)")
        total = -total if isinstance(total, (int, float)) else 0
        return (total, s.get("__epoch__") or 0)
    summaries.sort(key=_summary_key)

    # 사용감지 — 이벤트를 '사용 구간'으로 바꾸고, 최근 며칠치 일별 합계도 같이 준다.
    fsr_events.sort(key=lambda e: e["epoch"])
    intervals, daily = [], []
    if fsr_events or (info and _is_fsr_device(sn)):
        now_ts = datetime.now().timestamp()
        intervals = _fsr_intervals(fsr_events,
                                   sdt_obj.timestamp() if sdt_obj else 0,
                                   edt_obj.timestamp() if edt_obj else now_ts,
                                   now_ts)
        daily = _fsr_daily_totals(sn, target_id, available_for_sn[:FSR_DAILY_DAYS])

    # ── 기기별 '얼마나 오래' 통계 ───────────────────────────────────
    # Emfit  : 침대 재실 시간 (움직임 0 이면 매트 위에 사람이 없다고 본다)
    # Radar  : 자세별 시간
    # McKare : 구간 재실 시간
    win_end = edt_obj.timestamp() if edt_obj else datetime.now().timestamp()
    kind = _detail_kind(sn)
    bands, band_secs, band_counts = [], {}, {}
    if points:
        gap = _BAND_GAP_SEC.get(kind, 300)
        if kind == "emfit":
            samples = [(p["epoch"], ("재실" if (p.get("act") or 0) >= 1 else "이탈"))
                       for p in points if p.get("act") is not None]
        elif kind == "radar":
            samples = [(p["epoch"], p.get("posture")) for p in points if p.get("posture")]
        elif kind == "mckare":
            samples = [(p["epoch"], p.get("presence")) for p in points if p.get("presence")]
        else:
            samples = []
        if samples:
            bands = _sampled_bands(samples, gap, win_end)
            band_secs, band_counts = _band_totals(bands)

    return {
        "device": sn,
        "name": disp_name,
        "location": disp_loc if disp_loc and disp_loc != "-" else "미지정",
        "date": date,
        "start_dt": sdt_obj.isoformat() if sdt_obj else None,
        "end_dt": edt_obj.isoformat() if edt_obj else None,
        "available_dates": available_for_sn,
        "points": points,
        "summaries": summaries,
        "fsr_intervals": intervals,
        "fsr_daily": daily,
        "fsr_battery": [{"epoch": e["epoch"], "pct": e["battery"]}
                        for e in fsr_events if e.get("battery") is not None],
        # 기기별 '얼마나 오래' — 재실 시간 / 자세별 시간 / 구간 재실
        "kind": kind,
        "bands": bands,
        "band_seconds": band_secs,
        "band_counts": band_counts,
    }


def _detail_kind(sn):
    """상세페이지에서 쓸 기기 종류. 대시보드 카드와 같은 판정을 재사용한다."""
    ds = analyzer.get_device_statuses().get(sn)
    state = None
    if _has_data():
        try:
            state = analyzer.get_latest_states(DATA_FILES).get(sn)
        except Exception:
            pass
    return _v2_kind(sn, state, ds)


# 블록 한 장을 그리는 틀. (큰 f-string 밖에서 만들어 중괄호 이스케이프를 피한다)
def _detail_block(bid, title, body, hint=""):
    hint_html = f'<div class="chart-hint">{hint}</div>' if hint else ""
    return f"""
                    <div class="block-card" data-block-id="{bid}">
                        <div class="block-header">
                            <h3>{title}</h3>
                            <div class="block-actions">
                                <button class="arrow-btn" data-arrow="up" title="위로 이동">▲</button>
                                <button class="arrow-btn" data-arrow="down" title="아래로 이동">▼</button>
                                <span class="drag-handle" title="드래그해서 이동">⋮⋮</span>
                            </div>
                        </div>
                        {body}{hint_html}
                    </div>"""


def _chart_body(key):
    return (f'<div class="chart-scroll"><div class="chart-canvas-wrap" id="wrap-{key}">'
            f'<canvas id="chart-{key}"></canvas></div></div>')


_SCROLL_HINT = "↔ 가로 스크롤 · 30분 간격 눈금"

# 블록 id → (제목, 본문 HTML, 힌트)
_DETAIL_BLOCK_DEFS = {
    "summary": ("🛌 수면 요약", '<div id="summary-area"></div>', ""),
    "hr":      ("❤️ 심박수 (HR) — 분당", _chart_body("hr"), _SCROLL_HINT),
    "rr":      ("🫁 호흡수 (RR) — 분당", _chart_body("rr"), _SCROLL_HINT),
    "act":     ("🏃 활동량 (ACT)", _chart_body("act"), _SCROLL_HINT),
    "temp":    ("🌡️ 체온 (℃)", _chart_body("temp"), _SCROLL_HINT),
    "posture": ("🧭 자세", _chart_body("posture"), _SCROLL_HINT),
    # 사용 구간은 Chart.js 대신 직접 그린다 — 단순한 띠라 훨씬 가볍고 예측 가능하다.
    "usage":   ("🦶 사용 구간",
                '<div id="usage-summary" class="usage-summary"></div>'
                '<div id="usage-timeline" class="usage-timeline"></div>'
                '<div id="usage-ticks" class="usage-ticks"></div>'
                '<div id="usage-list" class="usage-list"></div>', ""),
    "daily":   ("📊 일별 사용시간",
                '<div class="daily-wrap"><canvas id="chart-daily"></canvas></div>',
                "최근 기록이 있는 날짜 기준"),
    # ── '얼마나 오래' 통계 — 띠 + 항목별 시간 막대 (같은 틀을 셋이 공유) ──
    "occupancy": ("🛏️ 침대 재실 시간",
                  '<div id="occupancy-total" class="band-total"></div>'
                  '<div id="occupancy-bar" class="band-bar"></div>'
                  '<div id="occupancy-ticks" class="band-ticks"></div>'
                  '<div id="occupancy-legend" class="band-legend"></div>'
                  '<p class="band-note">움직임(ACT)이 0이면 매트 위에 사람이 없는 것으로 보고 '
                  '<b>이탈</b>로 계산합니다.</p>', ""),
    "postures":  ("🧭 자세별 시간",
                  '<div id="postures-total" class="band-total"></div>'
                  '<div id="postures-bar" class="band-bar"></div>'
                  '<div id="postures-ticks" class="band-ticks"></div>'
                  '<div id="postures-legend" class="band-legend"></div>', ""),
    "presence":  ("📍 구간 재실 시간",
                  '<div id="presence-total" class="band-total"></div>'
                  '<div id="presence-bar" class="band-bar"></div>'
                  '<div id="presence-ticks" class="band-ticks"></div>'
                  '<div id="presence-legend" class="band-legend"></div>', ""),
}


def _build_detail_blocks(kind):
    """기기 종류에 맞는 블록들만 HTML 로 만든다."""
    ids = DEVICE_BLOCKS.get(kind) or DEVICE_BLOCKS["emfit"]
    return "".join(_detail_block(b, *_DETAIL_BLOCK_DEFS[b]) for b in ids if b in _DETAIL_BLOCK_DEFS), ids


@app.get("/device/{sn}", response_class=HTMLResponse)
def view_device(sn: str, request: Request, assignment: str = Query(None)):
    device_token = _require_device_access(request, sn)
    # device_token: None (admin) | device 토큰 | view(그룹) 토큰. is_admin/view 따로 판정.
    is_admin = _is_admin_authenticated(request)
    view = _resolve_view(request)
    sn_in_group = view is not None and sn in view["sns"]
    # admin은 명시적으로 그룹을 미리보기(?view=)로 들어왔을 때만 그룹 컨텍스트로 취급한다.
    # (전체 대시보드에서 그룹 기기를 눌렀을 때 잔류 쿠키 때문에 그룹으로 빨려가던 버그 fix)
    # 실사용자(그룹 토큰 보유, admin 아님)는 쿠키만으로도 그룹 컨텍스트.
    explicit_view = bool(request.query_params.get("view"))
    in_view = sn_in_group and (explicit_view or not is_admin)
    info = analyzer.DEVICE_INFO.get(sn)
    if info is None:
        return HTMLResponse("<p>기기를 찾을 수 없습니다.</p>", status_code=404)

    # 볼 배정 결정 — admin 은 ?assignment= 로 옛 배정 그래프도 조회 가능
    target = _resolve_device_stint(sn, assignment, is_admin)
    target_id = target["id"] if target else None
    is_closed_stint = bool(target and target.get("end"))
    if target:
        info = {"name": target.get("user", sn),
                "location": target.get("location", "-"),
                "group": target.get("group", "일반")}

    location_text = html.escape(str(info['location'] if info['location'] and info['location'] != '-' else '미지정'))
    name_safe = html.escape(str(info['name']))

    csv_query = f"assignment={target_id}" if target_id else f"device={sn}"
    lo = target["start"][:10] if target and target.get("start") else None
    hi = target["end"][:10] if target and target.get("end") else None

    available_for_sn = []
    if _has_data():
        try:
            available = analyzer.list_available(DATA_FILES)
            available_for_sn = [d for s, d in available if s == sn
                                and (not lo or d >= lo) and (not hi or d <= hi)]
        except Exception:
            pass
    today = available_for_sn[0] if available_for_sn else datetime.now(KST).strftime("%Y-%m-%d")
    if in_view:
        # view(그룹) 컨텍스트 우선 — admin이 테스트 중이든 진짜 그룹 사용자든 그룹 대시보드로 돌아감
        back_link = '<a href="/view" style="color:#1a73e8; text-decoration:none;">← 그룹 대시보드</a>'
    elif not is_admin:
        back_link = f'<a href="/d/{device_token}" style="color:#1a73e8; text-decoration:none;">← 메인</a>'
    elif assignment:
        back_link = '<a href="/devices" style="color:#1a73e8; text-decoration:none;">← 배정 이력</a>'
    else:
        back_link = '<a href="/dashboard" style="color:#1a73e8; text-decoration:none;">← 대시보드</a>'

    # 이름 수정 권한 — admin OR view(그룹) 권한자만, 활성 배정만 (종료된 옛 배정은 역사 기록 보존)
    can_edit_name = (is_admin or in_view) and not is_closed_stint
    if can_edit_name:
        name_safe_val = html.escape(str(info['name']), quote=True)
        edit_name_ui = f"""
        <button type="button" id="name-edit-btn" onclick="document.getElementById('name-edit-form').style.display='block'; this.style.display='none';"
            style="margin-left:8px; padding:4px 10px; background:transparent; color:#1a73e8; border:1px solid #1a73e8; border-radius:6px; cursor:pointer; font-size:0.8em; vertical-align:middle;"
            title="활성 배정의 사용자명 수정">✏️ 이름 수정</button>
        <form id="name-edit-form" method="post" action="/devices/edit_user" style="display:none; margin:10px 0 16px 0; padding:12px 14px; background:#fff8e1; border:1px solid #ffd54f; border-radius:8px;">
            <input type="hidden" name="sn" value="{sn}">
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <label style="font-size:0.9em; color:#5d4037;">새 이름:</label>
                <input type="text" name="user" value="{name_safe_val}" required maxlength="50" autofocus
                    style="flex:1; min-width:180px; padding:6px 10px; border:1px solid #ccc; border-radius:6px;">
                <button type="submit" style="padding:6px 14px; background:#16a085; color:white; border:none; border-radius:6px; cursor:pointer; font-weight:bold;">저장</button>
                <button type="button" onclick="document.getElementById('name-edit-form').style.display='none'; document.getElementById('name-edit-btn').style.display='inline-block';"
                    style="padding:6px 10px; background:#b0bec5; color:white; border:none; border-radius:6px; cursor:pointer;">취소</button>
            </div>
            <p style="margin:8px 0 0 0; font-size:0.78em; color:#8d6e63; line-height:1.4;">
                ⚠️ 이름을 바꾸면 이 활성 배정 <b>기간 전체</b>의 데이터가 새 이름으로 표시됩니다. (옛 배정/리포트는 그대로)
            </p>
        </form>
        """
    else:
        edit_name_ui = ""
    token = device_token or ""
    # API fetch(XHR) 인증을 쿠키에만 의존하지 않도록 — 토큰/뷰 토큰을 쿼리로도 실어 보낸다.
    # (모바일 브라우저가 백그라운드 요청에 쿠키를 빠뜨리면 401→로그인HTML→JSON파싱 폭발하던 버그 차단)
    if device_token is None:
        auth_qs = ""  # admin — 세션 쿠키로 충분
    elif view is not None and device_token == view.get("token"):
        auth_qs = f"view={device_token}"
    else:
        auth_qs = f"token={device_token}"
    initial_card_html = _build_single_card(sn, token)
    # 종료된 배정(과거 조회)이면 실시간 카드 대신 안내문, 활성 배정이면 실시간 카드
    if is_closed_stint:
        _period = f"{target.get('start') or '처음'} ~ {target.get('end')}"
        realtime_section = (
            '<div style="background:#eceff1; border-radius:10px; padding:12px 16px;'
            ' margin-bottom:20px; color:#546e7a; font-size:0.9em;">'
            f'📌 종료된 배정 — 과거 데이터 조회 전용 ({_period})</div>'
        )
        realtime_js = "false"
    else:
        realtime_section = (
            '<h3 style="color:#37474f; margin:20px 0 10px 0; font-size:0.95em;">'
            '📡 실시간 현황 <span style="color:#90a4ae; font-weight:normal;'
            ' font-size:0.9em;">— 15초마다 자동 갱신</span></h3>'
            f'<div id="status-card" style="margin-bottom:20px;">{initial_card_html}</div>'
        )
        realtime_js = "true"
    # 기기 종류별 블록 — 안 재는 값을 빈 그래프로 두지 않는다
    kind = _detail_kind(sn)
    blocks_html, block_ids = _build_detail_blocks(kind)
    block_ids_json = json.dumps(block_ids)
    # 날짜 버튼용 — 데이터 있는 날짜를 JS로 직렬화. (analyzer.list_available은 최신 → 과거 순)
    available_json = json.dumps(available_for_sn)
    # datetime-local 기본값 — 가장 최근 날짜의 00:00 ~ 23:55
    default_start = f"{today}T00:00"
    default_end = f"{today}T23:55"

    return f"""
    <html>
        <head>
            <title>{name_safe} · 돌봄기기 통합 대시보드</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
            <script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js"></script>
            <style>
                body {{ font-family: 'Malgun Gothic', sans-serif; padding:20px; background:#f0f2f5; margin:0; }}
                .container {{ max-width: 900px; margin: auto; }}
                /* min-width:0 + overflow:hidden — 자식 캔버스가 매우 넓어져도 부모가 화면 밖으로 안 나가게 */
                .block-card {{ background: white; padding: 20px; border-radius: 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); min-width: 0; overflow: hidden; position: relative; margin-bottom: 16px; }}
                .block-card.dragging {{ opacity: 0.5; }}
                .block-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}
                .block-header h3 {{ margin: 0; color: #37474f; font-size: 1em; flex: 1; }}
                .block-actions {{ display: flex; align-items: center; gap: 4px; }}
                .arrow-btn {{ background: #f5f5f5; border: 1px solid #e0e0e0; color: #607d8b; cursor: pointer; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; line-height: 1.3; }}
                .arrow-btn:hover:not(:disabled) {{ background: #e3f2fd; color: #1a73e8; border-color: #90caf9; }}
                .arrow-btn:disabled {{ opacity: 0.35; cursor: not-allowed; }}
                .drag-handle {{ cursor: grab; color: #cfd8dc; font-size: 1.2em; user-select: none; padding: 2px 6px; border-radius: 4px; line-height: 1; margin-left: 2px; }}
                .drag-handle:hover {{ background: #f5f5f5; color: #607d8b; }}
                /* ── 사용 구간 띠 (사용감지 센서 전용) ── */
                .usage-summary {{ font-size: 0.9em; color: #37474f; margin-bottom: 12px; }}
                .usage-timeline {{ position: relative; height: 34px; background: #eceff1;
                                   border-radius: 6px; overflow: hidden; }}
                .usage-timeline .seg {{ position: absolute; top: 0; bottom: 0; background: #26a69a;
                                        border-radius: 3px; min-width: 2px; }}
                .usage-timeline .seg.ongoing {{ background: repeating-linear-gradient(45deg,
                                        #26a69a, #26a69a 6px, #4db6ac 6px, #4db6ac 12px); }}
                .usage-ticks {{ position: relative; height: 18px; margin-top: 4px; }}
                .usage-ticks span {{ position: absolute; transform: translateX(-50%);
                                     font-size: 0.68em; color: #90a4ae; white-space: nowrap; }}
                .usage-list {{ margin-top: 12px; }}
                .usage-row {{ display: flex; justify-content: space-between; font-size: 0.85em;
                              color: #546e7a; padding: 5px 2px; border-bottom: 1px solid #eceff1; }}
                .usage-row .dur {{ font-weight: bold; color: #00695c; }}
                .daily-wrap {{ position: relative; height: 220px; }}
                /* ── '얼마나 오래' 통계 (재실·자세별·구간 재실 공용) ── */
                .band-total {{ font-size: 1.05em; color: #37474f; margin-bottom: 12px; }}
                .band-total b {{ color: #1a73e8; font-size: 1.15em; }}
                .band-bar {{ position: relative; height: 30px; background: #eceff1;
                             border-radius: 6px; overflow: hidden; }}
                .band-bar i {{ position: absolute; top: 0; bottom: 0; min-width: 1px; }}
                .band-ticks {{ position: relative; height: 18px; margin-top: 4px; }}
                .band-ticks span {{ position: absolute; transform: translateX(-50%);
                                    font-size: 0.68em; color: #90a4ae; white-space: nowrap; }}
                .band-legend {{ margin-top: 14px; }}
                .band-row {{ display: grid; grid-template-columns: 90px 1fr 62px 40px 44px;
                             align-items: center; gap: 8px; padding: 5px 0; font-size: 0.85em; }}
                .band-row .bl {{ display: flex; align-items: center; gap: 7px;
                                 font-weight: bold; color: #37474f; }}
                .band-row .bl i {{ width: 11px; height: 11px; border-radius: 3px; flex: none; }}
                .band-row .bt {{ position: relative; height: 9px; border-radius: 999px;
                                 background: #eceff1; overflow: hidden; }}
                .band-row .bt i {{ position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; }}
                .band-row .bv {{ text-align: right; font-weight: bold; color: #455a64; }}
                .band-row .bp {{ text-align: right; color: #90a4ae; }}
                .band-row .bn {{ text-align: right; color: #b0bec5; font-size: 0.92em; }}
                .band-note {{ margin: 12px 0 0; font-size: 0.78em; color: #90a4ae; line-height: 1.5; }}
                .band-note b {{ color: #607d8b; }}
                @media (max-width: 560px) {{
                    .band-row {{ grid-template-columns: 76px 1fr 58px 36px; }}
                    .band-row .bn {{ display: none; }}
                }}
                .drag-handle:active {{ cursor: grabbing; }}
                .chart-scroll {{ width: 100%; overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch; }}
                .chart-canvas-wrap {{ position: relative; height: 180px; }}
                .chart-hint {{ font-size: 0.75em; color: #90a4ae; margin-top: 4px; text-align: right; }}
                .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; }}
                .summary-item {{ background: #e3f2fd; padding: 12px; border-radius: 10px; text-align: center; }}
                .summary-item .value {{ font-size: 1.4em; font-weight: bold; color: #0d47a1; }}
                .summary-item .label {{ font-size: 0.75em; color: #455a64; margin-top: 4px; }}
                .summary-section + .summary-section {{ margin-top: 14px; padding-top: 14px; border-top: 1px dashed #eceff1; }}
                .summary-section-title {{ font-size: 0.85em; color: #607d8b; font-weight: bold; margin: 0 0 8px 0; }}

                /* 글로벌 시간 영역 */
                .time-bar {{ background:white; padding:14px; border-radius:10px; margin-bottom:20px; }}
                .datetime-row {{ display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom: 10px; }}
                .datetime-row label {{ display: flex; align-items: center; gap: 6px; font-size: 0.85em; color: #546e7a; }}
                .datetime-row input[type=datetime-local] {{ padding: 6px 8px; border: 1px solid #cfd8dc; border-radius: 6px; font-size: 0.95em; color: #1a237e; font-variant-numeric: tabular-nums; background: white; }}
                .datetime-row input[type=datetime-local]:focus {{ outline: none; border-color: #1a73e8; }}
                .time-sep {{ color: #90a4ae; font-weight: bold; }}
                .csv-btn {{ margin-left: auto; padding: 7px 14px; background: #1a73e8; color: white; text-decoration: none; border-radius: 6px; font-size: 0.85em; }}
                .quick-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
                .quick-btn {{ background: #eceff1; border: 1px solid transparent; color: #455a64; cursor: pointer; padding: 5px 10px; border-radius: 6px; font-size: 0.8em; }}
                .quick-btn:hover {{ background: #cfd8dc; }}
                .quick-btn.active {{ background: #1a73e8; color: white; border-color: #1a73e8; }}

                .date-buttons {{ display: flex; gap: 6px; overflow-x: auto; padding: 4px 2px; -webkit-overflow-scrolling: touch; user-select: none; scroll-behavior: smooth; }}
                .date-btn {{ flex: 0 0 auto; padding: 6px 10px; border: 1px solid #cfd8dc; background: white; border-radius: 6px; cursor: pointer; font-size: 0.85em; color: #455a64; min-width: 52px; transition: background 0.1s, color 0.1s, border-color 0.1s; white-space: nowrap; }}
                .date-btn:hover {{ background: #f5f5f5; }}
                .date-btn.active {{ background: #1a73e8; color: white; border-color: #1a73e8; font-weight: bold; }}
                .date-btn.hover-during-drag {{ background: #bbdefb; border-color: #1a73e8; }}
                .time-hint {{ font-size: 0.75em; color: #90a4ae; margin: 6px 0 0 0; text-align: right; }}

                @media (max-width: 600px) {{
                    body {{ padding: 10px; }}
                    .csv-btn {{ margin-left: 0; }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <p style="margin: 0 0 12px 0;">{back_link}</p>
                <h1 style="margin: 8px 0; color: #1a237e; display:inline-block;">{name_safe}</h1>{edit_name_ui}
                <p style="color: #607d8b; margin: 0 0 16px 0;">{location_text} · {sn}</p>

                {realtime_section}

                <div class="time-bar">
                    <div class="datetime-row">
                        <label>시작 <input type="datetime-local" id="dt-start" step="300" value="{default_start}"></label>
                        <span class="time-sep">~</span>
                        <label>종료 <input type="datetime-local" id="dt-end" step="300" value="{default_end}"></label>
                        <a id="csv-link" href="/report?date={today}&{csv_query}" class="csv-btn">📊 CSV 다운로드</a>
                    </div>
                    <div class="quick-row">
                        <button class="quick-btn" data-quick="today">오늘</button>
                        <button class="quick-btn" data-quick="yesterday">어제</button>
                        <button class="quick-btn" data-quick="last-night">지난 밤 (전날 18시 ~ 당일 12시)</button>
                        <button class="quick-btn" data-quick="last-24h">최근 24시간</button>
                    </div>
                    <div class="date-buttons" id="date-buttons"></div>
                    <p class="time-hint">🕒 5분 단위 자동 스냅 · 날짜 버튼: 그 날 00:00~23:55 자동 · 드래그도 가능</p>
                </div>

                <div id="data-status" style="text-align:center; color:#90a4ae; padding:20px;">로딩 중...</div>

                <div id="blocks-container">{blocks_html}
                </div>

                <p style="text-align:center; margin-top:30px;">
                    <a href="/help" style="display:inline-block; padding:10px 20px; background:#27ae60; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">📘 사용 가이드</a>
                    <a href="/feedback" style="display:inline-block; padding:10px 20px; background:#e67e22; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">💬 의견 보내기</a>
                </p>
            </div>
            <script>
                const sn = "{sn}";
                const token = "{token}";
                const authQS = "{auth_qs}";
                // 쿠키가 안 실려도 인증되도록 모든 API 요청에 토큰을 붙인다.
                function apiUrl(path) {{
                    if (!authQS) return path;
                    return path + (path.indexOf('?') >= 0 ? '&' : '?') + authQS;
                }}
                const charts = {{}};
                // 이 기기에 실제로 있는 블록만 그린다 (기기 종류마다 다름)
                const BLOCK_IDS = {block_ids_json};
                const CHART_KEYS = ['hr', 'rr', 'act', 'temp', 'posture'].filter(k => BLOCK_IDS.includes(k));
                const CHART_COLORS = {{ hr: '#e74c3c', rr: '#3498db', act: '#f39c12',
                                        temp: '#e2703f', posture: '#7e57c2' }};
                // 자세는 숫자 코드로 오므로 눈금에 한글 라벨을 붙인다
                const POSTURE_LABELS = {{ '-1': '감지대기', '0': '누움', '1': '뒤척임', '2': '앉음',
                                          '3': '걸터앉음', '4': '낙상', '5': '자리비움', '6': '배회' }};
                const AVAILABLE_DATES = {available_json};

                let lastPoints = [];
                let lastSummaries = [];
                let lastIntervals = [];
                let lastDaily = [];
                let lastBands = [];
                let lastBandSecs = {{}};
                let lastBandCounts = {{}};

                // ─ 5분 단위 강제 스냅 ─
                function snapTo5Min(dtLocal) {{
                    if (!dtLocal) return dtLocal;
                    const parts = dtLocal.split('T');
                    if (parts.length !== 2) return dtLocal;
                    const date = parts[0];
                    const tParts = parts[1].split(':');
                    if (tParts.length < 2) return dtLocal;
                    let h = parseInt(tParts[0], 10);
                    let m = parseInt(tParts[1], 10);
                    if (isNaN(h) || isNaN(m)) return dtLocal;
                    m = Math.round(m / 5) * 5;
                    if (m >= 60) {{ m -= 60; h += 1; }}
                    if (h >= 24) {{ h = 23; m = 55; }}
                    return date + 'T' + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
                }}

                function destroyCharts() {{
                    for (const id of Object.keys(charts)) {{
                        if (charts[id]) {{ charts[id].destroy(); charts[id] = null; }}
                    }}
                }}

                // ─ 차트: epoch(초) 기반 linear x축 ─
                function renderChart(key) {{
                    const id = 'chart-' + key;
                    if (charts[id]) {{ charts[id].destroy(); charts[id] = null; }}
                    const ctx = document.getElementById(id);
                    if (!ctx) return;

                    // 블록 id 와 데이터 필드 이름이 다른 경우 (자세 블록은 숫자 코드 pos 를 쓴다)
                    const field = (key === 'posture') ? 'pos' : key;

                    // {{x: epoch_sec, y: value}}. 5분 이상 갭이면 중간에 null 점 삽입해서 line 끊기.
                    const data = [];
                    let prevEpoch = null;
                    for (const p of lastPoints) {{
                        const v = p[field];
                        if (v === null || v === undefined) continue;
                        if (prevEpoch !== null && p.epoch - prevEpoch > 5 * 60) {{
                            data.push({{ x: prevEpoch + 1, y: null }});
                        }}
                        data.push({{ x: p.epoch, y: v }});
                        prevEpoch = p.epoch;
                    }}
                    const validData = data.filter(d => d.y !== null);
                    if (validData.length === 0) return;

                    const xMin = validData[0].x;
                    const xMax = validData[validData.length - 1].x;

                    // 캔버스 너비 — 분당 5px (시간 길이 비례). 최소 부모 너비.
                    const wrap = document.getElementById('wrap-' + key);
                    if (wrap) {{
                        const minutes = Math.max(30, Math.round((xMax - xMin) / 60));
                        const desired = Math.max(wrap.parentElement.clientWidth, minutes * 5);
                        wrap.style.width = desired + 'px';
                    }}

                    // 자정 가로지름 여부 (라벨 포맷용)
                    const dStart = new Date(xMin * 1000);
                    const dEnd = new Date(xMax * 1000);
                    const crossesMidnight = (dStart.toDateString() !== dEnd.toDateString());

                    // 30분 단위 ticks 균등 생성
                    const STEP = 30 * 60;
                    const firstTick = Math.ceil(xMin / STEP) * STEP;
                    const tickArr = [];
                    for (let t = firstTick; t <= xMax; t += STEP) tickArr.push(t);

                    function fmtLabel(epochSec) {{
                        const d = new Date(epochSec * 1000);
                        const h = String(d.getHours()).padStart(2, '0');
                        const m = String(d.getMinutes()).padStart(2, '0');
                        if (crossesMidnight && h === '00' && m === '00') {{
                            return (d.getMonth() + 1) + '/' + d.getDate();
                        }}
                        return h + ':' + m;
                    }}

                    const color = CHART_COLORS[key] || '#888';
                    // 자세는 연속값이 아니라 상태 코드라 계단식으로 그리고 곡선 보간을 끈다
                    const isPosture = (key === 'posture');
                    charts[id] = new Chart(ctx, {{
                        type: 'line',
                        data: {{
                            datasets: [{{
                                data,
                                borderColor: color,
                                backgroundColor: color + '22',
                                tension: isPosture ? 0 : 0.2,
                                stepped: isPosture ? 'before' : false,
                                pointRadius: 0,
                                spanGaps: false,
                                fill: !isPosture,
                            }}]
                        }},
                        options: {{
                            responsive: true,
                            maintainAspectRatio: false,
                            parsing: false,
                            scales: {{
                                x: {{
                                    type: 'linear',
                                    min: xMin,
                                    max: xMax,
                                    grid: {{ display: false }},
                                    ticks: {{
                                        autoSkip: false,
                                        maxRotation: 0,
                                        callback: (value) => fmtLabel(value),
                                    }},
                                    afterBuildTicks: (axis) => {{
                                        axis.ticks = tickArr.map(v => ({{ value: v }}));
                                    }},
                                }},
                                y: isPosture ? {{
                                    beginAtZero: false,
                                    ticks: {{ callback: (v) => POSTURE_LABELS[String(v)] || '' }},
                                }} : {{ beginAtZero: false }},
                            }},
                            plugins: {{
                                legend: {{ display: false }},
                                tooltip: isPosture ? {{ callbacks: {{
                                    label: (c) => POSTURE_LABELS[String(c.parsed.y)] || c.parsed.y
                                }} }} : {{}},
                            }}
                        }}
                    }});
                }}

                // ─ '얼마나 오래' 통계 (재실 시간 / 자세별 시간 / 구간 재실) ─
                // 셋 다 "상태가 이어진 구간"이라 같은 틀로 그린다.
                const BAND_COLORS = {{
                    '재실': '#1a73e8', '이탈': '#cfd8dc', '부재': '#cfd8dc',
                    '누움': '#3f7fd6', '오래누움': '#3f7fd6', '뒤척임': '#3fa88f',
                    '앉음': '#e2b52f', '걸터앉음': '#df6b3b', '배회': '#e8933c',
                    '낙상': '#d6455a', '자리비움': '#b0bec5', '감지 대기': '#cfd8dc',
                }};
                // 이 상태들은 '있는 시간'이 아니라 '없는 시간' — 합계에서 뺀다
                const BAND_AWAY = ['이탈', '부재', '자리비움', '감지 대기'];

                function renderBands(blockId) {{
                    const bar = document.getElementById(blockId + '-bar');
                    if (!bar) return;
                    const ticks = document.getElementById(blockId + '-ticks');
                    const legend = document.getElementById(blockId + '-legend');
                    const totalEl = document.getElementById(blockId + '-total');
                    const x0 = new Date(document.getElementById('dt-start').value).getTime() / 1000;
                    const x1 = new Date(document.getElementById('dt-end').value).getTime() / 1000;
                    const span = Math.max(1, x1 - x0);

                    if (!lastBands.length) {{
                        totalEl.innerHTML = '<span style="color:#90a4ae;">이 기간에 기록이 없습니다.</span>';
                        bar.innerHTML = ''; ticks.innerHTML = ''; legend.innerHTML = '';
                        return;
                    }}

                    bar.innerHTML = lastBands.map(b => {{
                        const l = ((b.start - x0) / span) * 100;
                        const w = ((b.end - b.start) / span) * 100;
                        const c = BAND_COLORS[b.label] || '#90a4ae';
                        const tip = `${{b.label}} · ${{fmtClock(b.start)}}~${{fmtClock(b.end)}} (${{fmtDur(b.end - b.start)}})`;
                        return `<i style="left:${{Math.max(0, l)}}%; width:${{Math.max(0.2, w)}}%; background:${{c}}" title="${{tip}}"></i>`;
                    }}).join('');

                    // 눈금
                    const stepH = (span / 3600) <= 8 ? 1 : ((span / 3600) <= 26 ? 3 : 12);
                    let t = Math.ceil(x0 / (stepH * 3600)) * (stepH * 3600);
                    let th = '';
                    for (; t <= x1; t += stepH * 3600) {{
                        th += `<span style="left:${{((t - x0) / span) * 100}}%">${{fmtClock(t)}}</span>`;
                    }}
                    ticks.innerHTML = th;

                    // 항목별 시간 — 긴 것부터
                    const rows = Object.entries(lastBandSecs).sort((a, b) => b[1] - a[1]);
                    const grand = rows.reduce((a, r) => a + r[1], 0) || 1;
                    legend.innerHTML = rows.map(([label, sec]) => {{
                        const pct = (sec / grand) * 100;
                        const c = BAND_COLORS[label] || '#90a4ae';
                        const n = lastBandCounts[label] || 0;
                        return `<div class="band-row">
                            <span class="bl"><i style="background:${{c}}"></i>${{label}}</span>
                            <span class="bt"><i style="width:${{pct}}%; background:${{c}}"></i></span>
                            <span class="bv">${{fmtDur(sec)}}</span>
                            <span class="bp">${{pct.toFixed(0)}}%</span>
                            <span class="bn">${{n}}회</span>
                        </div>`;
                    }}).join('');

                    // 머리말 — '있는 시간'만 합쳐서 보여준다
                    const present = rows.filter(r => !BAND_AWAY.includes(r[0]))
                                        .reduce((a, r) => a + r[1], 0);
                    const awayCnt = rows.filter(r => BAND_AWAY.includes(r[0]))
                                        .reduce((a, r) => a + (lastBandCounts[r[0]] || 0), 0);
                    const label = (blockId === 'occupancy') ? '침대 재실'
                                : (blockId === 'presence') ? '재실' : '측정된 시간';
                    totalEl.innerHTML = `${{label}} <b>${{fmtDur(present)}}</b>`
                        + (awayCnt ? ` · 자리 비움 ${{awayCnt}}회` : '');
                }}

                // ─ 사용 구간 띠 ─
                // Chart.js 대신 직접 그린다. 단순한 가로 띠라 라이브러리보다 가볍고
                // 시간 축을 창 범위에 정확히 맞추기도 쉽다.
                function fmtClock(epochSec) {{
                    const d = new Date(epochSec * 1000);
                    return String(d.getHours()).padStart(2, '0') + ':' +
                           String(d.getMinutes()).padStart(2, '0');
                }}
                function fmtDur(sec) {{
                    sec = Math.round(sec);
                    if (sec < 60) return sec + '초';
                    const m = Math.round(sec / 60);
                    if (m < 60) return m + '분';
                    return Math.floor(m / 60) + '시간 ' + (m % 60) + '분';
                }}
                function renderUsage() {{
                    const bar = document.getElementById('usage-timeline');
                    const ticks = document.getElementById('usage-ticks');
                    const sum = document.getElementById('usage-summary');
                    const list = document.getElementById('usage-list');
                    if (!bar) return;
                    const sEl = document.getElementById('dt-start');
                    const eEl = document.getElementById('dt-end');
                    const x0 = new Date(sEl.value).getTime() / 1000;
                    const x1 = new Date(eEl.value).getTime() / 1000;
                    const span = Math.max(1, x1 - x0);

                    const total = lastIntervals.reduce((a, i) => a + (i.end - i.start), 0);
                    const pct = Math.min(100, (total / span) * 100);
                    sum.innerHTML = lastIntervals.length
                        ? `이 기간에 <b>${{fmtDur(total)}}</b> 사용 · ${{lastIntervals.length}}회 · 기간의 ${{pct.toFixed(1)}}%`
                        : '<span style="color:#90a4ae;">이 기간에 사용 기록이 없습니다.</span>';

                    bar.innerHTML = lastIntervals.map(i => {{
                        const l = ((i.start - x0) / span) * 100;
                        const w = ((i.end - i.start) / span) * 100;
                        const cls = i.ongoing ? 'seg ongoing' : 'seg';
                        const tip = `${{fmtClock(i.start)}} ~ ${{i.ongoing ? '사용 중' : fmtClock(i.end)}} (${{fmtDur(i.end - i.start)}})`;
                        return `<div class="${{cls}}" style="left:${{Math.max(0, l)}}%; width:${{Math.max(0.4, w)}}%" title="${{tip}}"></div>`;
                    }}).join('');

                    // 눈금 — 범위 길이에 따라 간격 자동
                    const hours = span / 3600;
                    const stepH = hours <= 8 ? 1 : (hours <= 26 ? 3 : 12);
                    const step = stepH * 3600;
                    let t = Math.ceil(x0 / step) * step;
                    let th = '';
                    for (; t <= x1; t += step) {{
                        const l = ((t - x0) / span) * 100;
                        th += `<span style="left:${{l}}%">${{fmtClock(t)}}</span>`;
                    }}
                    ticks.innerHTML = th;

                    list.innerHTML = lastIntervals.length
                        ? lastIntervals.slice(-8).reverse().map(i =>
                            `<div class="usage-row"><span>${{fmtClock(i.start)}} ~ ${{i.ongoing ? '<b>사용 중</b>' : fmtClock(i.end)}}</span>`
                            + `<span class="dur">${{fmtDur(i.end - i.start)}}</span></div>`).join('')
                        : '';
                }}

                // ─ 일별 사용시간 ─
                function renderDaily() {{
                    const cv = document.getElementById('chart-daily');
                    if (!cv) return;
                    if (charts.daily) {{ charts.daily.destroy(); delete charts.daily; }}
                    if (!lastDaily.length) return;
                    const labels = lastDaily.map(d => d.date.slice(5).replace('-', '/'));
                    const hours = lastDaily.map(d => +(d.seconds / 3600).toFixed(2));
                    charts.daily = new Chart(cv.getContext('2d'), {{
                        type: 'bar',
                        data: {{ labels, datasets: [{{
                            data: hours, backgroundColor: '#00897b', borderRadius: 4,
                        }}] }},
                        options: {{
                            responsive: true, maintainAspectRatio: false,
                            scales: {{
                                y: {{ beginAtZero: true, title: {{ display: true, text: '시간' }} }},
                                x: {{ grid: {{ display: false }} }},
                            }},
                            plugins: {{
                                legend: {{ display: false }},
                                tooltip: {{ callbacks: {{
                                    label: (c) => fmtDur(lastDaily[c.dataIndex].seconds)
                                }} }},
                            }},
                        }}
                    }});
                }}

                function renderSummary() {{
                    const el = document.getElementById('summary-area');
                    if (!lastSummaries || lastSummaries.length === 0) {{
                        el.innerHTML = '<p style="color:#90a4ae; text-align:center; margin: 0; font-size:0.9em;">선택 범위에 수면 요약 데이터가 없습니다.</p>';
                        return;
                    }}
                    const fmt = (k, v) => k.includes('(분)') ? Number(v).toFixed(0) : v;
                    const sections = lastSummaries.map((s, idx) => {{
                        const endT = s.__end__ || '';
                        const endD = s.__date__ || '';
                        const items = Object.entries(s)
                            .filter(([k]) => !k.startsWith('__'))
                            .map(([k, v]) =>
                                `<div class="summary-item"><div class="value">${{fmt(k, v)}}</div><div class="label">${{k}}</div></div>`
                            ).join('');
                        let title = '';
                        const endLabel = endD ? (endD.slice(5).replace('-', '/') + ' ' + endT) : endT;
                        if (lastSummaries.length > 1) {{
                            title = `수면 #${{idx + 1}}` + (endLabel ? ` — ~${{endLabel}} 종료` : '');
                        }} else if (endLabel) {{
                            title = `~${{endLabel}} 종료`;
                        }}
                        const titleHtml = title ? `<p class="summary-section-title">${{title}}</p>` : '';
                        return `<div class="summary-section">${{titleHtml}}<div class="summary-grid">${{items}}</div></div>`;
                    }}).join('');
                    el.innerHTML = sections;
                }}

                // ─ 데이터 로드 ─
                async function loadFromInputs() {{
                    const startEl = document.getElementById('dt-start');
                    const endEl = document.getElementById('dt-end');
                    // 5분 강제 스냅 (입력 즉시 보정)
                    startEl.value = snapTo5Min(startEl.value);
                    endEl.value = snapTo5Min(endEl.value);
                    const sdt = startEl.value;
                    const edt = endEl.value;
                    if (!sdt || !edt) return;
                    if (sdt >= edt) {{
                        document.getElementById('data-status').textContent = '시작 시각이 종료 시각보다 이전이어야 합니다.';
                        return;
                    }}
                    document.getElementById('data-status').textContent = '로딩 중...';
                    // CSV 링크 — 시작 datetime의 날짜 기준 (CSV는 단일 날짜)
                    const csvDate = sdt.split('T')[0];
                    document.getElementById('csv-link').href = `/report?date=${{csvDate}}&{csv_query}`;
                    // 빠른 버튼/날짜 버튼 활성 표시 갱신
                    refreshActiveStates(sdt, edt);
                    try {{
                        const url = `/api/device/${{sn}}/timeseries?start_dt=${{encodeURIComponent(sdt)}}&end_dt=${{encodeURIComponent(edt)}}`;
                        const r = await fetch(apiUrl(url));
                        if (!r.ok) {{
                            destroyCharts();
                            const el = document.getElementById('data-status');
                            if (r.status === 401 || r.status === 403) {{
                                el.textContent = '⚠️ 접속 인증이 만료됐어요. 받으신 링크를 다시 한 번 열어주세요.';
                            }} else if (r.status === 503) {{
                                el.textContent = '🛠️ 서버 점검 중이에요. 잠시 후 자동으로 다시 시도합니다…';
                                setTimeout(loadFromInputs, 6000);
                            }} else {{
                                el.textContent = `데이터를 불러오지 못했어요 (오류 ${{r.status}}). 잠시 후 다시 시도해주세요.`;
                            }}
                            return;
                        }}
                        const d = await r.json();
                        lastSummaries = d.summaries || [];
                        lastPoints = d.points || [];
                        lastIntervals = d.fsr_intervals || [];
                        lastDaily = d.fsr_daily || [];
                        lastBands = d.bands || [];
                        lastBandSecs = d.band_seconds || {{}};
                        lastBandCounts = d.band_counts || {{}};
                        if (BLOCK_IDS.includes('summary')) renderSummary();
                        if (BLOCK_IDS.includes('usage')) renderUsage();
                        if (BLOCK_IDS.includes('daily')) renderDaily();
                        for (const b of ['occupancy', 'postures', 'presence']) {{
                            if (BLOCK_IDS.includes(b)) renderBands(b);
                        }}

                        // 사용감지 기기는 '측정값 개수'가 아니라 사용 횟수로 안내한다
                        const statusEl = document.getElementById('data-status');
                        if (BLOCK_IDS.includes('usage')) {{
                            const total = lastIntervals.reduce((a, i) => a + (i.end - i.start), 0);
                            statusEl.textContent = lastIntervals.length
                                ? `${{lastIntervals.length}}회 사용 · 총 ${{fmtDur(total)}}`
                                : '선택 범위에 사용 기록이 없습니다.';
                            return;
                        }}
                        if (lastPoints.length === 0) {{
                            statusEl.textContent = '선택 범위에 데이터가 없습니다.';
                            destroyCharts();
                            return;
                        }}
                        statusEl.textContent = `총 ${{lastPoints.length}}개 측정값`;
                        for (const k of CHART_KEYS) renderChart(k);
                    }} catch (e) {{
                        document.getElementById('data-status').textContent = '오류: ' + e.message;
                    }}
                }}

                function refreshActiveStates(sdt, edt) {{
                    // 날짜 버튼 — 시작날짜 == 종료날짜 + 시작 00:00 + 종료 23:55 일 때만 active
                    const sd = sdt.split('T')[0];
                    const ed = edt.split('T')[0];
                    const isSingleDay = (sd === ed && sdt.endsWith('T00:00') && edt.endsWith('T23:55'));
                    document.querySelectorAll('.date-btn').forEach(b => {{
                        b.classList.toggle('active', isSingleDay && b.dataset.date === sd);
                    }});
                    // 빠른 버튼은 명시적 클릭 외에 자동 매칭은 안 함 (사용자 임의 입력 시 노이즈 방지)
                    document.querySelectorAll('.quick-btn').forEach(b => b.classList.remove('active'));
                }}

                // ─ 빠른 범위 버튼 ─
                function fmtLocal(d) {{
                    const yy = d.getFullYear();
                    const mm = String(d.getMonth() + 1).padStart(2, '0');
                    const dd = String(d.getDate()).padStart(2, '0');
                    const hh = String(d.getHours()).padStart(2, '0');
                    const mn = String(d.getMinutes()).padStart(2, '0');
                    return yy + '-' + mm + '-' + dd + 'T' + hh + ':' + mn;
                }}
                function applyQuickRange(name, btn) {{
                    const now = new Date();
                    let start, end;
                    if (name === 'today') {{
                        start = new Date(now); start.setHours(0, 0, 0, 0);
                        end = new Date(now); end.setHours(23, 55, 0, 0);
                    }} else if (name === 'yesterday') {{
                        start = new Date(now); start.setDate(start.getDate() - 1); start.setHours(0, 0, 0, 0);
                        end = new Date(start); end.setHours(23, 55, 0, 0);
                    }} else if (name === 'last-night') {{
                        start = new Date(now); start.setDate(start.getDate() - 1); start.setHours(18, 0, 0, 0);
                        end = new Date(now); end.setHours(12, 0, 0, 0);
                    }} else if (name === 'last-24h') {{
                        end = new Date(now);
                        end.setSeconds(0); end.setMilliseconds(0);
                        end.setMinutes(Math.floor(end.getMinutes() / 5) * 5);
                        start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
                    }} else {{ return; }}
                    document.getElementById('dt-start').value = fmtLocal(start);
                    document.getElementById('dt-end').value = fmtLocal(end);
                    loadFromInputs();
                    if (btn) {{
                        document.querySelectorAll('.quick-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                    }}
                }}
                function setupQuickButtons() {{
                    document.querySelectorAll('.quick-btn').forEach(btn => {{
                        btn.addEventListener('click', () => applyQuickRange(btn.dataset.quick, btn));
                    }});
                }}

                // ─ 메인 datetime input 이벤트 ─
                function setupDatetimeInputs() {{
                    document.getElementById('dt-start').addEventListener('change', loadFromInputs);
                    document.getElementById('dt-end').addEventListener('change', loadFromInputs);
                }}

                // ─ 블록 순서 변경: 드래그 + ▲/▼ 버튼 ─
                async function applySavedOrder() {{
                    try {{
                        const r = await fetch(apiUrl('/api/preferences/order'));
                        if (!r.ok) return;
                        const d = await r.json();
                        const container = document.getElementById('blocks-container');
                        for (const blockId of (d.order || [])) {{
                            const el = container.querySelector(`[data-block-id="${{blockId}}"]`);
                            if (el) container.appendChild(el);
                        }}
                    }} catch (e) {{}}
                }}
                async function saveOrder() {{
                    const container = document.getElementById('blocks-container');
                    const order = Array.from(container.children).map(el => el.dataset.blockId).filter(Boolean);
                    try {{
                        await fetch(apiUrl('/api/preferences/order'), {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ order }}),
                        }});
                    }} catch (e) {{}}
                }}
                function setupSortable() {{
                    const container = document.getElementById('blocks-container');
                    new Sortable(container, {{
                        handle: '.drag-handle',
                        animation: 150,
                        ghostClass: 'dragging',
                        onEnd: () => {{ updateArrowDisabled(); saveOrder(); }},
                    }});
                }}
                function updateArrowDisabled() {{
                    const container = document.getElementById('blocks-container');
                    const cards = Array.from(container.children);
                    cards.forEach((card, i) => {{
                        const upBtn = card.querySelector('button[data-arrow="up"]');
                        const downBtn = card.querySelector('button[data-arrow="down"]');
                        if (upBtn) upBtn.disabled = (i === 0);
                        if (downBtn) downBtn.disabled = (i === cards.length - 1);
                    }});
                }}
                function setupArrowButtons() {{
                    document.querySelectorAll('button[data-arrow]').forEach(btn => {{
                        btn.addEventListener('click', () => {{
                            const card = btn.closest('.block-card');
                            if (!card) return;
                            const direction = btn.dataset.arrow;
                            const container = document.getElementById('blocks-container');
                            if (direction === 'up') {{
                                const prev = card.previousElementSibling;
                                if (prev) container.insertBefore(card, prev);
                            }} else {{
                                const next = card.nextElementSibling;
                                if (next) container.insertBefore(next, card);
                            }}
                            updateArrowDisabled();
                            saveOrder();
                        }});
                    }});
                }}

                // ─ 날짜 버튼 (단일 날짜 빠른 선택 — 그 날 00:00~23:55) ─
                function selectDate(date) {{
                    if (!date) return;
                    document.getElementById('dt-start').value = date + 'T00:00';
                    document.getElementById('dt-end').value = date + 'T23:55';
                    loadFromInputs();
                }}
                function setupDateButtons() {{
                    const wrap = document.getElementById('date-buttons');
                    wrap.innerHTML = '';
                    if (!AVAILABLE_DATES || AVAILABLE_DATES.length === 0) {{
                        wrap.innerHTML = '<span style="color:#90a4ae; font-size:0.85em;">데이터가 있는 날짜가 없습니다.</span>';
                        return;
                    }}
                    for (const d of AVAILABLE_DATES) {{
                        const btn = document.createElement('button');
                        btn.className = 'date-btn';
                        btn.dataset.date = d;
                        const parts = d.split('-');
                        btn.textContent = parseInt(parts[1], 10) + '/' + parseInt(parts[2], 10);
                        btn.title = d;
                        btn.addEventListener('click', () => selectDate(d));
                        wrap.appendChild(btn);
                    }}

                    // 드래그 선택 (마우스/터치)
                    let isDragging = false;
                    let lastDate = null;
                    function clearDragHover() {{
                        wrap.querySelectorAll('.date-btn').forEach(b => b.classList.remove('hover-during-drag'));
                    }}
                    function setDragHover(el) {{
                        if (!el || !el.classList.contains('date-btn')) return;
                        clearDragHover();
                        el.classList.add('hover-during-drag');
                        lastDate = el.dataset.date;
                    }}
                    wrap.addEventListener('mousedown', (e) => {{
                        if (e.target.classList && e.target.classList.contains('date-btn')) {{
                            isDragging = true;
                            lastDate = e.target.dataset.date;
                        }}
                    }});
                    wrap.addEventListener('mousemove', (e) => {{
                        if (!isDragging) return;
                        if (e.target.classList && e.target.classList.contains('date-btn')) {{
                            setDragHover(e.target);
                        }}
                    }});
                    document.addEventListener('mouseup', () => {{
                        if (!isDragging) return;
                        isDragging = false;
                        clearDragHover();
                        if (lastDate) selectDate(lastDate);
                        lastDate = null;
                    }});
                    wrap.addEventListener('touchstart', (e) => {{
                        if (!e.touches || e.touches.length === 0) return;
                        const t = e.touches[0];
                        const el = document.elementFromPoint(t.clientX, t.clientY);
                        if (el && el.classList && el.classList.contains('date-btn') && wrap.contains(el)) {{
                            isDragging = true;
                            lastDate = el.dataset.date;
                        }}
                    }}, {{ passive: true }});
                    wrap.addEventListener('touchmove', (e) => {{
                        if (!isDragging) return;
                        if (!e.touches || e.touches.length === 0) return;
                        const t = e.touches[0];
                        const el = document.elementFromPoint(t.clientX, t.clientY);
                        if (el && el.classList && el.classList.contains('date-btn') && wrap.contains(el)) {{
                            setDragHover(el);
                        }}
                    }}, {{ passive: true }});
                    wrap.addEventListener('touchend', () => {{
                        if (!isDragging) return;
                        isDragging = false;
                        clearDragHover();
                        if (lastDate) selectDate(lastDate);
                        lastDate = null;
                    }});
                }}

                // ─ 초기화 ─
                (async function init() {{
                    await applySavedOrder();
                    setupSortable();
                    setupArrowButtons();
                    updateArrowDisabled();
                    setupDatetimeInputs();
                    setupQuickButtons();
                    setupDateButtons();
                    loadFromInputs();
                }})();

                async function refreshStatusCard() {{
                    try {{
                        const r = await fetch(apiUrl(`/api/device/${{sn}}/card`));
                        if (r.ok) {{
                            document.getElementById('status-card').innerHTML = await r.text();
                        }}
                    }} catch (e) {{}}
                }}
                if ({realtime_js}) {{
                    setInterval(refreshStatusCard, 15000);
                    document.addEventListener('visibilitychange', () => {{
                        if (!document.hidden) refreshStatusCard();
                    }});
                }}
            </script>
        </body>
    </html>
    """


# 원본(raw) 대시보드 — 기존 디버깅용 뷰 (관리자 전용)
def _tail_lines(path, n=5, chunk=64 * 1024):
    """파일 끝에서부터 마지막 n줄만 읽는다 (오래된 → 최신 순으로 반환).

    86MB 짜리 로그를 통째로 메모리에 올리지 않으려고 뒤에서부터 조금씩 읽는다.
    파일이 아무리 커도 읽는 양은 마지막 몇 KB 뿐이다."""
    if not os.path.exists(path):
        return []
    out = b""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            while end > 0 and out.count(b"\n") <= n:
                step = min(chunk, end)
                end -= step
                f.seek(end)
                out = f.read(step) + out
    except Exception:
        return []
    lines = [l.decode("utf-8", errors="replace").strip()
             for l in out.split(b"\n") if l.strip()]
    return lines[-n:]


@app.get("/dashboard/raw", response_class=HTMLResponse)
async def view_dashboard_raw(request: Request, file: str = Query(None),
                             n: int = Query(5), _: str = Depends(require_admin)):
    """수신 원본 보기 — 파일을 골라 마지막 몇 줄을 확인한다.

    ⚠️ 파일을 통째로 읽지 않는다. emfit_data.jsonl 이 86MB 라 예전 방식(readlines)은
    페이지를 열 때마다 그만큼을 메모리에 올려서 느렸다. 끝에서부터 필요한 만큼만 읽는다."""
    admin_token = _get_token_from_request(request) or ""
    n = max(1, min(int(n or 5), 50))

    # 볼 수 있는 파일 — 측정 로그 + 이미지 목록. 경로를 밖에서 받지 않으므로 임의 파일 열람은 불가.
    viewable = list(DATA_FILES) + [MCKARE_IMAGE_LOG]
    labels = {
        LOG_FILE: "EMFIT QS", RADAR_LOG_FILE: "AI Radar",
        MCKARE_LOG_FILE: "McKare", FSR_LOG_FILE: "사용감지",
        MCKARE_IMAGE_LOG: "McKare 이미지 목록",
    }
    if file not in viewable:
        # 기본값 — 가장 최근에 갱신된 파일
        existing = [p for p in viewable if os.path.exists(p)]
        file = max(existing, key=os.path.getmtime) if existing else viewable[0]

    # 파일별 요약 (크기·최종 갱신) — stat 만 보므로 크기와 무관하게 즉시
    tabs = ""
    for p in viewable:
        exists = os.path.exists(p)
        size = os.path.getsize(p) if exists else 0
        mt = (datetime.fromtimestamp(os.path.getmtime(p), KST).strftime("%m-%d %H:%M")
              if exists else "-")
        on = (p == file)
        size_txt = (f"{size/1024/1024:.1f}MB" if size >= 1024*1024
                    else f"{size/1024:.0f}KB" if size else "없음")
        tabs += (
            f'<a href="/dashboard/raw?file={quote(p)}&n={n}" '
            f'style="display:inline-block; padding:9px 14px; margin:3px; border-radius:9px;'
            f' text-decoration:none; font-size:0.88em;'
            f' background:{"#1a73e8" if on else "#fff"}; color:{"#fff" if on else "#546e7a"};'
            f' border:1px solid {"transparent" if on else "#cfd8dc"};">'
            f'<b>{html.escape(labels.get(p, p))}</b>'
            f'<span style="opacity:0.75; font-size:0.85em;"> · {size_txt} · {mt}</span></a>')

    lines = _tail_lines(file, n)
    blocks = ""
    for ln in reversed(lines):                 # 최신이 위로
        try:
            body = json.dumps(json.loads(ln), indent=4, ensure_ascii=False)
        except Exception:
            body = ln
        blocks += (f'<pre style="background:#202124; color:#00ff9c; padding:16px; border-radius:10px;'
                   f' font-size:0.86em; line-height:1.5; white-space:pre-wrap; word-wrap:break-word;'
                   f' overflow-x:hidden; margin:0 0 12px;">{html.escape(body)}</pre>')
    if not blocks:
        blocks = ('<p style="text-align:center; color:#90a4ae; padding:30px;">'
                  '이 파일에는 아직 수신된 데이터가 없습니다.</p>')

    size = os.path.getsize(file) if os.path.exists(file) else 0
    n_opts = "".join(
        f'<a href="/dashboard/raw?file={quote(file)}&n={k}" style="display:inline-block;'
        f' padding:5px 12px; margin:2px; border-radius:7px; text-decoration:none; font-size:0.82em;'
        f' background:{"#e8f0fe" if k == n else "#fff"}; color:#1a73e8;'
        f' border:1px solid {"#1a73e8" if k == n else "#cfd8dc"};">{k}줄</a>'
        for k in (1, 5, 20, 50))
    file_label = html.escape(labels.get(file, file))
    size_label = f"{size/1024/1024:.2f}MB" if size >= 1024*1024 else f"{size/1024:.1f}KB"

    return f"""
    <html>
        <head>
            <title>수신 원본 데이터 · 돌봄기기 통합 대시보드</title>
            <meta http-equiv="refresh" content="15">
        </head>
        <body style="font-family: 'Malgun Gothic', sans-serif; padding:30px; background:#f0f2f5; line-height:1.6;">
            <div style="max-width:800px; margin:auto; background:white; padding:30px; border-radius:20px; box-shadow:0 10px 25px rgba(0,0,0,0.1);">
                <h1 style="text-align:center; color:#1a73e8; margin-bottom:6px;">🔎 수신 원본 데이터</h1>
                <p style="text-align:center; color:#7f8c8d; font-size:0.9em; margin-bottom:18px;">서버 시간: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}</p>

                <div style="text-align:center; margin-bottom:14px;">{tabs}</div>

                <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:12px; padding:12px 14px; background:#e8f0fe; border-radius:10px;">
                    <span style="color:#1a237e;"><b>{file_label}</b>
                        <span style="color:#5c6bc0; font-size:0.88em;"> · {size_label} · {file}</span></span>
                    <span>{n_opts}</span>
                </div>

                {blocks}

                <p style="text-align:center; margin-top:22px;">
                    <a href="/dashboard" style="color:#1a73e8;">← 관제 화면으로</a>
                </p>
            </div>
        </body>
    </html>
    """

# 리포트 다운로드 UI (관리자 전용 — 모든 기기 선택 가능)
@app.get("/reports", response_class=HTMLResponse)
def reports_ui(request: Request, _: str = Depends(require_admin)):
    admin_token = _get_token_from_request(request) or ""
    if not _has_data():
        return HTMLResponse("<p>데이터 파일이 없습니다.</p>", status_code=404)

    try:
        available = analyzer.list_available_assignments(DATA_FILES)
    except Exception as e:
        return HTMLResponse(f"<p>분석 로드 실패: {e}</p>", status_code=500)

    dates = sorted({d for _, d in available}, reverse=True)
    aids_with_data = {a for a, _ in available}

    min_date = dates[-1] if dates else ""
    max_date = dates[0] if dates else ""

    # 사람(배정 기간) 단위 옵션 — 데이터가 실제로 있는 배정만 노출
    stints = sorted(analyzer.list_assignments(),
                    key=lambda x: (x.get("sn", ""), x.get("start") or ""))
    device_options_html = ""
    for a in stints:
        if a.get("id") not in aids_with_data:
            continue
        start_lbl = (a.get("start") or "처음")[:16]
        end_lbl = (a.get("end") or "현재")[:16]
        mark = " ✅사용중" if not a.get("end") else ""
        label = f"{a.get('location', '-')} · {a.get('user', '?')}  ({start_lbl} ~ {end_lbl}){mark}"
        device_options_html += (
            f'<option value="{html.escape(a.get("id", ""))}">'
            f'{html.escape(label)}</option>\n'
        )
    if not device_options_html:
        device_options_html = '<option value="">— 데이터 없음 —</option>'

    return f"""
    <html>
        <head>
            <title>리포트 다운로드 · 돌봄기기 통합 대시보드</title>
            <meta charset="utf-8">
        </head>
        <body style="font-family: 'Malgun Gothic', sans-serif; padding:30px; background:#f0f2f5;">
            <div style="max-width:600px; margin:auto; background:white; padding:30px; border-radius:20px; box-shadow:0 10px 25px rgba(0,0,0,0.1);">
                <h1 style="text-align:center; color:#1a73e8;">📊 리포트 다운로드</h1>
                <p style="text-align:center; color:#7f8c8d; font-size:0.9em;">기간과 사람을 선택하면 날짜별 CSV 파일이 ZIP으로 묶여 다운로드됩니다.</p>
                <p style="text-align:center; color:#95a5a6; font-size:0.8em;">사람별 배정 기간으로 분리되어 — 다른 사용자의 데이터는 섞이지 않습니다.</p>
                <p style="text-align:center; color:#95a5a6; font-size:0.8em;">수집된 데이터 범위: {min_date} ~ {max_date}</p>
                <form action="/report_range" method="get" style="margin-top:30px;">
                    <div style="display:flex; gap:12px; margin-bottom:20px;">
                        <div style="flex:1;">
                            <label style="display:block; margin-bottom:8px; font-weight:bold; color:#2c3e50;">시작일</label>
                            <input type="date" name="start" required min="{min_date}" max="{max_date}" value="{min_date}"
                                   style="width:100%; padding:12px; font-size:1em; border:1px solid #ddd; border-radius:8px; box-sizing:border-box;">
                        </div>
                        <div style="flex:1;">
                            <label style="display:block; margin-bottom:8px; font-weight:bold; color:#2c3e50;">종료일</label>
                            <input type="date" name="end" required min="{min_date}" max="{max_date}" value="{max_date}"
                                   style="width:100%; padding:12px; font-size:1em; border:1px solid #ddd; border-radius:8px; box-sizing:border-box;">
                        </div>
                    </div>
                    <div style="margin-bottom:20px;">
                        <label style="display:block; margin-bottom:8px; font-weight:bold; color:#2c3e50;">사람 (배정 기간)</label>
                        <select name="assignment" required style="width:100%; padding:12px; font-size:1em; border:1px solid #ddd; border-radius:8px;">
                            {device_options_html}
                        </select>
                    </div>
                    <button type="submit" style="width:100%; padding:14px; background:#1a73e8; color:white; font-size:1.1em; font-weight:bold; border:none; border-radius:8px; cursor:pointer;">
                        ZIP 다운로드
                    </button>
                </form>
                <p style="text-align:center; margin-top:20px;">
                    <a href="/dashboard" style="color:#7f8c8d;">← 대시보드로 돌아가기</a>
                </p>
            </div>
        </body>
    </html>
    """


# ── 리포트 대상(배정/기기) 해석 헬퍼 ──
def _find_assignment(aid):
    """배정ID 로 배정 정보를 찾는다. 없으면 None."""
    if not aid:
        return None
    for a in analyzer.list_assignments():
        if a.get("id") == aid:
            return a
    return None


def _resolve_report_target(device, assignment):
    """리포트 다운로드 대상 해석.
    반환: (sn, assignment_id, 라벨용 user, 라벨용 location, stint).
    assignment(배정ID)가 오면 그 배정 기간으로 한정한다.
    device(SN)만 오면 그 기기 전체 — 파일명은 현재 등록 정보를 쓴다."""
    if assignment:
        stint = _find_assignment(assignment)
        if not stint:
            return None, None, None, None, None
        return stint["sn"], stint["id"], stint.get("user", stint["sn"]), \
            stint.get("location", "Unknown"), stint
    if device:
        di = analyzer.DEVICE_INFO.get(device, {"name": device, "location": "Unknown"})
        return device, None, di["name"], di["location"], None
    return None, None, None, None, None


# ── CSV 분리 규칙 ────────────────────────────────────────────────
# EMFIT QS 와 AI Radar 는 측정하는 게 달라서 CSV 를 따로 뽑는다.
# 레이더는 한 대가 BED(자세7종+생체) 와 FALL(자세3종+인원) 두 형식을 같이 보내는데,
# 이것도 성격이 달라 파일을 나눈다. 분석할 때 섞여 있으면 오히려 다루기 어렵다.
RADAR_CSV_KINDS = [("bed", "Radar-BED"), ("fall", "Radar-FALL")]


def _report_frames(date_str, sn, aid, kind=None):
    """그 날짜/기기의 CSV 목록을 [(파일명꼬리표, DataFrame), ...] 로 반환.

    Emfit  → [("리포트", df)]  ← 기존 파일명 그대로 (이미 받아둔 파일들과 섞이지 않게)
    Radar  → [("Radar-BED", df), ("Radar-FALL", df)]  (데이터 있는 것만)
    kind ('bed'/'fall') 를 주면 레이더 중 그 하나만."""
    try:
        df = analyzer.get_report_df(DATA_FILES, date_str, sn, assignment_id=aid)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    if "Radar모델" not in df.columns:
        return [("리포트", df)]

    frames = []
    for model, suffix in RADAR_CSV_KINDS:
        if kind and kind != model:
            continue
        sub = df[df["Radar모델"] == model]
        if sub.empty:
            continue
        # 그 형식이 채우지 않는 컬럼(BED 의 감지인원, FALL 의 심박 등)은 빼서 깔끔하게
        sub = sub.dropna(axis=1, how="all")
        frames.append((suffix, sub))
    return frames


def _csv_bytes(df):
    """엑셀에서 한글이 깨지지 않도록 BOM 붙인 UTF-8 CSV."""
    return ("﻿" + df.to_csv(index=False)).encode("utf-8")


def _attachment_headers(filename):
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}


# 리포트 CSV 다운로드 (admin 또는 해당 기기 토큰)
@app.get("/report")
def download_report(request: Request, date: str = Query(...),
                    device: str = Query(None), assignment: str = Query(None),
                    kind: str = Query(None)):
    sn, aid, label_user, label_loc, stint = _resolve_report_target(device, assignment)
    if sn is None:
        return PlainTextResponse("기기 또는 배정 정보가 올바르지 않습니다.", status_code=400)
    token = _require_device_access(request, sn)
    if token is not None:
        # 비관리자(기기 토큰)는 현재 배정 기간 데이터만 접근 가능
        _aa = _active_assignment(sn)
        if _aa is None or (aid and aid != _aa["id"]):
            raise HTTPException(status_code=403, detail="현재 배정 데이터만 받을 수 있습니다")
        aid = _aa["id"]
        label_user, label_loc = _aa.get("user", sn), _aa.get("location", "Unknown")
    if not _has_data():
        return PlainTextResponse("데이터 파일이 없습니다.", status_code=404)

    if kind and kind not in {m for m, _ in RADAR_CSV_KINDS}:
        return PlainTextResponse("kind 는 bed 또는 fall 이어야 합니다.", status_code=400)

    try:
        frames = _report_frames(date, sn, aid, kind=kind)
    except Exception as e:
        return PlainTextResponse(f"분석 오류: {e}", status_code=500)

    if not frames:
        return PlainTextResponse(
            f"해당 조건의 데이터가 없습니다 (date={date})",
            status_code=404,
        )

    # 파일이 하나면 CSV 그대로, 여러 개(레이더 BED+FALL)면 ZIP 으로 묶어 내려준다.
    if len(frames) == 1:
        suffix, df = frames[0]
        filename = f"{date}_{label_loc}_{label_user}_{suffix}.csv"
        return StreamingResponse(
            iter([_csv_bytes(df)]),
            media_type="text/csv; charset=utf-8",
            headers=_attachment_headers(filename),
        )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for suffix, df in frames:
            zf.writestr(f"{date}_{label_loc}_{label_user}_{suffix}.csv", _csv_bytes(df))
    zip_buf.seek(0)
    return StreamingResponse(
        iter([zip_buf.getvalue()]),
        media_type="application/zip",
        headers=_attachment_headers(f"{date}_{label_loc}_{label_user}.zip"),
    )


# 리포트 기간 다운로드 (ZIP) — admin 또는 해당 기기 토큰
@app.get("/report_range")
def download_report_range(
    request: Request,
    start: str = Query(...),
    end: str = Query(...),
    device: str = Query(None),
    assignment: str = Query(None),
):
    sn, aid, label_user, label_loc, stint = _resolve_report_target(device, assignment)
    if sn is None:
        return PlainTextResponse("기기 또는 배정 정보가 올바르지 않습니다.", status_code=400)
    token = _require_device_access(request, sn)
    if token is not None:
        # 비관리자(기기 토큰)는 현재 배정 기간 데이터만 접근 가능
        _aa = _active_assignment(sn)
        if _aa is None or (aid and aid != _aa["id"]):
            raise HTTPException(status_code=403, detail="현재 배정 데이터만 받을 수 있습니다")
        aid = _aa["id"]
        label_user, label_loc = _aa.get("user", sn), _aa.get("location", "Unknown")
    if not _has_data():
        return PlainTextResponse("데이터 파일이 없습니다.", status_code=404)

    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        return PlainTextResponse("날짜 형식이 올바르지 않습니다 (YYYY-MM-DD)", status_code=400)

    if start_dt > end_dt:
        return PlainTextResponse("시작일이 종료일보다 늦습니다", status_code=400)

    info = {"name": label_user, "location": label_loc}

    zip_buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        current = start_dt
        while current <= end_dt:
            date_str = current.strftime("%Y-%m-%d")
            # 레이더는 하루에 BED/FALL 두 파일이 나온다.
            for suffix, df in _report_frames(date_str, sn, aid):
                inner_name = f"{date_str}_{info['location']}_{info['name']}_{suffix}.csv"
                zf.writestr(inner_name, _csv_bytes(df))
                added += 1
            current += timedelta(days=1)

    if added == 0:
        return PlainTextResponse(
            f"해당 기간에 데이터가 없습니다 (start={start}, end={end})",
            status_code=404,
        )

    zip_filename = f"{start}_to_{end}_{info['location']}_{info['name']}.zip"
    encoded_filename = quote(zip_filename)
    zip_buf.seek(0)

    return StreamingResponse(
        iter([zip_buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        },
    )


# 도움말 / 사용 가이드
@app.get("/help", response_class=HTMLResponse)
def help_page():
    return """
    <html>
        <head>
            <title>사용 가이드 · 돌봄기기 통합 대시보드</title>
            <meta charset="utf-8">
        </head>
        <body style="font-family: 'Malgun Gothic', sans-serif; padding:30px; background:#f0f2f5; line-height:1.7;">
            <div style="max-width:900px; margin:auto; background:white; padding:40px; border-radius:20px; box-shadow:0 10px 25px rgba(0,0,0,0.08);">

                <h1 style="color:#1a73e8; border-bottom:2px solid #e8f0fe; padding-bottom:10px;">📘 돌봄기기 통합 대시보드 사용 가이드</h1>
                <p style="color:#7f8c8d;">EMFIT QS · AI Radar · McKare · 사용감지 센서 통합 관제 및 리포트 서비스.</p>

                <h2 style="color:#1a73e8; margin-top:40px;">1. 대시보드 보는 법</h2>

                <p>대시보드는 기기별 카드 형태로 현재 상태를 보여줍니다. 15초마다 자동 갱신.</p>

                <h3 style="color:#2c3e50;">카드 구성</h3>
                <ul>
                    <li><b>위치 / 이름</b>: 어느 방/누구의 기기인지.</li>
                    <li><b>❤️ HR</b>: 심박수 (분당)</li>
                    <li><b>🫁 RR</b>: 호흡수 (분당)</li>
                    <li><b>🏃 ACT</b>: 활동량 지표 (0에 가까우면 부재)</li>
                    <li><b>측정</b>: 마지막 HR/RR/ACT 측정 시각</li>
                    <li><b>통신</b>: 장비가 마지막으로 서버에 "살아있어요" 신호를 보낸 시각, 통상적으로 방금 전 ~ 1분 전으로 표시되나 그 이상으로 표시되었을 경우 연결이 끊긴 것임.</li>
                </ul>

                <h3 style="color:#2c3e50;">상태 아이콘 의미</h3>
                <table style="width:100%; border-collapse:collapse; margin-top:10px;">
                    <tr style="background:#e8f0fe;">
                        <th style="padding:8px; text-align:left; width:60px;">아이콘</th>
                        <th style="padding:8px; text-align:left;">상태</th>
                        <th style="padding:8px; text-align:left;">판정 기준</th>
                    </tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee; font-size:1.3em;">🛌</td><td style="padding:8px; border-top:1px solid #eee;">침대 위 재실</td><td style="padding:8px; border-top:1px solid #eee;">수면 데이터 또는 ACT &lt; 10</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee; font-size:1.3em;">🛏️</td><td style="padding:8px; border-top:1px solid #eee;">부재</td><td style="padding:8px; border-top:1px solid #eee;">ACT &lt; 1 (침대에 없음)</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee; font-size:1.3em;">🔴</td><td style="padding:8px; border-top:1px solid #eee;">끊김</td><td style="padding:8px; border-top:1px solid #eee;">장비 오프라인 (네트워크/전원)</td></tr>
                </table>

                <div style="background:#fff9e6; border-left:4px solid #ffd54f; padding:12px 16px; margin-top:20px; border-radius:6px;">
                    <b>💡 측정 vs 통신 의 차이</b><br>
                    <b>측정</b> = HR/RR/ACT 값이 찍힌 시각 (사람이 침대에 있을 때만)<br>
                    <b>통신</b> = 장비가 서버에 접속한 시각 (전원 켜져있으면 측정 유무 관계없이 주기적으로 들어옴)<br>
                    둘을 함께 보면 "장비가 살아있는지" 와 "사람이 사용 중인지" 를 분리해서 판단 가능.
                </div>

                <h2 style="color:#1a73e8; margin-top:40px;">2. 리포트 다운로드</h2>

                <p>대시보드 하단의 <b>📊 리포트 다운로드</b> 버튼을 누르세요.</p>

                <ol>
                    <li><b>시작일 / 종료일</b> 선택 (수집된 범위 내에서만 가능)</li>
                    <li><b>기기(사용자)</b> 선택</li>
                    <li><b>ZIP 다운로드</b> 클릭</li>
                </ol>

                <p>ZIP 파일 안에는 날짜별 CSV 파일이 들어있고, 데이터 없는 날짜는 자동으로 빠집니다.</p>

                <h3 style="color:#2c3e50;">CSV 컬럼 설명</h3>
                <table style="width:100%; border-collapse:collapse; margin-top:10px;">
                    <tr style="background:#e8f0fe;">
                        <th style="padding:8px; text-align:left;">컬럼</th>
                        <th style="padding:8px; text-align:left;">의미</th>
                    </tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">날짜 / 시간(KST)</td><td style="padding:8px; border-top:1px solid #eee;">측정 시점 (한국 시간)</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">사용자 / 위치</td><td style="padding:8px; border-top:1px solid #eee;">기기 등록 정보</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">유형</td><td style="padding:8px; border-top:1px solid #eee;">Live(실시간), HRV, SleepDetail(수면상세), Summary(수면요약)</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">심박수(HR)</td><td style="padding:8px; border-top:1px solid #eee;">분당 심박수</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">호흡수(RR)</td><td style="padding:8px; border-top:1px solid #eee;">분당 호흡수</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">활동량(ACT)</td><td style="padding:8px; border-top:1px solid #eee;">EMFIT QS 활동 지표 (0에 가까우면 부재)</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">심박변이도(RMSSD)</td><td style="padding:8px; border-top:1px solid #eee;">HRV 지표 (HRV 행만)</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">수면점수 / 총수면(분) 등</td><td style="padding:8px; border-top:1px solid #eee;">하루 수면 요약 (Summary 행만)</td></tr>
                </table>

                <h2 style="color:#1a73e8; margin-top:40px;">3. 자주 묻는 질문</h2>

                <h3 style="color:#2c3e50;">Q. 카드 값이 안 바뀌는 것 같아요</h3>
                <p>
                    기기마다 전송 주기가 다릅니다. EMFIT QS 는 30초, AI Radar 는 1~50초 주기로 보냅니다.
                    사용감지 센서는 <b>주기적으로 보내지 않고</b> 사용 시작·종료 때만 보내므로, 값이 안 바뀌는 게 정상입니다.
                    카드의 <b>측정</b>(또는 마지막 신호) 시각이 갱신되고 있다면 정상입니다.
                </p>

                <h3 style="color:#2c3e50;">Q. HR/RR/ACT 가 모두 "-" 로 떠요</h3>
                <p>
                    10분 이상 데이터 측정이 없거나 침대 위에 없는 상태입니다. 또는 장비의 연결이 끊겼을 때도 "-"로 표시되며, 이는 Q3 답변 참고 바랍니다.
                    <br>※ <b>사용감지 센서</b>는 심박·호흡을 아예 측정하지 않으므로 해당 칸이 없습니다. 대신 사용 중/미사용과 배터리를 보여줍니다.
                </p>

                <h3 style="color:#2c3e50;">Q. 🔴 끊김 이 뜨면 어떻게 하나요</h3>
                <p>
                    장비 자체의 전원이나 네트워크를 확인해야 합니다. 장비가 제대로 연결이 되어있는지(DC전원 또는 콘센트) 확인이 필요합니다. 사용 위치를 옮겼을 경우에는 와이파이 재연결이 필요할 수 있습니다.
                </p>

                <h3 style="color:#2c3e50;">Q. 수면 점수(Summary)가 이상해요 — REM/깊은수면이 0</h3>
                <p>
                    EMFIT QS 알고리즘이 수면 단계 분류에 실패하는 것으로 추정되며, 총수면(분) 값만 신뢰해서 보세요.
                </p>

                <h3 style="color:#2c3e50;">Q. 리포트 창을 띄우는데 너무 오래 걸려요</h3>
                <p>
                    서버 업데이트 및 재시작 후 첫 요청은 30초~3분 정도 걸릴 수 있습니다(데이터 재분석을 진행합니다). 이후로는 바로 응답합니다.
                </p>

                <hr style="margin:40px 0; border:0; border-top:1px solid #eee;">

                <p style="text-align:center;">
                    <a href="javascript:history.back()" style="display:inline-block; padding:10px 24px; background:#1a73e8; color:white; text-decoration:none; border-radius:8px; font-weight:bold;">← 이전 페이지로</a>
                </p>

                <p style="text-align:center; color:#bdc3c7; font-size:0.85em; margin-top:30px;">
                    관리자 매뉴얼(설치·운영·트러블슈팅)은 서버의 <code>MANUAL.md</code> 참고.
                </p>
            </div>
        </body>
    </html>
    """


# 기기 정보 편집 — DEVICE_INFO 를 대시보드에서 수정 (관리자 + 비밀번호)
@app.get("/devices", response_class=HTMLResponse)
def view_devices(request: Request, saved: int = 0, handover: int = 0, _: str = Depends(require_admin)):
    admin_token = _get_token_from_request(request) or ""

    # 기기 종류 태그 — 대시보드와 같은 색을 써서 어느 섹션 기기인지 바로 알아보게
    _KIND_TAG = {
        "emfit":  ("EMFIT", "#e05575"), "radar": ("Radar", "#7c5cd6"),
        "mckare": ("McKare", "#1fa39c"), "fsr":   ("사용감지", "#00897b"),
    }
    kind_counts = {}
    entries = []
    for sn in sorted(analyzer.DEVICE_INFO.keys()):
        info = analyzer.DEVICE_INFO[sn]
        kind = _detail_kind(sn)
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        entries.append((sn, info, kind))

    # 설치장소로 묶는다 — 같은 장소 기기가 붙어 있어야 훑기 쉽다
    by_loc = {}
    for sn, info, kind in entries:
        loc = str(info.get("location") or "").strip()
        by_loc.setdefault(loc if loc and loc != "-" else "미지정", []).append((sn, info, kind))

    rows_html = ""
    for loc in sorted(by_loc, key=lambda x: (x == "미지정", x)):   # '미지정'은 맨 뒤로
        members = by_loc[loc]
        rows_html += (f'<tr class="loc-head"><td colspan="5" style="padding:14px 6px 6px;'
                      f' color:#546e7a; font-size:0.9em; font-weight:bold;'
                      f' border-bottom:2px solid #eceff1;">📍 {html.escape(loc)}'
                      f' <span style="color:#b0bec5; font-weight:normal;">· {len(members)}대</span></td></tr>')
        for sn, info, kind in members:
            name = html.escape(str(info.get("name", "")))
            location = html.escape(str(info.get("location", "")))
            group = info.get("group", analyzer.DEFAULT_GROUP)
            hidden = bool(info.get("hidden"))
            tag_label, tag_color = _KIND_TAG.get(kind, ("기타", "#90a4ae"))
            group_opts = "".join(
                f'<option value="{html.escape(g)}"{" selected" if g == group else ""}>{html.escape(g)}</option>'
                for g in analyzer.GROUPS
            )
            # data-* 는 검색·필터가 쓰는 값 (화면 안에서만 걸러내므로 저장과 무관하게 즉시 반응)
            rows_html += f"""
        <tr class="dev-row{' is-hidden' if hidden else ''}" data-kind="{kind}"
            data-search="{name.lower()} {location.lower()} {sn.lower()}">
            <td style="padding:10px; font-family:monospace; color:#607d8b; white-space:nowrap; font-size:0.85em;">{sn}</td>
            <td style="padding:6px;"><input name="name_{sn}" value="{name}" style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box;"></td>
            <td style="padding:6px;"><input name="location_{sn}" value="{location}" style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box;"></td>
            <td style="padding:6px; white-space:nowrap;">
                <span style="display:inline-block; padding:2px 9px; border-radius:10px; background:{tag_color}; color:white; font-size:0.75em; font-weight:bold; margin-bottom:4px;">{tag_label}</span>
                <select name="group_{sn}" style="width:100%; padding:6px; border:1px solid #ddd; border-radius:6px; font-size:0.9em;">
                    {group_opts}
                </select>
            </td>
            <td style="padding:6px; text-align:center; white-space:nowrap;">
                <label class="eye-label" title="대시보드 카드에서 감춥니다 (데이터는 그대로 남습니다)">
                    <input type="checkbox" name="hidden_{sn}" {'checked' if hidden else ''} onchange="this.closest('tr').classList.toggle('is-hidden', this.checked); refreshCounts();">
                    <span class="eye-face">{'🚫' if hidden else '👁'}</span>
                </label>
            </td>
        </tr>
        """

    hidden_count = sum(1 for _, i, _ in entries if i.get("hidden"))
    filter_chips = f'<button type="button" class="chipbtn on" data-kind="all">전체 {len(entries)}</button>'
    for k, (lbl, _c) in _KIND_TAG.items():
        if kind_counts.get(k):
            filter_chips += f'<button type="button" class="chipbtn" data-kind="{k}">{lbl} {kind_counts[k]}</button>'

    saved_banner = ""
    if saved:
        saved_banner = '<div style="background:#e8f5e9; border-left:4px solid #43a047; padding:10px 14px; margin-bottom:16px; border-radius:6px; color:#2e7d32;">✅ 저장되었습니다.</div>'

    handover_banner = ""
    if handover:
        handover_banner = '<div style="background:#e3f2fd; border-left:4px solid #1976d2; padding:10px 14px; margin-bottom:16px; border-radius:6px; color:#0d47a1;">✅ 기기 이전이 등록되었습니다. 이전 사용자의 데이터와 자동으로 분리됩니다.</div>'

    # 기기 이전 폼 — 기기 선택지 (현재 사용자 표시)
    handover_dev_options = ""
    for sn in sorted(analyzer.DEVICE_INFO.keys()):
        di = analyzer.DEVICE_INFO[sn]
        cur = f"{di.get('location', '-')} / {di.get('name', '')}"
        handover_dev_options += f'<option value="{html.escape(sn)}">{html.escape(sn)} — 현재: {html.escape(cur)}</option>\n'

    # 배정 이력 표
    hist_rows = ""
    for a in sorted(analyzer.list_assignments(), key=lambda x: (x.get("sn", ""), x.get("start") or "")):
        start_lbl = a.get("start") or "처음"
        end_lbl = a.get("end") or "현재 사용중"
        bg = "background:#e8f5e9;" if not a.get("end") else ""
        a_sn = html.escape(str(a.get('sn', '')))
        a_id = html.escape(str(a.get('id', '')))
        hist_rows += f"""<tr onclick="location.href='/device/{a_sn}?assignment={a_id}'" style="cursor:pointer;{bg}" title="클릭하면 이 기간의 그래프 보기">
            <td style="padding:6px; font-family:monospace; color:#607d8b;">{a_sn}</td>
            <td style="padding:6px;">{html.escape(str(a.get('user', '')))}</td>
            <td style="padding:6px;">{html.escape(str(a.get('location', '')))}</td>
            <td style="padding:6px; font-size:0.85em; color:#555;">{html.escape(str(start_lbl))} ~ {html.escape(str(end_lbl))} 📈</td>
        </tr>"""

    now_local = datetime.now().strftime("%Y-%m-%dT%H:%M")

    # 기기 이전 폼의 그룹 선택지 — analyzer.GROUPS 에서 자동 생성
    handover_group_opts = "".join(
        f'<option value="{html.escape(g)}">{html.escape(g)}</option>' for g in analyzer.GROUPS
    )

    return f"""
    <html>
    <head>
        <title>기기 정보 관리 · 돌봄기기 통합 대시보드</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: 'Malgun Gothic', sans-serif; padding:20px; background:#f0f2f5; margin:0; }}
            .container {{ max-width: 900px; margin: auto; background:white; padding:24px; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }}
            table {{ width:100%; border-collapse: collapse; }}
            th {{ background:#e8f0fe; padding:10px; text-align:left; font-size:0.9em; color:#1a237e; }}
            tr {{ border-top: 1px solid #eee; }}
            /* 검색·필터 도구 */
            .dev-toolbar {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:14px 0; }}
            .dev-search {{ flex:1; min-width:180px; padding:9px 13px; border:1px solid #cfd8dc;
                           border-radius:8px; font-size:0.95em; box-sizing:border-box; }}
            .chipbtn {{ padding:7px 14px; border-radius:999px; border:1px solid #cfd8dc; background:#fff;
                        font-size:0.85em; color:#546e7a; font-weight:bold; cursor:pointer; white-space:nowrap; }}
            .chipbtn.on {{ background:#1a73e8; color:#fff; border-color:transparent; }}
            /* 숨김 토글 */
            .eye-label {{ cursor:pointer; user-select:none; }}
            .eye-label input {{ display:none; }}
            .eye-face {{ display:inline-block; padding:5px 10px; border:1px solid #dfe6ea;
                         border-radius:6px; font-size:1em; }}
            .dev-row.is-hidden {{ opacity:0.45; }}
            .dev-row.is-hidden .eye-face {{ background:#fff4e5; border-color:#ffcc80; }}
            .dev-row.filtered-out, .loc-head.filtered-out {{ display:none; }}
            #no-match {{ display:none; text-align:center; color:#90a4ae; padding:26px 0; }}
            @media (max-width: 600px) {{
                body {{ padding: 10px; }}
                .container {{ padding: 16px; }}
                table, thead, tbody, tr, td, th {{ display:block; }}
                tr {{ margin-bottom:14px; padding:10px; background:#fafafa; border-radius:8px; }}
                td {{ padding:4px 0 !important; }}
                td:first-child {{ font-weight:bold; }}
                .loc-head {{ margin-bottom:0; padding:6px 0 !important; background:none; }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <p style="margin:0 0 12px 0;"><a href="/dashboard" style="color:#1a73e8; text-decoration:none;">← 대시보드</a></p>
            <h1 style="color:#1a237e; margin:8px 0;">⚙️ 기기 정보 관리</h1>
            <p style="color:#607d8b;">아래 표는 <b>현재 사용중인</b> 정보 — 이름/위치 오타 수정용입니다.<br>
            사용자가 <b>아예 바뀌면</b> 아래쪽 <b>🔄 기기 이전</b>을 쓰세요. 그래야 과거 데이터가 안 섞입니다.</p>
            {saved_banner}{handover_banner}
            <div class="dev-toolbar">
                <input id="dev-search" class="dev-search" placeholder="🔍 이름 · 위치 · SN 검색" autocomplete="off">
                {filter_chips}
            </div>
            <form method="post" action="/devices/save">
                <table>
                    <thead>
                        <tr>
                            <th>SN</th><th>이름</th><th>위치</th><th>종류 / 그룹</th><th style="text-align:center;">표시</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
                <p id="no-match">검색 결과가 없습니다.</p>
                <script>
                    // 검색·필터는 화면 안에서만 걸러낸다 — 서버 왕복도, 저장도 필요 없다.
                    let kindFilter = 'all';
                    function applyFilter() {{
                        const q = (document.getElementById('dev-search').value || '').trim().toLowerCase();
                        let shown = 0;
                        document.querySelectorAll('tr.dev-row').forEach(function (tr) {{
                            const okKind = (kindFilter === 'all') || (tr.dataset.kind === kindFilter);
                            const okText = !q || (tr.dataset.search || '').indexOf(q) >= 0;
                            const show = okKind && okText;
                            tr.classList.toggle('filtered-out', !show);
                            if (show) shown++;
                        }});
                        // 장소 머리글은 그 아래에 보이는 기기가 하나도 없으면 같이 숨긴다
                        document.querySelectorAll('tr.loc-head').forEach(function (head) {{
                            let any = false;
                            let n = head.nextElementSibling;
                            while (n && !n.classList.contains('loc-head')) {{
                                if (n.classList.contains('dev-row') && !n.classList.contains('filtered-out')) {{
                                    any = true; break;
                                }}
                                n = n.nextElementSibling;
                            }}
                            head.classList.toggle('filtered-out', !any);
                        }});
                        document.getElementById('no-match').style.display = shown ? 'none' : 'block';
                    }}
                    function refreshCounts() {{
                        const n = document.querySelectorAll('tr.dev-row.is-hidden').length;
                        document.getElementById('hidden-count').textContent =
                            n ? '(현재 ' + n + '대 숨김 — 저장해야 반영됩니다)' : '';
                    }}
                    document.getElementById('dev-search').addEventListener('input', applyFilter);
                    document.querySelectorAll('.chipbtn').forEach(function (btn) {{
                        btn.addEventListener('click', function () {{
                            document.querySelectorAll('.chipbtn').forEach(b => b.classList.remove('on'));
                            btn.classList.add('on');
                            kindFilter = btn.dataset.kind;
                            applyFilter();
                        }});
                    }});
                    // 체크박스 상태에 맞춰 아이콘 글자도 바꾼다
                    document.querySelectorAll('.eye-label input').forEach(function (cb) {{
                        cb.addEventListener('change', function () {{
                            cb.parentElement.querySelector('.eye-face').textContent = cb.checked ? '🚫' : '👁';
                        }});
                    }});
                    refreshCounts();
                </script>
                <div style="margin-top:20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
                    <span style="color:#90a4ae; font-size:0.85em;">
                        👁 를 눌러 <b>🚫</b> 로 바꾸면 대시보드 카드에서 감춰집니다 —
                        데이터·리포트는 그대로 남고 언제든 되돌릴 수 있습니다.
                        <span id="hidden-count" style="color:#e65100; font-weight:bold;"></span>
                    </span>
                    <button type="submit" style="padding:12px 28px; background:#1a73e8; color:white; border:none; border-radius:8px; font-weight:bold; font-size:1em; cursor:pointer;">저장</button>
                </div>
            </form>

            <hr style="margin:28px 0; border:none; border-top:1px solid #eee;">
            <h2 style="color:#1a237e; font-size:1.15em;">🔄 기기 이전 (사용자 교체)</h2>
            <p style="color:#607d8b; font-size:0.9em;">기기를 다른 사람에게 옮길 때 사용하세요. 입력한 <b>이전 시각</b>을 기준으로,
            그 전 데이터는 이전 사용자에게 · 그 후 데이터는 새 사용자에게 자동으로 갈립니다.<br>
            💡 새 위치에서 데이터가 들어오기 <b>전(기기 이동 중)에 미리 등록</b>하면 가장 깔끔합니다.</p>
            <form method="post" action="/devices/handover" onsubmit="return confirm('이 기기를 새 사용자에게 이전할까요?');">
                <table>
                    <tbody>
                        <tr><td style="padding:6px; width:110px; color:#555; font-weight:bold;">기기</td>
                            <td style="padding:6px;"><select name="sn" required style="width:100%; padding:9px; border:1px solid #ddd; border-radius:6px; font-size:1em;">{handover_dev_options}</select></td></tr>
                        <tr><td style="padding:6px; color:#555; font-weight:bold;">새 사용자</td>
                            <td style="padding:6px;"><input name="user" required placeholder="예: 김영희" style="width:100%; padding:9px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box;"></td></tr>
                        <tr><td style="padding:6px; color:#555; font-weight:bold;">새 위치</td>
                            <td style="padding:6px;"><input name="location" required placeholder="예: A시설 201호" style="width:100%; padding:9px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box;"></td></tr>
                        <tr><td style="padding:6px; color:#555; font-weight:bold;">그룹</td>
                            <td style="padding:6px;"><select name="group" style="width:100%; padding:9px; border:1px solid #ddd; border-radius:6px; font-size:1em;">{handover_group_opts}</select></td></tr>
                        <tr><td style="padding:6px; color:#555; font-weight:bold;">이전 시각</td>
                            <td style="padding:6px;"><input type="datetime-local" name="when" value="{now_local}" step="60" style="width:100%; padding:9px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box;"></td></tr>
                    </tbody>
                </table>
                <div style="margin-top:14px; text-align:right;">
                    <button type="submit" style="padding:12px 28px; background:#1976d2; color:white; border:none; border-radius:8px; font-weight:bold; font-size:1em; cursor:pointer;">기기 이전 등록</button>
                </div>
            </form>

            <hr style="margin:28px 0; border:none; border-top:1px solid #eee;">
            <h2 style="color:#1a237e; font-size:1.15em;">📜 배정 이력</h2>
            <p style="color:#607d8b; font-size:0.9em;">줄을 클릭하면 그 기간의 그래프를 볼 수 있어요 📈 · 초록색 줄이 현재 사용 중인 배정입니다.</p>
            <table>
                <thead><tr><th>SN</th><th>사용자</th><th>위치</th><th>기간</th></tr></thead>
                <tbody>{hist_rows}</tbody>
            </table>
        </div>
    </body>
    </html>
    """


@app.post("/devices/save")
async def save_devices(request: Request, _: str = Depends(require_admin)):
    form = await request.form()
    new_data = {}
    for sn in list(analyzer.DEVICE_INFO.keys()):
        name = (form.get(f"name_{sn}") or "").strip()
        location = (form.get(f"location_{sn}") or "").strip()
        group = (form.get(f"group_{sn}") or analyzer.DEFAULT_GROUP).strip()
        if not name:
            name = sn
        if not location:
            location = "-"
        if group not in analyzer.GROUPS:
            group = analyzer.DEFAULT_GROUP
        # 체크박스는 체크됐을 때만 전송된다 → 없으면 표시(=숨김 해제)
        entry = {"name": name, "location": location, "group": group}
        if form.get(f"hidden_{sn}"):
            entry["hidden"] = True
        new_data[sn] = entry
    try:
        # 이름·위치만 바뀌면 라벨만 갈아끼우고(즉시), 새 배정이 생겼을 때만 재파싱한다.
        # 예전에는 무조건 재파싱해서 저장 후 몇 분씩 기다려야 했다.
        if analyzer.update_active_assignments(new_data):
            analyzer.invalidate_cache()
    except Exception as e:
        return PlainTextResponse(f"저장 실패: {e}", status_code=500)
    return RedirectResponse("/devices?saved=1", status_code=303)


@app.post("/devices/handover")
async def devices_handover(request: Request, _: str = Depends(require_admin)):
    """기기를 새 사용자에게 이전. 입력 시각 기준으로 이전/이후 데이터를 분리한다."""
    form = await request.form()
    sn = (form.get("sn") or "").strip()
    user = (form.get("user") or "").strip()
    location = (form.get("location") or "").strip() or "-"
    group = (form.get("group") or analyzer.DEFAULT_GROUP).strip()
    when = (form.get("when") or "").strip()

    if not sn or sn not in analyzer.DEVICE_INFO:
        return PlainTextResponse("기기를 선택하세요.", status_code=400)
    if not user:
        return PlainTextResponse("새 사용자 이름을 입력하세요.", status_code=400)
    if group not in analyzer.GROUPS:
        group = analyzer.DEFAULT_GROUP
    if not when:
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
    when = when.replace("T", " ")

    try:
        analyzer.handover(sn, user, location, group, when)
        analyzer.invalidate_cache()
    except Exception as e:
        return PlainTextResponse(f"기기 이전 처리 실패: {e}", status_code=500)
    return RedirectResponse("/devices?handover=1", status_code=303)


@app.post("/devices/edit_user")
async def devices_edit_user(request: Request):
    """활성 배정의 사용자명만 수정. admin 또는 view(그룹)+자기 SN 일 때 허용.
    옛 배정(end 있는 것)은 건드리지 않음 — 그건 역사 기록."""
    form = await request.form()
    sn = (form.get("sn") or "").strip()
    new_user = (form.get("user") or "").strip()
    if not sn or sn not in analyzer.DEVICE_INFO:
        return PlainTextResponse("기기를 찾을 수 없습니다.", status_code=400)
    if not new_user:
        return PlainTextResponse("이름을 입력하세요.", status_code=400)
    # 권한 검사 — admin OR (view 토큰 + sn 포함)
    if not _is_admin_authenticated(request):
        view = _resolve_view(request)
        if view is None or sn not in view["sns"]:
            raise HTTPException(status_code=403, detail="not allowed")
    try:
        ok = analyzer.update_active_user(sn, new_user)
        if not ok:
            return PlainTextResponse("활성 배정을 찾지 못했습니다.", status_code=400)
        # 재파싱 불필요 — update_active_user 가 라벨을 그 자리에서 갱신한다
    except Exception as e:
        return PlainTextResponse(f"이름 변경 실패: {e}", status_code=500)
    return RedirectResponse(f"/device/{sn}", status_code=303)


# 의견/개선사항 페이지 (관리자 ID/PW 또는 device 토큰 보유자)
@app.get("/feedback", response_class=HTMLResponse)
def view_feedback(request: Request, ok: int = 0):
    is_admin = _is_admin_authenticated(request)
    device_token = None
    view = None
    if not is_admin:
        t = _get_token_from_request(request)
        if t and t in _load_tokens():
            device_token = t
        else:
            view = _resolve_view(request)
            if view is None:
                raise HTTPException(status_code=401, detail="login required")
    if is_admin:
        back_link = '<a href="/dashboard" style="color:#1a73e8; text-decoration:none;">← 대시보드</a>'
    elif view is not None:
        back_link = '<a href="/view" style="color:#1a73e8; text-decoration:none;">← 그룹 대시보드</a>'
    else:
        back_link = f'<a href="/d/{device_token}" style="color:#1a73e8; text-decoration:none;">← 메인</a>'
    items = _load_feedback_items()
    total = len(items)

    items_html = ""
    # 최근 30개를 역순으로 — 최신 위
    visible = list(enumerate(items))[-30:]
    for idx, it in reversed(visible):
        nm = html.escape(str(it.get("name", "익명")))
        at = html.escape(str(it.get("at", "")))
        txt = html.escape(str(it.get("text", ""))).replace("\n", "<br>")
        status = it.get("status", "pending")
        s_label, s_color, s_icon = FEEDBACK_STATUSES.get(status, FEEDBACK_STATUSES["pending"])
        replies = it.get("replies") or []

        replies_html = ""
        for rp in replies:
            r_at = html.escape(str(rp.get("at", "")))
            r_txt = html.escape(str(rp.get("text", ""))).replace("\n", "<br>")
            replies_html += f"""
            <div style="background:#e8f5e9; border-left:3px solid #43a047; padding:8px 12px; margin:8px 0 0 24px; border-radius:6px;">
                <div style="font-size:0.78em; color:#2e7d32; margin-bottom:4px;">🛠️ 관리자 답글 · {r_at}</div>
                <div style="color:#1b5e20; line-height:1.5;">{r_txt}</div>
            </div>
            """

        admin_controls = ""
        if is_admin:
            options_html = "".join(
                f'<option value="{k}"{" selected" if k == status else ""}>{v[2]} {v[0]}</option>'
                for k, v in FEEDBACK_STATUSES.items()
            )
            admin_controls = f"""
            <div style="margin-top:10px; padding:10px; background:white; border:1px dashed #cfd8dc; border-radius:6px;">
                <form method="post" action="/feedback/status" style="display:inline-block; margin-right:12px;">
                    <input type="hidden" name="idx" value="{idx}">
                    <label style="font-size:0.85em; color:#37474f;">상태:
                        <select name="status" onchange="this.form.submit()" style="padding:4px 6px; border:1px solid #ddd; border-radius:4px; margin-left:4px;">
                            {options_html}
                        </select>
                    </label>
                </form>
                <form method="post" action="/feedback/reply" style="margin-top:8px;">
                    <input type="hidden" name="idx" value="{idx}">
                    <textarea name="text" required maxlength="2000" placeholder="관리자 답글" style="width:100%; min-height:60px; padding:8px; border:1px solid #ddd; border-radius:6px; font-size:0.9em; box-sizing:border-box; font-family:inherit;"></textarea>
                    <div style="text-align:right; margin-top:6px;">
                        <button type="submit" style="padding:6px 16px; background:#43a047; color:white; border:none; border-radius:6px; font-size:0.85em; cursor:pointer;">+ 답글 달기</button>
                    </div>
                </form>
            </div>
            """

        items_html += f"""
        <div style="background:#fafafa; border-left:4px solid #e67e22; padding:10px 14px; margin-bottom:14px; border-radius:6px;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:6px;">
                <div style="font-size:0.85em; color:#7f8c8d;">{nm} · {at}</div>
                <span style="font-size:0.8em; padding:3px 10px; border-radius:10px; background:{s_color}22; color:{s_color}; font-weight:bold;">{s_icon} {s_label}</span>
            </div>
            <div style="color:#263238; line-height:1.5;">{txt}</div>
            {replies_html}
            {admin_controls}
        </div>
        """
    if not items_html:
        items_html = '<p style="color:#90a4ae; text-align:center; padding:20px;">아직 의견이 없습니다.</p>'

    ok_banner = ""
    if ok:
        ok_banner = '<div style="background:#e8f5e9; border-left:4px solid #43a047; padding:10px 14px; margin-bottom:16px; border-radius:6px; color:#2e7d32;">✅ 의견이 등록되었습니다. 감사합니다.</div>'

    return f"""
    <html>
    <head>
        <title>의견 보내기 · 돌봄기기 통합 대시보드</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: 'Malgun Gothic', sans-serif; padding:20px; background:#f0f2f5; margin:0; }}
            .container {{ max-width: 700px; margin: auto; }}
            .card {{ background:white; padding:24px; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,0.05); margin-bottom:16px; }}
            input, textarea {{ width:100%; padding:10px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box; font-family: inherit; }}
            textarea {{ min-height: 120px; resize: vertical; }}
            label {{ display:block; font-weight:bold; color:#37474f; margin-bottom:6px; }}
            @media (max-width: 600px) {{
                body {{ padding: 10px; }}
                .card {{ padding:16px; }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <p style="margin:0 0 12px 0;">{back_link}</p>
            <h1 style="color:#e67e22; margin:8px 0;">💬 의견 / 개선사항</h1>
            <p style="color:#607d8b;">사용해보시면서 불편한 점, 추가했으면 하는 기능을 알려주세요.</p>
            {ok_banner}
            <div class="card">
                <form method="post" action="/feedback/submit">
                    <div style="margin-bottom:14px;">
                        <label for="name">이름 <span style="font-weight:normal; color:#90a4ae; font-size:0.85em;">(선택)</span></label>
                        <input type="text" id="name" name="name" maxlength="30" placeholder="익명">
                    </div>
                    <div style="margin-bottom:14px;">
                        <label for="text">내용</label>
                        <textarea id="text" name="text" required maxlength="2000" placeholder="예: 카드 글씨가 좀 더 컸으면 좋겠어요"></textarea>
                    </div>
                    <div style="text-align:right;">
                        <button type="submit" style="padding:12px 28px; background:#e67e22; color:white; border:none; border-radius:8px; font-weight:bold; font-size:1em; cursor:pointer;">제출</button>
                    </div>
                </form>
            </div>
            <h2 style="color:#37474f; margin-top:30px; font-size:1.1em;">최근 의견</h2>
            <div class="card">{items_html}</div>
        </div>
    </body>
    </html>
    """


@app.post("/feedback/submit")
async def submit_feedback(request: Request):
    form = await request.form()
    if not _is_admin_authenticated(request):
        token = _get_token_from_request(request)
        if not (token and token in _load_tokens()):
            # device 토큰도 없으면 view(그룹) 토큰 허용
            if _resolve_view(request) is None:
                raise HTTPException(status_code=401, detail="login required")
    text = (form.get("text") or "").strip()
    if not text:
        return RedirectResponse("/feedback", status_code=303)
    name = (form.get("name") or "").strip() or "익명"
    item = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "name": name[:30],
        "text": text[:2000],
        "status": "pending",
        "replies": [],
    }
    try:
        with _feedback_lock:
            with open(FEEDBACK_FILE, "a", encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as e:
        return PlainTextResponse(f"저장 실패: {e}", status_code=500)
    return RedirectResponse("/feedback?ok=1", status_code=303)


@app.post("/feedback/status")
async def feedback_status(request: Request, _: str = Depends(require_admin)):
    """관리자: 의견 상태 변경 (미조치/조치중/조치완료/조치불가)."""
    form = await request.form()
    try:
        idx = int(form.get("idx") or "-1")
    except ValueError:
        idx = -1
    new_status = (form.get("status") or "").strip()
    if new_status not in FEEDBACK_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    items = _load_feedback_items()
    if not (0 <= idx < len(items)):
        raise HTTPException(status_code=400, detail="invalid idx")
    items[idx]["status"] = new_status
    _save_feedback_items(items)
    return RedirectResponse("/feedback", status_code=303)


@app.post("/feedback/reply")
async def feedback_reply(request: Request, _: str = Depends(require_admin)):
    """관리자: 의견에 답글 달기."""
    form = await request.form()
    try:
        idx = int(form.get("idx") or "-1")
    except ValueError:
        idx = -1
    text = (form.get("text") or "").strip()
    if not text:
        return RedirectResponse("/feedback", status_code=303)
    items = _load_feedback_items()
    if not (0 <= idx < len(items)):
        raise HTTPException(status_code=400, detail="invalid idx")
    items[idx].setdefault("replies", []).append({
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "text": text[:2000],
    })
    _save_feedback_items(items)
    return RedirectResponse("/feedback", status_code=303)


def _discord_settings_page_html(cfg, banner=""):
    default_minutes = max(1, int(cfg.get("threshold_minutes") or 30))
    channels = cfg.get("channels") or {}
    device_channels = cfg.get("device_channels") or {}
    channel_options_html = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>' for name in sorted(channels)
    )

    rows = ""
    for d in _discord_device_snapshot(cfg):
        sn = d["sn"]
        esc_sn = html.escape(sn)
        alerted = _discord_alerted.get(sn, False)
        dot = "⚪" if d["connected"] is None else ("🔴" if d["connected"] is False else "🟢")
        alert_state = "발송됨" if alerted else "-"
        name = html.escape(d["name"])
        loc = html.escape(d["location"])
        is_override = d["threshold_minutes"] != default_minutes
        thr_val = d["threshold_minutes"] if is_override else ""
        current_channel = device_channels.get(sn, "")
        route_opts = f'<option value=""{" selected" if not current_channel else ""}>(기본 채널)</option>'
        for ch_name in sorted(channels):
            sel = " selected" if ch_name == current_channel else ""
            route_opts += f'<option value="{html.escape(ch_name)}"{sel}>{html.escape(ch_name)}</option>'
        rows += (f'<tr><td style="padding:8px; border-top:1px solid #eee;">{dot}</td>'
                 f'<td style="padding:8px; border-top:1px solid #eee;">{name}</td>'
                 f'<td style="padding:8px; border-top:1px solid #eee; color:#78909c;">{loc}</td>'
                 f'<td style="padding:8px; border-top:1px solid #eee; color:#78909c;">{d["last_seen_text"]}</td>'
                 f'<td style="padding:8px; border-top:1px solid #eee; color:#78909c;">{alert_state}</td>'
                 f'<td style="padding:8px; border-top:1px solid #eee;">'
                 f'<input type="number" name="thr_{esc_sn}" min="1" value="{thr_val}" '
                 f'placeholder="{default_minutes}(기본)" style="width:80px; padding:6px; border:1px solid #ddd; border-radius:6px;">'
                 f'</td>'
                 f'<td style="padding:8px; border-top:1px solid #eee;">'
                 f'<select name="route_{esc_sn}" style="padding:6px; border:1px solid #ddd; border-radius:6px;">{route_opts}</select>'
                 f'</td></tr>')

    # 채널 관리 표 — 기존 채널 + 새로 추가할 빈 칸 3개
    channel_rows = ""
    for i, (ch_name, ch_url) in enumerate(sorted(channels.items())):
        channel_rows += (
            f'<tr><td style="padding:6px;"><input name="ch_name_{i}" value="{html.escape(ch_name)}" '
            f'style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; box-sizing:border-box;"></td>'
            f'<td style="padding:6px;"><input type="url" name="ch_url_{i}" value="{html.escape(ch_url)}" '
            f'placeholder="https://discord.com/api/webhooks/..." '
            f'style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; box-sizing:border-box;"></td></tr>'
        )
    n_existing = len(channels)
    for i in range(n_existing, n_existing + 3):
        channel_rows += (
            f'<tr><td style="padding:6px;"><input name="ch_name_{i}" placeholder="예: A시설" '
            f'style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; box-sizing:border-box;"></td>'
            f'<td style="padding:6px;"><input type="url" name="ch_url_{i}" '
            f'placeholder="https://discord.com/api/webhooks/..." '
            f'style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; box-sizing:border-box;"></td></tr>'
        )

    webhook_val = html.escape(str(cfg.get("webhook_url") or ""))
    threshold_val = int(cfg.get("threshold_minutes") or 30)
    checked = "checked" if cfg.get("enabled") else ""

    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<title>디스코드 알림 설정 · 돌봄기기 통합 대시보드</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family:'Malgun Gothic',sans-serif; background:#f0f2f5; margin:0; padding:24px; color:#37474f; }}
.wrap {{ max-width:760px; margin:0 auto; }}
.tblwrap {{ overflow-x:auto; }}
h1 {{ color:#1a237e; font-size:1.4em; }}
.card {{ background:white; border-radius:14px; padding:24px; box-shadow:0 2px 8px rgba(0,0,0,0.06); margin-bottom:20px; }}
label {{ display:block; font-weight:bold; margin:14px 0 6px; font-size:0.92em; }}
input[type=url], input[type=number] {{ width:100%; padding:10px; border:1px solid #ddd; border-radius:8px;
    font-size:1em; box-sizing:border-box; }}
.hint {{ color:#90a4ae; font-size:0.82em; margin-top:4px; }}
.row {{ display:flex; align-items:center; gap:8px; margin-top:16px; }}
.btn {{ display:inline-block; padding:10px 22px; border:none; border-radius:8px; font-weight:bold;
    font-size:1em; cursor:pointer; text-decoration:none; }}
.btn-save {{ background:#1a73e8; color:white; }}
.btn-test {{ background:#5865F2; color:white; margin-left:8px; }}
.banner {{ padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:0.92em; }}
.banner.ok {{ background:#e8f5e9; color:#1b5e20; }}
.banner.err {{ background:#ffebee; color:#b71c1c; }}
table {{ width:100%; border-collapse:collapse; font-size:0.9em; }}
</style></head>
<body><div class="wrap">
<h1>🔔 디스코드 연결 끊김 알림</h1>
{banner}
<div class="card">
    <form method="post" action="/admin/discord/save">
        <label>Discord Webhook URL</label>
        <input type="url" name="webhook_url" placeholder="https://discord.com/api/webhooks/..." value="{webhook_val}">
        <div class="hint">기본 채널로 쓸 Webhook URL입니다. 모든 기기를 시설별 채널에 배정했다면 비워둘 수 있습니다.</div>

        <label>끊김 신고 확인 시간 (분)</label>
        <input type="number" name="threshold_minutes" min="1" value="{threshold_val}" required>
        <div class="hint">통신 시간이 오래됐다는 이유만으로는 알림을 보내지 않습니다.
            기기가 직접 끊김 상태를 보낸 뒤, 이 시간 동안 계속 끊김이면 알림을 보냅니다.
            나중에 언제든 이 화면에서 바꿀 수 있습니다.</div>

        <div class="row">
            <input type="checkbox" id="enabled" name="enabled" {checked} style="width:auto;">
            <label for="enabled" style="margin:0;">알림 켜기</label>
        </div>

        <div class="row">
            <button type="submit" class="btn btn-save">저장</button>
        </div>
    </form>
    <form method="post" action="/admin/discord/test" style="display:inline;">
        <button type="submit" class="btn btn-test">🧪 테스트 알림 보내기</button>
    </form>
    <form method="post" action="/admin/discord/baseline" style="display:inline;"
          onsubmit="return confirm('지금 끊겨있는 기기는 전부 무시하고, 앞으로 새로 끊기는 기기만 알림을 받습니다. 계속할까요?');">
        <button type="submit" class="btn" style="background:#78909c; color:white; margin-left:8px;">
            🙈 지금 끊긴 건 무시하고 시작
        </button>
    </form>
    <div class="hint" style="margin-top:8px;">
        알림을 처음 켤 때 누르면, 이미 오래 끊겨있던 기기들 때문에 한꺼번에 알림이 쏟아지는 걸 막을 수 있습니다.
        이후 새로 끊기는 기기만 알림이 옵니다.
    </div>
</div>

<div class="card">
    <b>시설별 채널</b>
    <div class="hint">시설(그룹)마다 다른 Discord 채널로 알림을 보내고 싶으면, 그 채널의 Webhook URL을 이름 붙여서 등록하세요.
        아래 기기 표에서 기기마다 어느 채널로 보낼지 고를 수 있습니다.</div>
    <form method="post" action="/admin/discord/channels">
    <div class="tblwrap"><table>
        <tr><th style="text-align:left; padding:6px;">채널 이름</th><th style="text-align:left; padding:6px;">Webhook URL</th></tr>
        {channel_rows}
    </table></div>
    <div class="row">
        <button type="submit" class="btn btn-save">채널 저장</button>
    </div>
    </form>
</div>

<div class="card">
    <b>현재 기기 상태</b> <span class="hint">(🟢 = 연결 신고, 🔴 = 끊김 신고, ⚪ = 신고 이력 없음)</span>
    <form method="post" action="/admin/discord/devices">
    <div class="tblwrap"><table>
        <tr><th style="text-align:left; padding:8px;"></th><th style="text-align:left; padding:8px;">기기</th>
            <th style="text-align:left; padding:8px;">위치</th><th style="text-align:left; padding:8px;">마지막 통신</th>
            <th style="text-align:left; padding:8px;">알림 상태</th>
            <th style="text-align:left; padding:8px;">알림 기준(분)</th><th style="text-align:left; padding:8px;">보낼 채널</th></tr>
        {rows}
    </table></div>
    <div class="hint" style="margin-top:8px;">
        알림 기준을 비워두면 위에서 설정한 기본값을 씁니다. 특정 기기가 끊김 신고 후 복구 신호를 늦게 보내는 편이면,
        그 기기만 여유를 더 준 숫자를 넣어주세요.
        "보낼 채널"을 (기본 채널) 그대로 두면 맨 위 기본 Webhook으로 갑니다.
    </div>
    <div class="row">
        <button type="submit" class="btn btn-save">기기별 설정 저장</button>
    </div>
    </form>
</div>

<p><a href="/devices" style="color:#1a73e8; text-decoration:none;">← 장비 관리로 돌아가기</a></p>
</div></body></html>"""


# 관리자: 디스코드 연결 끊김 알림 설정 (Webhook URL·임계값)
@app.get("/admin/discord", response_class=HTMLResponse)
def admin_discord(request: Request, saved: int = 0, test: int = -1, baseline: int = -1,
                   thresholds: int = 0, channels_saved: int = 0, _: str = Depends(require_admin)):
    cfg = _load_discord_config()
    banner = ""
    if saved:
        banner = '<div class="banner ok">✅ 저장되었습니다. 다음 점검 주기(최대 1분)부터 반영됩니다.</div>'
    elif test == 1:
        banner = '<div class="banner ok">✅ 테스트 메시지를 보냈습니다. 디스코드 채널을 확인해보세요.</div>'
    elif test == 0:
        banner = '<div class="banner err">❌ 전송 실패 — Webhook URL을 다시 확인해주세요.</div>'
    elif baseline >= 0:
        banner = (f'<div class="banner ok">✅ 현재 끊겨있는 기기 {baseline}대는 무시하도록 표시했습니다. '
                   '이 시점 이후 새로 끊기는 기기만 알림이 갑니다.</div>')
    elif thresholds:
        banner = '<div class="banner ok">✅ 기기별 설정을 저장했습니다.</div>'
    elif channels_saved:
        banner = '<div class="banner ok">✅ 채널을 저장했습니다.</div>'
    return HTMLResponse(_discord_settings_page_html(cfg, banner=banner))


@app.post("/admin/discord/save")
async def admin_discord_save(request: Request, _: str = Depends(require_admin)):
    form = await request.form()
    webhook_url = (form.get("webhook_url") or "").strip()
    try:
        threshold_minutes = max(1, int(form.get("threshold_minutes") or 30))
    except ValueError:
        threshold_minutes = 30
    cfg = _load_discord_config()  # 기존 device_overrides 를 이 폼이 덮어쓰지 않도록 유지
    cfg["webhook_url"] = webhook_url
    cfg["threshold_minutes"] = threshold_minutes
    cfg["enabled"] = form.get("enabled") == "on"
    _save_discord_config(cfg)
    return RedirectResponse("/admin/discord?saved=1", status_code=303)


@app.post("/admin/discord/devices")
async def admin_discord_devices(request: Request, _: str = Depends(require_admin)):
    """기기별 알림 기준(분)·보낼 채널 저장. 비워두거나 (기본 채널)이면 전역 기본값을 쓴다."""
    form = await request.form()
    cfg = _load_discord_config()
    channels = cfg.get("channels") or {}
    overrides, device_channels = {}, {}
    for key, raw in form.multi_items():
        raw = (raw or "").strip()
        if key.startswith("thr_"):
            sn = key[len("thr_"):]
            if not raw:
                continue
            try:
                minutes = int(raw)
            except ValueError:
                continue
            if minutes > 0:
                overrides[sn] = minutes
        elif key.startswith("route_"):
            sn = key[len("route_"):]
            if raw and raw in channels:
                device_channels[sn] = raw
    cfg["device_overrides"] = overrides
    cfg["device_channels"] = device_channels
    _save_discord_config(cfg)
    return RedirectResponse("/admin/discord?thresholds=1", status_code=303)


@app.post("/admin/discord/channels")
async def admin_discord_channels(request: Request, _: str = Depends(require_admin)):
    """시설별 채널(이름 → Webhook URL) 저장. 이름이 바뀌면 기기 배정도 새 이름으로 안 따라가니
    이름을 통째로 바꾸기보다는 URL만 갈아끼우는 걸 권장 — 화면에 안내 문구로도 표시."""
    form = await request.form()
    cfg = _load_discord_config()
    old_channels = cfg.get("channels") or {}
    pairs = {}
    idx = 0
    while f"ch_name_{idx}" in form or f"ch_url_{idx}" in form:
        name = (form.get(f"ch_name_{idx}") or "").strip()
        url = (form.get(f"ch_url_{idx}") or "").strip()
        if name and url:
            pairs[name] = url
        idx += 1
    cfg["channels"] = pairs
    # 이름이 사라진(삭제되거나 변경된) 채널을 가리키던 기기 배정은 기본 채널로 되돌린다 —
    # 존재하지 않는 채널 이름을 참조한 채로 두면 알림이 조용히 씹힌다.
    device_channels = cfg.get("device_channels") or {}
    cfg["device_channels"] = {sn: ch for sn, ch in device_channels.items() if ch in pairs}
    _save_discord_config(cfg)
    return RedirectResponse("/admin/discord?channels_saved=1", status_code=303)


@app.post("/admin/discord/test")
def admin_discord_test(_: str = Depends(require_admin)):
    cfg = _load_discord_config()
    ok = bool(cfg.get("webhook_url")) and _send_discord_message(
        cfg["webhook_url"], "🧪 테스트 알림 — 돌봄기기 통합 대시보드에서 보냈습니다."
    )
    return RedirectResponse(f"/admin/discord?test={1 if ok else 0}", status_code=303)


@app.post("/admin/discord/baseline")
def admin_discord_baseline(_: str = Depends(require_admin)):
    """'지금 끊겨있는 기기'를 전부 이미-알림-보냄 상태로 찍어둔다.
    알림을 켤 때 오래전부터 끊겨있던 기기까지 한꺼번에 쏟아지는 걸 막고,
    이 시점 이후 '새로' 끊기는 기기만 알림이 가게 하려는 용도."""
    cfg = _load_discord_config()
    marked = 0
    for d in _discord_device_snapshot(cfg):
        _discord_pending.pop(d["sn"], None)  # 확인 대기 중이던 것도 baseline 처리에 흡수
        if d["connected"] is False:
            _discord_alerted[d["sn"]] = True
            marked += 1
        else:
            _discord_alerted.pop(d["sn"], None)
    _save_discord_alerted()
    return RedirectResponse(f"/admin/discord?baseline={marked}", status_code=303)


# 디스코드 봇(discord_bot.py, 별도 프로세스)이 슬래시 명령어에 답할 때 쓰는 내부 API.
# 봇 프로세스가 로그 파일을 다시 파싱하지 않도록, 이미 메모리에 떠 있는 상태를 그대로 내려준다.
# 외부 노출 방지를 위해 localhost 요청만 허용한다 (인증 토큰 없이도 안전하도록).
@app.get("/internal/discord/status")
def internal_discord_status(request: Request):
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="localhost only")
    cfg = _load_discord_config()
    devices = _discord_device_snapshot(cfg)
    return JSONResponse({
        "threshold_minutes": cfg.get("threshold_minutes"),
        "total": len(devices),
        "connected": sum(1 for d in devices if d["connected"] is True),
        "disconnected": sum(1 for d in devices if d["connected"] is False),
        "unknown": sum(1 for d in devices if d["connected"] is None),
        "devices": devices,
    })


# 그룹(view) 편집 UI 용 — 기기 종류 태그. 대시보드 섹션과 같은 색을 써서 한눈에 구분된다.
_VIEW_KIND_TAG = {
    "emfit": ("EMFIT", "#e05575"), "radar": ("Radar", "#7c5cd6"),
    "mckare": ("McKare", "#1fa39c"), "fsr": ("사용감지", "#00897b"),
}


def _devices_by_location():
    """{설치장소: [SN, ...]} — 그룹 편집 체크박스를 장소별로 묶어 보여주기 위함.
    숨긴 기기도 포함한다 (그룹에 넣을지는 관리자가 정할 일)."""
    out = {}
    for sn in sorted(analyzer.DEVICE_INFO):
        loc = str(analyzer.DEVICE_INFO[sn].get("location") or "").strip()
        out.setdefault(loc if loc and loc != "-" else "미지정", []).append(sn)
    return out


# 관리자: 기기별 사용자 URL(토큰) 관리 — admin ID/비번 통과 시 접근
@app.get("/admin/tokens", response_class=HTMLResponse)
def admin_tokens(request: Request, view_saved: int = 0, _: str = Depends(require_admin)):
    tokens = _load_tokens()
    # SN → 발급된 device 토큰 목록 (admin 토큰 항목 "*"는 무시)
    by_sn = {}
    for tok, sn in tokens.items():
        if sn == "*":
            continue
        by_sn.setdefault(sn, []).append(tok)

    # 두 base — "이 페이지로 들어온 주소"(보통 내부 IP)와 "외부 DDNS"
    host = request.headers.get("host", "")
    request_base = f"{request.url.scheme}://{host}" if host else ""
    # 사용자가 외부로 들어왔으면 request_base ≈ EXTERNAL_BASE → 내부 base는 따로 표시 못 함
    is_external_now = request_base.startswith(EXTERNAL_BASE.split("://")[1] if "://" in EXTERNAL_BASE else EXTERNAL_BASE) or EXTERNAL_BASE in request_base
    bases = []
    if is_external_now:
        bases.append(("🌍 외부 (DDNS)", request_base))
    else:
        bases.append(("🏠 내부 (같은 와이파이)", request_base))
        bases.append(("🌍 외부 (DDNS)", EXTERNAL_BASE))

    def _url_block(t, prefix="d"):
        """URL 한 묶음(내부/외부 base × token) 렌더. prefix='d' (개별) 또는 'v' (그룹)."""
        path = f"/{prefix}/{t}"
        rows = ""
        for label, b in bases:
            full = f"{b}{path}"
            rows += f"""
            <div style="display:flex; gap:6px; align-items:center; margin-bottom:4px; flex-wrap:wrap;">
                <span style="min-width:170px; font-size:0.8em; color:#546e7a;">{label}</span>
                <input readonly value="{full}" onclick="this.select()" style="flex:1; min-width:200px; padding:6px 8px; font-family:monospace; font-size:0.8em; border:1px solid #ddd; border-radius:6px; background:#fafafa;">
                <button type="button" onclick="copyText('{full}', this)" style="padding:6px 10px; background:#1a73e8; color:white; border:none; border-radius:6px; cursor:pointer; font-size:0.85em;">📋</button>
            </div>
            """
        return rows

    rows_html = ""
    for sn in sorted(analyzer.DEVICE_INFO.keys()):
        info = analyzer.DEVICE_INFO[sn]
        name = html.escape(str(info.get("name", "")))
        location = html.escape(str(info.get("location", "")))
        device_toks = by_sn.get(sn, [])
        if device_toks:
            url_blocks = "".join(
                f"""
                <div style="border:1px solid #eee; border-radius:8px; padding:10px; margin-bottom:8px; background:white;">
                    {_url_block(t)}
                    <form method="post" action="/admin/tokens/revoke" style="margin:6px 0 0 0; text-align:right;">
                        <input type="hidden" name="target" value="{t}">
                        <button type="submit" onclick="return confirm('이 URL 묶음을 폐기할까요?\\n사용자에게 새 URL을 다시 보내야 합니다.');" style="padding:6px 10px; background:#e57373; color:white; border:none; border-radius:6px; cursor:pointer; font-size:0.8em;">🗑️ 이 묶음 폐기</button>
                    </form>
                </div>
                """
                for t in device_toks
            )
            urls_html = url_blocks
        else:
            urls_html = '<span style="color:#90a4ae; font-size:0.9em;">발급된 URL 없음</span>'

        rows_html += f"""
        <tr>
            <td style="padding:12px; vertical-align:top;">
                <div style="font-weight:bold; color:#1a237e;">{name}</div>
                <div style="font-size:0.85em; color:#607d8b;">{location}</div>
                <div style="font-family:monospace; font-size:0.75em; color:#90a4ae; margin-top:4px;">{sn}</div>
            </td>
            <td style="padding:12px;">{urls_html}</td>
            <td style="padding:12px; vertical-align:top; white-space:nowrap;">
                <form method="post" action="/admin/tokens/issue" style="margin:0;">
                    <input type="hidden" name="sn" value="{sn}">
                    <button type="submit" style="padding:8px 14px; background:#16a085; color:white; border:none; border-radius:6px; cursor:pointer; font-size:0.9em;">+ URL 발급</button>
                </form>
            </td>
        </tr>
        """

    # ── 그룹(view) URL 섹션 ─────────────────────────────────────
    views = _load_view_tokens()
    if views:
        view_rows = ""
        for vtok, vinfo in views.items():
            vname = html.escape(str(vinfo.get("name", "")))
            vsns = vinfo.get("sns") or []
            # SN 만으론 누구 건지 모르니 기기 이름을 같이 보여준다
            sn_chips = "".join(
                f'<span style="display:inline-block; background:#eef; color:#1a237e; padding:3px 8px;'
                f' border-radius:10px; font-size:0.78em; margin:2px;">'
                f'{html.escape(str((analyzer.DEVICE_INFO.get(s) or {}).get("name", s)))}'
                f'<span style="color:#9fa8da; font-family:monospace; font-size:0.85em;"> {html.escape(s)}</span></span>'
                for s in vsns
            ) or '<span style="color:#b0bec5; font-size:0.8em;">기기 없음</span>'

            # 편집 폼 — 토큰(=URL)은 그대로 두고 체크만 바꾼다
            edit_boxes = ""
            for _loc in sorted(_devices_by_location(), key=lambda x: (x == "미지정", x)):
                edit_boxes += (f'<div style="margin:8px 0 3px; color:#78909c; font-size:0.78em;'
                               f' font-weight:bold;">📍 {html.escape(_loc)}</div>')
                for _sn2 in _devices_by_location()[_loc]:
                    _i2 = analyzer.DEVICE_INFO[_sn2]
                    _checked = " checked" if _sn2 in vsns else ""
                    _tag, _color = _VIEW_KIND_TAG.get(_detail_kind(_sn2), ("기타", "#90a4ae"))
                    edit_boxes += (
                        f'<label style="display:block; padding:4px 2px; font-size:0.85em; cursor:pointer;">'
                        f'<input type="checkbox" name="sns" value="{html.escape(_sn2)}"{_checked}> '
                        f'<span style="display:inline-block; padding:1px 7px; border-radius:9px;'
                        f' background:{_color}; color:white; font-size:0.72em; font-weight:bold;">{_tag}</span> '
                        f'{html.escape(str(_i2.get("name", _sn2)))}'
                        f'<span style="color:#b0bec5; font-family:monospace; font-size:0.8em;"> {html.escape(_sn2)}</span>'
                        f'</label>')

            view_rows += f"""
            <tr>
                <td style="padding:12px; vertical-align:top;">
                    <div style="font-weight:bold; color:#1a237e;">{vname}
                        <span style="color:#90a4ae; font-weight:normal; font-size:0.85em;">· {len(vsns)}대</span></div>
                    <div style="margin-top:4px;">{sn_chips}</div>
                    <button type="button" class="view-edit-btn" data-target="edit-{vtok}"
                        style="margin-top:8px; padding:5px 12px; background:transparent; color:#1a73e8;
                               border:1px solid #1a73e8; border-radius:6px; cursor:pointer; font-size:0.8em;">
                        \u270f\ufe0f 기기 추가/제외</button>
                    <form method="post" action="/admin/views/edit" id="edit-{vtok}"
                          style="display:none; margin-top:10px; padding:12px; background:#f5f7fa;
                                 border:1px solid #cfd8dc; border-radius:8px;">
                        <input type="hidden" name="target" value="{vtok}">
                        <label style="display:block; font-size:0.8em; color:#546e7a; font-weight:bold;">그룹 이름</label>
                        <input name="name" value="{vname}" required maxlength="40"
                               style="width:100%; padding:6px 9px; margin:4px 0 8px; border:1px solid #cfd8dc;
                                      border-radius:6px; box-sizing:border-box;">
                        <div style="max-height:260px; overflow-y:auto; background:white; border:1px solid #e3e8ee;
                                    border-radius:6px; padding:8px;">{edit_boxes}</div>
                        <p style="margin:8px 0 0; font-size:0.75em; color:#78909c;">
                            \u2705 <b>URL 은 바뀌지 않습니다</b> — 이미 배포한 주소를 그대로 쓰시면 됩니다.</p>
                        <div style="text-align:right; margin-top:8px;">
                            <button type="button" class="view-edit-cancel" data-target="edit-{vtok}"
                                style="padding:6px 12px; background:#b0bec5; color:white; border:none;
                                       border-radius:6px; cursor:pointer; font-size:0.85em;">취소</button>
                            <button type="submit" style="padding:6px 16px; background:#16a085; color:white;
                                border:none; border-radius:6px; cursor:pointer; font-weight:bold; font-size:0.85em;">저장</button>
                        </div>
                    </form>
                </td>
                <td style="padding:12px;">
                    <div style="border:1px solid #eee; border-radius:8px; padding:10px; background:white;">
                        {_url_block(vtok, prefix='v')}
                        <form method="post" action="/admin/views/revoke" style="margin:6px 0 0 0; text-align:right;">
                            <input type="hidden" name="target" value="{vtok}">
                            <button type="submit" onclick="return confirm('이 그룹 URL을 폐기할까요?\n받은 사람에게 새 URL을 다시 보내야 합니다.');" style="padding:6px 10px; background:#e57373; color:white; border:none; border-radius:6px; cursor:pointer; font-size:0.8em;">\U0001f5d1\ufe0f 폐기</button>
                        </form>
                    </div>
                </td>
            </tr>
            """
        views_table_html = f"""
        <table style="margin-top:8px;">
            <thead>
                <tr>
                    <th style="width:30%;">그룹</th>
                    <th>URL</th>
                </tr>
            </thead>
            <tbody>{view_rows}</tbody>
        </table>
        <script>
            // '기기 추가/제외' 펼치기 — 한 번에 하나만 열어 화면이 복잡해지지 않게
            document.querySelectorAll('.view-edit-btn').forEach(function (b) {{
                b.addEventListener('click', function () {{
                    var f = document.getElementById(b.dataset.target);
                    var opening = (f.style.display === 'none' || !f.style.display);
                    document.querySelectorAll('form[id^="edit-"]').forEach(function (o) {{ o.style.display = 'none'; }});
                    f.style.display = opening ? 'block' : 'none';
                }});
            }});
            document.querySelectorAll('.view-edit-cancel').forEach(function (b) {{
                b.addEventListener('click', function () {{
                    document.getElementById(b.dataset.target).style.display = 'none';
                }});
            }});
        </script>
        """
    else:
        views_table_html = '<p style="color:#90a4ae; padding:12px 0;">발급된 그룹 URL이 없습니다.</p>'

    view_saved_banner = ('<div style="background:#e8f5e9; border-left:4px solid #43a047; padding:10px 14px;'
                         ' margin-bottom:14px; border-radius:6px; color:#2e7d32;">'
                         '✅ 그룹이 수정되었습니다. <b>URL 은 그대로입니다</b> — 다시 배포하지 않으셔도 됩니다.</div>'
                         ) if view_saved else ''

    sn_checkboxes = ""
    for sn in sorted(analyzer.DEVICE_INFO.keys()):
        info = analyzer.DEVICE_INFO[sn]
        dname = html.escape(str(info.get("name", "")))
        dloc = html.escape(str(info.get("location", "")))
        sn_checkboxes += f"""
        <label style="display:inline-flex; align-items:center; gap:6px; padding:6px 10px; margin:4px; background:white; border:1px solid #ddd; border-radius:6px; cursor:pointer;">
            <input type="checkbox" name="sns" value="{sn}">
            <span><b>{dname}</b> <span style="color:#90a4ae; font-size:0.85em;">— {dloc}</span></span>
        </label>
        """
    issue_view_form_html = f"""
    <form method="post" action="/admin/views/issue" style="margin-top:12px; padding:16px; background:#f5f7fa; border-radius:8px; border:1px dashed #cfd8dc;">
        <div style="display:flex; gap:10px; align-items:center; margin-bottom:12px; flex-wrap:wrap;">
            <label style="font-weight:bold; color:#1a237e;">그룹 이름:</label>
            <input type="text" name="name" placeholder="예: OO요양원 실증, OO병원 실증 등" required style="flex:1; min-width:200px; padding:8px 10px; border:1px solid #ccc; border-radius:6px;">
        </div>
        <div style="margin-bottom:12px;">
            <div style="font-weight:bold; color:#1a237e; margin-bottom:6px;">포함할 기기 (체크):</div>
            {sn_checkboxes}
        </div>
        <div style="text-align:right;">
            <button type="submit" style="padding:8px 16px; background:#16a085; color:white; border:none; border-radius:6px; cursor:pointer; font-weight:bold;">+ 그룹 URL 발급</button>
        </div>
    </form>
    """

    return f"""
    <html>
    <head>
        <title>사용자 URL 관리 · 돌봄기기 통합 대시보드</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: 'Malgun Gothic', sans-serif; padding:20px; background:#f0f2f5; margin:0; }}
            .container {{ max-width: 1000px; margin: auto; background:white; padding:24px; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }}
            table {{ width:100%; border-collapse: collapse; }}
            th {{ background:#e8f0fe; padding:10px; text-align:left; font-size:0.9em; color:#1a237e; }}
            tr {{ border-top: 1px solid #eee; }}
            .info-box {{ background:#fff9e6; border-left:4px solid #ffd54f; padding:12px 16px; margin:16px 0; border-radius:6px; line-height:1.6; }}
        </style>
    </head>
    <body>
        <div class="container">
            <p style="margin:0 0 12px 0;"><a href="/dashboard" style="color:#1a73e8; text-decoration:none;">← 대시보드</a></p>
            <h1 style="color:#1a237e; margin:8px 0;">🔑 사용자 URL 관리</h1>

            <div class="info-box">
                <b>💡 사용 방법</b><br>
                1. 기기별로 <b>+ URL 발급</b> 버튼을 눌러 고유 URL을 만듭니다.<br>
                2. 만들어진 URL을 <b>📋 복사</b> 후 해당 사용자(가족/피험자)에게 카카오톡 등으로 전달합니다.<br>
                3. 사용자는 그 URL만으로 자기 기기 데이터를 볼 수 있습니다(로그인/비밀번호 불필요).<br>
                4. URL이 유출되었거나 사용자가 바뀌면 <b>🗑️ 폐기</b> 후 새로 발급하세요.
            </div>

            <h2 style="color:#1a237e; margin:24px 0 8px 0; font-size:1.15em;">👤 개별 사용자 URL</h2>
            <table>
                <thead>
                    <tr>
                        <th style="width:30%;">기기</th>
                        <th>발급된 사용자 URL</th>
                        <th style="width:140px;">발급</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>

            <h2 style="color:#1a237e; margin:36px 0 8px 0; font-size:1.15em; padding-top:20px; border-top:2px solid #e8f0fe;">👥 그룹 보기 URL</h2>
            <div class="info-box">
                <b>💡 그룹 URL이란?</b><br>
                여러 기기를 한 페이지에 모아 보여주는 URL입니다. 대상 기기 선택 후 URL 발급을 진행하면, 선택된 기기에 대해서만 대시보드가 생성됩니다.
            </div>
            {view_saved_banner}{views_table_html}
            {issue_view_form_html}
        </div>
        <script>
            // HTTPS 아닐 때 navigator.clipboard 가 안 돼서 execCommand fallback 추가
            function copyText(text, btn) {{
                function done(ok) {{
                    btn.textContent = ok ? '✓' : '✗';
                    setTimeout(function() {{ btn.textContent = '📋'; }}, 1500);
                }}
                if (navigator.clipboard && window.isSecureContext) {{
                    navigator.clipboard.writeText(text).then(
                        function() {{ done(true); }},
                        function() {{ done(false); }}
                    );
                    return;
                }}
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                ta.style.left = '-9999px';
                document.body.appendChild(ta);
                ta.select();
                var ok = false;
                try {{ ok = document.execCommand('copy'); }} catch (e) {{}}
                document.body.removeChild(ta);
                done(ok);
            }}
        </script>
    </body>
    </html>
    """


@app.post("/admin/tokens/issue")
async def admin_tokens_issue(request: Request, _: str = Depends(require_admin)):
    form = await request.form()
    sn = (form.get("sn") or "").strip()
    if sn not in analyzer.DEVICE_INFO:
        raise HTTPException(status_code=400, detail="unknown device")
    tokens = _load_tokens()
    new_tok = secrets.token_urlsafe(16)
    while new_tok in tokens:
        new_tok = secrets.token_urlsafe(16)
    tokens[new_tok] = sn
    _save_tokens(tokens)
    return RedirectResponse("/admin/tokens", status_code=303)


@app.post("/admin/tokens/revoke")
async def admin_tokens_revoke(request: Request, _: str = Depends(require_admin)):
    form = await request.form()
    target = (form.get("target") or "").strip()
    tokens = _load_tokens()
    if target in tokens:
        del tokens[target]
        _save_tokens(tokens)
    return RedirectResponse("/admin/tokens", status_code=303)


@app.post("/admin/views/issue")
async def admin_views_issue(request: Request, _: str = Depends(require_admin)):
    """그룹(view) URL 발급. 폼: name + sns (체크박스 다중)."""
    form = await request.form()
    name = (form.get("name") or "").strip()
    sns = [s for s in form.getlist("sns") if s in analyzer.DEVICE_INFO]
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not sns:
        raise HTTPException(status_code=400, detail="at least one device required")
    views = _load_view_tokens()
    tokens = _load_tokens()
    new_tok = secrets.token_urlsafe(16)
    while new_tok in views or new_tok in tokens:
        new_tok = secrets.token_urlsafe(16)
    views[new_tok] = {"name": name, "sns": sns}
    _save_view_tokens(views)
    return RedirectResponse("/admin/tokens", status_code=303)


@app.post("/admin/views/edit")
async def admin_views_edit(request: Request, _: str = Depends(require_admin)):
    """기존 그룹의 이름·기기 목록을 수정한다. **토큰(=URL)은 그대로 둔다.**

    받는 분들에게 이미 나간 주소를 바꾸지 않고 기기만 더하거나 빼기 위한 기능이다.
    (기기가 늘 때마다 새 URL 을 발급해 다시 배포하는 건 현실적이지 않다)"""
    form = await request.form()
    target = (form.get("target") or "").strip()
    views = _load_view_tokens()
    cur = views.get(target)
    if not isinstance(cur, dict):
        raise HTTPException(status_code=400, detail="unknown view token")

    name = (form.get("name") or "").strip() or str(cur.get("name") or "")
    sns = [s for s in form.getlist("sns") if s in analyzer.DEVICE_INFO]
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not sns:
        # 기기를 모두 빼면 그룹 대시보드가 '접근할 수 있는 그룹이 없습니다'가 되어버린다.
        # 폐기하려는 의도라면 폐기 버튼을 써야 하므로, 여기서는 막는다.
        raise HTTPException(status_code=400, detail="at least one device required")

    before = set(cur.get("sns") or [])
    views[target] = {"name": name, "sns": sns}
    _save_view_tokens(views)
    added, removed = set(sns) - before, before - set(sns)
    print(f"[view] 그룹 '{name}' 수정 — 추가 {sorted(added)} / 제외 {sorted(removed)} (URL 유지)", flush=True)
    return RedirectResponse("/admin/tokens?view_saved=1", status_code=303)


@app.post("/admin/views/revoke")
async def admin_views_revoke(request: Request, _: str = Depends(require_admin)):
    form = await request.form()
    target = (form.get("target") or "").strip()
    views = _load_view_tokens()
    if target in views:
        del views[target]
        _save_view_tokens(views)
    return RedirectResponse("/admin/tokens", status_code=303)


# 관리자 진입 단축 — /admin → /dashboard (인증 없으면 자동으로 /login으로)
@app.get("/admin")
def admin_entry():
    return RedirectResponse("/dashboard", status_code=303)


def _safe_next(next_url):
    """open redirect 방지 — 슬래시로 시작하고 // 안 시작하는 path만 허용."""
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/dashboard"
    return next_url


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/dashboard"):
    next_url = _safe_next(next)
    if _is_admin_authenticated(request):
        return RedirectResponse(next_url, status_code=303)
    return HTMLResponse(_login_page_html(next_url=next_url))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = _safe_next(form.get("next") or "/dashboard")
    if not _check_basic_credentials(username, password):
        return HTMLResponse(
            _login_page_html(error="아이디 또는 비밀번호가 올바르지 않습니다.", next_url=next_url),
            status_code=401,
        )
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        _make_session_cookie(),
        max_age=60 * 60 * 24 * 30,  # 30일
        httponly=True,
        samesite="lax",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# 피험자가 받는 URL — /d/{token} 진입 시 쿠키 저장 + 자기 기기 페이지로 리다이렉트
@app.get("/d/{token}", response_class=HTMLResponse)
def view_one_device(token: str):
    tokens = _load_tokens()
    mapped = tokens.get(token)
    if mapped is None or mapped == "*":
        raise HTTPException(status_code=401, detail="invalid token")
    if mapped not in analyzer.DEVICE_INFO:
        return HTMLResponse(
            f"<p style='font-family:sans-serif; padding:40px; text-align:center;'>이 URL에 연결된 기기({mapped})가 더 이상 등록되어 있지 않습니다. 관리자에게 문의해주세요.</p>",
            status_code=404,
        )
    # 쿠키 + URL 토큰 둘 다 — 일부 모바일 브라우저가 fetch(XHR)에 쿠키를 빠뜨리는 걸 대비.
    resp = RedirectResponse(f"/device/{mapped}?token={token}", status_code=303)
    resp.set_cookie(ADMIN_COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


# 루트 GET — 안내 페이지 (POST는 별도 라우트, 데이터 수신용)
@app.get("/", response_class=HTMLResponse)
def root_page():
    return """
    <html><head><meta charset="utf-8"><title>돌봄기기 통합 대시보드</title>
    <style>body{font-family:'Malgun Gothic',sans-serif; padding:40px; background:#f0f2f5;}
    .box{max-width:520px; margin:60px auto; background:white; padding:30px; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,0.06); text-align:center;}</style></head>
    <body><div class="box">
        <h1 style="color:#1a73e8;">📡 돌봄기기 통합 대시보드</h1>
        <p style="color:#546e7a;">전달받으신 개인 URL로 접속해주세요.<br>
        URL을 받지 못하셨다면 관리자에게 문의해주세요.</p>
    </div></body></html>
    """


# 데이터 수신용 (Emfit 서버 전용)
@app.post("/")
async def receive_data(request: Request):
    try:
        data = await request.json()
        # 수신 시간 추가 (데이터 분석용)
        data["server_received_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

        analyzer.ingest_realtime_record(data)
            
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# AI Radar 전용 데이터 수신 경로.
# 기존 Emfit POST / 처리와 분리하여 서로의 payload를 혼동하지 않게 한다.
@app.post("/radar")
async def receive_radar_data(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    # RMR602A API 문서의 두 형식을 구분한다.
    # BED: MAC/POS/BR/HR/ERR, FALL: macAddress/pose/pnum/좌표
    is_bed = bool(data.get("MAC")) and "POS" in data
    is_fall = bool(data.get("macAddress")) and "pose" in data
    if not (is_bed or is_fall):
        raise HTTPException(
            status_code=400,
            detail="unsupported radar payload: MAC+POS or macAddress+pose required",
        )

    record = dict(data)
    # PDF 예시에는 ERR 키 앞에 공백이 있는 표기가 있어 둘 다 허용한다.
    if "ERR" not in record and " ERR" in record:
        record["ERR"] = record[" ERR"]
    record["server_received_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record["data_source"] = "ai_radar"
    record["radar_model"] = "bed" if is_bed else "fall"

    try:
        with open(RADAR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"log write failed: {e}")

    analyzer.ingest_realtime_record(record)

    # ── NRCarec 경보 ──────────────────────────────────────────────
    # BED 모델의 POS(3=걸터앉음, 4=낙상), FALL 모델의 pose(4=낙상)를 본다.
    # 레이더가 1초마다 보내므로 should_send 로 반복 발송을 막는다.
    try:
        _pos = record.get("POS") if is_bed else record.get("pose")
        _pos = int(_pos) if _pos is not None else None
        _kind = {3: "bedside", 4: "fall"}.get(_pos)
        # FALL 모델에는 걸터앉음이 없다(2=재실/4=낙상/5=자리비움).
        if _kind and not (is_fall and _kind == "bedside"):
            _sn = record.get("MAC") or record.get("macAddress") or "radar"
            if should_send(_sn, _kind):
                _room, _patient_name = _nrcarec_patient_context(_sn)
                send_alert(_kind, room=_room, patient_name=_patient_name, device_id=_sn)
    except Exception as _e:
        # 알림이 실패해도 센서 수신은 계속돼야 한다.
        print(f"[NRCarec] 경보 발송 실패: {_e}")

    return {
        "status": "success",
        "source": "ai_radar",
        "model": record["radar_model"],
    }


# ── McKare(JCFT VSR22) 전용 데이터 수신 경로 ───────────────────────────
# Emfit(POST /)·라닉스(POST /radar)와 분리해 payload 혼동을 막는다.
# ⚠️ 성공 시 반드시 201 Created 로 응답한다 — McKare 센서 펌웨어가 201 을 성공으로
#    판단하므로, 200 을 주면 센서가 실패로 보고 재전송을 반복할 수 있다(문서 10장).
_MCKARE_REQUIRED = ["macAddress", "wifiRssi", "respirationDetection", "activityDetection",
                    "respirationRate", "heartRate", "fallDetection", "temperature"]


def _load_mckare_apikey():
    """mckare_apikey.txt 가 있고 비어있지 않으면 그 값을 반환(ApiKey 검증에 사용).
    파일이 없으면 None → ApiKey 미검증(라닉스처럼 열어서 수신)."""
    if not os.path.exists(MCKARE_APIKEY_FILE):
        return None
    try:
        with open(MCKARE_APIKEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        return key or None
    except Exception:
        return None


# JCFT 표준 경로 — 문서(JCFT-MCK-API-IM-001) 6장의
#   https://{mckare-api-address}/data-receiver/device-measurement
# 를 그대로 흉내 낸다. 센서 펌웨어가 이 경로로 쏘게 되어 있어서, 우리 쪽이
# 그 모양을 갖춰줘야 한다. 기존 /mckare 는 이미 쓰고 있으므로 별칭으로 남긴다.
# 하이픈이 문서상 정식이지만, 언더스코어로 잘못 전달되는 경우가 잦아 둘 다 받는다.
# 문서의 {mckare-api-address} 가 '호스트만'인지 '경로까지 포함'인지 명시돼 있지 않다.
# 펌웨어에 무엇을 넣든 닿도록 접두어 있는 형태와 없는 형태를 모두 연다.
# (경로만 다르고 처리는 완전히 동일하므로 열어둬도 부작용이 없다)
_MCKARE_PATHS = [
    "/data-receiver/device-measurement",          # 문서 정식 (호스트만 설정하는 경우)
    "/mckare/data-receiver/device-measurement",   # base 에 /mckare 까지 넣는 경우
    "/data_receiver/device_measurement",          # 언더스코어 표기 대응
    "/mckare/data_receiver/device_measurement",
    "/mckare",                                    # 기존 경로 (하위 호환)
]

# 이미지 수신 경로 — AI 110(열화상 탑재) 대응. VSR 22 문서에는 없는 규격이다.
_MCKARE_IMAGE_PATHS = [
    "/data-receiver/device-image",
    "/mckare/data-receiver/device-image",
    "/data_receiver/device_image",
    "/mckare/data_receiver/device_image",
]


@app.post("/mckare")
async def receive_mckare_data(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"statusCode": 400, "message": "invalid JSON", "error": "Bad Request"},
                            status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"statusCode": 400, "message": "JSON object required", "error": "Bad Request"},
                            status_code=400)

    # (선택) ApiKey 검증 — 키 파일이 설정돼 있을 때만. JCFT 가 센서에 키를 넣을 수 있으면 활성화.
    expected = _load_mckare_apikey()
    if expected and request.headers.get("ApiKey") != expected:
        return JSONResponse({"statusCode": 401, "message": "API key is missing or invalid."},
                            status_code=401)

    missing = [k for k in _MCKARE_REQUIRED if k not in data]
    if missing:
        return JSONResponse({"statusCode": 400,
                             "message": [f"{k} is required." for k in missing],
                             "error": "Bad Request"}, status_code=400)

    record = dict(data)
    record["server_received_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record["data_source"] = "mckare"

    try:
        with open(MCKARE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        return JSONResponse({"statusCode": 500, "message": f"log write failed: {e}"}, status_code=500)

    analyzer.ingest_realtime_record(record)

    # McKare 규격에 맞춘 201 Created 응답 (statusCode/message 형식도 문서와 동일하게)
    return JSONResponse({"statusCode": 201, "message": "created"}, status_code=201)


# 표준 경로들을 같은 처리기에 연결한다. (경로만 다르고 동작은 동일)
for _p in _MCKARE_PATHS:
    if _p != "/mckare":
        app.add_api_route(_p, receive_mckare_data, methods=["POST"])


# ── McKare AI 110 열화상 이미지 수신 ──────────────────────────────────
# ⚠️ JCFT 문서(VSR 22 기준)에는 이미지 규격이 없다. AI 110 은 열화상이 붙은
#    다른 모델이라 별도 규격이 있을 텐데 아직 못 받았다.
#    그래서 흔히 쓰이는 세 가지 방식을 모두 받아둔다 — 규격을 몰라도 데이터를 잃지 않고,
#    실제로 뭐가 들어오는지 보면 규격을 역으로 확인할 수 있다.
#      (1) multipart/form-data  — 파일 업로드의 표준. 가장 가능성 높음
#      (2) application/json     — base64 문자열로 담아 보내는 방식
#      (3) 원시 바이너리         — Content-Type: image/jpeg 등으로 본문에 그대로
_IMAGE_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/bmp": ".bmp", "image/webp": ".webp", "application/octet-stream": ".bin",
}
# 파일 시그니처로 실제 형식을 판별한다 (Content-Type 을 못 믿는 경우 대비)
_IMAGE_MAGIC = [
    (b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"BM", ".bmp"), (b"RIFF", ".webp"), (b"GIF8", ".gif"),
]


def _guess_image_ext(blob, content_type=""):
    """바이트 앞부분(시그니처)으로 형식 판별. 모르면 Content-Type, 그것도 없으면 .bin."""
    for magic, ext in _IMAGE_MAGIC:
        if blob.startswith(magic):
            return ext
    return _IMAGE_EXT.get((content_type or "").split(";")[0].strip().lower(), ".bin")


def _mckare_image_mac(*sources):
    """여러 곳(폼 필드·JSON·헤더·쿼리)에서 기기 식별자를 찾는다.
    필드 이름을 모르므로 흔한 후보를 모두 훑는다."""
    keys = ("macAddress", "mac_address", "mac", "deviceId", "device_id", "device", "serial")
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in keys:
            for actual in src:
                if str(actual).lower() == k.lower() and src[actual]:
                    compact = str(src[actual]).strip().replace(":", "").replace("-", "").upper()
                    if compact:
                        return compact
    return "UNKNOWN"


async def receive_mckare_image(request: Request):
    """열화상 이미지 수신. 형식을 가리지 않고 받아 파일로 저장하고 목록에 기록한다."""
    expected = _load_mckare_apikey()
    if expected and request.headers.get("ApiKey") != expected:
        return JSONResponse({"statusCode": 401, "message": "API key is missing or invalid."},
                            status_code=401)

    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    blob, meta, how = None, {}, ""

    try:
        if ctype == "multipart/form-data":
            form = await request.form()
            for key, val in form.multi_items():
                if hasattr(val, "read"):                 # 업로드된 파일
                    blob = await val.read()
                    meta["field"] = key
                    meta["filename"] = getattr(val, "filename", "") or ""
                    meta["upload_content_type"] = getattr(val, "content_type", "") or ""
                else:
                    meta[key] = str(val)                 # 같이 온 텍스트 필드
            how = "multipart"
        elif ctype == "application/json":
            data = await request.json()
            if not isinstance(data, dict):
                return JSONResponse({"statusCode": 400, "message": "JSON object required",
                                     "error": "Bad Request"}, status_code=400)
            # base64 가 담겼을 만한 필드 이름을 모두 뒤진다
            for k in ("image", "imageData", "image_data", "data", "file",
                      "thermal", "thermalImage", "base64", "content"):
                for actual in data:
                    if str(actual).lower() == k.lower() and isinstance(data[actual], str) and data[actual]:
                        raw = data[actual]
                        if "," in raw[:64] and raw[:5] == "data:":   # data:image/jpeg;base64,....
                            raw = raw.split(",", 1)[1]
                        try:
                            import base64 as _b64
                            blob = _b64.b64decode(raw, validate=False)
                            meta["field"] = actual
                        except Exception:
                            pass
                        break
                if blob is not None:
                    break
            meta.update({k: v for k, v in data.items() if not isinstance(v, (dict, list))
                         and len(str(v)) < 200})
            how = "json-base64"
        else:
            blob = await request.body()                  # 원시 바이너리
            how = "raw"
    except Exception as e:
        return JSONResponse({"statusCode": 400, "message": f"could not read body: {e}",
                             "error": "Bad Request"}, status_code=400)

    if not blob:
        return JSONResponse({"statusCode": 400,
                             "message": "image payload not found. "
                                        "send multipart file, JSON base64, or raw image body.",
                             "error": "Bad Request"}, status_code=400)
    if len(blob) > MCKARE_IMAGE_MAX_BYTES:
        return JSONResponse({"statusCode": 413,
                             "message": f"image too large ({len(blob)} bytes)"}, status_code=413)

    now = datetime.now(KST)
    mac = _mckare_image_mac(meta, dict(request.query_params), dict(request.headers))
    ext = _guess_image_ext(blob, meta.get("upload_content_type") or ctype)
    day_dir = os.path.join(MCKARE_IMAGE_DIR, now.strftime("%Y-%m-%d"))
    fname = f"{mac}_{now.strftime('%H%M%S')}_{secrets.token_hex(3)}{ext}"
    path = os.path.join(day_dir, fname)

    try:
        os.makedirs(day_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
        with open(MCKARE_IMAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "macAddress": mac,
                "server_received_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "path": path.replace("\\", "/"),
                "bytes": len(blob),
                "format": ext.lstrip("."),
                "how": how,                 # 어떤 방식으로 왔는지 — 규격 확인용
                "content_type": ctype,
                "meta": meta,               # 같이 온 필드 전부 (규격 파악에 쓴다)
                "data_source": "mckare_image",
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        return JSONResponse({"statusCode": 500, "message": f"image save failed: {e}"},
                            status_code=500)

    print(f"[mckare] 이미지 수신: {mac} {len(blob)}바이트 {ext} ({how}) → {path}", flush=True)
    return JSONResponse({"statusCode": 201, "message": "created"}, status_code=201)


for _p in _MCKARE_IMAGE_PATHS:
    app.add_api_route(_p, receive_mckare_image, methods=["POST"])


# ── ESP32 압력 사용감지 센서 전용 데이터 수신 경로 ──────────────────────
# 돌봄기기에 부착해 '지금 쓰이고 있는가'를 보는 센서. 다른 센서들과 분리해
# payload 혼동을 막는다. 보드가 event 를 그때그때 쏘는 방식이라 전송량은 적지만,
# 사용 시작/종료가 짝을 이뤄야 의미가 있어 유실에 민감하다.
_FSR_REQUIRED = ["deviceId", "event"]


@app.post("/jy01")
async def receive_fsr_data(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"invalid JSON: {e}"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"status": "error", "message": "JSON object required"}, status_code=400)

    missing = [k for k in _FSR_REQUIRED if k not in data]
    if missing:
        return JSONResponse(
            {"status": "error", "message": f"missing field(s): {', '.join(missing)}"},
            status_code=400,
        )

    record = dict(data)
    record["server_received_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    record["data_source"] = "fsr"

    try:
        with open(FSR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        # 저장 실패는 반드시 5xx 로 알려야 보드가 재전송할 수 있다.
        return JSONResponse({"status": "error", "message": f"log write failed: {e}"},
                            status_code=500)

    analyzer.ingest_realtime_record(record)

    try:
        _fsr_last_update(record)
    except Exception:
        pass

    return {"status": "success", "source": "fsr", "device": str(data.get("deviceId"))}

# ============================================================================
#  원격 FSR 튜닝 — app.py 맨 아래 (`if __name__ == "__main__":` 앞) 에 붙여넣기
#
#  게이트웨이는 사용자 집 공유기 뒤에 있어 서버가 먼저 접속할 수 없다.
#  그래서 게이트웨이가 주기적으로 물어보는 폴링 구조로 만든다.
#
#      게이트웨이 --(N초마다)--> GET  /jy01/cmd?gw=gw01
#                 <------------ ECE33445000,th1,900,42   (CSV 한 줄)
#                 --(적용 후)--> POST /jy01/cmd/ack
#
#  노드가 딥슬립 중이면 어떤 방식으로도 즉시 전달할 수 없다. 전화로 값을
#  맞추려면 사용자가 노드 버튼을 3초 눌러 FSR 모드로 들여야 하고, 그때는
#  노드가 1초마다 통신하므로 게이트웨이 폴링만 짧으면 실시간에 가까워진다.
#  게이트웨이는 FSR 모드 노드가 있으면 폴링을 5초로 자동 전환한다.
# ============================================================================

GW_CMD_FILE = "gw_commands.json"   # 대기·완료 명령 이력
_gw_cmd_lock = threading.Lock()

GW_CMD_SENT_TTL = 90 # jy 추가, gw_poll_command_new

# 게이트웨이가 FSR 모드 노드의 실시간 값을 올려주는 곳.
# 초당 1건이라 파일에 쓰면 금방 커지므로 메모리에만 둔다. 서버가 재시작되면
# 사라지지만, 튜닝 중에만 쓰는 값이라 문제되지 않는다.
_fsr_live = {}                     # mac -> {"ts":…, "gw":…, "fsr":[…], …}
_fsr_live_lock = threading.Lock()
FSR_LIVE_TTL = 30                  # 초. 이보다 오래된 값은 '끊김'으로 본다

# 노드에 내릴 수 있는 명령. (표시이름, 최소, 최대) — None이면 값 없는 동작 명령
GW_CMD_SPEC = {
    "th":     ("임계 상승폭 (전체)", 30, 3000),
    "th1":    ("임계 상승폭 1번",    30, 3000),
    "th2":    ("임계 상승폭 2번",    30, 3000),
    "th3":    ("임계 상승폭 3번",    30, 3000),
    "hyst":   ("해제 비율 (전체)",   30, 100),
    "hyst1":  ("해제 비율 1번",      30, 100),
    "hyst2":  ("해제 비율 2번",      30, 100),
    "hyst3":  ("해제 비율 3번",      30, 100),
    "nhit":   ("판정 센서 수",        1, 3),
    "poll":   ("폴링 주기(초)",       5, 3600),
    "hb":     ("생존신고 주기(초)",  30, 86400),
    "led":    ("LED (0/1)",           0, 1),
    "base":   ("무부하 기준 측정",  None, None),
    "cal":    ("캘리브레이션 시작", None, None),
    "calend": ("캘리브레이션 종료", None, None),
    "apply":  ("산정값 적용",        None, None),
    "exit":   ("FSR 모드 종료",     None, None),
    "reboot": ("노드 재부팅",        None, None),
}


def _load_gw_cmds():
    try:
        with open(GW_CMD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except Exception:
        pass
    return {"next_id": 1, "items": []}


def _save_gw_cmds(data):
    tmp = GW_CMD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, GW_CMD_FILE)


def _gw_cmd_prune(data, keep=200):
    """완료·만료 항목이 무한정 쌓이지 않게 최근 것만 남긴다."""
    items = data["items"]
    if len(items) > keep:
        done = [i for i in items if i["status"] != "pending"]
        pend = [i for i in items if i["status"] == "pending"]
        data["items"] = pend + done[-keep:]


def _gw_cmd_add(mac, cmd, val, gw=""):
    """명령을 대기열에 넣는다. 같은 노드의 같은 항목이 이미 대기 중이면
    덮어쓴다 — 값을 두 번 바꾸면 마지막 것만 의미가 있기 때문."""
    mac = (mac or "").upper().replace(":", "").replace("-", "")
    with _gw_cmd_lock:
        data = _load_gw_cmds()
        for it in data["items"]:
            if it["status"] == "pending" and it["mac"] == mac and it["cmd"] == cmd:
                it["val"] = val
                it["at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                _save_gw_cmds(data)
                return it["id"]
        cid = data["next_id"]
        data["next_id"] = cid + 1
        data["items"].append({
            "id": cid, "mac": mac, "cmd": cmd, "val": val, "gw": gw,
            "status": "pending", "result": "",
            "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "done_at": "",
        })
        _gw_cmd_prune(data)
        _save_gw_cmds(data)
        return cid

# 게이트웨이가 가져갔지만 결과 보고가 없는 명령을 언제 다시 내줄지.
#
# 노드는 딥슬립 주기(기본 poll 5초)만큼 늦게 받고, 게이트웨이도 최대
# 30초 주기로 폴링한다. 여기에 재전송 여유를 더해 90초로 잡았다.
# 더 짧으면 아직 전달 중인 명령을 중복 발행하고, 길면 게이트웨이가
# 재부팅했을 때 사용자가 그만큼 오래 기다린다.


@app.get("/jy01/cmd", response_class=PlainTextResponse)
def gw_poll_command(gw: str = ""):
    """게이트웨이가 대기 명령을 가져간다.

    CSV 한 줄로 응답한다 — 보드에서 JSON 파서를 쓰지 않아도 되고,
    IRAM 여유가 빠듯한 상황에서 라이브러리를 하나 덜 넣을 수 있다.

        MAC,항목,값,명령ID
        none                (대기 없음)

    한 번에 하나씩만 준다. 여러 개를 몰아주면 중간에 실패했을 때
    어디까지 적용됐는지 서버가 알 수 없다.

    ★ 같은 노드에 대해서는 앞 명령의 결과 보고를 받기 전까지 다음 명령을
      내주지 않는다.

      게이트웨이는 노드당 대기 명령을 하나만 들고 있어서, 결과를 안 기다리고
      연달아 내주면 앞의 것이 조용히 덮여 사라진다. 튜닝 화면에서 th1·th2·th3
      을 차례로 누르는 것은 정상적인 사용 흐름인데, 이전 구현에서는 마지막
      하나만 적용되고 나머지는 화면에 '대기'로 영원히 남았다.

      노드가 다르면 서로 영향이 없으므로 그대로 진행한다.

    ★ 가져간 지 GW_CMD_SENT_TTL 초가 지나도록 보고가 없으면 다시 대기로
      되돌린다. 게이트웨이의 대기 명령은 RAM에만 있어 재부팅하면 사라지는데,
      이 복구가 없으면 서버는 'sent' 상태로 멈춘 채 재발행하지 않는다.
    """
    now = datetime.now(KST)

    with _gw_cmd_lock:
        data = _load_gw_cmds()
        dirty = False

        # 1) 응답 없이 오래된 sent 를 회수한다
        for it in data["items"]:
            if it["status"] != "sent":
                continue
            sent_at = it.get("sent_at")
            if not sent_at:
                it["status"] = "pending"
                dirty = True
                continue
            try:
                t = datetime.strptime(sent_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except ValueError:
                it["status"] = "pending"
                dirty = True
                continue
            if (now - t).total_seconds() > GW_CMD_SENT_TTL:
                it["status"] = "pending"
                it["retries"] = int(it.get("retries", 0)) + 1
                dirty = True

        # 2) 아직 결과를 기다리는 중인 노드는 건너뛴다
        busy = {it["mac"] for it in data["items"] if it["status"] == "sent"}

        for it in data["items"]:
            if it["status"] != "pending":
                continue
            if it.get("gw") and gw and it["gw"] != gw:
                continue
            if it["mac"] in busy:
                continue

            it["status"] = "sent"
            it["sent_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            _save_gw_cmds(data)
            val = it["val"] if it["val"] is not None else 0
            return f"{it['mac']},{it['cmd']},{val},{it['id']}"

        if dirty:
            _save_gw_cmds(data)

    return "none"



@app.post("/jy01/cmd/ack")
async def gw_ack_command(request: Request):
    """노드가 명령을 적용했다고 게이트웨이가 보고한다."""
    try:
        data_in = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)

    cid = data_in.get("id")
    result = str(data_in.get("result") or "ok")
    if cid is None:
        return JSONResponse({"status": "error", "message": "id required"},
                            status_code=400)

    with _gw_cmd_lock:
        data = _load_gw_cmds()
        for it in data["items"]:
            if it["id"] == int(cid):
                it["status"] = "done" if result == "ok" else "fail"
                it["result"] = result
                it["done_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                _save_gw_cmds(data)
                return {"status": "success"}
    return {"status": "success", "note": "unknown id"}


@app.post("/jy01/live")
async def gw_fsr_live(request: Request):
    """FSR 모드 노드의 실시간 센서값. 파일에 쓰지 않고 메모리에만 둔다."""
    try:
        d = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)

    mac = str(d.get("mac") or "").upper()
    if not mac:
        return JSONResponse({"status": "error", "message": "mac required"},
                            status_code=400)

    with _fsr_live_lock:
        _fsr_live[mac] = {
            "ts": datetime.now(KST).timestamp(),
            "at": datetime.now(KST).strftime("%H:%M:%S"),
            "gw": str(d.get("gw") or ""),
            "name": str(d.get("name") or ""),
            "fsr": d.get("fsr") or [0, 0, 0],
            "base": d.get("base") or [0, 0, 0],
            "th": d.get("th") or [0, 0, 0],
            "hyst": d.get("hyst") or [0, 0, 0],
            "mask": int(d.get("mask") or 0),
            "nhit": int(d.get("nhit") or 1),
            "batt": int(d.get("batt") or 0),
            "rssi": int(d.get("rssi") or 0),
            "cal": int(d.get("cal") or 0),
        }
    return {"status": "success"}


@app.get("/api/fsr/live")
def api_fsr_live(_: str = Depends(require_admin)):
    """튜닝 화면이 1초마다 읽어가는 실시간 값."""
    now = datetime.now(KST).timestamp()
    out = {}
    with _fsr_live_lock:
        for mac, v in _fsr_live.items():
            age = now - v["ts"]
            item = dict(v)
            item["age"] = round(age, 1)
            item["stale"] = age > FSR_LIVE_TTL
            out[mac] = item

    with _gw_cmd_lock:
        cmds = _load_gw_cmds()["items"]
    recent = [c for c in cmds if c["status"] == "pending" or c["status"] == "sent"]
    done = [c for c in cmds if c["status"] in ("done", "fail")][-8:]
    return {"live": out, "pending": recent, "recent": list(reversed(done))}


@app.post("/api/fsr/cmd")
async def api_fsr_cmd(request: Request, _: str = Depends(require_admin)):
    """튜닝 화면에서 명령을 등록한다."""
    try:
        d = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)

    mac = str(d.get("mac") or "").strip()
    cmd = str(d.get("cmd") or "").strip()
    if not mac or cmd not in GW_CMD_SPEC:
        return JSONResponse({"status": "error", "message": "bad mac/cmd"},
                            status_code=400)

    label, lo, hi = GW_CMD_SPEC[cmd]
    val = None
    if lo is not None:
        try:
            val = int(d.get("val"))
        except Exception:
            return JSONResponse({"status": "error", "message": "value required"},
                                status_code=400)
        if not (lo <= val <= hi):
            return JSONResponse(
                {"status": "error", "message": f"{label} 범위 {lo}~{hi}"},
                status_code=400)

    gw = str(d.get("gw") or "")
    if not gw:
        with _fsr_last_lock:
            gw = str((_fsr_last.get(mac) or {}).get("gw") or "")

    cid = _gw_cmd_add(mac, cmd, val, gw)
    return {"status": "success", "id": cid}


@app.get("/fsr-tune", response_class=HTMLResponse)
def view_fsr_tune(_: str = Depends(require_admin)):
    """원격 FSR 튜닝 화면.

    전화로 사용자와 통화하며 값을 맞추는 용도라 실시간성이 중요하다.
    1초마다 값을 갱신하고, 명령은 누르는 즉시 대기열에 들어간다.
    """
    return """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FSR 원격 튜닝</title>
<style>
 body{font-family:-apple-system,'Malgun Gothic',sans-serif;margin:0;
      background:#eceff1;color:#263238}
 .wrap{max-width:900px;margin:0 auto;padding:16px}
 h1{font-size:1.3em;margin:8px 0 4px}
 .sub{color:#607d8b;font-size:.9em;margin-bottom:16px;line-height:1.6}
 .card{background:#fff;border-radius:10px;padding:16px;margin-bottom:14px;
       box-shadow:0 1px 3px rgba(0,0,0,.12)}
 .name{font-size:1.15em;font-weight:bold}
 .meta{color:#78909c;font-size:.85em;margin-top:2px}
 .stale{background:#ffebee;border-left:4px solid #e53935}
 .sensors{display:flex;gap:10px;margin:14px 0}
 .sen{flex:1;background:#f5f7f8;border-radius:8px;padding:10px;text-align:center}
 .sen.hit{background:#e8f5e9;box-shadow:inset 0 0 0 2px #43a047}
 .sen .n{font-size:.75em;color:#90a4ae}
 .sen .v{font-size:1.5em;font-weight:bold;margin:2px 0}
 .sen .t{font-size:.72em;color:#78909c}
 .bar{height:6px;background:#e0e0e0;border-radius:3px;margin-top:6px;overflow:hidden}
 .bar i{display:block;height:100%;background:#43a047;width:0}
 .row{display:flex;gap:6px;align-items:center;margin:6px 0;flex-wrap:wrap}
 .row label{width:110px;font-size:.85em;color:#546e7a}
 input[type=number]{width:80px;padding:6px;border:1px solid #cfd8dc;border-radius:6px}
 button{padding:7px 12px;border:0;border-radius:6px;background:#00897b;color:#fff;
        cursor:pointer;font-size:.88em}
 button:hover{background:#00695c}
 button.g{background:#546e7a} button.g:hover{background:#37474f}
 button.r{background:#c62828} button.r:hover{background:#8e0000}
 .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px;
       padding-top:12px;border-top:1px solid #eceff1}
 .log{font-size:.8em;color:#607d8b;margin-top:10px;line-height:1.7}
 .badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.75em;
        margin-right:5px}
 .b-pend{background:#fff3e0;color:#e65100}
 .b-done{background:#e8f5e9;color:#2e7d32}
 .b-fail{background:#ffebee;color:#c62828}
 .empty{text-align:center;color:#90a4ae;padding:50px 20px;line-height:1.9}
</style></head><body><div class="wrap">
<h1>FSR 원격 튜닝</h1>
<div class="sub">
 사용자에게 <b>노드 버튼을 3초 누르라고</b> 안내하면 FSR 모드로 들어가 값이 여기 나타납니다.<br>
 딥슬립 중인 노드에는 명령이 즉시 전달되지 않습니다 — 반드시 FSR 모드에서 조정하세요.
</div>
<div id="list"></div>
<div class="card"><div style="font-weight:bold;margin-bottom:8px">최근 명령</div>
 <div id="hist" class="log">-</div></div>
</div>
<script>
const $=s=>document.querySelector(s);
let LIVE={};

async function send(mac,cmd,valSel){
  let val=null;
  if(valSel){
    const el=document.querySelector(valSel);
    if(!el||el.value==='') { alert('값을 입력하세요'); return; }
    val=parseInt(el.value);
  }
  const r=await fetch('/api/fsr/cmd',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mac,cmd,val})});
  const j=await r.json();
  if(j.status!=='success') alert(j.message||'실패');
  tick();
}

function card(mac,d){
  const hit=i=>(d.mask>>i)&1;
  const sens=[0,1,2].map(i=>{
    const on=d.base[i]+d.th[i];
    const pct=Math.min(100,Math.max(0,(d.fsr[i]-d.base[i])/Math.max(1,d.th[i])*100));
    return `<div class="sen ${hit(i)?'hit':''}">
      <div class="n">${i+1}번</div><div class="v">${d.fsr[i]}</div>
      <div class="t">기준 ${d.base[i]} · 감지 ${on}</div>
      <div class="bar"><i style="width:${pct}%"></i></div></div>`;
  }).join('');

  const num=(cmd,label,ph)=>`<div class="row">
     <label>${label}</label>
     <input type="number" id="i-${mac}-${cmd}" placeholder="${ph}">
     <button onclick="send('${mac}','${cmd}','#i-${mac}-${cmd}')">전송</button></div>`;

  return `<div class="card ${d.stale?'stale':''}">
    <div class="name">${d.name||mac} ${d.cal?'<span class="badge b-pend">CAL 측정중</span>':''}</div>
    <div class="meta">${mac} · ${d.gw} · 배터리 ${d.batt}% · ${d.rssi}dBm
      · ${d.stale?'<b style="color:#c62828">'+Math.round(d.age)+'초 끊김</b>':d.at}</div>
    <div class="sensors">${sens}</div>
    ${num('th1','임계 1번','상승폭 mV')}
    ${num('th2','임계 2번','상승폭 mV')}
    ${num('th3','임계 3번','상승폭 mV')}
    ${num('th','임계 일괄','상승폭 mV')}
    ${num('hyst','해제 비율','30~100')}
    ${num('nhit','판정 센서 수','1~3')}
    <div class="acts">
      <button class="g" onclick="send('${mac}','base')">기준 측정</button>
      <button class="g" onclick="send('${mac}','cal')">CAL 시작</button>
      <button class="g" onclick="send('${mac}','calend')">CAL 종료</button>
      <button onclick="send('${mac}','apply')">산정값 적용</button>
      <button class="g" onclick="send('${mac}','exit')">FSR 모드 종료</button>
      <button class="r" onclick="if(confirm('노드를 재부팅합니다'))send('${mac}','reboot')">재부팅</button>
    </div></div>`;
}

async function tick(){
  try{
    const j=await(await fetch('/api/fsr/live')).json();
    LIVE=j.live;
    const macs=Object.keys(LIVE);
    $('#list').innerHTML = macs.length
      ? macs.map(m=>card(m,LIVE[m])).join('')
      : `<div class="card empty">FSR 모드인 노드가 없습니다.<br>
          사용자에게 노드 버튼을 3초 누르라고 안내하세요.<br>
          <span style="font-size:.9em">흰색 LED가 길게 한 번 켜지면 진입한 것입니다.</span></div>`;

    let h='';
    (j.pending||[]).forEach(c=>{
      h+=`<div><span class="badge b-pend">대기</span>${c.mac} ${c.cmd}`
       + (c.val!==null?' = '+c.val:'')+` <span style="color:#b0bec5">${c.at}</span></div>`;
    });
    (j.recent||[]).forEach(c=>{
      const b=c.status==='done'?'b-done':'b-fail';
      h+=`<div><span class="badge ${b}">${c.status==='done'?'완료':'실패'}</span>`
       + `${c.mac} ${c.cmd}`+(c.val!==null?' = '+c.val:'')
       + ` <span style="color:#b0bec5">${c.done_at||c.at}</span></div>`;
    });
    $('#hist').innerHTML = h || '<span style="color:#b0bec5">없음</span>';
  }catch(e){}
}
tick(); setInterval(tick,1000);
</script></body></html>"""

# ============================================================================
#  하트비트 원격 조정 — app.py 에 추가할 내용
#
#  기존 /fsr-tune 은 FSR 모드 노드만 다룬다. 값을 실시간으로 보면서 반복
#  조정하는 화면이라 그게 맞다. 다만 사용자에게 전화해서 "버튼 3초 눌러
#  주세요" 를 매번 부탁해야 한다.
#
#  이 화면은 반대다. 이미 아는 값을 조용히 한 번 넣는 용도.
#  노드는 하트비트(기본 240초)마다 깨서 신호를 보내고, 게이트웨이는 그때
#  대기 중인 명령을 함께 실어 보낸다. 사용자는 아무것도 하지 않아도 된다.
#  대신 반영까지 최대 하트비트 주기만큼 걸리고, 결과 확인은 그다음
#  하트비트에나 가능하다.
#
#  게이트웨이·노드 펌웨어 수정은 필요 없다. 명령 발행은 기존
#  /api/fsr/cmd 를 그대로 쓴다.
#
#  ★ 단, 게이트웨이가 이벤트 JSON 에 mac/base/th/hyst/hb 를 실어 보내야
#    한다. 그 버전으로 굽지 않았다면 이 화면에 노드가 뜨긴 하지만 현재
#    설정값이 비어 있고 명령도 낼 수 없다 (MAC 을 모르기 때문).
# ============================================================================


# ────────────────────────────────────────────────────────────────────────────
#  [1] receive_fsr_data() 안에 세 줄 추가
#
#      기존 코드에서 이 부분을 찾아
#
#          return {"status": "success", "source": "fsr", "device": str(data.get("deviceId"))}
#
#      바로 위에 아래 세 줄을 넣는다. (들여쓰기 4칸)
#
#          try:
#              _fsr_last_update(record)
#          except Exception:
#              pass          # 화면용 부가기능이라 수신 자체를 막으면 안 된다
# ────────────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────────────
#  [2] 아래 전체를 app.py 맨 아래 (`if __name__ == "__main__":` 앞) 에 붙여넣기
# ────────────────────────────────────────────────────────────────────────────

# 노드별 마지막 상태. 파일에 쓰지 않고 메모리에만 둔다 —
# 원본은 이미 FSR_LOG_FILE 에 남고, 이건 화면 표시용 캐시일 뿐이다.
_fsr_last = {}
_fsr_last_lock = threading.Lock()


def _fsr_last_update(record):
    """POST /jy01 로 들어온 이벤트에서 노드 상태를 갱신한다.

    mac 이 없으면 저장하지 않는다. 화면의 목적이 원격 조정인데 MAC 이
    없으면 명령을 낼 수 없어서, 조정할 수 없는 카드를 띄우는 것은 오히려
    혼란스럽다. (게이트웨이 펌웨어가 구버전이면 mac 이 없다)
    """
    mac = str(record.get("mac") or "").upper()
    if not mac:
        return

    with _fsr_last_lock:
        _fsr_last[mac] = {
            "ts":    datetime.now(KST).timestamp(),
            "at":    datetime.now(KST).strftime("%m-%d %H:%M:%S"),
            "name":  str(record.get("deviceId") or ""),
            "gw":    str(record.get("gw") or ""),
            "event": str(record.get("event") or ""),
            "fsr":   record.get("fsr")  or [0, 0, 0],
            "base":  record.get("base") or [0, 0, 0],
            "th":    record.get("th")   or [0, 0, 0],
            "hyst":  record.get("hyst") or [0, 0, 0],
            "mask":  int(record.get("mask") or 0),
            "nhit":  int(record.get("nhit") or 1),
            "hb":    int(record.get("hb") or 0),
            "batt":  int(record.get("battery_pct") or 0),
            "rssi":  int(record.get("rssi") or 0),
            "drops": int(record.get("drops") or 0),
        }


@app.get("/api/fsr/nodes")
def api_fsr_nodes(_: str = Depends(require_admin)):
    """조정 화면이 주기적으로 읽어가는 노드 목록."""
    now = datetime.now(KST).timestamp()
    out = []

    with _fsr_last_lock:
        for mac, v in _fsr_last.items():
            it = dict(v)
            it["mac"] = mac
            age = now - v["ts"]
            it["age"] = int(age)

            # 무소식 판정은 노드가 알려준 hb 에서 계산한다. 노드마다 hb 를
            # 다르게 둘 수 있어 서버가 상수로 추측하면 멀쩡한 노드가 끊김으로
            # 뜬다. 게이트웨이와 같은 배수(2.5)를 쓴다.
            hb = v["hb"] or 240
            it["stale"] = age > max(hb * 2.5, 60)
            it["wait"]  = hb            # 명령 반영까지 최대 이만큼
            out.append(it)

    out.sort(key=lambda x: x["name"] or x["mac"])

    with _gw_cmd_lock:
        cmds = _load_gw_cmds()["items"]
    pending = [c for c in cmds if c["status"] in ("pending", "sent")]
    recent  = [c for c in cmds if c["status"] in ("done", "fail")][-10:]
    return {"nodes": out, "pending": pending, "recent": list(reversed(recent))}


@app.get("/fsr-nodes", response_class=HTMLResponse)
def view_fsr_nodes(_: str = Depends(require_admin)):
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FSR 노드 설정</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      background:#eef1f5;margin:0;padding:16px;color:#222}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:#667;font-size:13px;margin:0 0 4px;line-height:1.6}
 .warn{background:#fff8e1;border-left:4px solid #ffb300;padding:10px 12px;
       border-radius:4px;font-size:13px;margin:12px 0;line-height:1.6}
 .card{background:#fff;border-radius:8px;padding:16px;margin:12px 0;
       box-shadow:0 1px 3px rgba(0,0,0,.08)}
 .card.stale{opacity:.55;border-left:4px solid #d33}
 .card.used{border-left:4px solid #2e7d32}
 .hd{display:flex;justify-content:space-between;align-items:baseline;
     flex-wrap:wrap;gap:8px;margin-bottom:10px}
 .nm{font-size:17px;font-weight:600}
 .meta{color:#778;font-size:12px}
 table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}
 th,td{padding:5px 6px;text-align:right;border-bottom:1px solid #eee}
 th:first-child,td:first-child{text-align:left;color:#667}
 .hit{color:#2e7d32;font-weight:600}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
 .row label{font-size:12px;color:#556;min-width:74px}
 input{width:78px;padding:6px;border:1px solid #ccd;border-radius:4px;font-size:14px}
 button{padding:7px 13px;border:0;border-radius:4px;background:#3949ab;
        color:#fff;font-size:13px;cursor:pointer}
 button:hover{background:#283593}
 button.sec{background:#607d8b}
 button.sec:hover{background:#455a64}
 .cl{font-size:13px;padding:5px 0;border-bottom:1px solid #f0f0f0}
 .ok{color:#2e7d32}.ng{color:#c62828}.wt{color:#ef6c00}
 .none{color:#889;text-align:center;padding:28px 0}
</style></head><body>

<h1>FSR 노드 설정</h1>
<p class="sub">노드가 하트비트로 깰 때 설정이 반영됩니다. 사용자가 버튼을 누를 필요가 없습니다.</p>
<div class="warn">
 <b>반영까지 시간이 걸립니다.</b> 값을 보내면 노드가 다음에 깰 때까지 기다렸다가 적용됩니다
 (보통 몇 분). 화면의 현재값도 그다음 신호가 와야 갱신됩니다.<br>
 값을 보면서 바로바로 맞춰야 한다면 <a href="/fsr-tune">실시간 튜닝 화면</a>을 쓰세요.
</div>
<p class="sub"><a href="/dashboard">← 대시보드</a></p>

<div id="nodes"></div>

<div class="card">
 <div class="nm" style="font-size:15px">명령 이력</div>
 <div id="cmds" style="margin-top:8px"></div>
</div>

<script>
const $=s=>document.querySelector(s);
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function mmss(s){ if(s<60) return s+'초 전';
  if(s<3600) return Math.floor(s/60)+'분 전'; return Math.floor(s/3600)+'시간 전'; }

async function send(mac,cmd,val){
  const r=await fetch('/api/fsr/cmd',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mac:mac,cmd:cmd,val:val})});
  const j=await r.json();
  if(j.status!=='success'){ alert('실패: '+(j.message||'')); return; }
  load();
}
function sendField(mac,cmd,id){
  const el=document.getElementById(id);
  const v=parseInt(el.value,10);
  if(isNaN(v)){ alert('숫자를 입력하세요'); return; }
  send(mac,cmd,v);
}

function nodeCard(n){
  const hit=k=>(n.mask>>k)&1;
  const cell=k=>{
    const unset=(n.th[k]>=3300);
    const det=unset?'-':(n.base[k]+n.th[k]);
    return {cur:n.fsr[k], base:n.base[k], th:unset?'미설정':('+'+n.th[k]), det:det, on:hit(k)};
  };
  const c=[0,1,2].map(cell);
  const cls='card'+(n.stale?' stale':'')+((!n.stale&&n.mask)?' used':'');
  return `<div class="${cls}">
   <div class="hd">
     <div><span class="nm">${esc(n.name||n.mac)}</span>
       <span class="meta"> ${esc(n.mac)} · ${esc(n.gw)}</span></div>
     <div class="meta">${n.stale?'<b style="color:#c62828">응답 없음</b> · ':''}
       ${esc(n.at)} (${mmss(n.age)}) · 배터리 ${n.batt}% · ${n.rssi}dBm
       ${n.drops?' · <b style="color:#ef6c00">폐기 '+n.drops+'</b>':''}</div>
   </div>
   <table>
    <tr><th>센서</th><th>1</th><th>2</th><th>3</th></tr>
    <tr><td>현재</td>${c.map(x=>`<td class="${x.on?'hit':''}">${x.cur}</td>`).join('')}</tr>
    <tr><td>기준</td>${c.map(x=>`<td>${x.base}</td>`).join('')}</tr>
    <tr><td>임계</td>${c.map(x=>`<td>${x.th}</td>`).join('')}</tr>
    <tr><td>감지선</td>${c.map(x=>`<td>${x.det}</td>`).join('')}</tr>
   </table>
   <div class="meta">해제 비율 ${n.hyst.join('/')}% · 판정 센서수 ${n.nhit}개
     · 하트비트 ${n.hb}초 → 반영까지 최대 ${Math.ceil(n.wait/60)}분</div>

   <div class="row">
     <label>센서1 임계</label><input id="t1_${n.mac}" placeholder="${n.th[0]}">
     <button onclick="sendField('${n.mac}','th1','t1_${n.mac}')">전송</button>
     <label>센서2 임계</label><input id="t2_${n.mac}" placeholder="${n.th[1]}">
     <button onclick="sendField('${n.mac}','th2','t2_${n.mac}')">전송</button>
   </div>
   <div class="row">
     <label>센서3 임계</label><input id="t3_${n.mac}" placeholder="${n.th[2]}">
     <button onclick="sendField('${n.mac}','th3','t3_${n.mac}')">전송</button>
     <label>전체 임계</label><input id="ta_${n.mac}" placeholder="일괄">
     <button onclick="sendField('${n.mac}','th','ta_${n.mac}')">전송</button>
   </div>
   <div class="row">
     <label>해제 비율</label><input id="hy_${n.mac}" placeholder="${n.hyst[0]}">
     <button onclick="sendField('${n.mac}','hyst','hy_${n.mac}')">전송</button>
     <label>판정 센서수</label><input id="nh_${n.mac}" placeholder="${n.nhit}">
     <button onclick="sendField('${n.mac}','nhit','nh_${n.mac}')">전송</button>
   </div>
   <div class="row">
     <button class="sec" onclick="if(confirm('산정된 값을 실제 설정으로 반영합니다. 계속할까요?'))send('${n.mac}','apply',0)">산정값 적용</button>
     <button class="sec" onclick="if(confirm('노드를 재부팅합니다. 계속할까요?'))send('${n.mac}','reboot',0)">재부팅</button>
   </div>
  </div>`;
}

function cmdLine(c){
  const s={pending:['wt','대기'],sent:['wt','전달 중'],
           done:['ok','완료'],fail:['ng','실패']}[c.status]||['','?'];
  const v=(c.val===null||c.val===undefined)?'':(' = '+c.val);
  return `<div class="cl"><span class="${s[0]}">[${s[1]}]</span>
    ${esc(c.mac)} · ${esc(c.cmd)}${esc(v)}
    <span class="meta"> ${esc(c.created||'')}${c.retries?' · 재시도 '+c.retries:''}</span></div>`;
}

async function load(){
  try{
    const r=await fetch('/api/fsr/nodes'); const j=await r.json();
    $('#nodes').innerHTML = j.nodes.length
      ? j.nodes.map(nodeCard).join('')
      : '<div class="card"><div class="none">아직 신호를 받은 노드가 없습니다.<br>'
        +'게이트웨이가 켜져 있고 노드가 등록되어 있는지 확인하세요.</div></div>';
    const all=[...j.pending, ...j.recent];
    $('#cmds').innerHTML = all.length ? all.map(cmdLine).join('')
                                      : '<div class="meta">없음</div>';
  }catch(e){ /* 일시적 오류는 다음 주기에 회복된다 */ }
}
load(); setInterval(load, 5000);
</script></body></html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
