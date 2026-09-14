"""ESP32 압력(FSR) 사용감지 센서 payload normalization.

돌봄기기에 붙여 '지금 이 기기가 쓰이고 있는가'를 보는 센서다.
압력이 감지되면(press) **사용 중**, 압력이 사라지면(release) **미사용**.
Emfit/라닉스/McKare 와 달리 생체신호(심박·호흡)는 재지 않는다.

ESP32 보드(jy01)가 보내는 이벤트 payload 만 이 모듈에서 해석한다.
파서를 분리해둬서 보드 펌웨어 규격이 바뀌어도 다른 기기 처리에 번지지 않는다.

예상 payload:
  {"deviceId": "fb-A3F2", "event": "press", "uptime_ms": 123456,
   "battery_mv": 3850, "battery_pct": 62, "duration_ms": 0}

다른 센서와 성격이 다른 점:
  · 이벤트 기반이다. 조용한 게 정상이므로 '몇 분 무소식'을 이상으로 보면 안 된다.
  · 생체값이 없다. 대신 배터리가 있어 잔량을 화면에 띄운다.
  · duration_ms 는 직전에 압력이 유지된 시간 = '그때 얼마나 오래 썼는지'.

⚠️ uptime_ms 는 '부팅 후 경과 시간'이지 절대 시각이 아니다. 측정 시각으로 쓰면
   1970년 데이터가 되므로 타임스탬프로 절대 사용하지 않는다.
   보드가 epoch(ts/timestamp/time/epoch/...)를 보내면 그것을 쓰고,
   없으면 McKare 처럼 서버 수신시각(server_received_at)을 KST 로 해석한다.
"""

from datetime import datetime, timedelta, timezone


KST = timezone(timedelta(hours=9))

# 보드 펌웨어마다 event 표기가 조금씩 다를 수 있어 넉넉하게 받아준다.
# press = 압력 감지(사용 시작), release = 압력 해제(사용 종료)
PRESS_EVENTS = {"press", "pressed", "down", "on", "push", "1", "true"}
RELEASE_EVENTS = {"release", "released", "up", "off", "0", "false"}
# 생존신고(keep-alive) — 보드가 '나 살아있다'고 알리거나 재부팅했을 때 보내는 신호.
# ⚠️ 여기의 "heartbeat" 는 통신 용어(주기적 생존 신호)이지 심박(HR)과 아무 관계가 없다.
#    이 서버는 심박을 다루므로 헷갈리기 쉬워 상수 이름을 KEEPALIVE 로 쓴다.
#    (analyzer._process_line 의 '상태 하트비트'도 같은 통신 용어다)
# 이 이벤트들은 사용/미사용 상태를 바꾸지 않는다 — 안 그러면 보드가 재부팅할 때마다
# 화면의 사용 상태가 초기화되거나 'boot' 같은 날 문자열이 그대로 표시된다.
KEEPALIVE_EVENTS = {"heartbeat", "alive", "ping", "status", "boot", "hello", "init"}

# 센서 이상 — 펌웨어가 '압력선이 빠졌다/센서를 못 읽겠다'를 직접 알려주는 이벤트.
# 서버는 침묵만으로는 고장을 알 수 없으므로(안 쓰는 것과 구분 불가),
# 보드가 이렇게 알려주면 그게 가장 확실한 근거가 된다.
FAULT_EVENTS = {"error", "fault", "disconnect", "disconnected", "unplugged",
                "sensor_error", "sensorfault", "open", "opencircuit", "nc"}

# 화면에 그대로 나가는 문구라 센서 용어(눌림/해제)가 아니라 의미(사용/미사용)로 적는다.
EVENT_LABEL = {"press": "사용 중", "release": "미사용",
               "keepalive": "생존신고", "fault": "센서 이상"}

# 절대 시각(epoch)으로 인정하는 필드 이름 후보. uptime_ms 는 의도적으로 제외.
_TS_FIELDS = ("ts", "timestamp", "time", "epoch", "date_occurred", "measured_at", "epoch_ms")

# epoch 초와 밀리초를 가르는 경계. 1e11 초 = 서기 5138년이라
# 정상적인 '초' 값은 절대 넘지 않고, 현재의 '밀리초' 값(~1.7e12)은 항상 넘는다.
_MS_THRESHOLD = 1e11


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


def _device_id(value):
    """deviceId 정규화.

    MAC 주소 형태(구분자 떼면 12자리 16진수)면 라닉스·McKare 와 똑같이
    '구분자 없는 대문자'로 통일한다. 안 그러면 펌웨어가 'A1:B2:C3:D4:E5:F6'으로
    보내다가 'ece334450058' 로 바꾸는 순간 다른 기기로 잡혀 카드가 두 개 생긴다.
    MAC 이 아니면(예: 'jy01', 'fb-A3F2') 사람이 정한 이름이므로 그대로 둔다."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    compact = s.replace(":", "").replace("-", "").upper()
    if len(compact) == 12 and all(c in "0123456789ABCDEF" for c in compact):
        return compact
    return s


def _event_kind(raw):
    """event 문자열 → 'press' | 'release' | 'keepalive' | 'fault' | None(알 수 없음)."""
    if raw is None:
        return None
    e = str(raw).strip().lower()
    if e in PRESS_EVENTS:
        return "press"
    if e in RELEASE_EVENTS:
        return "release"
    if e in KEEPALIVE_EVENTS:
        return "keepalive"
    if e in FAULT_EVENTS:
        return "fault"
    return None


def _timestamp(row):
    """측정 시각(epoch 초, float). 보드가 epoch 을 보내면 그것을, 아니면 서버 수신시각."""
    for k in _TS_FIELDS:
        if k not in row:
            continue
        n = _number(row.get(k))
        if n is None or n <= 0:
            continue
        return float(n) / 1000.0 if n > _MS_THRESHOLD else float(n)

    received = row.get("server_received_at")
    if not received:
        return None
    try:
        dt = datetime.fromisoformat(str(received).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.timestamp()


def parse_fsr_payload(row):
    """ESP32 FSR payload 정규화. FSR 이 아니면 None.

    식별 기준: data_source == 'fsr' (서버가 /jy01 수신 시 찍어줌) 이거나,
    deviceId + event 조합을 가진 경우. 다른 센서들은 macAddress/MAC 을 쓰고
    event 필드가 없어서 서로 겹치지 않는다."""
    if not isinstance(row, dict):
        return None
    if row.get("data_source") == "fsr":
        pass
    elif not (row.get("deviceId") and "event" in row):
        return None

    sn = _device_id(row.get("deviceId"))
    ts = _timestamp(row)
    if not sn or ts is None:
        return None

    raw_event = row.get("event")
    kind = _event_kind(raw_event)
    # 알 수 없는 event 도 기록은 남긴다 — 펌웨어가 새 이벤트를 추가해도 데이터를 잃지 않게.
    label = EVENT_LABEL.get(kind) or (str(raw_event).strip() if raw_event is not None else "-")

    return {
        "sn": sn,
        "ts": ts,
        "server_received_at": row.get("server_received_at"),
        "event": kind or (str(raw_event).strip().lower() if raw_event is not None else None),
        "event_label": label,
        # 사용 여부. 생존신고/이상/미상 이벤트는 사용 상태를 바꾸지 않으므로 None 으로 둔다.
        "in_use": True if kind == "press" else (False if kind == "release" else None),
        # 센서 이상 여부 — 펌웨어가 직접 알려준 경우만 True.
        "fault": kind == "fault",
        "duration_ms": _integer(row.get("duration_ms")),
        "battery_pct": _integer(row.get("battery_pct")),
        "battery_mv": _integer(row.get("battery_mv")),
        "uptime_ms": _integer(row.get("uptime_ms")),
    }
