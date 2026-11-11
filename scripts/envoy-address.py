#!/usr/bin/env python3
"""Discover Envoy's Azure public address and publish it for LiveKit init config."""
import argparse
import ipaddress
import json
import subprocess
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--kubeconfig',required=True)
p.add_argument('--restart',action='store_true',help='Restart LiveKit to use the updated address and refresh TURN alias credentials')
a=p.parse_args()
k=['kubectl','--kubeconfig',a.kubeconfig,'-n','media']
s=json.loads(subprocess.check_output(k+['get','service','webrtc-envoy','-o','json']))
address=s.get('status',{}).get('loadBalancer',{}).get('ingress',[{}])[0].get('ip')
if not address:raise SystemExit('Envoy public IP not assigned yet; retry after the Azure LB is ready.')
ipaddress.ip_address(address)
cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'livekit-public-endpoint','namespace':'media'},'data':{'host':address}}
subprocess.run(k+['apply','-f','-'],input=json.dumps(cm).encode(),check=True)
if a.restart:subprocess.check_call(k+['rollout','restart','deployment/livekit-server'])
print('LiveKit public TURN alias:',address+':3478/UDP')
