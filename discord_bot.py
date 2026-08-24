"""디스코드 봇 — 슬래시 명령어(/현황, /상세보기)로 돌봄기기 연결 현황을 답해준다.

app.py(대시보드)와는 별도 프로세스로 돈다. 기기 데이터를 직접 읽지 않고
app.py 가 이미 메모리에 들고 있는 상태를 /internal/discord/status(localhost 전용)로
불러다 쓴다 — 봇 하나 띄우자고 500MB+ 로그 파일을 또 파싱하지 않기 위함.

실행: venv/bin/python discord_bot.py  (systemd 서비스로 상시 실행 권장)
토큰: discord_bot_token.txt 파일에서 읽는다 (git에 올리지 않음).
"""
import os
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
        msg += "\n\n" + "\n".join(
            f"🔴 {d['name']}" + (f" ({d['location']})" if d["location"] else "") + f" — {d['last_seen_text']}"
            for d in offline
        )
    await interaction.response.send_message(msg)


@tree.command(name="상세보기", description="돌봄기기 전체 목록과 상태")
async def detail_command(interaction: discord.Interaction):
    try:
        data = _fetch_status()
    except Exception as e:
        await interaction.response.send_message(f"⚠️ 대시보드 서버에 연결하지 못했습니다: {e}", ephemeral=True)
        return

    lines = [
        f"{_dot(d['connected'])} {d['name']}" + (f" ({d['location']})" if d["location"] else "")
        + f" — {d['last_seen_text']}"
        for d in data["devices"]
    ]
    body = "\n".join(lines) if lines else "등록된 기기가 없습니다."
    if len(body) > 1900:  # 디스코드 메시지 2000자 제한
        body = body[:1900] + "\n… (기기가 많아 일부 생략)"
    await interaction.response.send_message(f"📋 **전체 기기 목록** ({data['total']}대)\n\n{body}")


@client.event
async def on_ready():
    await tree.sync()
    print(f"[discord_bot] 로그인 완료: {client.user} — 슬래시 명령어 등록됨", flush=True)


if __name__ == "__main__":
    client.run(_load_token())
