#!/usr/bin/env python3
"""Bootstrap a constrained labmad intermediate for cert-manager; keep the root key local."""
import argparse
import base64
import json
from pathlib import Path
import secrets
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kubeconfig')
parser.add_argument('--namespace', default='media')
parser.add_argument('--ca-cert', type=Path, required=True)
parser.add_argument('--ca-key', type=Path, required=True)
args = parser.parse_args()
kubectl = ['kubectl'] + (['--kubeconfig', args.kubeconfig] if args.kubeconfig else [])


def run(command, **kwargs):
    return subprocess.check_output(command, **kwargs)


def openssl(*arguments):
    return run(['openssl', *map(str, arguments)], stderr=subprocess.PIPE)


# Validate the issuer before creating or uploading any credentials.
openssl('x509', '-in', args.ca_cert, '-checkend', str(366 * 86400), '-noout')
ca_public = openssl('x509', '-in', args.ca_cert, '-pubkey', '-noout')
key_public = openssl('pkey', '-in', args.ca_key, '-passin', 'pass:', '-pubout')
if ca_public != key_public:
    raise SystemExit('CA key does not match the CA certificate')
domain = run(kubectl + ['get', 'ingresses.config.openshift.io', 'cluster',
                       '-o', 'jsonpath={.spec.domain}']).decode().strip()
if not domain or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in domain):
    raise SystemExit('Invalid cluster ingress domain')
hosts = ['livekit.' + domain, 'livekit-test.' + domain]
with tempfile.TemporaryDirectory(prefix='webrtc-tls-') as directory:
    directory = Path(directory)  # private mode 0700; local intermediate key deleted on exit
    key = directory / 'tls.key'
    csr = directory / 'tls.csr'
    cert = directory / 'tls.crt'
    ext = directory / 'extensions.cnf'
    ext.write_text('basicConstraints=critical,CA:TRUE,pathlen:0\n'
                   'keyUsage=critical,keyCertSign,cRLSign\n'
                   'subjectKeyIdentifier=hash\n'
                   'authorityKeyIdentifier=keyid,issuer\n'
                   'nameConstraints=critical,' + ','.join('permitted;DNS:' + host for host in hosts) +
                   ',excluded;IP:0.0.0.0/0.0.0.0,excluded;IP:0:0:0:0:0:0:0:0/0:0:0:0:0:0:0:0\n')
    openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
            '-out', csr, '-subj', '/CN=LiveKit labmad intermediate')
    key.chmod(0o600)
    openssl('x509', '-req', '-in', csr, '-CA', args.ca_cert, '-CAkey', args.ca_key,
            '-passin', 'pass:', '-set_serial', '0x' + secrets.token_hex(16),
            '-days', '365', '-sha256', '-extfile', ext, '-out', cert)
    openssl('verify', '-CAfile', args.ca_cert, cert)
    existing = run(kubectl + ['-n', args.namespace, 'get', 'secret', 'livekit-labmad-issuer',
                             '--ignore-not-found', '-o', 'json'])
    metadata = {'name': 'livekit-labmad-issuer', 'namespace': args.namespace}
    if existing.strip():
        metadata['resourceVersion'] = json.loads(existing)['metadata']['resourceVersion']
    ca = args.ca_cert.read_bytes()
    payload = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata,
               'type': 'kubernetes.io/tls', 'data': {
                   'tls.crt': base64.b64encode(cert.read_bytes() + ca).decode(),
                   'tls.key': base64.b64encode(key.read_bytes()).decode(),
                   'ca.crt': base64.b64encode(ca).decode(),
               }}
    # No last-applied annotation or private material in Git, logs, or command arguments.
    run(kubectl + ['replace' if existing.strip() else 'create', '-f', '-'],
        input=json.dumps(payload).encode())
    print(openssl('x509', '-in', cert, '-noout', '-issuer', '-dates').decode().strip())
    print('Updated cert-manager intermediate CA Secret restricted to: ' + ', '.join(hosts))
