"""Render private LiveKit config with a public Envoy TURN alias at pod startup."""
import hashlib
import os
from pathlib import Path
import secrets
import time


def livekit_base62(data):
    # Wire-compatible with github.com/jxskiss/base62 (MIT), used by LiveKit.
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    number = int.from_bytes(data, 'big')
    remaining = len(data) * 8
    out = []
    while remaining > 0:
        value = number & 63
        size = 6
        if value & 30 == 30:
            if remaining > 6 or value > 31:
                size = 5
            value &= 31
        out.append(alphabet[value])
        number >>= size
        remaining -= size
    return ''.join(out)


key, secret = Path('/etc/livekit/keys/keys.yaml').read_text().strip().split(':', 1)
participant = 'envoy-' + secrets.token_hex(12)
expiry = int(time.time()) + 365 * 86400
username = livekit_base62(f'{key}|{participant}|{expiry}'.encode())
password = livekit_base62(hashlib.sha256(f'{secret.strip()}|{participant}|{expiry}'.encode()).digest())
config = Path('/template/livekit.yaml').read_text()
for token, value in {
    'PUBLIC_TURN_HOST_PLACEHOLDER': os.environ['PUBLIC_TURN_HOST'],
    'TURN_USERNAME_PLACEHOLDER': username,
    'TURN_PASSWORD_PLACEHOLDER': password,
    'POD_CIDR_PLACEHOLDER': os.environ['POD_IP'] + '/32',
}.items():
    config = config.replace(token, value)
Path('/runtime/livekit.yaml').write_text(config)
print('Prepared LiveKit embedded TURN public alias; credentials valid until', time.strftime('%Y-%m-%d', time.gmtime(expiry)), flush=True)
