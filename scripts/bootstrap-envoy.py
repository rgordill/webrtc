#!/usr/bin/env python3
"""Install the Envoy Gateway release matching this cluster's Gateway API schemas."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kubeconfig', required=True)
parser.add_argument('--allow-missing-route-crds', action='store_true',
                    help='Authorize the temporary CREATE-only OpenShift policy exception')
args = parser.parse_args()
kubectl = ['kubectl', '--kubeconfig', args.kubeconfig]
root = Path(__file__).resolve().parents[1]
gateway_api = 'v1.5.1'
envoy_gateway = 'v1.8.6'
policy = 'openshift-ingress-operator-gatewayapi-crd-admission'
exception_name = 'webrtc-bootstrap-missing-route-crds'


def read_crds():
    return {d['metadata']['name']: d for d in json.loads(subprocess.check_output(
        kubectl + ['get', 'crds', '-o', 'json']))['items']}


def fingerprint(resource):
    return hashlib.sha256(json.dumps(resource['spec'], sort_keys=True).encode()).hexdigest()


existing = read_crds()
protected = {name: d for name, d in existing.items()
             if d['spec']['group'] == 'gateway.networking.k8s.io'}
versions = {d['metadata'].get('annotations', {}).get('gateway.networking.k8s.io/bundle-version')
            for d in protected.values()}
if versions != {gateway_api}:
    raise SystemExit(f'Expected Gateway API {gateway_api}; found {versions}. Refusing a mixed-version install.')
before = {name: fingerprint(d) for name, d in protected.items()}
missing = []
for path in sorted((root / 'manifests/envoy-operator/crds').glob('*.yaml')):
    d = yaml.safe_load(path.read_text())
    assert d['metadata']['annotations']['gateway.networking.k8s.io/bundle-version'] == gateway_api
    if d['metadata']['name'] not in existing:
        missing.append(d)
if missing and not args.allow_missing_route_crds:
    raise SystemExit('Missing protected route CRDs. Review the exception and use --allow-missing-route-crds only with explicit admin approval.')

policy_present = subprocess.run(kubectl + ['get', 'validatingadmissionpolicy', policy],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
added = False
try:
    if missing and policy_present:
        patch = (root / 'manifests/envoy-operator/route-crd-bootstrap-exception.json').read_text()
        subprocess.run(kubectl + ['patch', 'validatingadmissionpolicy', policy,
                                  '--type=json', '-p', patch], check=True)
        added = True
    if missing:
        # Create only: never apply or update an OpenShift-owned Gateway API CRD.
        subprocess.run(kubectl + ['create', '-f', '-'],
                       input=yaml.safe_dump_all(missing).encode(), check=True)
finally:
    if added:
        current = json.loads(subprocess.check_output(kubectl + [
            'get', 'validatingadmissionpolicy', policy, '-o', 'json']))
        conditions = current['spec']['matchConditions']
        index = next(i for i, c in enumerate(conditions) if c['name'] == exception_name)
        patch = [{'op': 'test', 'path': f'/spec/matchConditions/{index}', 'value': conditions[index]},
                 {'op': 'remove', 'path': f'/spec/matchConditions/{index}'}]
        subprocess.run(kubectl + ['patch', 'validatingadmissionpolicy', policy,
                                  '--type=json', '-p', json.dumps(patch)], check=True)
        print('Restored OpenShift Gateway API CRD admission protection.', flush=True)
after = read_crds()
assert all(fingerprint(after[name]) == digest for name, digest in before.items()), 'Existing schema changed'
print(f'Existing Gateway API {gateway_api} schemas unchanged.', flush=True)

# Extension CRDs come from the matching Envoy release, not the newest chart.
rendered = subprocess.check_output(['helm', 'template', 'webrtc-envoy-crds',
    'oci://docker.io/envoyproxy/gateway-crds-helm', '--version', envoy_gateway,
    '--set', 'crds.envoyGateway.enabled=true', '--set', 'crds.gatewayAPI.enabled=false'])
crds = [d for d in yaml.safe_load_all(rendered) if d and d.get('kind') == 'CustomResourceDefinition']
assert all(d['spec']['group'] != 'gateway.networking.k8s.io' for d in crds)
subprocess.run(kubectl + ['apply', '--server-side', '--field-manager=webrtc-envoy-bootstrap', '-f', '-'],
               input=yaml.safe_dump_all(crds).encode(), check=True)
subprocess.run(['helm', 'upgrade', '--install', 'webrtc-envoy',
    'oci://docker.io/envoyproxy/gateway-helm', '--version', envoy_gateway,
    '--namespace', 'envoy-gateway-system', '--create-namespace', '--kubeconfig', args.kubeconfig,
    '-f', str(root / 'manifests/envoy-operator/values.yaml'), '--wait', '--timeout', '5m'], check=True)

namespace = json.loads(subprocess.check_output(kubectl + ['get', 'namespace', 'media', '-o', 'json']))
uid = namespace['metadata']['annotations']['openshift.io/sa.scc.uid-range'].split('/')[0]
subprocess.run(kubectl + ['-n', 'openshift-gitops', 'annotate', 'secret',
    'openshift-gitops-cluster-configuration', 'media-uid=' + uid, '--overwrite'], check=True)
print('Published media SCC UID for namespace-safe Envoy containers:', uid)

subprocess.run(kubectl + ['-n', 'envoy-gateway-system', 'rollout', 'restart', 'deployment/envoy-gateway'], check=True)
subprocess.run(kubectl + ['-n', 'envoy-gateway-system', 'rollout', 'status', 'deployment/envoy-gateway', '--timeout=120s'], check=True)
subprocess.run(kubectl + ['apply', '-f', str(root / 'manifests/envoy-operator/udp-buffer-policy.yaml')], check=True)
