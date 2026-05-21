from fastapi import FastAPI, Request, Query, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse, RedirectResponse
import json, os, uvicorn, threading, io, zipfile, html, secrets, hmac, hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import pandas as pd
import analyzer

# SemVer (MAJOR.MINOR.PATCH) — 변경 시 CHANGELOG.md 같이 업데이트.
# MAJOR: 기존 사용 방식이 깨지는 변경 / MINOR: 기능 추가 / PATCH: 버그·자잘한 수정.
VERSION = "3.0.2"

app = FastAPI()
LOG_FILE = "emfit_data.jsonl"
FEEDBACK_FILE = "feedback.jsonl"
TOKENS_FILE = "device_tokens.json"
ADMIN_PW_FILE = "admin_password.txt"
PREFERENCES_FILE = "preferences.json"  # viewer 단위 UI 환경설정 (블록 순서 등)
ADMIN_COOKIE = "emfit_admin"  # device 토큰 쿠키 이름 (변수 이름은 옛 잔재)
SESSION_COOKIE = "emfit_session"  # 관리자 로그인 세션 쿠키
# 관리자 ID — 비번은 ADMIN_PW_FILE에서 읽음. ID 변경 원하면 환경변수로.
ADMIN_USERNAME = os.environ.get("EMFIT_ADMIN_USER", "operator")
# 세션 쿠키 서명용 비밀키 — 서버 부팅 시 1회 생성. 재시작하면 모두 로그아웃됨.
_SESSION_SECRET = secrets.token_bytes(32)
# 외부 접속 URL (DDNS). 내부 base는 사용자가 들어온 host에서 자동 추출.
EXTERNAL_BASE = os.environ.get("EMFIT_EXTERNAL_BASE", "http://monitoring.example.com")
_tokens_lock = threading.Lock()
_feedback_lock = threading.Lock()
_prefs_lock = threading.Lock()

# 기기별 페이지 블록 순서의 기본값 (수면요약 → HR → RR → ACT)
DEFAULT_BLOCK_ORDER = ["summary", "hr", "rr", "act"]

# Emfit 데이터는 모두 KST 기준으로 저장됨 (analyzer가 UTC → Asia/Seoul 변환).
# 프런트는 datetime-local로 KST 시각을 입력하고, 그대로 KST aware datetime으로 해석.
KST = timezone(timedelta(hours=9))


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
    <title>점검 중 · Emfit</title>
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
        if os.path.exists(LOG_FILE):
            analyzer.warmup(LOG_FILE)
    except Exception as e:
        print(f"[startup] 워밍업 실패: {e}", flush=True)
    finally:
        _SERVER_READY = True
        print("[startup] 파싱 완료 — 서버 준비됨", flush=True)


# 서비스 시작 시 백그라운드에서 캐시 워밍업 (첫 요청 느림 방지)
threading.Thread(target=_warmup_then_ready, daemon=True).start()


@app.middleware("http")
async def _maintenance_gate(request: Request, call_next):
    """워밍업이 안 끝났으면 점검 페이지(503)로 응답한다.
    단, Emfit 기기의 데이터 수신(POST /)은 점검 중에도 받아 데이터 유실을 막는다."""
    if not _SERVER_READY:
        if not (request.method == "POST" and request.url.path == "/"):
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
        <title>관리자 로그인 · Emfit</title>
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
            <h1>Emfit QS 대시보드</h1>
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
        <title>접근 불가 · Emfit</title>
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


def _card_sort_key(sn, state, ds, now):
    """이상 상태일수록 위로. 같은 카테고리에서는 SN 순."""
    if ds is not None and not ds.get("connected"):
        return (0, sn)
    if state is None:
        return (1, sn)
    try:
        last_dt = datetime.strptime(f"{state['날짜']} {state['시간(KST)']}", "%Y-%m-%d %H:%M:%S")
        mins_ago = (now - last_dt).total_seconds() / 60
    except Exception:
        return (2, sn)
    if mins_ago > 10:
        return (2, sn)
    act = state.get("활동량(ACT)")
    if isinstance(act, (int, float)) and act < 1:
        return (3, sn)
    return (4, sn)


def _render_inactive_card(sn, info, ds, now, token=""):
    """비활성 기기용 minimal 카드. 측정값은 안 보여주고 위치/이름/마지막 통신만."""
    location_text = info['location'] if info['location'] and info['location'] != '-' else '미지정'
    if ds is None:
        last_text = "통신 이력 없음"
    else:
        last_seen_ts = ds.get("last_seen_ts")
        if isinstance(last_seen_ts, (int, float)):
            last_text = f"마지막 통신: {_format_ago(max(0, int(now.timestamp() - last_seen_ts)))}"
        else:
            last_text = "통신 끊김"
    return f"""
    <a href="/device/{sn}" style="display:block; text-decoration:none; color:inherit;">
    <div style="background:#fafafa; padding:12px 14px; border-radius:10px; border:1px solid #e0e0e0; transition:transform 0.1s;" onmouseover="this.style.transform='translateY(-2px)'" onmouseout="this.style.transform='translateY(0)'">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
                <div style="font-size:0.75em; color:#78909c;">{location_text}</div>
                <div style="font-size:1em; font-weight:bold; color:#546e7a;">{info['name']}</div>
            </div>
            <div style="font-size:1.3em; opacity:0.6;">💤</div>
        </div>
        <div style="margin-top:8px; font-size:0.78em; color:#90a4ae;">{last_text}</div>
        <div style="margin-top:4px; color:#b0bec5; font-size:0.6em;">{sn}</div>
    </div>
    </a>
    """


def _render_card(sn, info, state, ds, now, token=""):
    location_text = info['location'] if info['location'] and info['location'] != '-' else '미지정'

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
        <a href="/device/{sn}" style="display:block; text-decoration:none; color:inherit;">
        <div style="background:#fff8e1; padding:16px; border-radius:14px; border:2px solid #ffd54f; transition:transform 0.1s;" onmouseover="this.style.transform='translateY(-2px)'" onmouseout="this.style.transform='translateY(0)'">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                    <div style="font-size:0.85em; color:#5d4037;">{location_text}</div>
                    <div style="font-size:1.3em; font-weight:bold; color:#263238;">{info['name']}</div>
                </div>
                <div style="font-size:1.8em;">{conn_icon}</div>
            </div>
            <div style="text-align:center; margin-top:20px; color:#5d4037; font-size:1em; font-weight:bold;">측정 데이터 없음</div>
            <div style="text-align:center; margin-top:6px; color:{conn_color}; font-size:0.9em; font-weight:bold;">{conn_text}</div>
            <div style="text-align:center; margin-top:6px; color:#90a4ae; font-size:0.65em;">{sn}</div>
        </div>
        </a>
        """

    try:
        last_dt = datetime.strptime(f"{state['날짜']} {state['시간(KST)']}", "%Y-%m-%d %H:%M:%S")
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
    act_str = f"{act_num:.0f}" if act_num is not None else "-"

    # 상태는 3가지로 단순화: 끊김 / 부재 / 재실
    # 부재 = 측정 대기(10분 이상 데이터 없음) + 활동량 0 (Emfit은 침대 위에서만 측정)
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
        act_str = "-"
    else:
        hr_str = f"{hr:.0f}" if isinstance(hr, (int, float)) else "-"
        rr_str = f"{rr:.0f}" if isinstance(rr, (int, float)) else "-"

    return f"""
    <a href="/device/{sn}" style="display:block; text-decoration:none; color:inherit;">
    <div style="background:{bg}; padding:16px; border-radius:14px; border:2px solid {border}; transition:transform 0.1s;" onmouseover="this.style.transform='translateY(-2px)'" onmouseout="this.style.transform='translateY(0)'">
        <div style="display:flex; justify-content:space-between; align-items:flex-start;">
            <div>
                <div style="font-size:0.85em; color:#455a64;">{location_text}</div>
                <div style="font-size:1.3em; font-weight:bold; color:#1a237e;">{info['name']}</div>
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
                <div style="font-size:0.7em; color:#e67e22; font-weight:bold;">🏃 ACT</div>
                <div style="font-weight:bold; font-size:2.4em; color:#212121; line-height:1.1;">{act_str}</div>
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


def _build_cards_payload(token=""):
    """대시보드 카드 영역 + 헤더 요약을 HTML 조각으로 빌드. /dashboard 첫 렌더와 /api/cards 갱신 양쪽에서 재사용."""
    now = datetime.now()
    now_ts = now.timestamp()

    latest = {}
    if os.path.exists(LOG_FILE):
        try:
            latest = analyzer.get_latest_states(LOG_FILE)
        except Exception:
            latest = {}

    statuses = analyzer.get_device_statuses()

    active_sns, inactive_sns = [], []
    connected_count = 0
    for sn in analyzer.DEVICE_INFO.keys():
        ds = statuses.get(sn)
        if _is_active(ds, now_ts):
            active_sns.append(sn)
            if ds and ds.get("connected"):
                connected_count += 1
        else:
            inactive_sns.append(sn)

    active_sns.sort(key=lambda s: _card_sort_key(s, latest.get(s), statuses.get(s), now))
    inactive_sns.sort()

    active_html = "\n".join(
        _render_card(sn, analyzer.DEVICE_INFO[sn], latest.get(sn), statuses.get(sn), now, token)
        for sn in active_sns
    ) or '<p style="grid-column:1/-1; text-align:center; color:#90a4ae; padding:40px;">활성 기기가 없습니다.</p>'

    inactive_html = ""
    if inactive_sns:
        cards = "\n".join(
            _render_inactive_card(sn, analyzer.DEVICE_INFO[sn], statuses.get(sn), now, token)
            for sn in inactive_sns
        )
        inactive_html = f"""
            <h2 style="color:#90a4ae; margin-top:40px; font-size:1em; font-weight:normal; border-top:1px solid #cfd8dc; padding-top:20px;">
                💤 비활성 기기 ({len(inactive_sns)}대) <span style="font-size:0.85em; color:#b0bec5;">— 7일 이상 통신 없음</span>
            </h2>
            <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:10px; margin-top:15px;">
                {cards}
            </div>
        """

    total = len(analyzer.DEVICE_INFO)
    summary_parts = [f'<b style="color:#2e7d32;">{connected_count}</b> / {total} 연결됨']
    if inactive_sns:
        summary_parts.append(f'<span style="color:#90a4ae;">비활성 {len(inactive_sns)}</span>')
    summary_html = ' · '.join(summary_parts)

    return {
        "active": active_html,
        "inactive": inactive_html,
        "summary": summary_html,
        "now": now.strftime('%Y-%m-%d %H:%M:%S'),
    }


# 관제 대시보드 (기기별 카드 그리드) — 관리자 전용
@app.get("/dashboard", response_class=HTMLResponse)
def view_dashboard(request: Request, _: str = Depends(require_admin)):
    admin_token = _get_token_from_request(request) or ""
    p = _build_cards_payload(admin_token)
    return f"""
    <html>
        <head>
            <title>Emfit 관제 화면</title>
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
                    <h1 style="color:#1a73e8; margin:0;">📡 Emfit 실시간 관제</h1>
                    <div style="color:#7f8c8d; font-size:0.9em;">
                        <span id="clock">{p['now']}</span> · <span id="header-summary">{p['summary']}</span>
                    </div>
                </div>

                <div id="active-cards" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); gap:15px; margin-top:20px;">
                    {p['active']}
                </div>

                <div id="inactive-section">
                    {p['inactive']}
                </div>

                <p class="nav-buttons" style="text-align:center; margin-top:30px;">
                    <a href="/reports" style="display:inline-block; padding:10px 20px; background:#1a73e8; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">📊 리포트 다운로드</a>
                    <a href="/devices" style="display:inline-block; padding:10px 20px; background:#8e44ad; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">⚙️ 기기 정보</a>
                    <a href="/admin/tokens" style="display:inline-block; padding:10px 20px; background:#16a085; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🔑 사용자 URL 관리</a>
                    <a href="/feedback" style="display:inline-block; padding:10px 20px; background:#e67e22; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">💬 의견 보기</a>
                    <a href="/help" style="display:inline-block; padding:10px 20px; background:#27ae60; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">📘 사용 가이드</a>
                    <a href="/dashboard/raw" style="display:inline-block; padding:10px 20px; background:#7f8c8d; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🔎 원본 데이터</a>
                    <a href="/logout" style="display:inline-block; padding:10px 20px; background:#b0bec5; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">🚪 로그아웃</a>
                </p>

                <p style="text-align:center; color:#bdc3c7; font-size:0.8em; margin-top:10px;">
                    15초마다 자동 갱신 · 카드를 누르면 기기별 상세 그래프
                </p>
                <p style="text-align:center; color:#90a4ae; font-size:0.75em; margin-top:20px;">
                    Emfit Server v{VERSION}
                </p>
            </div>
            <script>
                async function refreshCards() {{
                    try {{
                        const r = await fetch('/api/cards');
                        if (!r.ok) return;
                        const d = await r.json();
                        document.getElementById('active-cards').innerHTML = d.active;
                        document.getElementById('inactive-section').innerHTML = d.inactive;
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


def _build_single_card(sn, token=""):
    """단일 기기 카드 HTML — /device/{sn} 페이지의 실시간 현황 영역에 사용."""
    info = analyzer.DEVICE_INFO.get(sn)
    if info is None:
        return ""
    now = datetime.now()
    now_ts = now.timestamp()
    latest = {}
    if os.path.exists(LOG_FILE):
        try:
            latest = analyzer.get_latest_states(LOG_FILE)
        except Exception:
            pass
    statuses = analyzer.get_device_statuses()
    state = latest.get(sn)
    ds = statuses.get(sn)
    if _is_active(ds, now_ts):
        return _render_card(sn, info, state, ds, now, token)
    return _render_inactive_card(sn, info, ds, now, token)


@app.get("/api/device/{sn}/card", response_class=HTMLResponse)
def api_device_card(sn: str, request: Request):
    _require_device_access(request, sn)
    token = _get_token_from_request(request) or ""
    return HTMLResponse(_build_single_card(sn, token))


def _require_device_access(request: Request, sn: str):
    """admin 세션이면 무조건 통과 (남아있는 device 토큰 쿠키 무시).
    그 외엔 device 토큰이 그 SN과 매핑되어야 통과.
    토큰은 있는데 SN 불일치면 403 — 외부 사용자가 다른 기기 보려는 시도 차단."""
    # admin 먼저 검사 — admin이 테스트로 /d/{token} 접속한 후 토큰 쿠키가 남아있어도
    # 자기 다른 기기에 접근할 수 있게 해야 함.
    if _is_admin_authenticated(request):
        return None
    t = _get_token_from_request(request)
    if t:
        tokens = _load_tokens()
        mapped = tokens.get(t)
        if mapped == sn:
            return t  # device 토큰 통과
        if mapped is not None and mapped != "*":
            raise HTTPException(status_code=403, detail="other device")
    raise HTTPException(status_code=401, detail="login required")


def _get_viewer_id(request: Request):
    """현재 viewer 식별자. admin 세션이면 'admin', device 토큰 보유면 토큰 값.
    UI 환경설정(블록 순서 등)을 어떤 단위로 저장/조회할지 결정하는 키."""
    if _is_admin_authenticated(request):
        return "admin"
    t = _get_token_from_request(request)
    if t:
        return t
    return None


@app.get("/api/preferences/order")
def api_get_block_order(request: Request):
    """현재 viewer의 블록 순서 반환. 저장된 게 없으면 기본 순서."""
    vid = _get_viewer_id(request)
    if vid is None:
        raise HTTPException(status_code=401, detail="login required")
    prefs = _load_preferences()
    order = (prefs.get(vid) or {}).get("block_order") or DEFAULT_BLOCK_ORDER
    # 알려진 블록만 남기고 누락분 뒤에 채워서 신뢰 가능한 순서 보장
    known = set(DEFAULT_BLOCK_ORDER)
    cleaned = [b for b in order if b in known]
    for b in DEFAULT_BLOCK_ORDER:
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
    known = set(DEFAULT_BLOCK_ORDER)
    cleaned = [b for b in order if isinstance(b, str) and b in known]
    if not cleaned:
        raise HTTPException(status_code=400, detail="no valid blocks in order")
    # 누락된 기본 블록은 뒤에 채움
    for b in DEFAULT_BLOCK_ORDER:
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
    if os.path.exists(LOG_FILE):
        try:
            available = analyzer.list_available(LOG_FILE)
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
    if os.path.exists(LOG_FILE):
        for d in dates_to_load:
            try:
                df = analyzer.get_report_df(LOG_FILE, d, sn, assignment_id=target_id)
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

                pt = {"time": t, "date": d, "epoch": epoch, "type": rtype}
                for k, jk in [("심박수(HR)", "hr"), ("호흡수(RR)", "rr"), ("활동량(ACT)", "act")]:
                    v = row.get(k)
                    pt[jk] = float(v) if isinstance(v, (int, float)) and pd.notna(v) else None
                points.append(pt)

    # 시간 순 정렬 (multi-day 합쳤을 때 필수)
    points.sort(key=lambda p: p.get("epoch", 0))

    # summary 정렬: 총수면(분) 큰 순 → 종료 epoch 이른 순
    def _summary_key(s):
        total = s.get("총수면(분)")
        total = -total if isinstance(total, (int, float)) else 0
        return (total, s.get("__epoch__") or 0)
    summaries.sort(key=_summary_key)

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
    }


@app.get("/device/{sn}", response_class=HTMLResponse)
def view_device(sn: str, request: Request, assignment: str = Query(None)):
    device_token = _require_device_access(request, sn)
    is_admin = device_token is None  # device 토큰 없이 통과 = admin
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

    location_text = info['location'] if info['location'] and info['location'] != '-' else '미지정'

    csv_query = f"assignment={target_id}" if target_id else f"device={sn}"
    lo = target["start"][:10] if target and target.get("start") else None
    hi = target["end"][:10] if target and target.get("end") else None

    available_for_sn = []
    if os.path.exists(LOG_FILE):
        try:
            available = analyzer.list_available(LOG_FILE)
            available_for_sn = [d for s, d in available if s == sn
                                and (not lo or d >= lo) and (not hi or d <= hi)]
        except Exception:
            pass
    today = available_for_sn[0] if available_for_sn else datetime.now(KST).strftime("%Y-%m-%d")
    if not is_admin:
        back_link = f'<a href="/d/{device_token}" style="color:#1a73e8; text-decoration:none;">← 메인</a>'
    elif assignment:
        back_link = '<a href="/devices" style="color:#1a73e8; text-decoration:none;">← 배정 이력</a>'
    else:
        back_link = '<a href="/dashboard" style="color:#1a73e8; text-decoration:none;">← 대시보드</a>'
    token = device_token or ""
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
    # 날짜 버튼용 — 데이터 있는 날짜를 JS로 직렬화. (analyzer.list_available은 최신 → 과거 순)
    available_json = json.dumps(available_for_sn)
    # datetime-local 기본값 — 가장 최근 날짜의 00:00 ~ 23:55
    default_start = f"{today}T00:00"
    default_end = f"{today}T23:55"

    return f"""
    <html>
        <head>
            <title>{info['name']} · Emfit</title>
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
                <h1 style="margin: 8px 0; color: #1a237e;">{info['name']}</h1>
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

                <div id="blocks-container">
                    <div class="block-card" data-block-id="summary">
                        <div class="block-header">
                            <h3>🛌 수면 요약</h3>
                            <div class="block-actions">
                                <button class="arrow-btn" data-arrow="up" title="위로 이동">▲</button>
                                <button class="arrow-btn" data-arrow="down" title="아래로 이동">▼</button>
                                <span class="drag-handle" title="드래그해서 이동">⋮⋮</span>
                            </div>
                        </div>
                        <div id="summary-area"></div>
                    </div>
                    <div class="block-card" data-block-id="hr">
                        <div class="block-header">
                            <h3>❤️ 심박수 (HR) — 분당</h3>
                            <div class="block-actions">
                                <button class="arrow-btn" data-arrow="up" title="위로 이동">▲</button>
                                <button class="arrow-btn" data-arrow="down" title="아래로 이동">▼</button>
                                <span class="drag-handle" title="드래그해서 이동">⋮⋮</span>
                            </div>
                        </div>
                        <div class="chart-scroll"><div class="chart-canvas-wrap" id="wrap-hr"><canvas id="chart-hr"></canvas></div></div>
                        <div class="chart-hint">↔ 가로 스크롤 · 30분 간격 눈금</div>
                    </div>
                    <div class="block-card" data-block-id="rr">
                        <div class="block-header">
                            <h3>🫁 호흡수 (RR) — 분당</h3>
                            <div class="block-actions">
                                <button class="arrow-btn" data-arrow="up" title="위로 이동">▲</button>
                                <button class="arrow-btn" data-arrow="down" title="아래로 이동">▼</button>
                                <span class="drag-handle" title="드래그해서 이동">⋮⋮</span>
                            </div>
                        </div>
                        <div class="chart-scroll"><div class="chart-canvas-wrap" id="wrap-rr"><canvas id="chart-rr"></canvas></div></div>
                        <div class="chart-hint">↔ 가로 스크롤 · 30분 간격 눈금</div>
                    </div>
                    <div class="block-card" data-block-id="act">
                        <div class="block-header">
                            <h3>🏃 활동량 (ACT)</h3>
                            <div class="block-actions">
                                <button class="arrow-btn" data-arrow="up" title="위로 이동">▲</button>
                                <button class="arrow-btn" data-arrow="down" title="아래로 이동">▼</button>
                                <span class="drag-handle" title="드래그해서 이동">⋮⋮</span>
                            </div>
                        </div>
                        <div class="chart-scroll"><div class="chart-canvas-wrap" id="wrap-act"><canvas id="chart-act"></canvas></div></div>
                        <div class="chart-hint">↔ 가로 스크롤 · 30분 간격 눈금</div>
                    </div>
                </div>

                <p style="text-align:center; margin-top:30px;">
                    <a href="/help" style="display:inline-block; padding:10px 20px; background:#27ae60; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">📘 사용 가이드</a>
                    <a href="/feedback" style="display:inline-block; padding:10px 20px; background:#e67e22; color:white; text-decoration:none; border-radius:8px; font-weight:bold; margin:4px;">💬 의견 보내기</a>
                </p>
            </div>
            <script>
                const sn = "{sn}";
                const token = "{token}";
                const charts = {{}};
                const CHART_KEYS = ['hr', 'rr', 'act'];
                const CHART_COLORS = {{ hr: '#e74c3c', rr: '#3498db', act: '#f39c12' }};
                const AVAILABLE_DATES = {available_json};

                let lastPoints = [];
                let lastSummaries = [];

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

                    // {{x: epoch_sec, y: value}}. 5분 이상 갭이면 중간에 null 점 삽입해서 line 끊기.
                    const data = [];
                    let prevEpoch = null;
                    for (const p of lastPoints) {{
                        const v = p[key];
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
                    charts[id] = new Chart(ctx, {{
                        type: 'line',
                        data: {{
                            datasets: [{{
                                data,
                                borderColor: color,
                                backgroundColor: color + '22',
                                tension: 0.2,
                                pointRadius: 0,
                                spanGaps: false,
                                fill: true,
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
                                y: {{ beginAtZero: false }},
                            }},
                            plugins: {{ legend: {{ display: false }} }}
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
                        const r = await fetch(url);
                        const d = await r.json();
                        lastSummaries = d.summaries || [];
                        lastPoints = d.points || [];
                        renderSummary();
                        if (lastPoints.length === 0) {{
                            document.getElementById('data-status').textContent = '선택 범위에 데이터가 없습니다.';
                            destroyCharts();
                            return;
                        }}
                        document.getElementById('data-status').textContent = `총 ${{lastPoints.length}}개 측정값`;
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
                        const r = await fetch('/api/preferences/order');
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
                        await fetch('/api/preferences/order', {{
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
                        const r = await fetch(`/api/device/${{sn}}/card`);
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
@app.get("/dashboard/raw", response_class=HTMLResponse)
async def view_dashboard_raw(request: Request, _: str = Depends(require_admin)):
    admin_token = _get_token_from_request(request) or ""
    count = 0
    last_data = "아직 수신된 데이터가 없습니다."
    formatted_json = ""

    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
            count = len(lines)
            if count > 0:
                last_data = lines[-1]

    try:
        parsed_json = json.loads(last_data)
        formatted_json = json.dumps(parsed_json, indent=4, ensure_ascii=False)
    except Exception:
        formatted_json = last_data

    return f"""
    <html>
        <head>
            <title>Emfit Raw Data</title>
            <meta http-equiv="refresh" content="15">
        </head>
        <body style="font-family: 'Malgun Gothic', sans-serif; padding:30px; background:#f0f2f5; line-height:1.6;">
            <div style="max-width:800px; margin:auto; background:white; padding:30px; border-radius:20px; box-shadow:0 10px 25px rgba(0,0,0,0.1);">
                <h1 style="text-align:center; color:#1a73e8; margin-bottom:10px;">🔎 마지막 수신 원본</h1>
                <p style="text-align:center; color:#7f8c8d; font-size:0.9em; margin-bottom:20px;">서버 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                <div style="margin: 20px 0; padding: 15px; background: #e8f0fe; border-radius: 10px;">
                    <p style="margin:0;"><b>총 로그:</b> <span style="color:#e74c3c;">{count:,}개</span></p>
                </div>
                <pre style="background:#202124; color:#00ff00; padding:20px; border-radius:10px;
                           font-size:0.95em; line-height:1.5; white-space: pre-wrap; word-wrap: break-word; overflow-x: hidden;">{formatted_json}</pre>
                <p style="text-align:center; margin-top:25px;">
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
    if not os.path.exists(LOG_FILE):
        return HTMLResponse("<p>데이터 파일이 없습니다.</p>", status_code=404)

    try:
        available = analyzer.list_available_assignments(LOG_FILE)
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
            <title>Emfit 리포트 다운로드</title>
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


# 리포트 CSV 다운로드 (admin 또는 해당 기기 토큰)
@app.get("/report")
def download_report(request: Request, date: str = Query(...),
                    device: str = Query(None), assignment: str = Query(None)):
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
    if not os.path.exists(LOG_FILE):
        return PlainTextResponse("데이터 파일이 없습니다.", status_code=404)

    try:
        df = analyzer.get_report_df(LOG_FILE, date, sn, assignment_id=aid)
    except Exception as e:
        return PlainTextResponse(f"분석 오류: {e}", status_code=500)

    if df.empty:
        return PlainTextResponse(
            f"해당 조건의 데이터가 없습니다 (date={date})",
            status_code=404,
        )

    filename = f"{date}_{label_loc}_{label_user}_리포트.csv"

    csv_bytes = ("﻿" + df.to_csv(index=False)).encode("utf-8")
    encoded_filename = quote(filename)

    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":  f"attachment; filename*=UTF-8''{encoded_filename}"
        },
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
    if not os.path.exists(LOG_FILE):
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
            try:
                df = analyzer.get_report_df(LOG_FILE, date_str, sn, assignment_id=aid)
            except Exception:
                df = None
            if df is not None and not df.empty:
                csv_bytes = ("﻿" + df.to_csv(index=False)).encode("utf-8")
                inner_name = f"{date_str}_{info['location']}_{info['name']}_리포트.csv"
                zf.writestr(inner_name, csv_bytes)
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
            <title>Emfit QS 대시보드 — 사용 가이드</title>
            <meta charset="utf-8">
        </head>
        <body style="font-family: 'Malgun Gothic', sans-serif; padding:30px; background:#f0f2f5; line-height:1.7;">
            <div style="max-width:900px; margin:auto; background:white; padding:40px; border-radius:20px; box-shadow:0 10px 25px rgba(0,0,0,0.08);">

                <h1 style="color:#1a73e8; border-bottom:2px solid #e8f0fe; padding-bottom:10px;">📘 Emfit QS 서버 사용 가이드</h1>
                <p style="color:#7f8c8d;">Emfit 침대 센서 실시간 관제 및 리포트 서비스.</p>

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
                    <tr><td style="padding:8px; border-top:1px solid #eee;">활동량(ACT)</td><td style="padding:8px; border-top:1px solid #eee;">Emfit 활동 지표 (0에 가까우면 부재)</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">심박변이도(RMSSD)</td><td style="padding:8px; border-top:1px solid #eee;">HRV 지표 (HRV 행만)</td></tr>
                    <tr><td style="padding:8px; border-top:1px solid #eee;">수면점수 / 총수면(분) 등</td><td style="padding:8px; border-top:1px solid #eee;">하루 수면 요약 (Summary 행만)</td></tr>
                </table>

                <h2 style="color:#1a73e8; margin-top:40px;">3. 자주 묻는 질문</h2>

                <h3 style="color:#2c3e50;">Q. 카드 값이 안 바뀌는 것 같아요</h3>
                <p>
                    Emfit은 30초 주기로 데이터를 PUSH 하고 있습니다. 30초 이내 새로고침 시 같은 값이 보일 수 있습니다.
                    카드의 <b>측정</b> 시각이 업데이트 되고있다면 정상이며, 계속 같은 시각이라면 Emfit 장비에서 새 측정이 없는 상태입니다.
                </p>

                <h3 style="color:#2c3e50;">Q. HR/RR/ACT 가 모두 "-" 로 떠요</h3>
                <p>
                    10분 이상 데이터 측정이 없거나 침대 위에 없는 상태입니다. 또는 장비의 연결이 끊겼을 때도 "-"로 표시되며, 이는 Q3 답변 참고 바랍니다.
                </p>

                <h3 style="color:#2c3e50;">Q. 🔴 끊김 이 뜨면 어떻게 하나요</h3>
                <p>
                    장비 자체의 전원이나 네트워크를 확인해야 합니다. 장비가 제대로 연결이 되어있는지(DC전원 또는 콘센트) 확인이 필요합니다. 사용 위치를 옮겼을 경우에는 와이파이 재연결이 필요할 수 있습니다.
                </p>

                <h3 style="color:#2c3e50;">Q. 수면 점수(Summary)가 이상해요 — REM/깊은수면이 0</h3>
                <p>
                    Emfit 알고리즘이 수면 단계 분류에 실패하는 것으로 추정되며, 총수면(분) 값만 신뢰해서 보세요.
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
    rows_html = ""
    for sn in sorted(analyzer.DEVICE_INFO.keys()):
        info = analyzer.DEVICE_INFO[sn]
        name = html.escape(str(info.get("name", "")))
        location = html.escape(str(info.get("location", "")))
        group = info.get("group", analyzer.DEFAULT_GROUP)
        group_opts = "".join(
            f'<option value="{html.escape(g)}"{" selected" if g == group else ""}>{html.escape(g)}</option>'
            for g in analyzer.GROUPS
        )
        rows_html += f"""
        <tr>
            <td style="padding:10px; font-family:monospace; color:#607d8b; white-space:nowrap;">{sn}</td>
            <td style="padding:6px;"><input name="name_{sn}" value="{name}" style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box;"></td>
            <td style="padding:6px;"><input name="location_{sn}" value="{location}" style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; font-size:1em; box-sizing:border-box;"></td>
            <td style="padding:6px;">
                <select name="group_{sn}" style="width:100%; padding:8px; border:1px solid #ddd; border-radius:6px; font-size:1em;">
                    {group_opts}
                </select>
            </td>
        </tr>
        """

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
        <title>기기 정보 관리 · Emfit</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: 'Malgun Gothic', sans-serif; padding:20px; background:#f0f2f5; margin:0; }}
            .container {{ max-width: 900px; margin: auto; background:white; padding:24px; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }}
            table {{ width:100%; border-collapse: collapse; }}
            th {{ background:#e8f0fe; padding:10px; text-align:left; font-size:0.9em; color:#1a237e; }}
            tr {{ border-top: 1px solid #eee; }}
            @media (max-width: 600px) {{
                body {{ padding: 10px; }}
                .container {{ padding: 16px; }}
                table, thead, tbody, tr, td, th {{ display:block; }}
                tr {{ margin-bottom:14px; padding:10px; background:#fafafa; border-radius:8px; }}
                td {{ padding:4px 0 !important; }}
                td:first-child {{ font-weight:bold; }}
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
            <form method="post" action="/devices/save">
                <table>
                    <thead>
                        <tr>
                            <th>SN</th><th>이름</th><th>위치</th><th>그룹</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
                <div style="margin-top:20px; text-align:right;">
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
        new_data[sn] = {"name": name, "location": location, "group": group}
    try:
        analyzer.update_active_assignments(new_data)
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


# 의견/개선사항 페이지 (관리자 ID/PW 또는 device 토큰 보유자)
@app.get("/feedback", response_class=HTMLResponse)
def view_feedback(request: Request, ok: int = 0):
    is_admin = _is_admin_authenticated(request)
    device_token = None
    if not is_admin:
        t = _get_token_from_request(request)
        if t and t in _load_tokens():
            device_token = t
        else:
            raise HTTPException(status_code=401, detail="login required")
    back_link = '<a href="/dashboard" style="color:#1a73e8; text-decoration:none;">← 대시보드</a>' if is_admin else f'<a href="/d/{device_token}" style="color:#1a73e8; text-decoration:none;">← 메인</a>'
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
        <title>의견 보내기 · Emfit</title>
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
        if not token or token not in _load_tokens():
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


# 관리자: 기기별 사용자 URL(토큰) 관리 — admin ID/비번 통과 시 접근
@app.get("/admin/tokens", response_class=HTMLResponse)
def admin_tokens(request: Request, _: str = Depends(require_admin)):
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

    def _url_block(t):
        path = f"/d/{t}"
        rows = ""
        for label, b in bases:
            full = f"{b}{path}"
            rows += f"""
            <div style="display:flex; gap:6px; align-items:center; margin-bottom:4px; flex-wrap:wrap;">
                <span style="min-width:170px; font-size:0.8em; color:#546e7a;">{label}</span>
                <input readonly value="{full}" onclick="this.select()" style="flex:1; min-width:200px; padding:6px 8px; font-family:monospace; font-size:0.8em; border:1px solid #ddd; border-radius:6px; background:#fafafa;">
                <button type="button" onclick="navigator.clipboard.writeText('{full}'); this.textContent='✓'; setTimeout(()=>this.textContent='📋', 1500);" style="padding:6px 10px; background:#1a73e8; color:white; border:none; border-radius:6px; cursor:pointer; font-size:0.85em;">📋</button>
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

    return f"""
    <html>
    <head>
        <title>사용자 URL 관리 · Emfit</title>
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
        </div>
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
    resp = RedirectResponse(f"/device/{mapped}", status_code=303)
    resp.set_cookie(ADMIN_COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


# 루트 GET — 안내 페이지 (POST는 별도 라우트, 데이터 수신용)
@app.get("/", response_class=HTMLResponse)
def root_page():
    return """
    <html><head><meta charset="utf-8"><title>Emfit</title>
    <style>body{font-family:'Malgun Gothic',sans-serif; padding:40px; background:#f0f2f5;}
    .box{max-width:520px; margin:60px auto; background:white; padding:30px; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,0.06); text-align:center;}</style></head>
    <body><div class="box">
        <h1 style="color:#1a73e8;">📡 Emfit QS 대시보드</h1>
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
            
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)