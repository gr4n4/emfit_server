"""AI Radar 경보를 NRCarec(nursing-care-app) 으로 보낸다.

NRCarec 은 Firebase Hosting 에 올린 정적 웹앱이라 서버가 없다. POST 를 받을
수단이 없으므로 레이더 API 를 직접 수신할 수 없고, 24시간 떠 있는 이쪽에서
보내야 한다.

두 갈래로 보낸다.
 - Firestore notification_log : 앱·대시보드가 구독해 경보음 + 확인 팝업
 - FCM 푸시                   : 앱이 꺼져 있을 때 잠금화면 알림

서비스 계정 키 경로는 환경변수 NRCAREC_SERVICE_ACCOUNT 로 준다.
"""

import datetime
import os

import firebase_admin
from firebase_admin import credentials, firestore, messaging

_app = None


def _init():
    global _app
    if _app is None:
        cred = credentials.Certificate(os.environ['NRCAREC_SERVICE_ACCOUNT'])
        _app = firebase_admin.initialize_app(cred, name='nrcarec')
    return _app


# 종류별 표시 정보. NRCarec 이 이 값으로 아이콘과 색을 고른다.
KINDS = {
    'fall': ('낙상 감지', '/icons/notify-fall.png'),
    'bedside': ('걸터앉음 감지', '/icons/notify-bedside.png'),
}


def _wanted(db, kind):
    """앱의 알림 설정을 따른다. settings/notifications 의 sensorAlerts.

    간호사가 앱에서 걸터앉음을 꺼 두거나 시간대를 정할 수 있다. 앱도 같은 값을
    보고 팝업을 거르지만, 꺼 둔 알림은 애초에 보내지 않는 게 맞다.
    """
    doc = db.collection('settings').document('notifications').get()
    s = (doc.to_dict() or {}).get('sensorAlerts', {}) if doc.exists else {}

    if kind == 'fall':
        return s.get('fall', True)
    if not s.get('bedside', True):
        return False

    # 걸터앉음만 시간대 제한. 시작과 끝이 같으면 종일.
    def mins(v):
        try:
            h, m = str(v).split(':')
            return int(h) * 60 + int(m)
        except Exception:
            return None

    a, b = mins(s.get('bedsideStart')), mins(s.get('bedsideEnd'))
    if a is None or b is None or a == b:
        return True
    now = datetime.datetime.now()
    m = now.hour * 60 + now.minute
    # 자정을 넘기는 구간(22:00~06:00)도 다룬다.
    return (a <= m < b) if a < b else (m >= a or m < b)


_last_sent = {}   # (device_id, kind) -> timestamp


def should_send(device_id, kind, cooldown_sec=180):
    """같은 기기의 같은 경보를 반복해 보내지 않는다.

    낙상 모델 레이더는 1초마다 상태를 보낸다. 그대로 두면 낙상이 10초 이어질 때
    알림이 10번 간다.
    """
    key = (device_id, kind)
    now = datetime.datetime.now().timestamp()
    if now - _last_sent.get(key, 0) < cooldown_sec:
        return False
    _last_sent[key] = now
    return True


def send_alert(kind, room, patient_name, device_id, detail=''):
    """
    kind         : 'fall' 또는 'bedside'
    room         : '421'      (호 빼고 숫자만)
    patient_name : '홍길동'
    device_id    : 레이더 기기 식별자. 같은 기기의 같은 경보를 묶는 데 쓴다.
    detail       : 덧붙일 설명(선택)
    """
    _init()
    title, icon = KINDS[kind]

    who = f'{room}호 {patient_name}님' if room else f'{patient_name}님'
    body = f'{who} · {title[:-3]}이 감지되었습니다.'
    if detail:
        body += f' {detail}'

    # 같은 기기·같은 종류는 하나로 묶는다. 알림창에 쌓이지 않고 최신 것으로 바뀐다.
    tag = f'{kind}_{device_id}'

    db = firestore.client(app=_app)

    if not _wanted(db, kind):
        print(f'[NRCarec] {kind} 는 설정에서 꺼져 있어 보내지 않음')
        return 0

    # 1) 기록 — NRCarec 이 구독해 소리와 팝업을 띄우고, 알림 기록에도 남는다.
    doc_id = f'{tag}_{int(datetime.datetime.now().timestamp())}'
    db.collection('notification_log').document(doc_id).set({
        'sentAt': firestore.SERVER_TIMESTAMP,
        'kind': kind,               # 'fall' | 'bedside'
        'title': title,
        'body': body,
        'room': room,
        'patientName': patient_name,
        'deviceId': device_id,
    })

    # 2) 푸시 — 앱이 꺼져 있어도 닿는다.
    tokens = [d.id for d in db.collection('push_tokens').stream()]
    if not tokens:
        return 0

    res = messaging.send_each_for_multicast(
        messaging.MulticastMessage(
            tokens=tokens,
            # 반드시 data 만 쓴다. notification 을 같이 보내면 브라우저가 한 번,
            # 서비스워커가 또 한 번 띄워 알림이 두 번 뜬다.
            data={
                'title': title,
                'body': body,
                'kind': kind,
                'icon': icon,
                'tag': tag,
                'url': '/',
            },
            webpush=messaging.WebpushConfig(
                headers={'Urgency': 'high', 'TTL': '600'},
            ),
        ),
        app=_app,
    )

    # 만료된 토큰 정리
    for token, r in zip(tokens, res.responses):
        if not r.success and r.exception and \
                'registration-token-not-registered' in str(r.exception):
            db.collection('push_tokens').document(token).delete()

    return res.success_count
