"""Garmin 워치 payload normalization.

다른 센서들과 결정적으로 다른 점: **기기가 서버로 보내지 않는다.**
워치 → 폰 앱 → Garmin 클라우드까지만 가므로, 젯슨의 garmin_poller.py 가 클라우드에서
당겨와 localhost 의 POST /garmin 으로 넣어준다. 즉 이 payload 를 만든 주체는 기기가
아니라 우리 폴러다. 그래서 규격이 흔들릴 일이 없고, data_source == 'garmin' 태그
하나로 확실히 식별된다.

폴러가 보내는 형태 (Garmin 원본 필드명을 그대로 싣고, 필요한 것만 골라 담는다):
  {"data_source": "garmin",
   "account": "example-account-01",
   "sn": "garmin-example-account-01",
   "date": "2026-09-13",
   "server_received_at": "2026-09-14 15:00:00",
   "last_sync_gmt": "2026-09-13T15:00:00.0",     ← user_summary.wellnessEndTimeGmt
   "hr": [[1789225200000, 79], ...],              ← 증분 심박 (epoch ms, bpm)
   "summary": {"restingHeartRate": 68, "totalPushes": 19, ...},
   "sleep":   {"sleepTimeSeconds": 13860, "deepSleepSeconds": 1800, ...}}

⚠️ 한국어 컬럼 매핑을 폴러가 아니라 **여기서** 한다. 매핑이 바뀌어도 서버만 고치면 되고,
   로그 파일에는 Garmin 원본 필드명이 남아 나중에 규격을 되짚을 수 있다.

수면 컬럼 이름은 Emfit 의 '수면종료요약' 행과 **일부러 똑같이** 맞췄다
(수면점수 · 총수면(분) · 깊은수면(분) · REM수면(분) · 얕은수면(분) · 각성시간(분)).
같은 사람에게 침대 센서와 워치가 동시에 붙으므로, 같은 컬럼에 나란히 놓여야
"EMFIT 이 못 읽는 수면 단계를 워치가 읽는가"를 그대로 대조할 수 있다.

다른 센서와 성격이 다른 점:
  · 같은 날짜를 폴링할 때마다 그날 데이터가 통째로 다시 온다 → 중복 제거는 저장 단계
    (analyzer._store_garmin_record)에서 한다. 파서는 받은 것을 그대로 정규화만 한다.
  · '연결됨'을 알려주는 필드가 없다. 마지막 동기화 시각(last_sync_gmt)의 경과로 판정한다.
    워치는 폰을 거쳐 올라오므로 수십 분~수 시간 지연이 정상이다.
  · 걸음(totalSteps)과 밀기(totalPushes)는 상호 배타다 — 휠체어 사용자는 걸음이 아예
    안 잡히고 밀기로 집계될 수 있다 (익명화된 실측 예: 계정 A는 pushes만,
    계정 B는 steps만 제공).
"""

import re
from datetime import datetime, timedelta, timezone


KST = timezone(timedelta(hours=9))
UTC = timezone.utc

# epoch 초와 밀리초를 가르는 경계 (fsr_parser 와 같은 기준).
_MS_THRESHOLD = 1e11

# SN 규칙. ⚠️ 12자리 16진수로 만들면 _is_radar_device 가 AI Radar 로 오인한다.
SN_PREFIX = "garmin-"
# analyzer._ensure_garmin_device 의 등록 조건과 같은 모양을 여기서도 막는다.
# (파서가 통과시키고 등록에서 걸리면 '데이터는 쌓이는데 화면엔 없는' 상태가 된다)
_SN_RE = re.compile(r"garmin-[A-Za-z0-9][A-Za-z0-9_.-]{1,48}$")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(n) if n.is_integer() else n


def _integer(value):
    n = _number(value)
    return int(n) if n is not None else None


def _minutes(seconds):
    """초 → 분(소수 1자리). Emfit 수면 요약과 같은 단위로 맞춘다."""
    n = _number(seconds)
    return round(n / 60, 1) if n is not None else None


def _epoch_from_gmt(value):
    """'2026-09-13T15:00:00.0' (GMT) → epoch 초. 실패하면 None."""
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.0", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    return None


def _epoch_seconds(value):
    """epoch(초 또는 밀리초) → 초. Garmin 은 밀리초로 주는 곳이 많다."""
    n = _number(value)
    if n is None or n <= 0:
        return None
    return float(n) / 1000.0 if n > _MS_THRESHOLD else float(n)


def _date_end_epoch(date_str, now_ts=None):
    """일별 요약 행에 쓸 타임스탬프 = 그 날짜의 끝(KST), 단 오늘이면 '지금'.

    ⚠️ last_sync_gmt 를 쓰면 안 된다. 09-13 의 wellnessEndTimeGmt 는 실제로
    '2026-09-13T15:00:00.0' GMT = KST 09-14 00:00 이라, 요약 행이 하루 뒤 날짜로
    저장돼 버린다 (2026-09-14 실측으로 확인).
    """
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d").replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None
    end = d + timedelta(hours=23, minutes=59, seconds=59)
    now = now_ts if now_ts is not None else datetime.now(KST).timestamp()
    return min(end.timestamp(), now)


# ── 일별 요약 매핑 ────────────────────────────────────────────────────
# (한국어 컬럼, Garmin 필드, 변환). 값이 없으면 컬럼 자체를 만들지 않는다 —
# 계정·기기마다 제공 지표가 다르다 (예: 일부 계정에는 averageSpo2가 없을 수 있다).
_SUMMARY_MAP = (
    ("안정시심박",      "restingHeartRate",              _integer),
    ("최저심박",        "minHeartRate",                  _integer),
    ("최고심박",        "maxHeartRate",                  _integer),
    ("호흡수(RR)",      "avgWakingRespirationValue",     _number),
    ("산소포화도",      "averageSpo2",                   _number),
    ("걸음수",          "totalSteps",                    _integer),
    ("밀기",            "totalPushes",                   _integer),
    ("이동거리(m)",     "totalDistanceMeters",           _number),
    ("좌식(분)",        "sedentarySeconds",              _minutes),
    ("활동(분)",        "activeSeconds",                 _minutes),
    ("고강도(분)",      "highlyActiveSeconds",           _minutes),
    ("활동칼로리",      "activeKilocalories",            _number),
    ("스트레스평균",    "averageStressLevel",            _integer),
    ("스트레스최대",    "maxStressLevel",                _integer),
    ("바디배터리",      "bodyBatteryMostRecentValue",    _integer),
)

# 수면 — Emfit '수면종료요약' 행과 컬럼 이름을 맞춘다 (위 docstring 참고).
_SLEEP_MAP = (
    ("총수면(분)",     "sleepTimeSeconds",       _minutes),
    ("깊은수면(분)",   "deepSleepSeconds",       _minutes),
    ("REM수면(분)",    "remSleepSeconds",        _minutes),
    ("얕은수면(분)",   "lightSleepSeconds",      _minutes),
    ("각성시간(분)",   "awakeSleepSeconds",      _minutes),
)


def _build_daily(summary, sleep):
    """요약 + 수면 → 한국어 컬럼 dict. 값이 하나도 없으면 None."""
    out = {}
    if isinstance(summary, dict):
        for col, key, conv in _SUMMARY_MAP:
            v = conv(summary.get(key))
            if v is not None:
                out[col] = v
    if isinstance(sleep, dict):
        for col, key, conv in _SLEEP_MAP:
            v = conv(sleep.get(key))
            if v is not None:
                out[col] = v
        # 수면점수는 한 겹 더 들어가 있다: sleepScores.overall.value
        scores = sleep.get("sleepScores")
        overall = scores.get("overall") if isinstance(scores, dict) else None
        if isinstance(overall, dict):
            score = _integer(overall.get("value"))
            if score is not None:
                out["수면점수"] = score
                qual = overall.get("qualifierKey")
                if qual:
                    out["수면평가"] = str(qual)
    return out or None


def parse_garmin_payload(row):
    """폴러가 보낸 Garmin payload 정규화. Garmin 이 아니면 None.

    반환:
      {"sn", "account", "date", "server_received_at",
       "last_sync_ts": epoch초|None,
       "hr": [(epoch초, bpm), ...],          ← 시각 오름차순, 값 없는 구간은 제외
       "daily": {한국어 컬럼: 값} | None,
       "daily_ts": epoch초|None,
       "auth_error": str|None}
    """
    if not isinstance(row, dict) or row.get("data_source") != "garmin":
        return None

    sn = str(row.get("sn") or "").strip()
    if not sn:
        account = str(row.get("account") or "").strip()
        if not account:
            return None
        sn = f"{SN_PREFIX}{account}"
    # 다른 기기 SN 을 덮어쓰지 않도록 접두사와 모양을 강제한다.
    if not _SN_RE.match(sn):
        return None

    date_str = str(row.get("date") or "").strip()

    # 인증 실패 보고 — 폴러가 401 을 만났을 때 보낸다. 측정값은 없다.
    # '데이터 없음'(워치를 안 찼다)과 반드시 구분해야 하므로 별도 필드로 올린다.
    auth_error = row.get("auth_error")
    auth_error = str(auth_error)[:200] if auth_error else None

    hr = []
    for item in row.get("hr") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        ts = _epoch_seconds(item[0])
        bpm = _integer(item[1])
        # Garmin 은 측정이 없는 구간을 null 로 채워 보낸다 — 그대로 저장하면
        # 빈 행만 늘어나므로 값이 있는 것만 남긴다.
        if ts is None or bpm is None or bpm <= 0:
            continue
        hr.append((ts, bpm))
    hr.sort(key=lambda x: x[0])

    daily = _build_daily(row.get("summary"), row.get("sleep"))

    return {
        "sn": sn,
        "account": str(row.get("account") or "").strip() or sn[len(SN_PREFIX):],
        "date": date_str or None,
        "server_received_at": row.get("server_received_at"),
        "last_sync_ts": _epoch_from_gmt(row.get("last_sync_gmt")),
        "hr": hr,
        "daily": daily,
        "daily_ts": _date_end_epoch(date_str) if daily else None,
        "auth_error": auth_error,
    }
