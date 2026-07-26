"""RANIX RMR602A AI Radar payload normalization.

This module only understands Radar-specific field names.  Keeping that logic
outside analyzer.py prevents changes to the existing Emfit parser from
spreading through the rest of the dashboard code.
"""

from datetime import datetime, timedelta, timezone


KST = timezone(timedelta(hours=9))

BED_POSTURES = {
    -1: "감지 대기",
    0: "누움",
    1: "앉음",
    2: "배회",
    3: "걸터앉음",
    4: "낙상",
    5: "자리비움",
    6: "뒤척임",
}

FALL_POSTURES = {
    2: "재실",
    4: "낙상",
    5: "자리비움",
}


def _number(value):
    """Return an int/float for numeric strings, otherwise None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _integer(value):
    number = _number(value)
    return int(number) if number is not None else None


def _positive_number(value):
    number = _number(value)
    return number if number is not None and number > 0 else None


def _mac(value):
    if value is None:
        return None
    normalized = str(value).strip().replace(":", "").replace("-", "").upper()
    return normalized or None


def _timestamp(row):
    """Return a Unix timestamp.

    Radar payloads do not contain a measurement time, so app.py's
    server_received_at value is interpreted as KST.  A numeric
    date_occurred is also accepted if future firmware adds one.
    """
    occurred = row.get("date_occurred")
    numeric = _number(occurred)
    if numeric is not None:
        return float(numeric)

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


def parse_radar_payload(row):
    """Normalize a BED or FALL model payload; return None for non-Radar rows."""
    if not isinstance(row, dict):
        return None

    is_bed = bool(row.get("MAC")) and "POS" in row
    is_fall = bool(row.get("macAddress")) and "pose" in row
    if not (is_bed or is_fall):
        return None

    model = "bed" if is_bed else "fall"
    sn = _mac(row.get("MAC") if is_bed else row.get("macAddress"))
    ts = _timestamp(row)
    if not sn or ts is None:
        return None

    pos = _integer(row.get("POS") if is_bed else row.get("pose"))
    posture_map = BED_POSTURES if is_bed else FALL_POSTURES
    posture = posture_map.get(pos, f"알 수 없음({pos})")

    err = _integer(row.get("ERR", row.get(" ERR"))) if is_bed else None
    connected = err != 1

    return {
        "sn": sn,
        "ts": ts,
        "server_received_at": row.get("server_received_at"),
        "model": model,
        "pos": pos,
        "posture": posture,
        "hr": _positive_number(row.get("HR")) if is_bed else None,
        "rr": _positive_number(row.get("BR")) if is_bed else None,
        "err": err,
        "connected": connected,
        "fall": pos == 4,
        "person_count": _integer(row.get("pnum")) if is_fall else None,
    }
