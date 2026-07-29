"""JCFT McKare (VSR 22) payload normalization.

McKare 전용 필드 이름만 이 모듈에서 해석한다. Emfit/라닉스 파서를 건드리지 않도록
분리해서, 한 기기 규격이 바뀌어도 다른 기기 처리에 번지지 않게 한다.

VSR 22 필드(문서 기준):
  macAddress, wifiRssi, respirationDetection(=Occupancy 0~3), activityDetection,
  respirationRate, heartRate, fallDetection(0~2), temperature, firmwareVersion(선택)
※ respirationDetection 은 '호흡'이 아니라 '재실(Occupancy)' 코드임에 주의.
"""

from datetime import datetime, timedelta, timezone


KST = timezone(timedelta(hours=9))

# Occupancy: 0=재실없음(Idle), 1~3=재실 단계
OCCUPANCY_PRESENT = {1, 2, 3}
# Fall: 0=미감지, 1=의심, 2=확정
FALL_LABEL = {0: "정상", 1: "낙상의심", 2: "낙상확정"}


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


def _mac(value):
    if value is None:
        return None
    s = str(value).strip().replace(":", "").replace("-", "").upper()
    return s or None


def _timestamp(row):
    """McKare 는 측정 시각 필드가 없으므로 server_received_at 을 KST 로 해석."""
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


def parse_mckare_payload(row):
    """McKare(VSR22) payload 정규화. McKare 가 아니면 None.

    라닉스 FALL 도 macAddress 를 쓰므로, 'pose' 가 있으면 라닉스로 보고 제외한다.
    McKare 는 respirationDetection/heartRate 를 가지거나 data_source=='mckare' 로 식별."""
    if not isinstance(row, dict):
        return None
    if "pose" in row or "POS" in row:          # 라닉스(FALL/BED) 는 제외
        return None
    has_mac = bool(row.get("macAddress"))
    looks_mckare = ("respirationDetection" in row or "heartRate" in row
                    or row.get("data_source") == "mckare")
    if not (has_mac and looks_mckare):
        return None

    sn = _mac(row.get("macAddress"))
    ts = _timestamp(row)
    if not sn or ts is None:
        return None

    occ = _integer(row.get("respirationDetection"))     # 0~3 재실 코드
    fall = _integer(row.get("fallDetection"))            # 0~2
    return {
        "sn": sn,
        "ts": ts,
        "server_received_at": row.get("server_received_at"),
        "hr": _integer(row.get("heartRate")),
        "rr": _integer(row.get("respirationRate")),
        "act": _integer(row.get("activityDetection")),
        "temp": _number(row.get("temperature")),
        "occupancy": occ,
        "present": occ in OCCUPANCY_PRESENT,
        "fall_code": fall,
        "fall": fall is not None and fall >= 1,
        "wifi_rssi": _number(row.get("wifiRssi")),
        "firmware": row.get("firmwareVersion"),
    }
