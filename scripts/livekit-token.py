#!/usr/bin/env python3
"""Bootstrap private LiveKit keys, or mint a one-hour room-scoped JWT."""
import argparse
import base64
import hashlib
import hmac
import json
import secrets
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kubeconfig')
parser.add_argument('--namespace', default='media')
sub = parser.add_subparsers(dest='command', required=True)
sub.add_parser('bootstrap', help='Create secret only if absent; never rotate existing keys')
token = sub.add_parser('token')
token.add_argument('--room', required=True)
token.add_argument('--identity', required=True)
args = parser.parse_args()
kubectl = ['kubectl'] + (['--kubeconfig', args.kubeconfig] if args.kubeconfig else [])

def run(arguments, **kwargs):
    return subprocess.check_output(kubectl + arguments, **kwargs)

if args.command == 'bootstrap':
    run(['apply', '-f', '-'], input=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': args.namespace}}).encode())
    existing = run(['-n', args.namespace, 'get', 'secret', 'livekit-keys', '--ignore-not-found', '-o', 'name'])
    if existing.strip():
        print('Keeping existing livekit-keys Secret.')
    else:
        secret = secrets.token_urlsafe(48)
        body = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'livekit-keys', 'namespace': args.namespace}, 'type': 'Opaque', 'stringData': {'keys.yaml': 'webrtc: ' + secret + '\n'}}
        # create, never apply: a concurrent bootstrap must not overwrite keys.
        run(['create', '-f', '-'], input=json.dumps(body).encode())
        print('Created livekit-keys Secret. Credentials stay out of Git.')
else:
    data = json.loads(run(['-n', args.namespace, 'get', 'secret', 'livekit-keys', '-o', 'json']))
    key, secret = base64.b64decode(data['data']['keys.yaml']).decode().strip().split(':', 1)
    now = int(time.time())
    payload = {'iss': key, 'sub': args.identity, 'nbf': now - 5, 'exp': now + 3600, 'video': {'roomJoin': True, 'room': args.room, 'canPublish': True, 'canSubscribe': True}}
    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip('=')
    unsigned = '.'.join(encode(json.dumps(obj, separators=(',', ':')).encode()) for obj in [{'alg': 'HS256', 'typ': 'JWT'}, payload])
    print(unsigned + '.' + encode(hmac.new(secret.strip().encode(), unsigned.encode(), hashlib.sha256).digest()))
