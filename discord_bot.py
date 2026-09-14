"""디스코드 봇 — 슬래시 명령어(/현황, /상세보기, /배터리)로 돌봄기기 현황을 답해준다.

app.py(대시보드)와는 별도 프로세스로 돈다. 기기 데이터를 직접 읽지 않고
app.py 가 이미 메모리에 들고 있는 상태를 /internal/discord/status(localhost 전용)로
불러다 쓴다 — 봇 하나 띄우자고 500MB+ 로그 파일을 또 파싱하지 않기 위함.

/admin/discord 에 등록해둔 시설별 채널(예: A시설, B시설) 이름으로
/a시설 같은 명령어도 봇 시작 시 자동으로 만들어진다 — 그 시설 기기만 걸러서 보여줌.
새 시설을 추가했으면 봇을 한 번 재시작해야 명령어가 새로 생긴다.

실행: venv/bin/python discord_bot.py  (systemd 서비스로 상시 실행 권장)
토큰: discord_bot_token.txt 파일에서 읽는다 (git에 올리지 않음).
"""
import asyncio
import os
import re

import discord
import requests
from discord import app_commands

_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_HERE, "discord_bot_token.txt")
STATUS_API = "http://127.0.0.1:8080/internal/discord/status"


def _load_token():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise RuntimeError(f"{TOKEN_FILE} 이 비어있습니다 — 봇 토큰을 채워주세요.")
    return token


def _fetch_status():
    resp = requests.get(STATUS_API, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _dot(connected):
    return {True: "🟢", False: "🔴"}.get(connected, "⚪")


def _format_device_line(d):
    """예: 🟢 김돌봄(우리집, EMFIT QS) - 방금 — 위치·기기종류를 괄호 안에 같이 보여준다."""
    tag_parts = [p for p in (d.get("location"), d.get("kind_label")) if p]
    tag = f"({', '.join(tag_parts)})" if tag_parts else ""
    return f"{_dot(d['connected'])} {d['name']}{tag} - {d['last_seen_text']}"


client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)


@tree.command(name="현황", description="돌봄기기 연결 현황 요약")
async def status_command(interaction: discord.Interaction):
    try:
        data = _fetch_status()
    except Exception as e:
        await interaction.response.send_message(f"⚠️ 대시보드 서버에 연결하지 못했습니다: {e}", ephemeral=True)
        return

    msg = (
        f"📡 **돌봄기기 현황** (끊김 기준: {data['threshold_minutes']}분)\n"
        f"전체 {data['total']}대 · 🟢 연결 {data['connected']} · 🔴 끊김 {data['disconnected']}"
    )
    if data["unknown"]:
        msg += f" · ⚪ 이력없음 {data['unknown']}"

    offline = [d for d in data["devices"] if d["connected"] is False]
    if offline:
        msg += "\n\n" + "\n".join(_format_device_line(d) for d in offline)
    await interaction.response.send_message(msg)


def _battery_icon(pct, threshold):
    """잔량 표시. 기준 이하면 눈에 띄게 — 화면의 작은 글씨로는 놓치기 쉬워서
    이 명령어를 만든 것이므로, 여기서는 교체가 필요한 것부터 보이게 한다."""
    if pct <= 5:
        return "🔴"
    if pct <= threshold:
        return "🪫"
    if pct <= threshold + 15:
        return "🔋"
    return "🟢"


@tree.command(name="배터리", description="사용감지 센서 배터리 잔량")
async def battery_command(interaction: discord.Interaction):
    try:
        data = _fetch_status()
    except Exception as e:
        await interaction.response.send_message(f"⚠️ 대시보드 서버에 연결하지 못했습니다: {e}", ephemeral=True)
        return

    threshold = data.get("battery_threshold_pct") or 20
    devices = [d for d in data["devices"] if isinstance(d.get("battery_pct"), int)]
    if not devices:
        await interaction.response.send_message(
            "배터리를 보고하는 기기가 없습니다.\n"
            "(사용감지 센서만 배터리를 보내며, 값은 기기가 신호를 보낼 때 갱신됩니다)"
        )
        return

    # 잔량이 적은 것부터 — 교체해야 할 것이 맨 위에 오도록.
    devices.sort(key=lambda d: d["battery_pct"])
    low = [d for d in devices if d["battery_pct"] <= threshold]
    # 지금은 사용감지 센서만 배터리를 보내므로 종류를 줄마다 붙이면 같은 글자만 반복된다.
    # 나중에 배터리를 보내는 다른 종류가 생기면 그때만 표시한다.
    show_kind = len({d.get("kind") for d in devices}) > 1

    lines = []
    for d in devices:
        pct = d["battery_pct"]
        tag_parts = [d.get("location")]
        if show_kind:
            tag_parts.append(d.get("kind_label"))
        tag_parts = [p for p in tag_parts if p]
        tag = f"({', '.join(tag_parts)})" if tag_parts else ""
        # ⚠️ 사용감지 센서는 신호를 보낼 때만 배터리가 갱신된다. 며칠 전 값을 지금 값처럼
        # 보여주면 '아직 여유 있다'고 잘못 판단하게 되므로, 오래된 값은 언제 기준인지 밝힌다.
        age = d.get("age_sec")
        stale = f"  ·  {d.get('last_seen_text')} 기준" if isinstance(age, (int, float)) and age > 86400 else ""
        lines.append(f"{_battery_icon(pct, threshold)} {d['name']}{tag} - **{pct}%**{stale}")

    msg = f"🔋 **배터리 잔량** (교체 기준: {threshold}% 이하)\n"
    msg += f"전체 {len(devices)}대"
    if low:
        msg += f" · 🪫 교체 필요 **{len(low)}대**"
    msg += "\n\n" + "\n".join(lines)
    if len(msg) > 1900:  # 디스코드 메시지 2000자 제한
        msg = msg[:1900] + "\n… (기기가 많아 일부 생략)"
    await interaction.response.send_message(msg)


@tree.command(name="상세보기", description="돌봄기기 전체 목록과 상태")
async def detail_command(interaction: discord.Interaction):
    try:
        data = _fetch_status()
    except Exception as e:
        await interaction.response.send_message(f"⚠️ 대시보드 서버에 연결하지 못했습니다: {e}", ephemeral=True)
        return

    lines = [_format_device_line(d) for d in data["devices"]]
    body = "\n".join(lines) if lines else "등록된 기기가 없습니다."
    if len(body) > 1900:  # 디스코드 메시지 2000자 제한
        body = body[:1900] + "\n… (기기가 많아 일부 생략)"
    await interaction.response.send_message(f"📋 **전체 기기 목록** ({data['total']}대)\n\n{body}")


# 디스코드 슬래시 명령어 이름 규칙: 소문자·숫자·유니코드 문자·하이픈·밑줄만, 1~32자, 공백 불가.
# 한글은 대소문자 구분이 없어 그대로 써도 되지만, 공백·길이는 안전하게 다듬는다.
_CMD_NAME_RE = re.compile(r"[^\w\-]", re.UNICODE)


def _slugify_command_name(name):
    slug = _CMD_NAME_RE.sub("_", name.strip()).strip("_-").lower()
    return slug[:32] or None


def _make_channel_command(cmd_name, channel_name):
    """시설(채널) 하나를 담당하는 슬래시 명령어를 만들어 tree 에 등록한다.

    channel_name 을 함수 인자로 받아 클로저로 갇히게 하는 게 핵심 — 반복문 안에서
    바로 만들면 모든 명령어가 마지막 채널 이름 하나만 참조하게 되는 흔한 실수를 피한다.
    """
    async def handler(interaction: discord.Interaction):
        try:
            data = _fetch_status()
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 대시보드 서버에 연결하지 못했습니다: {e}", ephemeral=True)
            return

        devices = [d for d in data["devices"] if d.get("channel") == channel_name]
        total = len(devices)
        connected_n = sum(1 for d in devices if d["connected"] is True)
        disconnected_n = sum(1 for d in devices if d["connected"] is False)
        unknown_n = sum(1 for d in devices if d["connected"] is None)

        msg = (
            f"📍 **{channel_name} 현황**\n"
            f"전체 {total}대 · 🟢 연결 {connected_n} · 🔴 끊김 {disconnected_n}"
        )
        if unknown_n:
            msg += f" · ⚪ 이력없음 {unknown_n}"

        if devices:
            msg += "\n\n" + "\n".join(_format_device_line(d) for d in devices)
        else:
            msg += "\n\n이 채널에 배정된 기기가 없습니다. /admin/discord 에서 배정해주세요."
        await interaction.response.send_message(msg)

    tree.command(name=cmd_name, description=f"{channel_name} 기기 연결 현황")(handler)


async def _register_facility_commands(retries=20, delay_sec=3):
    """/admin/discord 에 등록된 시설(채널) 목록으로 /a시설 같은 명령어를 동적으로 만든다.

    봇과 대시보드(emfit.service)를 거의 동시에 재시작하면, 대시보드가 600MB+ 로그를
    파싱하는 동안 이 API가 아직 안 떠 있을 수 있다 — 그래서 바로 포기하지 않고 잠깐씩
    쉬며 재시도한다(기본 20회 × 3초 = 최대 1분 정도 기다림)."""
    data = None
    for attempt in range(1, retries + 1):
        try:
            data = _fetch_status()
            break
        except Exception as e:
            if attempt == retries:
                print(f"[discord_bot] 시설별 명령어 등록 실패(재시도 {retries}회 소진): {e}", flush=True)
                return
            print(f"[discord_bot] 대시보드 응답 대기 중... ({attempt}/{retries}) {e}", flush=True)
            await asyncio.sleep(delay_sec)

    seen_names = {"현황", "상세보기", "배터리"}  # 고정 명령어와 겹치면 등록이 통째로 실패하니 미리 막는다
    for channel_name in data.get("channels") or []:
        cmd_name = _slugify_command_name(channel_name)
        if not cmd_name or cmd_name in seen_names:
            print(f"[discord_bot] 채널 '{channel_name}' → 명령어 이름을 만들 수 없어 건너뜀(중복 또는 빈 이름)", flush=True)
            continue
        seen_names.add(cmd_name)
        _make_channel_command(cmd_name, channel_name)
        print(f"[discord_bot] /{cmd_name} → '{channel_name}' 채널 전용 명령어 등록", flush=True)


@client.event
async def on_ready():
    await _register_facility_commands()
    await tree.sync()
    print(f"[discord_bot] 로그인 완료: {client.user} — 슬래시 명령어 등록됨", flush=True)


if __name__ == "__main__":
    client.run(_load_token())
