import pandas as pd
import glob
import os
import json
import re
import argparse
from datetime import datetime, timezone, timedelta
from radar_parser import parse_radar_payload
from mckare_parser import parse_mckare_payload
from fsr_parser import parse_fsr_payload

# KST 고정 오프셋(+09:00). 한국은 현재 서머타임이 없어 'Asia/Seoul' 과 동일하며,
# 행마다 pandas Timestamp 를 만드는 것보다 표준 datetime 이 10배 이상 빠르다.
_KST = timezone(timedelta(hours=9))
_UTC = timezone.utc

# [설정] 기기 정보 — device_info.json 파일에 저장. 대시보드 /devices 에서 편집 가능.
import threading as _threading

_DEVICE_INFO_FILE = "device_info.json"
_DEVICE_INFO_LOCK = _threading.Lock()

_DEFAULT_DEVICE_INFO = {
    "EMFIT-DEMO-01": {"name": "돌봄A", "location": "-", "group": "일반"},
    "EMFIT-DEMO-02": {"name": "돌봄B", "location": "테스트 공간", "group": "일반"},
    "EMFIT-DEMO-03": {"name": "사용자-C", "location": "A시설", "group": "일반"},
    "EMFIT-DEMO-04": {"name": "사용자-D", "location": "사용자-D님 가정", "group": "뇌성마비"},
    "EMFIT-DEMO-05": {"name": "사용자E", "location": "301호", "group": "일반"},
    # AI Radar(라닉스 RMR602A). 실물 기기가 두 번 바뀌었다:
    #   A1B2C3D4E5F6 → A1B2C3D4E5F7 → A1B2C3D4E5F8 (2026-08-06 현재 이 한 대만 보유)
    # 옛 두 대는 아래 _RETIRED_SNS 로 정리한다.
    "A1B2C3D4E5F8": {"name": "AI Radar", "location": "-", "group": "일반"},
}

_RADAR_DEVICE_DEFAULTS = {
    "A1B2C3D4E5F8": _DEFAULT_DEVICE_INFO["A1B2C3D4E5F8"],
}

# ESP32 압력 사용감지 센서(돌봄기기에 부착) — 알려진 기기는 여기에 적어두면
# 데이터가 없어도 대시보드에 자리를 잡는다. name/location 은 /devices 에서 편집 가능.
# 여기 없는 ID 로 데이터가 들어오면 _ensure_fsr_device 가 자동 등록한다(아래 참고).
# SN 은 보드가 보내는 deviceId 와 같아야 한다. MAC 을 쓸 경우 구분자 없는 대문자
# (fsr_parser._device_id 가 그렇게 정규화한다).
# 지금은 비어 있다 — 실물 보드(jy02, jy03 …)가 처음 데이터를 보내면 자동 등록되므로
# 여기에 미리 적어둘 필요가 없다. 데이터 없이도 자리를 잡아둬야 하는 기기만 넣는다.
_FSR_DEVICE_DEFAULTS = {}

# ── 퇴역 기기 정리 (1회성 마이그레이션) ─────────────────────────────
# 교체·철거된 기기의 '기본 등록'을 배정 목록에서 뺀다. 기본 등록은 서버가 자동으로
# 넣어준 자리라, 안 쓰게 되면 대시보드에 빈 카드로 계속 남는 게 오히려 헷갈린다.
#
# ⚠️ 단, 사람이 손댄 흔적이 있으면 절대 건드리지 않는다 (아래 _is_untouched_default).
#    이름·위치를 고쳤거나 배정 이력이 갈라졌다면 그건 사람이 의미를 부여한 기록이고,
#    코드가 임의로 지울 대상이 아니다. 그 경우엔 화면에 남고 사용자가 직접 정리하면 된다.
# 값 = 시드 당시 이름. 그 이름·기본 위치 그대로일 때만 지운다(사람이 손댄 건 보존).
# 값이 None 이면 강제 제거 — 이름·위치를 바꿨더라도 지운다.
#   ⚠️ 강제는 "사용자가 이 기기를 명시적으로 없애라고 한 경우"에만 쓴다.
#      측정 데이터가 있는 기기를 강제로 빼면 과거 기록이 Unknown(SN) 으로 표시된다.
_RETIRED_SNS = {
    # 2026-08-03 AI Radar 교체 — 제조사 사용 권장 중단.
    # 2026-08-06 강제 제거로 전환: 위치를 '테스트 공간'로 바꿔둔 탓에 안 지워지고 있었다.
    # 수신 데이터가 없는 기기라 잃을 기록도 없다.
    "A1B2C3D4E5F6": None,
    # 2026-08-06 AI Radar 재교체 — 설치 시 다른 실물이 와서 MAC 변경. 보유 기기는 A1B2C3D4E5F8 한 대뿐.
    "A1B2C3D4E5F7": None,
    # 2026-08-06 사용감지 테스트 잔재 정리 — 실제 운영에 쓰지 않는 자리.
    "B1C2D3E4F5A6": None,
    "CONNECTIVITY-TEST": None,
}

# 기기 종류(kind) — 대시보드가 어느 섹션에 넣을지 판단하는 값.
# ⚠️ 종류를 SN 모양으로 추측하면 안 된다. ESP32 의 MAC 도 12자리 16진수라
#    레이더(라닉스)와 생김새가 같아서, 데이터가 도착하기 전에는 구분할 수 없다.
#    그래서 배정 정보에 kind 를 박아둔다.
KIND_FSR = "fsr"
# 자동 등록 상한 — /jy01 은 인증이 없어서, 장난성 요청으로 기기 목록이
# 무한히 불어나지 않도록 막아둔다. 실제로 늘릴 일이 있으면 이 값을 올리면 된다.
_FSR_AUTO_REGISTER_LIMIT = 20

# 대상자 그룹 — 여기 리스트에만 추가하면 기기 정보·이전 폼의 선택지와 검증에 자동 반영된다.
GROUPS = ["일반", "뇌성마비", "척수손상", "근육병", "발달장애"]
DEFAULT_GROUP = GROUPS[0]


def _load_device_info():
    if os.path.exists(_DEVICE_INFO_FILE):
        try:
            with open(_DEVICE_INFO_FILE, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[analyzer] device_info.json 로드 실패, 기본값 사용: {e}", flush=True)
    try:
        with open(_DEVICE_INFO_FILE, "w", encoding='utf-8') as f:
            json.dump(_DEFAULT_DEVICE_INFO, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return dict(_DEFAULT_DEVICE_INFO)


DEVICE_INFO = _load_device_info()


def replace_all_device_info(new_data):
    """DEVICE_INFO 전체를 갱신하고 파일에 저장. 같은 dict 객체를 mutate해서 import 방식 무관하게 반영."""
    with _DEVICE_INFO_LOCK:
        DEVICE_INFO.clear()
        DEVICE_INFO.update(new_data)
        with open(_DEVICE_INFO_FILE, "w", encoding='utf-8') as f:
            json.dump(DEVICE_INFO, f, ensure_ascii=False, indent=2)


# ───────────────────────── 배정 이력 (assignments) ─────────────────────────
# device_info.json  = "지금 이 기기를 누가 쓰는가" (기기당 한 줄).
# assignments.json  = "이 기기를 과거에 누가, 언제부터 언제까지 썼는가" (기간별 여러 줄).
# 데이터는 SN(기기 시리얼)으로만 들어오므로, 측정 시각을 배정 이력과 대조해
# '그때 그 기기를 쓰던 사람'을 찾아 붙인다. → 기기를 옮겨도 과거 데이터가 안 섞인다.
_ASSIGNMENTS_FILE = "assignments.json"
_ASSIGNMENTS_LOCK = _threading.Lock()


def _seed_assignments_from_device_info():
    """assignments.json 이 없을 때 — 현재 device_info.json 의 각 기기를
    '첫 배정'으로 삼는다. start=None(태초부터), end=None(현재 사용중)."""
    seeded = []
    for sn, info in DEVICE_INFO.items():
        seeded.append({
            "id": f"{sn}-1",
            "sn": sn,
            "user": info.get("name", sn),
            "location": info.get("location", "-"),
            "group": info.get("group", "일반"),
            "start": None,
            "end": None,
        })
    return seeded


def _load_assignments():
    if os.path.exists(_ASSIGNMENTS_FILE):
        try:
            with open(_ASSIGNMENTS_FILE, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception as e:
            print(f"[analyzer] assignments.json 로드 실패, device_info 로 시드: {e}", flush=True)
    seeded = _seed_assignments_from_device_info()
    try:
        with open(_ASSIGNMENTS_FILE, "w", encoding='utf-8') as f:
            json.dump(seeded, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return seeded


def _save_assignments():
    with open(_ASSIGNMENTS_FILE, "w", encoding='utf-8') as f:
        json.dump(ASSIGNMENTS, f, ensure_ascii=False, indent=2)


def _parse_dt(s):
    """배정 start/end 문자열 → naive datetime(KST 기준). None/'' → None.
    측정 시각(_to_naive_kst)도 naive datetime 이라 datetime 끼리 바로 비교된다."""
    if not s:
        return None
    try:
        return pd.Timestamp(str(s).replace("T", " ")).to_pydatetime()
    except Exception:
        return None


def _to_naive_kst(dt):
    """측정 시각(tz 유무 무관)을 naive KST datetime 으로 정규화.
    add_to_storage 가 넘기는 표준 datetime 이 주 경로이고, 혹시 pandas Timestamp 가
    들어와도 처리한다. 반환값은 _parse_dt(배정 경계)와 비교 가능한 naive 값."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone(_KST).replace(tzinfo=None)
        return dt
    ts = pd.Timestamp(dt)
    if ts.tzinfo is not None:
        ts = ts.tz_convert('Asia/Seoul').tz_localize(None)
    return ts.to_pydatetime()


# {sn: [(start_ts, end_ts, assignment_dict), ...]} — resolve 를 빠르게 하려고 미리 파싱
_ASSIGN_INDEX = {}


def _rebuild_assign_index():
    idx = {}
    for a in ASSIGNMENTS:
        sn = a.get("sn")
        if not sn:
            continue
        idx.setdefault(sn, []).append(
            (_parse_dt(a.get("start")), _parse_dt(a.get("end")), a))
    _ASSIGN_INDEX.clear()
    _ASSIGN_INDEX.update(idx)


def resolve_assignment(sn, dt):
    """dt 시점에 sn 기기를 쓰던 배정을 반환 ({id,sn,user,location,group,...}).
    매칭 실패 시 활성 배정 → 마지막 배정 → Unknown 순으로 폴백."""
    entries = _ASSIGN_INDEX.get(sn, [])
    if entries:
        target = _to_naive_kst(dt)
        for start, end, a in entries:
            if (start is None or target >= start) and (end is None or target < end):
                return a
        for start, end, a in entries:
            if end is None:
                return a
        return entries[-1][2]
    return {"id": sn, "sn": sn, "user": f"Unknown({sn})",
            "location": "Unknown", "group": "일반", "start": None, "end": None}


def _rebuild_device_info():
    """ASSIGNMENTS 의 활성 배정(end=None)으로 DEVICE_INFO 를 갱신.
    실시간 대시보드 등 '현재' 정보를 쓰는 코드와 호환을 유지한다."""
    def _entry(a, sn):
        e = {"name": a.get("user", sn),
             "location": a.get("location", "-"),
             "group": a.get("group", "일반")}
        # kind 는 기기 종류(사용감지 등). 대시보드 섹션 분류에 쓰므로 같이 넘긴다.
        if a.get("kind"):
            e["kind"] = a["kind"]
        # hidden 은 '대시보드 카드에서만 감춤'. 데이터·리포트·기기관리에는 그대로 남는다.
        if a.get("hidden"):
            e["hidden"] = True
        return e

    new_info = {}
    for a in ASSIGNMENTS:
        sn = a.get("sn")
        if sn and not a.get("end"):
            new_info[sn] = _entry(a, sn)
    for a in ASSIGNMENTS:  # 활성 배정이 없는 기기는 마지막 배정으로
        sn = a.get("sn")
        if sn and sn not in new_info:
            new_info[sn] = _entry(a, sn)
    with _DEVICE_INFO_LOCK:
        DEVICE_INFO.clear()
        DEVICE_INFO.update(new_info)
        try:
            with open(_DEVICE_INFO_FILE, "w", encoding='utf-8') as f:
                json.dump(DEVICE_INFO, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def list_assignments():
    """UI 표시용 — 전체 배정 이력 사본."""
    return [dict(a) for a in ASSIGNMENTS]


def handover(sn, user, location, group, when):
    """기기 sn 을 새 사용자에게 이전. when 시점에 활성 배정을 닫고(end=when),
    when 부터 새 배정을 연다. when='YYYY-MM-DD HH:MM'. 새 배정ID 반환."""
    when_norm = str(when).replace("T", " ").strip()
    with _ASSIGNMENTS_LOCK:
        count = 0
        for a in ASSIGNMENTS:
            if a.get("sn") == sn:
                count += 1
                if not a.get("end"):
                    a["end"] = when_norm
        new_id = f"{sn}-{count + 1}"
        # 기기 종류는 사용자가 바뀌어도 그대로다 — 이전 배정에서 물려받는다.
        prev_kind = next((a.get("kind") for a in ASSIGNMENTS
                          if a.get("sn") == sn and a.get("kind")), None)
        new_entry = {
            "id": new_id, "sn": sn, "user": user,
            "location": location, "group": group,
            "start": when_norm, "end": None,
        }
        if prev_kind:
            new_entry["kind"] = prev_kind
        # 숨김 상태도 물려받는다 — 사용자가 바뀌어도 같은 실물 기기다.
        # 다시 쓰기 시작했다면 /devices 에서 표시로 되돌리면 된다.
        if any(a.get("sn") == sn and a.get("hidden") for a in ASSIGNMENTS):
            new_entry["hidden"] = True
        ASSIGNMENTS.append(new_entry)
        _save_assignments()
        _rebuild_assign_index()
    _rebuild_device_info()
    return new_id


def update_active_user(sn, new_user):
    """sn 기기의 활성 배정 user 이름만 수정 (location/group/start 등은 보존).
    종료된 배정은 건드리지 않음 — 옛 배정의 이름은 그 시기의 역사 기록이므로 보존.
    활성 배정이 없으면 False."""
    new_user = (str(new_user) or "").strip()
    if not new_user:
        return False
    with _ASSIGNMENTS_LOCK:
        target = None
        for a in ASSIGNMENTS:
            if a.get("sn") == sn and not a.get("end"):
                target = a
                break
        if target is None:
            return False
        target["user"] = new_user
        _save_assignments()
        _rebuild_assign_index()
        aid, loc = target.get("id"), target.get("location", "-")
    _rebuild_device_info()
    # 배정 경계는 그대로다 → 재파싱 없이 라벨만 갈아끼운다 (수분 → 즉시)
    relabel_assignment(aid, new_user, loc)
    return True


def update_active_assignments(updates):
    """{sn: {name, location, group}} — 각 기기의 활성 배정 필드를 수정(오타 수정 등).

    반환: True 면 새 배정이 생겨 재파싱이 필요하고, False 면 라벨만 바뀌어
    (이 함수가 이미 갱신했으므로) 호출부가 따로 할 일이 없다."""
    structural = False
    relabels = []
    with _ASSIGNMENTS_LOCK:
        for sn, info in updates.items():
            active = [a for a in ASSIGNMENTS if a.get("sn") == sn and not a.get("end")]
            if active:
                target = active[-1]
            else:
                cnt = sum(1 for a in ASSIGNMENTS if a.get("sn") == sn)
                target = {"id": f"{sn}-{cnt + 1}", "sn": sn,
                          "start": None, "end": None}
                prev_kind = next((a.get("kind") for a in ASSIGNMENTS
                                  if a.get("sn") == sn and a.get("kind")), None)
                if prev_kind:
                    target["kind"] = prev_kind
                ASSIGNMENTS.append(target)
                structural = True     # 없던 배정이 생겼다 → 재파싱 필요
            target["user"] = info.get("name", sn)
            target["location"] = info.get("location", "-")
            target["group"] = info.get("group", "일반")
            # 숨김은 켤 때만 키를 남기고, 끄면 지운다 (파일에 쓸데없는 false 가 안 쌓이게)
            if info.get("hidden"):
                target["hidden"] = True
            else:
                target.pop("hidden", None)
            relabels.append((target.get("id"), target["user"], target["location"]))
        _save_assignments()
        _rebuild_assign_index()
    _rebuild_device_info()
    if not structural:
        # 이름·위치만 바뀐 경우 — 재파싱 없이 라벨만 갈아끼운다
        for aid, user, loc in relabels:
            relabel_assignment(aid, user, loc)
    return structural


def relabel_assignment(assignment_id, user, location):
    """이미 읽어둔 레코드의 '사용자/위치' 라벨만 그 자리에서 갈아끼운다.

    왜 필요한가 — 레코드에는 파싱할 때 사용자명이 박혀서 저장된다. 그래서 지금까지는
    이름만 고쳐도 invalidate_cache() 로 캐시를 통째로 버리고 86MB 를 다시 읽었다.
    저장 버튼을 누르고 몇 분씩 기다려야 했던 이유다.

    하지만 **이름 수정은 배정 경계(start/end)를 건드리지 않는다.** 어떤 레코드가
    어느 배정에 속하는지가 그대로이므로, 라벨만 바꾸면 재파싱과 결과가 같다.
    (기기 이전처럼 경계가 바뀌는 경우는 여전히 invalidate_cache 가 맞다)

    반환: 갈아끼운 레코드 수."""
    n = 0
    for slot in _caches.values():
        st = slot.get("storage")
        if not st:
            continue
        for recs in st.values():
            for r in recs:
                if r.get("배정ID") == assignment_id:
                    r["사용자"] = user
                    r["위치"] = location
                    n += 1
    # 최신상태·날짜목록 캐시는 레코드 사본을 들고 있을 수 있어 비운다.
    # (둘 다 다시 만드는 비용이 작다 — 재파싱과 달리 storage 는 그대로 쓴다)
    _latest_states_cache["key"] = None
    _latest_states_cache["result"] = None
    _avail_assign_cache["key"] = None
    _avail_assign_cache["result"] = None
    print(f"[analyzer] 라벨 갱신: 배정 {assignment_id} → {user} / {location} ({n}건, 재파싱 없음)", flush=True)
    return n


def invalidate_cache():
    """파싱 캐시 무효화 — 배정 경계가 바뀌었을 때만 쓴다(기기 이전 등).
    다음 조회 때 전체 재파싱하여 과거 데이터까지 올바른 사용자로 다시 라벨링한다.
    ⚠️ 로그가 크면 수십 초~수 분 걸린다. 이름만 바뀌는 경우엔 relabel_assignment 를 쓸 것."""
    _caches.clear()
    _merged_cache["key"] = None
    _merged_cache["storage"] = None
    _latest_states_cache["key"] = None
    _latest_states_cache["result"] = None
    _avail_assign_cache["key"] = None
    _avail_assign_cache["result"] = None


# ── 모듈 초기화: 배정 이력 로드 후 DEVICE_INFO 동기화 ──
ASSIGNMENTS = _load_assignments()
_assignments_changed = False


def _is_untouched_default(entry, sn, seeded_name):
    """서버가 자동으로 넣어준 기본 등록 그대로인지 (사람이 손대지 않았는지)."""
    return (entry.get("id") == f"{sn}-1"
            and entry.get("user") == seeded_name
            and entry.get("location") in ("-", None, "")
            and not entry.get("start")
            and not entry.get("end"))


for _sn, _seeded_name in _RETIRED_SNS.items():
    _rows = [a for a in ASSIGNMENTS if a.get("sn") == _sn]
    if not _rows:
        continue
    _force = _seeded_name is None
    if _force:
        for _r in _rows:
            ASSIGNMENTS.remove(_r)
        _assignments_changed = True
        print(f"[analyzer] 퇴역 기기 정리: {_sn} — 강제 제거 ({len(_rows)}건)", flush=True)
    elif len(_rows) == 1 and _is_untouched_default(_rows[0], _sn, _seeded_name):
        ASSIGNMENTS.remove(_rows[0])
        _assignments_changed = True
        print(f"[analyzer] 퇴역 기기 정리: {_sn} ({_seeded_name}) — 기본 등록 상태라 목록에서 제거", flush=True)
    else:
        print(f"[analyzer] 퇴역 기기 {_sn} 은 수정 이력이 있어 그대로 둔다 "
              f"— 지우려면 _RETIRED_SNS 값을 None 으로", flush=True)

_assigned_sns = {a.get("sn") for a in ASSIGNMENTS}
for _sn, _info, _kind in ([(s, i, None) for s, i in _RADAR_DEVICE_DEFAULTS.items()]
                          + [(s, i, KIND_FSR) for s, i in _FSR_DEVICE_DEFAULTS.items()]):
    # 퇴역시킨 SN 은 기본 목록에 남아 있어도 다시 만들지 않는다
    # (위에서 지운 걸 여기서 되살리면 정리가 무효화된다)
    if _sn in _RETIRED_SNS:
        continue
    if _sn not in _assigned_sns:
        _entry = {
            "id": f"{_sn}-1",
            "sn": _sn,
            "user": _info.get("name", _sn),
            "location": _info.get("location", "-"),
            "group": _info.get("group", "일반"),
            "start": None,
            "end": None,
        }
        if _kind:
            _entry["kind"] = _kind
        ASSIGNMENTS.append(_entry)
        _assigned_sns.add(_sn)   # 두 기본값 목록에 같은 SN 이 있어도 중복 등록되지 않게
        _assignments_changed = True
if _assignments_changed:
    _save_assignments()
_rebuild_assign_index()
_rebuild_device_info()


def add_to_storage(storage, sn, ts, dtype, extra):
    if not ts: return
    try:
        # 타임스탬프 → KST datetime (초 단위 기준, 범위 벗어나면 밀리초로 재해석).
        # pandas 대신 표준 datetime 사용 — 행마다 도는 핫패스라 파싱 속도가 10배 이상 빨라진다.
        epoch = float(ts)
        try:
            dt = datetime.fromtimestamp(epoch, _UTC).astimezone(_KST)
        except (ValueError, OverflowError, OSError):
            dt = datetime.fromtimestamp(epoch / 1000, _UTC).astimezone(_KST)

        date_key = dt.strftime('%Y-%m-%d')
        time_str = dt.strftime('%H:%M:%S')
        
        stint = resolve_assignment(sn, dt)
        base = {
            "날짜": date_key,
            "시간(KST)": time_str,
            "사용자": stint["user"],
            "위치": stint["location"],
            "배정ID": stint["id"],
            "유형": dtype
        }
        # None 값인 컬럼들도 구조 유지를 위해 기본값 채움.
        # 단 Radar 는 활동량/심박변이도를 아예 측정하지 않으므로 그 두 칸을 만들지 않는다.
        # (만들어두면 CSV 에 끝까지 빈 컬럼으로 남아 보기 어려워진다)
        if dtype == "Radar":
            default_fields = {
                "심박수(HR)": None, "호흡수(RR)": None, "상태설명": ""
            }
        elif dtype == "McKare":
            # McKare 는 체온·재실 코드가 고유. 심박변이도(HRV)는 측정 안 함.
            default_fields = {
                "심박수(HR)": None, "호흡수(RR)": None, "활동량(ACT)": None, "상태설명": ""
            }
        elif dtype == "FSR":
            # 사용감지 센서는 생체신호를 아예 측정하지 않는다. 사용 여부와 배터리가
            # 전부라 HR/RR/ACT 칸을 만들지 않는다 (만들면 CSV 에 빈 컬럼만 남는다).
            default_fields = {"상태설명": ""}
        else:
            default_fields = {
                "심박수(HR)": None, "호흡수(RR)": None, "활동량(ACT)": None,
                "심박변이도(RMSSD)": None, "상태설명": ""
            }
        default_fields.update(extra)
        base.update(default_fields)
        
        key = (sn, date_key)
        if key not in storage:
            storage[key] = []
        storage[key].append(base)
    except Exception as e:
        print(f"[데이터 추가 오류] 기기: {sn}, 에러: {e}")

# 로그 파일별 파싱 캐시: {경로: {"mtime":..., "storage":..., "offset":...}}
# Emfit 과 Radar 를 다른 파일에 쌓으므로, 한쪽에 데이터가 들어와도
# 다른 쪽은 다시 읽지 않도록 파일마다 슬롯을 따로 둔다.
_caches = {}
_merged_cache = {"key": None, "storage": None}  # 여러 파일을 합친 결과
_cache_lock = None  # lazy init to avoid import-time threading

# 장비별 최신 하트비트: {sn: {"connected": bool, "status_code": int, "last_seen_ts": int,
#                           "status_since_ts": int, "server_received_at": str}}
_device_status = {}


def get_device_statuses():
    return dict(_device_status)


def _store_radar_record(storage, radar):
    """정규화된 AI Radar 1건을 공통 저장소와 연결상태에 반영."""
    sn = radar["sn"]
    ts = radar["ts"]
    add_to_storage(storage, sn, ts, "Radar", {
        "심박수(HR)": radar.get("hr"),
        "호흡수(RR)": radar.get("rr"),
        "자세(POS)": radar.get("pos"),
        "자세": radar.get("posture"),
        "낙상": bool(radar.get("fall")),
        "Radar오류(ERR)": radar.get("err"),
        "감지인원": radar.get("person_count"),
        "Radar모델": radar.get("model"),
        "상태설명": f"AI Radar {radar.get('model', '').upper()} - {radar.get('posture')}",
    })
    _device_status[sn] = {
        "connected": bool(radar.get("connected")),
        "status_code": radar.get("err"),
        "last_seen_ts": int(ts),
        "status_since_ts": int(ts),
        "server_received_at": radar.get("server_received_at"),
        "source": "ai_radar",
    }


def _store_mckare_record(storage, mck):
    """정규화된 McKare(VSR22) 1건을 공통 저장소와 연결상태에 반영."""
    sn = mck["sn"]
    ts = mck["ts"]
    present = mck.get("present")
    add_to_storage(storage, sn, ts, "McKare", {
        "심박수(HR)": mck.get("hr"),
        "호흡수(RR)": mck.get("rr"),
        "활동량(ACT)": mck.get("act"),
        "체온": mck.get("temp"),
        "재실코드": mck.get("occupancy"),
        "재실": "재실" if present else "부재",
        "낙상": bool(mck.get("fall")),
        "낙상코드": mck.get("fall_code"),
        "상태설명": f"McKare {'재실' if present else '부재'}",
    })
    _device_status[sn] = {
        "connected": True,   # McKare 는 전송 자체가 살아있음의 신호
        "status_code": mck.get("fall_code"),
        "last_seen_ts": int(ts),
        "status_since_ts": int(ts),
        "server_received_at": mck.get("server_received_at"),
        "source": "mckare",
    }


def _ensure_fsr_device(sn):
    """FSR 보드가 등록 안 된 SN 으로 데이터를 보내면 배정을 하나 만들어준다.

    이게 없으면 대시보드는 DEVICE_INFO 에 있는 기기만 그리므로,
    '데이터는 파일에 쌓이는데 화면에는 안 보이는' 상황이 된다.
    보드 ID 를 바꾸거나 두 번째 보드를 붙일 때 서버를 못 만져도 바로 뜨게 하는 장치.
    이름은 나중에 /devices 화면에서 편집하면 된다."""
    if sn in DEVICE_INFO:
        return
    # 퇴역시킨 기기는 되살리지 않는다.
    # 로그 파일에 옛 데이터가 남아 있으면 재파싱 때마다 다시 등록돼 정리가 무효화된다.
    if sn in _RETIRED_SNS:
        return
    # /jy01 은 인증이 없으므로 아무 문자열이나 기기로 만들어주지 않는다.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}", sn):
        return
    auto_count = sum(1 for a in ASSIGNMENTS if a.get("auto") == "fsr")
    if auto_count >= _FSR_AUTO_REGISTER_LIMIT:
        print(f"[analyzer] FSR 자동 등록 상한({_FSR_AUTO_REGISTER_LIMIT}) 도달 — {sn} 건너뜀", flush=True)
        return
    ASSIGNMENTS.append({
        "id": f"{sn}-1",
        "sn": sn,
        "user": f"돌봄기기 {sn}",
        "location": "-",
        "group": DEFAULT_GROUP,
        "start": None,
        "end": None,
        "kind": KIND_FSR,    # 대시보드 섹션 분류용 — SN 모양으로 추측하지 않게
        "auto": "fsr",       # 자동 등록 표시 — 상한 계산에 쓴다
    })
    try:
        _save_assignments()
    except Exception as e:
        print(f"[analyzer] FSR 자동 등록 저장 실패({sn}): {e}", flush=True)
    _rebuild_assign_index()
    _rebuild_device_info()
    print(f"[analyzer] FSR 기기 자동 등록: {sn}", flush=True)


def _store_fsr_record(storage, fsr):
    """정규화된 사용감지 센서 1건을 공통 저장소와 연결상태에 반영."""
    sn = fsr["sn"]
    ts = fsr["ts"]

    # 남의 기기 SN 을 덮어쓰지 않도록 방어.
    # /jy01 은 인증이 없어서, 누가 레이더나 Emfit 의 SN 을 deviceId 로 넣어 보내면
    # 그 기기 카드가 '사용감지'로 뒤바뀔 수 있다. 이미 다른 종류로 등록된 SN 이면 버린다.
    existing = DEVICE_INFO.get(sn)
    if existing is not None and existing.get("kind") != KIND_FSR:
        print(f"[analyzer] FSR 기록 거부 — {sn} 은 이미 다른 종류의 기기로 등록됨", flush=True)
        return

    _ensure_fsr_device(sn)   # 배정을 먼저 만들어야 add_to_storage 가 이름/위치를 제대로 붙인다
    is_fault = bool(fsr.get("fault"))
    add_to_storage(storage, sn, ts, "FSR", {
        "이벤트": fsr.get("event"),
        "이벤트설명": fsr.get("event_label"),
        "사용중": fsr.get("in_use"),
        "센서이상": is_fault,
        "사용시간(ms)": fsr.get("duration_ms"),
        "배터리(%)": fsr.get("battery_pct"),
        "배터리(mV)": fsr.get("battery_mv"),
        "가동시간(ms)": fsr.get("uptime_ms"),
        "상태설명": f"돌봄기기 {fsr.get('event_label')}",
    })

    prev = _device_status.get(sn) or {}
    # 이상 상태는 걸어둔다(latch) — 정상 이벤트(press/release)가 와야 풀린다.
    # 이상 알림 한 번 오고 조용해지면 그게 바로 '확인 필요'한 상황이기 때문.
    if is_fault:
        fault = True
    elif fsr.get("in_use") is not None:
        fault = False
    else:
        fault = bool(prev.get("fault"))

    _device_status[sn] = {
        # 이벤트 기반이라 '조용함'이 정상이다. 전송이 왔다는 것 자체가 살아있다는 뜻.
        "connected": True,
        "status_code": None,
        "last_seen_ts": int(ts),
        "status_since_ts": int(ts),
        "server_received_at": fsr.get("server_received_at"),
        "source": "fsr",
        "battery_pct": fsr.get("battery_pct"),
        "fault": fault,
        # 이 보드가 생존신고를 보내는 펌웨어인지 기억한다.
        # 보내는 보드라면 '조용함 = 이상'이 성립하지만, 안 보내는 보드는
        # 하루 종일 안 쓴 것과 고장을 구분할 수 없으므로 시간 기준을 적용하면 안 된다.
        "keepalive_seen": bool(prev.get("keepalive_seen")) or fsr.get("event") == "keepalive",
    }


def _process_line(line, storage):
    if not line:
        return
    try:
        row = json.loads(line)
    except Exception:
        return

    # 상태 하트비트: row 에 status_at 이 있고 data 의 item 이 serialnumber 를 씀
    if "status_at" in row and isinstance(row.get("data"), list):
        received_at = row.get("server_received_at")
        for item in row.get("data", []):
            if not isinstance(item, dict):
                continue
            item_sn = item.get("serialnumber")
            if not item_sn:
                continue
            _device_status[item_sn] = {
                "connected": bool(item.get("connected", False)),
                "status_code": item.get("status"),
                "last_seen_ts": item.get("lastseenalive"),
                "status_since_ts": item.get("statussince"),
                "server_received_at": received_at,
            }
        return

    # AI Radar 데이터는 Emfit과 필드 이름이 완전히 다르므로 별도 해석기로 분리한다.
    radar = parse_radar_payload(row)
    if radar is not None:
        _store_radar_record(storage, radar)
        return

    # McKare(VSR22) 도 별도 해석기로 분리. (라닉스보다 뒤에 시도 — pose 있으면 라닉스로 감)
    mck = parse_mckare_payload(row)
    if mck is not None:
        _store_mckare_record(storage, mck)
        return

    # ESP32 압력 사용감지 센서 — deviceId + event 조합이라 위 센서들과 겹치지 않는다.
    fsr = parse_fsr_payload(row)
    if fsr is not None:
        _store_fsr_record(storage, fsr)
        return

    sn = row.get("device")

    # 실시간 데이터
    try:
        if "data" in row:
            for item in row.get("data", []) or []:
                if not isinstance(item, dict):
                    continue
                item_sn = item.get("device", sn)
                add_to_storage(storage, item_sn, item.get("date_occurred"), "Live", {
                    "심박수(HR)": item.get("heart_rate"),
                    "호흡수(RR)": item.get("respiration_rate"),
                    "활동량(ACT)": item.get("activity"),
                    "상태설명": "실시간측정"
                })
    except Exception:
        pass

    # HRV 데이터 — 수면 종합 payload의 hrv_data는 문자열 요약치라 dict 리스트가 아님. 그 경우 스킵.
    try:
        hrv_list = row.get("hrv_data")
        if isinstance(hrv_list, list):
            for hrv in hrv_list:
                if not isinstance(hrv, dict):
                    continue
                item_sn = hrv.get("device", sn)
                add_to_storage(storage, item_sn, hrv.get("date_occurred"), "HRV", {
                    "심박변이도(RMSSD)": hrv.get("rmssd"),
                    "상태설명": "HRV측정"
                })
    except Exception:
        pass

    # 수면 상세 + 종료 요약
    try:
        if any(k in row for k in ["sleep_score", "calc_data", "duration_in_sleep"]):
            calc_list = row.get("calc_data", [])
            if isinstance(calc_list, str):
                try:
                    calc_list = json.loads(calc_list)
                except Exception:
                    calc_list = []
            if isinstance(calc_list, list):
                for entry in calc_list:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 4:
                        add_to_storage(storage, sn, entry[0], "SleepDetail", {
                            "심박수(HR)": entry[1], "호흡수(RR)": entry[2],
                            "활동량(ACT)": entry[3], "상태설명": "수면상세기록"
                        })
            ts_end = row.get("to") or row.get("date_occurred")
            if ts_end and "sleep_score" in row:
                add_to_storage(storage, sn, ts_end, "Summary", {
                    "수면점수": row.get("sleep_score"),
                    "총수면(분)": round((row.get("duration_in_sleep") or 0) / 60, 1),
                    "얕은수면(분)": round((row.get("duration_in_light") or 0) / 60, 1),
                    "REM수면(분)": round((row.get("duration_in_rem") or 0) / 60, 1),
                    "깊은수면(분)": round((row.get("duration_in_deep") or 0) / 60, 1),
                    "각성시간(분)": round((row.get("duration_awake") or 0) / 60, 1),
                    "상태설명": "수면종료요약"
                })
    except Exception:
        pass


def _normalize_paths(paths):
    """단일 경로 문자열이든 경로 목록이든 → 리스트로 통일."""
    if isinstance(paths, (str, bytes, os.PathLike)):
        return [os.fspath(paths)]
    return [os.fspath(p) for p in paths]


def _load_storage_file(jsonl_path):
    """파일 하나를 파싱해 storage 를 돌려준다. 파일마다 캐시 슬롯이 따로 있어서
    Emfit 로그에 새 줄이 붙어도 Radar 로그를 다시 읽지 않는다."""
    import time
    slot = _caches.setdefault(jsonl_path,
                              {"mtime": None, "storage": None, "offset": 0})

    current_mtime = os.path.getmtime(jsonl_path)
    current_size = os.path.getsize(jsonl_path)

    if slot["mtime"] == current_mtime and slot["storage"] is not None:
        return slot["storage"]

    can_increment = slot["storage"] is not None and current_size >= slot["offset"]
    if can_increment:
        storage = slot["storage"]
        start_offset = slot["offset"]
        mode = "증분"
    else:
        storage = {}
        start_offset = 0
        mode = "전체"

    t0 = time.time()
    name = os.path.basename(jsonl_path)
    print(f"[analyzer] {name} {mode} 파싱 시작: offset {start_offset} → {current_size} "
          f"({(current_size-start_offset)/1024/1024:.1f}MB)", flush=True)

    with open(jsonl_path, 'rb') as f:
        f.seek(start_offset)
        content = f.read()

    last_newline = content.rfind(b'\n')
    if last_newline == -1:
        slot["mtime"] = current_mtime
        slot["storage"] = storage
        return storage

    complete = content[:last_newline + 1]
    final_offset = start_offset + last_newline + 1

    lines_processed = 0
    for raw in complete.split(b'\n'):
        line = raw.decode('utf-8', errors='ignore').strip()
        if line:
            _process_line(line, storage)
            lines_processed += 1

    slot["mtime"] = current_mtime
    slot["storage"] = storage
    slot["offset"] = final_offset

    elapsed = time.time() - t0
    print(f"[analyzer] {name} {mode} 파싱 완료: +{lines_processed}줄, "
          f"누적 {len(storage)}개 조합, {elapsed:.1f}초", flush=True)
    return storage


def _cache_signature(paths):
    """현재 캐시 상태 지문 — 파일이 하나도 안 바뀌었으면 값이 같다."""
    return tuple((p, (_caches.get(p) or {}).get("mtime"))
                 for p in _normalize_paths(paths))


def _load_storage(paths):
    """Emfit·Radar 등 여러 로그 파일을 읽어 하나의 storage 로 합쳐 돌려준다.

    기기(SN)가 서로 달라서 (SN, 날짜) 키가 겹치지 않지만,
    혹시 겹쳐도 잃어버리지 않도록 리스트를 이어 붙인다."""
    import threading
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = threading.Lock()

    with _cache_lock:
        existing = [p for p in _normalize_paths(paths) if os.path.exists(p)]
        if not existing:
            return {}

        storages = [_load_storage_file(p) for p in existing]
        if len(storages) == 1:
            return storages[0]

        key = _cache_signature(existing)
        if _merged_cache["key"] == key and _merged_cache["storage"] is not None:
            return _merged_cache["storage"]

        merged = {}
        for st in storages:
            for k, records in st.items():
                if k in merged:
                    merged[k] = merged[k] + records
                else:
                    merged[k] = records
        _merged_cache["key"] = key
        _merged_cache["storage"] = merged
        return merged


def warmup(jsonl_path):
    """백그라운드에서 호출용. 캐시를 미리 채워둔다."""
    try:
        _load_storage(jsonl_path)
    except Exception as e:
        print(f"[analyzer] 워밍업 실패: {e}", flush=True)


def get_report_df(jsonl_path, date_str, device_sn, assignment_id=None):
    storage = _load_storage(jsonl_path)
    records = storage.get((device_sn, date_str), [])
    if assignment_id:
        records = [r for r in records if r.get("배정ID") == assignment_id]
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df = df.drop_duplicates(subset=["시간(KST)", "유형", "상태설명"], keep='first')

    # SleepDetail이 커버하는 분(HH:MM)에서는 Live 행을 버린다.
    # Emfit이 동일 시간대를 Live(실시간 raw)와 calc_data(후처리) 둘 다 보내므로.
    sleep_minutes = set(df.loc[df["유형"] == "SleepDetail", "시간(KST)"].str[:5])
    if sleep_minutes:
        drop_mask = (df["유형"] == "Live") & df["시간(KST)"].str[:5].isin(sleep_minutes)
        df = df[~drop_mask]

    df = df.sort_values(by="시간(KST)")

    # Summary(수면종료요약) 행을 맨 위로 올린다. 그날의 수면점수/총수면 등을 한눈에 보기 위함.
    summary_mask = df["유형"] == "Summary"
    if summary_mask.any():
        df = pd.concat([df[summary_mask], df[~summary_mask]], ignore_index=True)

    # 배정ID 는 내부 구분용 — CSV 에는 노출하지 않는다.
    if "배정ID" in df.columns:
        df = df.drop(columns=["배정ID"])

    return df


def list_available(jsonl_path):
    storage = _load_storage(jsonl_path)
    return sorted(storage.keys(), key=lambda k: (k[1], k[0]), reverse=True)


_avail_assign_cache = {"key": None, "result": None}


def list_available_assignments(jsonl_path):
    """데이터가 실제로 존재하는 (배정ID, 날짜) 조합 목록. 최신 날짜순.
    전체 레코드를 훑으므로 파일 mtime 기준 캐시 — 데이터가 바뀔 때만 재계산."""
    storage = _load_storage(jsonl_path)
    cache_key = _cache_signature(jsonl_path)
    if (_avail_assign_cache["key"] == cache_key
            and _avail_assign_cache["result"] is not None):
        return _avail_assign_cache["result"]
    combos = set()
    for records in storage.values():
        for r in records:
            aid = r.get("배정ID")
            if aid and r.get("날짜"):
                combos.add((aid, r.get("날짜")))
    result = sorted(combos, key=lambda k: k[1], reverse=True)
    _avail_assign_cache["key"] = cache_key
    _avail_assign_cache["result"] = result
    return result


_latest_states_cache = {"key": None, "result": None}

# BED/FALL 은 전송 주기가 달라서(BED 5~50초, FALL 1초) 한쪽만 보면 정보가 사라진다.
# 아래 시간(초) 안에 들어온 기록끼리는 "같은 지금"으로 보고 합친다.
# BED 가 자세 변화 없을 때 최대 50초 간격이므로 그보다 넉넉하게 잡는다.
_RADAR_MERGE_WINDOW_SEC = 120


def _record_dt(rec):
    """레코드의 날짜/시간 문자열 → datetime. 실패 시 None."""
    try:
        return datetime.strptime(f"{rec['날짜']} {rec['시간(KST)']}", "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _merge_radar_states(bed, fall):
    """같은 기기의 BED/FALL 최신 기록을 하나의 '현재 상태'로 합친다.

    BED  = 자세 7종(누움/앉음/배회/걸터앉음/낙상/자리비움/뒤척임) + 심박·호흡
    FALL = 자세 3종(재실/낙상/자리비움) + 감지인원, 1초마다

    합치는 규칙:
      - 시각은 둘 중 최신 기준 (카드의 '측정' 시각이 뒤처지지 않도록)
      - 자세는 BED 우선 (더 세밀함). BED 가 오래됐으면 FALL 로 대체
      - 낙상은 둘 중 하나라도 최근에 감지했으면 낙상 (안전 우선)
      - 심박·호흡은 BED 에만 있으므로 BED 에서 가져옴
    """
    if bed is None:
        return fall
    if fall is None:
        return bed

    bed_dt, fall_dt = _record_dt(bed), _record_dt(fall)
    if bed_dt is None or fall_dt is None:
        return bed if fall_dt is None else fall

    newest_dt = max(bed_dt, fall_dt)
    base = dict(bed if bed_dt >= fall_dt else fall)
    base["날짜"] = newest_dt.strftime("%Y-%m-%d")
    base["시간(KST)"] = newest_dt.strftime("%H:%M:%S")

    bed_fresh = (newest_dt - bed_dt).total_seconds() <= _RADAR_MERGE_WINDOW_SEC
    fall_fresh = (newest_dt - fall_dt).total_seconds() <= _RADAR_MERGE_WINDOW_SEC

    if bed_fresh:
        # 자세·생체는 BED 가 원본. FALL 이 최신이라 base 가 FALL 이어도 BED 값으로 채운다.
        base["자세"] = bed.get("자세")
        base["자세(POS)"] = bed.get("자세(POS)")
        base["심박수(HR)"] = bed.get("심박수(HR)")
        base["호흡수(RR)"] = bed.get("호흡수(RR)")
        base["Radar오류(ERR)"] = bed.get("Radar오류(ERR)")
    if fall_fresh:
        base["감지인원"] = fall.get("감지인원")

    # 낙상은 둘 중 하나라도 잡으면 낙상으로 본다 (놓치는 것보다 오탐이 낫다).
    # 낙상 플래그와 POS=4 를 둘 다 보는 이유: 한쪽만 채워져 들어와도 놓치지 않기 위해.
    def _is_fall(r):
        return bool(r.get("낙상")) or r.get("자세(POS)") == 4

    fall_detected = ((bed_fresh and _is_fall(bed))
                     or (fall_fresh and _is_fall(fall)))
    base["낙상"] = fall_detected
    if fall_detected:
        base["자세"] = "낙상"
        base["자세(POS)"] = 4

    base["Radar모델"] = "bed+fall"
    base["상태설명"] = f"AI Radar 통합 - {base.get('자세')}"
    return base


def get_latest_states(jsonl_path):
    """기기별 가장 최근 실시간 레코드를 반환. {device_sn: record}

    카드 대시보드가 15초마다 호출하므로, 데이터(파일 mtime)가 그대로면
    전체 storage 풀스캔을 건너뛰고 이전 결과를 그대로 돌려준다."""
    storage = _load_storage(jsonl_path)
    cache_key = _cache_signature(jsonl_path)
    if (_latest_states_cache["key"] == cache_key
            and _latest_states_cache["result"] is not None):
        return _latest_states_cache["result"]
    latest = {}
    radar_by_model = {}  # {sn: {"bed": 최신기록, "fall": 최신기록}}
    fsr_in_use = {}      # {sn: 사용/미사용이 확정된 최신 FSR 기록}
    # 최신 상태는 각 기기의 '최근 날짜'에만 있으므로, 전체(수십만 레코드)를 훑지 않고
    # 기기별 최근 며칠 버킷만 스캔한다. (몇 달치 과거는 최신 상태 계산에 무의미)
    # 오래 쉰 기기는 어차피 _device_status(마지막 통신)로 비활성 처리되므로 안전.
    dates_by_sn = {}
    for (sn, date) in storage.keys():
        dates_by_sn.setdefault(sn, []).append(date)
    RECENT_DAYS = 2
    for sn, dates in dates_by_sn.items():
        for date in sorted(dates, reverse=True)[:RECENT_DAYS]:
            for r in storage[(sn, date)]:
                dtype = r.get("유형")
                if dtype not in ("Live", "SleepDetail", "Radar", "McKare", "FSR"):
                    continue
                if dtype in ("Live", "SleepDetail") and r.get("심박수(HR)") is None:
                    continue
                if dtype == "Radar" and r.get("심박수(HR)") is None and r.get("자세(POS)") is None:
                    continue
                if dtype == "McKare" and r.get("심박수(HR)") is None and r.get("재실코드") is None:
                    continue
                # FSR 은 생체값이 없으므로 이벤트가 붙어 있으면 유효한 기록으로 본다.
                if dtype == "FSR" and not r.get("이벤트"):
                    continue
                this_key = (r["날짜"], r["시간(KST)"])
                if dtype == "FSR" and r.get("사용중") is not None:
                    # 생존신고(keep-alive)가 마지막이어도 '지금 사용 중인지'를 잃지 않도록
                    # 사용/미사용이 확정된 기록을 따로 기억해둔다.
                    cur_p = fsr_in_use.get(sn)
                    if cur_p is None or this_key >= (cur_p["날짜"], cur_p["시간(KST)"]):
                        fsr_in_use[sn] = r
                if dtype == "Radar":
                    # BED/FALL 을 따로 모아두고 아래에서 합친다.
                    # (안 그러면 1초마다 오는 FALL 이 BED 의 심박·호흡을 영영 덮어씀)
                    slot = radar_by_model.setdefault(sn, {})
                    model = r.get("Radar모델") or "bed"
                    cur_m = slot.get(model)
                    # '>=' 로 비교: 시각이 초 단위라 같은 초에 여러 건이 들어올 수 있는데,
                    # 그때는 저장 리스트가 도착순이므로 '나중에 온 것'을 최신으로 택한다.
                    # ('>' 였을 땐 같은 초의 맨 처음 건을 붙들어 자세가 갱신되지 않았음)
                    if cur_m is None or this_key >= (cur_m["날짜"], cur_m["시간(KST)"]):
                        slot[model] = r
                cur = latest.get(sn)
                cur_key = (cur["날짜"], cur["시간(KST)"]) if cur else ("", "")
                if this_key >= cur_key:
                    latest[sn] = r
    for sn, slot in radar_by_model.items():
        merged = _merge_radar_states(slot.get("bed"), slot.get("fall"))
        if merged is not None:
            latest[sn] = merged
    for sn, rec in fsr_in_use.items():
        cur = latest.get(sn)
        # 마지막 기록이 사용 여부를 모르는 이벤트(생존신고 등)면 직전 확정값을 채워 넣는다.
        if cur is not None and cur.get("유형") == "FSR" and cur.get("사용중") is None:
            merged = dict(cur)
            merged["사용중"] = rec.get("사용중")
            merged["사용판정시각"] = f"{rec['날짜']} {rec['시간(KST)']}"
            latest[sn] = merged
    _latest_states_cache["key"] = cache_key
    _latest_states_cache["result"] = latest
    return latest


def start_analysis(target_date=None, target_device=None):
    print("--- 분석 프로그램을 시작합니다 ---")
    
    jsonl_files = glob.glob("*.jsonl")
    if not jsonl_files:
        print("❌ 오류: .jsonl 파일이 현재 폴더에 없습니다.")
        return

    latest_file = max(jsonl_files, key=os.path.getmtime)
    print(f"📂 읽는 중: {latest_file}")

    storage = {}
    line_count = 0
    extracted_types = {"Live": 0, "HRV": 0, "SleepDetail": 0, "Summary": 0, "Radar": 0}

    with open(latest_file, 'r', encoding='utf-8') as f:
        for line in f:
            line_count += 1
            line = line.strip()
            if not line: continue
            
            try:
                row = json.loads(line)
                radar = parse_radar_payload(row)
                if radar is not None:
                    _store_radar_record(storage, radar)
                    extracted_types["Radar"] += 1
                    continue
                sn = row.get("device")
                
                # 1. 실시간 데이터 (data)
                if "data" in row:
                    for item in row.get("data", []):
                        item_sn = item.get("device", sn)
                        add_to_storage(storage, item_sn, item.get("date_occurred"), "Live", {
                            "심박수(HR)": item.get("heart_rate"),
                            "호흡수(RR)": item.get("respiration_rate"),
                            "활동량(ACT)": item.get("activity"),
                            "상태설명": "실시간측정"
                        })
                        extracted_types["Live"] += 1
                
                # 2. HRV 데이터
                if "hrv_data" in row:
                    for hrv in row.get("hrv_data", []):
                        item_sn = hrv.get("device", sn)
                        add_to_storage(storage, item_sn, hrv.get("date_occurred"), "HRV", {
                            "심박변이도(RMSSD)": hrv.get("rmssd"),
                            "상태설명": "HRV측정"
                        })
                        extracted_types["HRV"] += 1

                # 3. 수면 데이터 (SleepDetail & Summary)
                # sleep_score가 있거나 calc_data가 있는 경우 모두 체크
                if any(k in row for k in ["sleep_score", "calc_data", "duration_in_sleep"]):
                    
                    # (1) 상세 그래프 데이터
                    calc_list = row.get("calc_data", [])
                    if isinstance(calc_list, str):
                        try: calc_list = json.loads(calc_list)
                        except: calc_list = []

                    for entry in calc_list:
                        if len(entry) >= 4:
                            add_to_storage(storage, sn, entry[0], "SleepDetail", {
                                "심박수(HR)": entry[1], "호흡수(RR)": entry[2],
                                "활동량(ACT)": entry[3], "상태설명": "수면상세기록"
                            })
                            extracted_types["SleepDetail"] += 1

                    # (2) 수면 요약 데이터
                    ts_end = row.get("to") or row.get("date_occurred")
                    if ts_end and "sleep_score" in row:
                        add_to_storage(storage, sn, ts_end, "Summary", {
                            "수면점수": row.get("sleep_score"),
                            "총수면(분)": round(row.get("duration_in_sleep", 0)/60, 1),
                            "실제수면(분)": round(row.get("duration_actual_sleep", 0) / 60, 1),
                            "REM수면(분)": round(row.get("duration_rem_sleep", 0) / 60, 1),
                            "깊은수면(분)": round(row.get("duration_deep_sleep", 0) / 60, 1),
                            "상태설명": "수면종료요약"
                        })
                        extracted_types["Summary"] += 1

            except Exception as e:
                print(f"⚠️ {line_count}행 처리 중 스킵: {e}")
                continue

    print(f"\n📊 데이터 추출 요약:")
    for k, v in extracted_types.items():
        print(f" - {k}: {v}건")

    if not storage:
        print("❌ 추출된 데이터가 하나도 없습니다. JSON 구조를 확인하세요.")
        return

    saved_count = 0
    for (sn, date_str), records in storage.items():
        if target_date and date_str != target_date: continue
        if target_device and sn != target_device: continue

        df = pd.DataFrame(records)
        df = df.drop_duplicates(subset=["시간(KST)", "유형", "상태설명"], keep='first')
        df = df.sort_values(by="시간(KST)")
        df = df.drop(columns=["배정ID"], errors="ignore")
        
        info = DEVICE_INFO.get(sn, {"name": sn, "location": "Unknown"})
        file_name = f"{date_str}_{info['location']}_{info['name']}_리포트.csv"
        
        df.to_csv(file_name, index=False, encoding='utf-8-sig')
        print(f"✅ {file_name} 저장 완료 ({len(df)}건)")
        saved_count += 1

    if saved_count == 0:
        print("⚠️ 필터 조건(날짜/기기)에 맞는 데이터가 없어 파일이 생성되지 않았습니다.")
    
    print(f"\n--- 분석 종료 (총 {line_count}줄 처리됨) ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="조회할 날짜 (YYYY-MM-DD)")
    parser.add_argument("--device", type=str, help="기기 SN")
    
    args = parser.parse_args()
    start_analysis(target_date=args.date, target_device=args.device)