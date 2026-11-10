"""Keep one internal HLS URL ingress publishing into the configured LiveKit room."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SERVER = os.environ['LIVEKIT_URL'].rstrip('/')
SOURCE = os.environ['SOURCE_URL']
ROOM = os.environ['ROOM_NAME']
IDENTITY = os.environ['PARTICIPANT_IDENTITY']
NAME = os.environ['PARTICIPANT_NAME']
KEY, SECRET = Path('/etc/livekit/keys/keys.yaml').read_text().strip().split(':', 1)
SECRET = SECRET.strip()


def encode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def call(method, payload):
    now = int(time.time())
    claims = {'iss': KEY, 'nbf': now - 5, 'exp': now + 120,
              'video': {'ingressAdmin': True}}
    message = '.'.join(encode(json.dumps(value).encode()) for value in
                       [{'alg': 'HS256', 'typ': 'JWT'}, claims])
    token = message + '.' + encode(hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest())
    request = Request(SERVER + '/twirp/livekit.Ingress/' + method,
                      data=json.dumps(payload).encode(),
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=45) as response:
        return json.load(response)


def reconcile():
    # Wait for actual HLS content, not merely nginx's /health response.
    with urlopen(SOURCE, timeout=10) as response:
        if b'#EXTM3U' not in response.read(65536):
            raise RuntimeError('Source is not an HLS playlist')
    items = call('ListIngress', {'room_name': ROOM}).get('items', [])
    owned = [item for item in items if item.get('participant_identity') == IDENTITY
             and item.get('name') == 'internal-video-stream']
    for item in owned:
        state = item.get('state', {}).get('status', 'ENDPOINT_INACTIVE')
        if state in ('ENDPOINT_BUFFERING', 'ENDPOINT_PUBLISHING', 1, 2):
            print(f'Ingress {item["ingress_id"]}: {state}', flush=True)
            return
    # URL ingresses are single-use; recreate after a source/worker failure.
    for item in owned:
        call('DeleteIngress', {'ingress_id': item['ingress_id']})
    result = call('CreateIngress', {
        'input_type': 'URL_INPUT', 'name': 'internal-video-stream', 'url': SOURCE,
        'room_name': ROOM, 'participant_identity': IDENTITY, 'participant_name': NAME,
        'audio': {'name': 'Video soundtrack', 'preset': 'OPUS_STEREO_96KBPS'},
        'video': {'name': 'Provided video', 'preset': 'H264_720P_30FPS_1_LAYER'},
    })
    print(f'Created ingress {result["ingress_id"]} for room {ROOM}', flush=True)


while True:
    try:
        reconcile()
    except HTTPError as error:
        # Log API errors without logging bearer tokens or signing keys.
        print(f'API/source HTTP {error.code}: {error.read(2048).decode(errors="replace")}', flush=True)
    except Exception as error:
        print(f'Waiting to publish: {error}', flush=True)
    time.sleep(30)
