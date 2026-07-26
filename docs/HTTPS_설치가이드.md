# HTTPS 설치 가이드 (Caddy)

> Emfit 관제 서버에 HTTPS(자물쇠)를 붙이는 절차.
> **설계 원칙: 지금 잘 도는 Emfit 경로(공유기 80 → 젯슨 8080)는 절대 건드리지 않는다.**
> 443 규칙을 "추가"만 하므로, 문제가 생겨도 추가한 것만 지우면 원상복구된다.

## 왜 Caddy인가
- 무료·오픈소스, 사용량 요금 없음
- 인증서(Let's Encrypt) 자동 발급 + 90일마다 자동 갱신 → 신경 쓸 일 없음
- 앱(app.py) 코드 수정 0 — Caddy가 앞에 서서 8080 앱으로 넘겨주기만 함

## 완성 후 구조
```
                     ┌───────────────────────────────┐
  인터넷 ─ 80 ───────┼─→ 젯슨:8080 (앱)   ← Emfit·기존, 그대로
                     │
         ─ 443(신규) ┼─→ 젯슨:443 (Caddy) ─→ localhost:8080 (앱)
                     │        ↑ 자물쇠 자동 관리
                     └───────────────────────────────┘
```
- **대시보드/로그인** → `https://monitoring.example.com` (Caddy, 비번 보호)
- **Emfit 수신** → `http://monitoring.example.com/` (80, 지금 그대로 — 무변경)
- **AI Radar 수신** → HTTP·HTTPS 둘 다 가능 (기기가 되는 쪽 선택)

## 우리 환경 값
| 항목 | 값 |
|---|---|
| 공유기 | TP-Link Archer BE550 |
| 젯슨 내부 IP | `JETSON_HOST` |
| 도메인 | `monitoring.example.com` |
| 앱 | systemd 서비스 `emfit`, 포트 8080 |

---

## 사전 점검 (변경 없음 — 확인만)

젯슨 SSH 접속 후:
```bash
ssh operator@jetson-host

# 1) 80/443 포트를 젯슨에서 이미 쓰는 게 있는지 (없어야 정상)
sudo ss -tlnp | grep -E ':(80|443)\b' || echo "80/443 로컬 점유 없음 → OK"

# 2) 앱이 8080에서 살아있는지
systemctl is-active emfit
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/dashboard   # 401 또는 200 이면 정상

# 3) 도메인이 우리 공유기(현재 외부 IP)를 가리키는지
curl -s ifconfig.me ; echo    # 이 IP 와 tplinkdns 가 같은 곳을 가리키면 인증서 발급 가능
```

---

## 1단계. Caddy 설치 (ARM64)

공식 저장소에서 설치 (Ubuntu/Debian 계열):
```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

caddy version    # 버전 찍히면 설치 성공
```

---

## 2단계. Caddy 설정

`/etc/caddy/Caddyfile` 을 아래 내용으로 교체:
```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak   # 원본 백업
sudo nano /etc/caddy/Caddyfile
```

파일 내용 (기존 것 다 지우고 이것만):
```caddyfile
{
    # 포트 80은 기존 Emfit 이 쓰므로 Caddy 는 443 에서만 인증서를 받는다.
    auto_https disable_redirects
}

monitoring.example.com {
    reverse_proxy localhost:8080
}
```
> `disable_redirects` = Caddy 가 80 포트를 가로채 HTTPS 로 튕기는 동작을 끔.
> 이게 있어야 Emfit·레이더의 HTTP POST 가 리다이렉트로 깨지지 않는다.

설정 문법 검사 후 반영:
```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

---

## 3단계. 공유기에 443 추가 (⚠️ 여기서 처음으로 외부 설정 변경)

TP-Link 관리자 → **고급 → NAT 전달 → 포트 포워딩 → 추가**:

| 필드 | 값 |
|---|---|
| 서비스 이름 | `emfit_https` |
| 장치 IP 주소 | `JETSON_HOST` |
| 외부 포트 | `443` |
| 내부 포트 | `443` |
| 프로토콜 | TCP |

> 기존 `emfit`(80→8080) 규칙은 **그대로 둔다.** 이 443 규칙만 추가.

---

## 4단계. 인증서 발급 & HTTPS 확인

443 포워딩을 켜면 Caddy 가 몇 초~1분 안에 자동으로 인증서를 받아온다.
```bash
# Caddy 로그에서 인증서 발급 확인 (certificate obtained 문구)
sudo journalctl -u caddy --no-pager | tail -30

# 젯슨 자신에서 HTTPS 응답 확인
curl -sI https://monitoring.example.com/dashboard | head -3
```
그다음 **바깥(휴대폰 LTE 등)에서 브라우저로**:
```
https://monitoring.example.com/dashboard
```
주소창에 자물쇠가 뜨고 로그인 화면이 나오면 성공.

---

## 5단계. Emfit 이 계속 잘 들어오는지 검증 (가장 중요)

HTTPS 붙였다고 Emfit 이 끊기면 안 된다. 80 경로는 안 건드렸으니 그대로여야 정상:
```bash
# 새 데이터가 계속 쌓이는지 (몇 줄 찍히면 정상)
tail -f ~/emfit_server/emfit_data.jsonl
# Ctrl+C 로 종료

# 마지막 수신 시각 확인
tail -1 ~/emfit_server/emfit_data.jsonl | python3 -c "import sys,json; print(json.load(sys.stdin).get('server_received_at'))"
```
대시보드 카드의 "통신" 시각이 계속 갱신되면 Emfit 정상.

---

## (선택) 6단계. HTTPS 붙은 뒤 코드 정리

HTTPS 가 확인되면 아래 둘을 반영하면 더 깔끔하다. **HTTPS 성공 후에만.**

1. 발급 URL 을 https 로:
   - systemd 서비스에 환경변수 추가하거나 app.py 의 기본값 변경
   - `EMFIT_EXTERNAL_BASE=https://monitoring.example.com`
2. 쿠키 Secure 플래그 (추후 코드 작업에서)

---

## 되돌리기 (문제 생겼을 때)

**Emfit 은 어차피 80→8080 그대로라 영향받지 않는다.** HTTPS 만 취소하면 됨:
```bash
# 1) 공유기: 추가했던 443(emfit_https) 규칙 삭제 (또는 상태 토글 OFF)

# 2) 젯슨: Caddy 정지
sudo systemctl stop caddy
sudo systemctl disable caddy

# (완전 제거까지 원하면)
sudo apt remove -y caddy
```
공유기 80 규칙과 앱(emfit 서비스)은 처음부터 안 건드렸으므로 그대로 살아있음.

---

## 체크리스트
- [ ] 사전 점검 3개 통과
- [ ] Caddy 설치 (`caddy version`)
- [ ] Caddyfile 작성 + `caddy validate` 통과
- [ ] 공유기 443 규칙 추가
- [ ] 인증서 발급 로그 확인
- [ ] 외부에서 `https://.../dashboard` 자물쇠 + 로그인 화면
- [ ] Emfit 데이터 계속 수신됨 (tail 확인)
- [ ] (선택) EXTERNAL_BASE https 로 변경
