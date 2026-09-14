/* 돌봄기기 사용감지 센서 — ESP32
 * 서버: POST /jy01  (Emfit 관제 서버 v3.5.0)
 *
 * [필요 라이브러리] Arduino IDE > 라이브러리 매니저 > "WiFiManager" (by tzapu) 설치
 *
 * [현장 설치]  전원을 켜면 "CareSensor-XXXX" 라는 WiFi 가 생깁니다.
 *              폰으로 거기 접속 → 뜨는 화면에서 그 집 WiFi 선택 + 비번 입력 → 끝.
 *              한 번 하면 이후 자동 접속. 노트북 없이 설치할 수 있습니다.
 * [WiFi 재설정] BOOT 버튼(GPIO0)을 누른 채로 전원을 켜면 설정 화면이 다시 뜹니다.
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiManager.h>

// ═══ 설정 ═══════════════════════════════════════════════════════
// 같은 WiFi 면 내부 주소가 안정적. 다른 망이면 아래 주석 쪽을 쓰세요.
const char* SERVER_URL = "http://JETSON_HOST:8080/jy01";
// const char* SERVER_URL = "https://monitoring.example.com/jy01";

const int   FSR_PIN      = 34;    // ⚠️ ADC1 만 가능 — GPIO 32·33·34·35·36·39
const int   BATT_PIN     = 35;    // 배터리 분압 핀 (측정 회로 없으면 -1)
const float BATT_DIVIDER = 2.0f;  // 저항 분압비

const int PRESS_THRESHOLD   = 1200;   // 이 값 넘으면 눌림 (0~4095)
const int RELEASE_THRESHOLD = 800;    // 이 값 아래로 내려가야 해제

const unsigned long DEBOUNCE_MS  = 300;
const unsigned long KEEPALIVE_MS = 600000;   // 생존신고 10분
// ════════════════════════════════════════════════════════════════

bool isPressed = false, candidate = false, faultReported = false, wifiWasDown = false;
unsigned long candidateSince = 0, pressStartedAt = 0, lastKeepalive = 0;

int batteryMv() {
  if (BATT_PIN < 0) return -1;
  long s = 0;
  for (int i = 0; i < 8; i++) s += analogReadMilliVolts(BATT_PIN);
  return (int)((s / 8) * BATT_DIVIDER);
}

int batteryPct() {
  int mv = batteryMv();
  if (mv < 0) return -1;                       // 측정 회로 없음
  int p = (mv - 3300) * 100 / (4200 - 3300);   // 리튬 1셀 기준
  return p < 0 ? 0 : (p > 100 ? 100 : p);
}

bool sendEvent(const char* event, unsigned long durationMs) {
  if (WiFi.status() != WL_CONNECTED) return false;

  WiFiClient client;
  HTTPClient http;
  http.begin(client, SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(5000);

  char body[256];
  snprintf(body, sizeof(body),
    "{\"deviceId\":\"%s\",\"event\":\"%s\",\"duration_ms\":%lu,"
    "\"battery_pct\":%d,\"battery_mv\":%d,\"uptime_ms\":%lu}",
    WiFi.macAddress().c_str(), event, durationMs,
    batteryPct(), batteryMv(), millis());

  int code = http.POST(body);          // 200=성공 400=필드누락 5xx=서버오류
  http.end();
  Serial.printf("[전송] %-9s -> %d\n", event, code);
  return code == 200;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  analogReadResolution(12);
  pinMode(0, INPUT_PULLUP);            // BOOT 버튼
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  WiFiManager wm;
  if (digitalRead(0) == LOW) {         // 버튼 누른 채 부팅 → WiFi 재설정
    Serial.println("[WiFi] 저장된 설정 삭제");
    wm.resetSettings();
  }

  String mac = WiFi.macAddress();
  mac.replace(":", "");
  char ap[32];
  snprintf(ap, sizeof(ap), "CareSensor-%s", mac.substring(8).c_str());

  Serial.printf("[WiFi] 설정용 WiFi 이름: %s\n", ap);
  wm.setConfigPortalTimeout(180);      // 3분간 설정 없으면 재부팅
  if (!wm.autoConnect(ap)) {
    Serial.println("[WiFi] 설정 시간초과 — 재부팅");
    ESP.restart();
  }

  Serial.printf("[WiFi] 접속됨 IP=%s\n", WiFi.localIP().toString().c_str());
  Serial.printf("[MAC ] %s  <-- 서버 등록값과 같아야 함\n", WiFi.macAddress().c_str());

  sendEvent("boot", 0);
  lastKeepalive = millis();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {          // 끊기면 자동 재접속을 기다린다
    wifiWasDown = true;
    delay(500);
    return;
  }
  if (wifiWasDown) {                            // 돌아오면 현재 상태를 서버와 맞춘다
    wifiWasDown = false;
    sendEvent(isPressed ? "press" : "release", 0);
  }

  int raw = analogRead(FSR_PIN);
  unsigned long now = millis();

  if (raw <= 5) {                               // 배선 빠짐 → "센서 확인 필요"
    if (!faultReported) { sendEvent("error", 0); faultReported = true; }
    delay(200);
    return;
  }
  faultReported = false;

  bool want = isPressed;                        // 히스테리시스
  if (!isPressed && raw >= PRESS_THRESHOLD)       want = true;
  else if (isPressed && raw <= RELEASE_THRESHOLD) want = false;

  if (want != candidate) { candidate = want; candidateSince = now; }

  if (candidate != isPressed && (now - candidateSince) >= DEBOUNCE_MS) {   // 디바운스
    isPressed = candidate;
    if (isPressed) { pressStartedAt = now; sendEvent("press", 0); }
    else           { sendEvent("release", now - pressStartedAt); }
  }

  if (now - lastKeepalive >= KEEPALIVE_MS) { sendEvent("heartbeat", 0); lastKeepalive = now; }

  // 임계값 맞출 때 이 줄 주석 해제 → 시리얼 모니터(115200)로 값 확인
  // Serial.printf("raw=%d\n", raw);

  delay(50);
}
