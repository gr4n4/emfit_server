/* ═══════════════════════════════════════════════════════════════════════
 * 돌봄기기 사용감지 센서 — ESP32 예제 펌웨어
 *
 *   압력(FSR) 감지 → 서버로 "사용 중" / "미사용" 전송
 *   서버: Emfit 관제 서버 v3.5.0 의 POST /jy01
 *
 *   Arduino IDE 에서 보드를 "ESP32 Dev Module" 로 선택하고 업로드.
 *   필요한 라이브러리는 ESP32 보드 패키지에 모두 포함되어 있음(추가 설치 없음).
 *
 * ── 이 펌웨어가 보내는 것 ────────────────────────────────────────────
 *   press     : 압력 감지됨      → 대시보드 "사용 중"
 *   release   : 압력 사라짐      → 대시보드 "미사용"  (duration_ms = 사용 시간)
 *   heartbeat : 10분마다 생존신고 → 전원 빠짐·방전을 서버가 1시간 안에 감지
 *   error     : 센서 연결 이상   → 대시보드 "센서 확인 필요"
 *
 *   ※ heartbeat 를 보내야 서버의 '전원·통신 확인' 감지가 켜집니다.
 *     안 보내면 보드가 통째로 죽어도 서버는 알 수 없습니다(조용함 ≠ 고장).
 * ═══════════════════════════════════════════════════════════════════════ */

#include <WiFi.h>
#include <HTTPClient.h>

/* ═════ 여기만 환경에 맞게 고치세요 ═══════════════════════════════════ */

const char* WIFI_SSID = "여기에_WiFi_이름";
const char* WIFI_PASS = "여기에_WiFi_비밀번호";

// 젯슨과 같은 WiFi 를 쓰면 아래(내부 주소)가 더 안정적입니다.
// 인터넷이 잠깐 끊겨도 같은 건물 안에서는 계속 전송됩니다.
const char* SERVER_URL = "http://JETSON_HOST:8080/jy01";
// 다른 망에서 쓸 때는 이쪽:
// const char* SERVER_URL = "http://monitoring.example.com/jy01";

// ⚠️ FSR 은 반드시 ADC1 핀(GPIO 32·33·34·35·36·39)에 연결하세요.
//    ADC2 핀(GPIO 0·2·4·12~15·25~27)은 WiFi 를 켜면 값을 못 읽습니다. 흔한 함정입니다.
const int FSR_PIN = 34;

// 배터리 전압 분압 회로가 연결된 핀. 측정 회로가 없으면 -1 로 두세요.
const int   BATT_PIN     = 35;
const float BATT_DIVIDER = 2.0f;    // 저항 분압비 (예: 100k+100k → 2.0)

// 압력 판정 기준 (0~4095). 실제 센서로 시리얼 모니터를 보며 맞추세요.
// 두 값을 벌려두는 이유(히스테리시스): 경계에서 눌림/해제가 덜덜 떨리는 걸 막습니다.
const int PRESS_THRESHOLD   = 1200;   // 이 값을 넘으면 '눌림'
const int RELEASE_THRESHOLD = 800;    // 이 값 아래로 내려가야 '해제'

const unsigned long DEBOUNCE_MS  = 300;                  // 상태 확정까지 유지돼야 하는 시간
const unsigned long KEEPALIVE_MS = 10UL * 60UL * 1000UL; // 생존신고 주기(10분)

/* ═════ 여기부터는 그대로 두셔도 됩니다 ═══════════════════════════════ */

bool          isPressed      = false;   // 현재 확정된 사용 상태
bool          candidate      = false;   // 바뀌려는 중인 상태
unsigned long candidateSince = 0;       // 그 상태가 시작된 시각
unsigned long pressStartedAt = 0;       // 눌리기 시작한 시각 (사용 시간 계산용)
unsigned long lastKeepalive  = 0;
bool          faultReported  = false;   // 같은 이상을 반복해서 보내지 않도록

// ── 배터리 ──────────────────────────────────────────────────────────
int readBatteryMv() {
  if (BATT_PIN < 0) return -1;
  // analogReadMilliVolts 는 보드마다 다른 ADC 오차를 보정해 줍니다(analogRead 보다 정확).
  long sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(BATT_PIN);
  return (int)((sum / 8) * BATT_DIVIDER);
}

int readBatteryPct() {
  int mv = readBatteryMv();
  if (mv < 0) return -1;
  // 리튬이온 1셀 기준: 4200mV = 100%, 3300mV = 0%
  int pct = (int)((mv - 3300) * 100.0f / (4200 - 3300));
  if (pct > 100) pct = 100;
  if (pct < 0)   pct = 0;
  return pct;
}

// ── 서버 전송 ───────────────────────────────────────────────────────
bool sendEvent(const char* event, unsigned long durationMs) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[전송] WiFi 끊김 — 건너뜀");
    return false;
  }

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
    readBatteryPct(), readBatteryMv(), millis());

  int code = http.POST(body);
  http.end();

  // 서버는 저장 성공 시 200 을 줍니다. 400 은 필드 누락, 5xx 는 서버 저장 실패.
  Serial.printf("[전송] %-9s -> %d  %s\n", event, code, body);
  return (code == 200);
}

// ── WiFi ────────────────────────────────────────────────────────────
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[WiFi] '%s' 접속 시도...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] 접속됨  IP=%s\n", WiFi.localIP().toString().c_str());
    // ⚠️ 이 MAC 주소가 서버에 등록되어야 할 값입니다. 서버의 값과 다르면
    //    서버가 자동 등록하고 로그에 'FSR 기기 자동 등록' 을 남깁니다.
    Serial.printf("[WiFi] 이 보드의 MAC = %s  <-- 서버 등록값과 같아야 함\n",
                  WiFi.macAddress().c_str());
  } else {
    Serial.println("[WiFi] 접속 실패 — 잠시 후 재시도");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== 돌봄기기 사용감지 센서 시작 ===");

  analogReadResolution(12);            // 0~4095
  analogSetPinAttenuation(FSR_PIN, ADC_11db);   // 최대 약 3.3V 까지 읽기
  if (BATT_PIN >= 0) analogSetPinAttenuation(BATT_PIN, ADC_11db);

  connectWiFi();

  // 부팅했다고 알림 (서버에서 생존신고로 처리 — 사용 상태는 바뀌지 않음)
  sendEvent("boot", 0);
  lastKeepalive = millis();
}

void loop() {
  connectWiFi();                       // 끊기면 자동 재접속

  int raw = analogRead(FSR_PIN);
  unsigned long now = millis();

  /* ── 센서 연결 확인 ──────────────────────────────────────────────
     FSR 배선이 빠지면 핀이 뜬 상태(floating)가 되어 값이 0 근처에 붙습니다.
     풀다운 저항을 쓰는 회로라면 이 판정이 맞고, 회로가 다르면 조건을 바꾸세요.
     확실치 않으면 이 블록을 통째로 지워도 나머지는 정상 동작합니다.        */
  if (raw <= 5) {
    if (!faultReported) {
      Serial.println("[센서] 값이 0 — 배선 확인 필요");
      sendEvent("error", 0);           // 서버: "센서 확인 필요 / 압력 센서 연결 확인"
      faultReported = true;
    }
    delay(200);
    return;
  }
  if (faultReported) {                 // 정상 복귀
    Serial.println("[센서] 정상 복귀");
    faultReported = false;
  }

  /* ── 눌림/해제 판정 (히스테리시스 + 디바운스) ───────────────────── */
  bool wantPressed = isPressed;
  if (!isPressed && raw >= PRESS_THRESHOLD)        wantPressed = true;
  else if (isPressed && raw <= RELEASE_THRESHOLD)  wantPressed = false;

  if (wantPressed != candidate) {      // 바뀌려는 조짐 — 타이머 시작
    candidate = wantPressed;
    candidateSince = now;
  }

  // 같은 상태가 DEBOUNCE_MS 만큼 유지되면 확정
  if (candidate != isPressed && (now - candidateSince) >= DEBOUNCE_MS) {
    isPressed = candidate;
    if (isPressed) {
      pressStartedAt = now;
      sendEvent("press", 0);                       // 대시보드: "사용 중"
    } else {
      unsigned long used = now - pressStartedAt;   // 이번에 쓴 시간
      sendEvent("release", used);                  // 대시보드: "미사용" + 사용 시간
    }
  }

  /* ── 생존신고 ────────────────────────────────────────────────────
     이걸 보내야 서버가 '전원 빠짐·배터리 방전'을 감지할 수 있습니다.        */
  if (now - lastKeepalive >= KEEPALIVE_MS) {
    sendEvent("heartbeat", 0);
    lastKeepalive = now;
  }

  // 임계값 맞출 때 이 줄의 주석을 풀고 시리얼 모니터(115200)로 값을 보세요.
  // Serial.printf("raw=%d  pressed=%d\n", raw, isPressed);

  delay(50);
}
