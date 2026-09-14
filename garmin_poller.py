"""Garmin 워치 수집기 — 클라우드에서 당겨와 서버의 /garmin 으로 넣는다.

다른 센서는 기기가 서버로 보내지만 워치는 그게 안 된다(워치 → 폰 → Garmin 클라우드).
그래서 이 프로세스가 주기적으로 당겨와 localhost 의 POST /garmin 으로 넣어주고,
그 뒤부터는 다른 센서와 똑같은 길(로그 적재 → analyzer)을 탄다.

실행 (한 번 돌고 끝남 — 주기 실행은 systemd timer 로):
    python3 garmin_poller.py --all-accounts
    python3 garmin_poller.py --account example-account-04 --days 2 --dry-run

설치:
    pip install "garminconnect==0.2.38" "garth>=0.5.17,<0.6.0"

토큰:
    기본 위치는 홈 폴더의 ~/.garmin_example-account-* (젯슨: /home/operator/...).
    각 폴더에 oauth1_token.json, oauth2_token.json 이 있어야 한다. chmod 600 권장.

⚠️ 알아둘 것 세 가지
 1. **증분 전송**: Garmin 은 같은 날짜를 물어볼 때마다 그날 전체를 다시 준다. 그대로
    보내면 심박이 중복 적재되므로, 계정별 '마지막으로 보낸 심박 시각'을 상태 파일에
    기록해 그보다 새로운 것만 보낸다. 일별 요약은 값이 바뀌었을 때만 보낸다.
    (서버 쪽 analyzer 에도 중복 방어가 한 겹 더 있다 — 상태 파일이 날아갔을 때 대비)
 2. **인증 실패는 따로 보고한다**: 토큰이 죽은 것과 '어르신이 워치를 안 찼다'는 완전히
    다른 문제인데, 둘 다 데이터가 없다는 점은 같다. 구분하지 않으면 토큰 만료가
    조용히 묻힌다. 그래서 인증 실패 시 auth_error 를 실어 보내 화면·알림이 구분하게 한다.
 3. **토큰 저장은 기본 꺼져 있다**: garth 0.5.x 는 갱신된 oauth2 를 자동 저장하지 않아
    매 실행마다 oauth1 으로 다시 교환한다. --save-tokens 를 주면 저장하는데, 이 dump 는
    oauth1 파일까지 덮어쓰므로 **토큰 폴더를 백업한 뒤에** 켜는 것을 권한다.
    (oauth1 은 재발급이 불가능한 유일한 자산이다)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from garminconnect import Garmin  # noqa: E402

TOKEN_DIR_PREFIX = ".garmin_"
TOKEN_GLOB = ".garmin_example-account-*"
REQUIRED_TOKEN_FILES = ("oauth1_token.json", "oauth2_token.json")

STATE_FILE = Path(__file__).resolve().parent / "garmin_poll_state.json"
DEFAULT_SERVER = "http://127.0.0.1:8080"

# 레이트리밋 방어 — 이관받은 garmin 프로젝트가 쓰던 값 그대로.
# 동시에 여러 계정이 TLS 핸드셰이크를 걸면 서버가 끊는다(SSLZeroReturnError).
SLEEP_BETWEEN_ENDPOINTS = 0.5
SLEEP_BETWEEN_DATES = 2.0
SLEEP_BETWEEN_ACCOUNTS = 0.5

# 상태 파일에 날짜별 서명을 무한정 쌓지 않도록 정리하는 기준.
STATE_KEEP_DAYS = 14

# user_summary 에서 가져갈 필드. 96개 중 쓰는 것만 골라 보낸다 —
# 전부 보내면 로그가 몇 배로 커지고, 정작 파서는 아래 값만 쓴다.
# (한국어 컬럼 매핑은 서버의 garmin_parser 가 한다. 여기서는 원본 이름 그대로 전달)
SUMMARY_FIELDS = (
    "calendarDate", "restingHeartRate", "minHeartRate", "maxHeartRate",
    "avgWakingRespirationValue", "averageSpo2",
    "totalSteps", "totalPushes", "totalDistanceMeters", "dailyStepGoal",
    "sedentarySeconds", "sleepingSeconds", "activeSeconds", "highlyActiveSeconds",
    "moderateIntensityMinutes", "vigorousIntensityMinutes",
    "activeKilocalories", "totalKilocalories",
    "averageStressLevel", "maxStressLevel",
    "bodyBatteryMostRecentValue", "bodyBatteryHighestValue", "bodyBatteryLowestValue",
    "wellnessStartTimeGmt", "wellnessEndTimeGmt",
)

SLEEP_FIELDS = (
    "calendarDate", "sleepTimeSeconds", "deepSleepSeconds", "remSleepSeconds",
    "lightSleepSeconds", "awakeSleepSeconds", "sleepScores",
    "averageRespirationValue", "averageSpO2Value", "avgSleepStress",
)


# ── 계정·상태 ─────────────────────────────────────────────────────────

def account_label(token_dir: Path) -> str:
    name = token_dir.name
    return name[len(TOKEN_DIR_PREFIX):] if name.startswith(TOKEN_DIR_PREFIX) else name


def discover_accounts(token_root: Path) -> list[Path]:
    if not token_root.is_dir():
        return []
    return [d.resolve() for d in sorted(token_root.glob(TOKEN_GLOB))
            if d.is_dir() and all((d / f).is_file() for f in REQUIRED_TOKEN_FILES)]


def token_dir_for(token_root: Path, account: str) -> Path:
    p = Path(account)
    if p.is_dir():
        return p.resolve()
    name = account
    if not name.startswith(TOKEN_DIR_PREFIX):
        if not name.startswith("operator"):
            name = f"operator{name}"
        name = f"{TOKEN_DIR_PREFIX}{name}"
    return (token_root / name).resolve()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        # 상태 파일이 깨져도 수집은 계속한다. 중복이 좀 생길 뿐이고,
        # 서버 쪽 analyzer 가 한 번 더 걸러준다.
        print(f"[garmin] 상태 파일 읽기 실패, 처음부터 수집: {e}", flush=True)
        return {}


def save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)   # 쓰다 만 파일이 남지 않게 원자적으로 교체
    except Exception as e:  # noqa: BLE001
        print(f"[garmin] 상태 파일 저장 실패: {e}", flush=True)


def prune_state(acct_state: dict, keep_dates: set) -> None:
    sigs = acct_state.get("daily_sig") or {}
    for d in list(sigs):
        if d not in keep_dates:
            del sigs[d]


# ── 조회 ──────────────────────────────────────────────────────────────

def pick(src, fields) -> dict:
    if not isinstance(src, dict):
        return {}
    return {k: src[k] for k in fields if k in src}


def safe_call(name: str, fn, label: str):
    """엔드포인트 하나가 실패해도 나머지는 계속 — 부분 수집이 무수집보다 낫다."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"    [{label}] {name} 실패: {str(e)[:100]}", flush=True)
        return None


def fetch_date(api, label: str, date_str: str) -> dict:
    """하루치 — 요약 · 심박 · 수면 세 가지만 부른다.

    30분마다 15개 엔드포인트를 다 부르면 레이트리밋에 걸린다. 대시보드 카드와
    리포트에 실제로 쓰는 것만 받는다."""
    summary = safe_call("user_summary", lambda: api.get_user_summary(date_str), label)
    time.sleep(SLEEP_BETWEEN_ENDPOINTS)
    hr_raw = safe_call("heart_rates", lambda: api.get_heart_rates(date_str), label)
    time.sleep(SLEEP_BETWEEN_ENDPOINTS)
    sleep_raw = safe_call("sleep_data", lambda: api.get_sleep_data(date_str), label)

    sleep_dto = (sleep_raw or {}).get("dailySleepDTO") if isinstance(sleep_raw, dict) else None
    return {
        "summary": pick(summary, SUMMARY_FIELDS),
        "sleep": pick(sleep_dto, SLEEP_FIELDS),
        "hr_values": (hr_raw or {}).get("heartRateValues") if isinstance(hr_raw, dict) else None,
        "last_sync_gmt": (summary or {}).get("wellnessEndTimeGmt") if isinstance(summary, dict) else None,
    }


def build_payload(label: str, date_str: str, fetched: dict,
                  last_hr_ts: float, prev_sig: str | None) -> tuple[dict | None, float, str | None]:
    """서버로 보낼 payload 를 만든다. 보낼 게 없으면 (None, ...) 을 돌려준다.

    반환: (payload, 새 last_hr_ts, 새 서명)
    """
    # 심박 — 마지막으로 보낸 시각 이후만. Garmin 은 측정이 없는 구간을 null 로 채워 준다.
    new_hr = []
    max_ts = last_hr_ts
    for item in fetched.get("hr_values") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        ts, bpm = item[0], item[1]
        if ts is None or bpm is None:
            continue
        if ts <= last_hr_ts:
            continue
        new_hr.append([ts, bpm])
        max_ts = max(max_ts, ts)

    # 일별 요약 — 내용이 바뀌었을 때만 보낸다. 동기화가 없으면 값이 그대로라
    # 매 폴링마다 보내면 같은 줄이 하루 48개씩 쌓인다.
    summary, sleep = fetched.get("summary") or {}, fetched.get("sleep") or {}
    sig = None
    if summary or sleep:
        sig = json.dumps({"s": summary, "z": sleep}, sort_keys=True, ensure_ascii=False)
    send_daily = bool(sig) and sig != prev_sig

    if not new_hr and not send_daily:
        return None, max_ts, prev_sig

    payload = {
        "data_source": "garmin",
        "account": label,
        "sn": f"garmin-{label}",
        "date": date_str,
        "last_sync_gmt": fetched.get("last_sync_gmt"),
    }
    if new_hr:
        payload["hr"] = new_hr
    if send_daily:
        payload["summary"] = summary
        payload["sleep"] = sleep
    return payload, max_ts, (sig if send_daily else prev_sig)


# ── 전송 ──────────────────────────────────────────────────────────────

def post_payload(server: str, payload: dict, dry_run: bool) -> bool:
    if dry_run:
        hr_n = len(payload.get("hr") or [])
        has_daily = "summary" in payload
        print(f"    [dry-run] {payload['sn']} {payload['date']} "
              f"심박 {hr_n}건 · 일별요약 {'있음' if has_daily else '없음'}", flush=True)
        return True

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{server.rstrip('/')}/garmin", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return 200 <= res.status < 300
    except urllib.error.HTTPError as e:
        # 503 = 서버가 아직 로그 파싱(워밍업) 중. 수신 경로는 열려 있으므로 보통
        # 여기까지 오지 않지만, 왔다면 다음 주기에 다시 보낸다 (상태를 갱신하지 않으므로).
        print(f"    전송 실패 HTTP {e.code}: {str(e.reason)[:80]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"    전송 실패: {str(e)[:120]}", flush=True)
    return False


def report_auth_error(server: str, label: str, message: str, dry_run: bool) -> None:
    """인증 실패를 서버에 알린다 — '데이터 없음'과 구분되게."""
    payload = {
        "data_source": "garmin",
        "account": label,
        "sn": f"garmin-{label}",
        "date": date.today().isoformat(),
        "auth_error": message[:200],
    }
    post_payload(server, payload, dry_run)


# ── 계정 처리 ─────────────────────────────────────────────────────────

def poll_account(token_dir: Path, dates: list[str], server: str,
                 state: dict, dry_run: bool, save_tokens: bool) -> bool:
    label = account_label(token_dir)
    print(f"  [{label}]", flush=True)

    try:
        api = Garmin()
        api.login(str(token_dir))
    except Exception as e:  # noqa: BLE001
        # 토큰이 죽었다 — 사람이 개입해야 하는 유일한 경우라 서버에 알린다.
        msg = str(e)
        print(f"    로그인 실패: {msg[:150]}", flush=True)
        report_auth_error(server, label, msg, dry_run)
        return False

    acct = state.setdefault(label, {})
    last_hr_ts = float(acct.get("last_hr_ts") or 0)
    sigs = acct.setdefault("daily_sig", {})

    sent = 0
    for i, date_str in enumerate(dates):
        fetched = fetch_date(api, label, date_str)
        payload, new_ts, new_sig = build_payload(
            label, date_str, fetched, last_hr_ts, sigs.get(date_str)
        )
        if payload is not None:
            if post_payload(server, payload, dry_run):
                sent += 1
                # 전송에 성공했을 때만 상태를 갱신한다 — 실패한 구간은 다음 주기에 다시 간다.
                last_hr_ts = new_ts
                if new_sig is not None:
                    sigs[date_str] = new_sig
        else:
            print(f"    {date_str}: 새 데이터 없음", flush=True)

        if i < len(dates) - 1:
            time.sleep(SLEEP_BETWEEN_DATES)

    acct["last_hr_ts"] = last_hr_ts
    acct["last_poll"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prune_state(acct, set(dates) | set(list(sigs)[-STATE_KEEP_DAYS:]))

    if save_tokens and not dry_run:
        # garth 0.5.x 는 갱신된 oauth2 를 자동 저장하지 않는다.
        # ⚠️ 이 dump 는 oauth1 파일까지 덮어쓴다 (백업 전제).
        try:
            api.garth.dump(str(token_dir))
        except Exception as e:  # noqa: BLE001
            print(f"    토큰 저장 실패: {str(e)[:100]}", flush=True)

    print(f"    전송 {sent}건", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Garmin 워치 수집기")
    ap.add_argument("--account", help="계정 (예: example-account-04 / 0004 / 토큰폴더 경로)")
    ap.add_argument("--all-accounts", action="store_true")
    ap.add_argument("--token-root", type=Path, default=Path(os.path.expanduser("~")),
                    help="토큰 폴더 위치 (기본: 홈 디렉터리)")
    ap.add_argument("--days", type=int, default=2,
                    help="오늘을 포함한 최근 N일 (기본 2 — 어제 것이 늦게 동기화되는 일이 잦다)")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--dry-run", action="store_true", help="전송하지 않고 요약만 출력")
    ap.add_argument("--save-tokens", action="store_true",
                    help="갱신된 토큰 저장 (oauth1 까지 덮어씀 — 백업 후 사용)")
    args = ap.parse_args()

    if not args.account and not args.all_accounts:
        ap.error("--account 또는 --all-accounts 중 하나가 필요합니다")

    today = date.today()
    dates = [(today - timedelta(days=i)).isoformat()
             for i in range(max(1, args.days) - 1, -1, -1)]

    if args.all_accounts:
        token_dirs = discover_accounts(args.token_root)
        if not token_dirs:
            print(f"[garmin] 토큰 폴더 없음: {args.token_root}/{TOKEN_GLOB}", flush=True)
            return 1
    else:
        td = token_dir_for(args.token_root, args.account)
        if not all((td / f).is_file() for f in REQUIRED_TOKEN_FILES):
            print(f"[garmin] 토큰 파일 없음: {td}", flush=True)
            return 1
        token_dirs = [td]

    print(f"[garmin] 계정 {len(token_dirs)}개 · {dates[0]}~{dates[-1]} · 서버 {args.server}"
          f"{' (dry-run)' if args.dry_run else ''}", flush=True)
    if not args.save_tokens and not args.dry_run:
        print("[garmin] 토큰 저장 꺼짐 — 매 실행마다 oauth1 으로 재교환합니다. "
              "토큰 폴더를 백업했다면 --save-tokens 를 권장합니다.", flush=True)

    state = load_state()
    ok = 0
    for i, token_dir in enumerate(token_dirs):
        if i > 0:
            time.sleep(SLEEP_BETWEEN_ACCOUNTS)
        if poll_account(token_dir, dates, args.server, state, args.dry_run, args.save_tokens):
            ok += 1

    if not args.dry_run:
        save_state(state)

    print(f"[garmin] 완료 — 성공 {ok}/{len(token_dirs)}계정", flush=True)
    return 0 if ok == len(token_dirs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
