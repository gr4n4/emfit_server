import pandas as pd
import glob
import os
import json
import argparse
from datetime import datetime

# [설정] 기기 정보 — device_info.json 파일에 저장. 대시보드 /devices 에서 편집 가능.
import threading as _threading

_DEVICE_INFO_FILE = "device_info.json"
_DEVICE_INFO_LOCK = _threading.Lock()

_DEFAULT_DEVICE_INFO = {
    "EMFIT-DEMO-01": {"name": "돌봄A", "location": "-", "group": "일반"},
    "EMFIT-DEMO-02": {"name": "돌봄B", "location": "테스트 공간", "group": "일반"},
    "EMFIT-DEMO-03": {"name": "사용자-C", "location": "A시설", "group": "일반"},
    "EMFIT-DEMO-04": {"name": "사용자-D", "location": "사용자-D님 가정", "group": "뇌성마비"},
    "EMFIT-DEMO-05": {"name": "사용자E", "location": "301호", "group": "일반"}
}

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
    """배정 start/end 문자열 → naive Timestamp(KST 기준). None/'' → None."""
    if not s:
        return None
    try:
        return pd.Timestamp(str(s).replace("T", " "))
    except Exception:
        return None


def _to_naive_kst(dt):
    """측정 시각(tz 유무 무관)을 naive KST Timestamp 로 정규화."""
    ts = pd.Timestamp(dt)
    if ts.tzinfo is not None:
        ts = ts.tz_convert('Asia/Seoul').tz_localize(None)
    return ts


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
    new_info = {}
    for a in ASSIGNMENTS:
        sn = a.get("sn")
        if sn and not a.get("end"):
            new_info[sn] = {"name": a.get("user", sn),
                            "location": a.get("location", "-"),
                            "group": a.get("group", "일반")}
    for a in ASSIGNMENTS:  # 활성 배정이 없는 기기는 마지막 배정으로
        sn = a.get("sn")
        if sn and sn not in new_info:
            new_info[sn] = {"name": a.get("user", sn),
                            "location": a.get("location", "-"),
                            "group": a.get("group", "일반")}
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
        ASSIGNMENTS.append({
            "id": new_id, "sn": sn, "user": user,
            "location": location, "group": group,
            "start": when_norm, "end": None,
        })
        _save_assignments()
        _rebuild_assign_index()
    _rebuild_device_info()
    return new_id


def update_active_assignments(updates):
    """{sn: {name, location, group}} — 각 기기의 활성 배정 필드를 수정(오타 수정 등)."""
    with _ASSIGNMENTS_LOCK:
        for sn, info in updates.items():
            active = [a for a in ASSIGNMENTS if a.get("sn") == sn and not a.get("end")]
            if active:
                target = active[-1]
            else:
                cnt = sum(1 for a in ASSIGNMENTS if a.get("sn") == sn)
                target = {"id": f"{sn}-{cnt + 1}", "sn": sn,
                          "start": None, "end": None}
                ASSIGNMENTS.append(target)
            target["user"] = info.get("name", sn)
            target["location"] = info.get("location", "-")
            target["group"] = info.get("group", "일반")
        _save_assignments()
        _rebuild_assign_index()
    _rebuild_device_info()


def invalidate_cache():
    """파싱 캐시 무효화 — 배정 이력이 바뀌면 다음 조회 때 전체 재파싱하여
    과거 데이터까지 올바른 사용자로 다시 라벨링한다."""
    _cache["mtime"] = None
    _cache["path"] = None
    _cache["storage"] = None
    _cache["offset"] = 0


# ── 모듈 초기화: 배정 이력 로드 후 DEVICE_INFO 동기화 ──
ASSIGNMENTS = _load_assignments()
_rebuild_assign_index()
_rebuild_device_info()


def add_to_storage(storage, sn, ts, dtype, extra):
    if not ts: return
    try:
        # 타임스탬프 변환 (초 단위 기준, 실패 시 밀리초 시도)
        try:
            dt = pd.to_datetime(float(ts), unit='s').tz_localize('UTC').tz_convert('Asia/Seoul')
        except (ValueError, str):
            dt = pd.to_datetime(float(ts), unit='ms').tz_localize('UTC').tz_convert('Asia/Seoul')
        
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
        # None 값인 컬럼들도 구조 유지를 위해 기본값 채움
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

_cache = {"mtime": None, "path": None, "storage": None, "offset": 0}
_cache_lock = None  # lazy init to avoid import-time threading

# 장비별 최신 하트비트: {sn: {"connected": bool, "status_code": int, "last_seen_ts": int,
#                           "status_since_ts": int, "server_received_at": str}}
_device_status = {}


def get_device_statuses():
    return dict(_device_status)


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


def _load_storage(jsonl_path):
    import time, threading
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = threading.Lock()

    with _cache_lock:
        current_mtime = os.path.getmtime(jsonl_path)
        current_size = os.path.getsize(jsonl_path)

        if _cache["path"] == jsonl_path and _cache["mtime"] == current_mtime:
            return _cache["storage"]

        same_file = _cache["path"] == jsonl_path
        can_increment = (
            same_file
            and _cache["storage"] is not None
            and current_size >= _cache["offset"]
        )

        if can_increment:
            storage = _cache["storage"]
            start_offset = _cache["offset"]
            mode = "증분"
        else:
            storage = {}
            start_offset = 0
            mode = "전체"

        t0 = time.time()
        print(f"[analyzer] {mode} 파싱 시작: offset {start_offset} → {current_size} ({(current_size-start_offset)/1024/1024:.1f}MB)", flush=True)

        with open(jsonl_path, 'rb') as f:
            f.seek(start_offset)
            content = f.read()

        last_newline = content.rfind(b'\n')
        if last_newline == -1:
            _cache["mtime"] = current_mtime
            _cache["path"] = jsonl_path
            _cache["storage"] = storage
            return storage

        complete = content[:last_newline + 1]
        final_offset = start_offset + last_newline + 1

        lines_processed = 0
        for raw in complete.split(b'\n'):
            line = raw.decode('utf-8', errors='ignore').strip()
            if line:
                _process_line(line, storage)
                lines_processed += 1

        _cache["mtime"] = current_mtime
        _cache["path"] = jsonl_path
        _cache["storage"] = storage
        _cache["offset"] = final_offset

        elapsed = time.time() - t0
        print(f"[analyzer] {mode} 파싱 완료: +{lines_processed}줄, 누적 {len(storage)}개 조합, {elapsed:.1f}초", flush=True)
        return storage


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


def list_available_assignments(jsonl_path):
    """데이터가 실제로 존재하는 (배정ID, 날짜) 조합 목록. 최신 날짜순."""
    storage = _load_storage(jsonl_path)
    combos = set()
    for records in storage.values():
        for r in records:
            aid = r.get("배정ID")
            if aid and r.get("날짜"):
                combos.add((aid, r.get("날짜")))
    return sorted(combos, key=lambda k: k[1], reverse=True)


def get_latest_states(jsonl_path):
    """기기별 가장 최근 HR 포함 레코드를 반환. {device_sn: record}"""
    storage = _load_storage(jsonl_path)
    latest = {}
    for (sn, _date), records in storage.items():
        for r in records:
            if r.get("유형") not in ("Live", "SleepDetail"):
                continue
            if r.get("심박수(HR)") is None:
                continue
            cur = latest.get(sn)
            this_key = (r["날짜"], r["시간(KST)"])
            cur_key = (cur["날짜"], cur["시간(KST)"]) if cur else ("", "")
            if this_key > cur_key:
                latest[sn] = r
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
    extracted_types = {"Live": 0, "HRV": 0, "SleepDetail": 0, "Summary": 0}

    with open(latest_file, 'r', encoding='utf-8') as f:
        for line in f:
            line_count += 1
            line = line.strip()
            if not line: continue
            
            try:
                row = json.loads(line)
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