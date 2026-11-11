#!/usr/bin/env python3
"""Verify the file room in the relay-only player and hosted LiveKit Meet (Playwright)."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kubeconfig', required=True)
parser.add_argument('--url', required=True, help='HTTPS URL of the livekit-test page')
parser.add_argument('--server-url', required=True, help='Public LiveKit WSS URL')
parser.add_argument('--room', default='video-stream')
parser.add_argument('--observe-seconds',type=int,default=5,help='Duration to verify increasing media counters')
parser.add_argument('--expected-turn-host', help='Require selected relay candidates to use this public TURN host')
parser.add_argument('--allow-private-ca', action='store_true')
args = parser.parse_args()
from playwright.async_api import async_playwright
ROOT=str(Path(__file__).resolve().parents[1])
KUBE=args.kubeconfig
STATS='''async()=>Promise.all(window.testPCs.map(async pc=>{const s=await pc.getStats();return {state:pc.connectionState,policy:pc.getConfiguration().iceTransportPolicy,pairs:[...s.values()].filter(t=>t.type==='transport'&&t.selectedCandidatePairId).map(t=>{const pair=s.get(t.selectedCandidatePairId),local=s.get(pair.localCandidateId),remote=s.get(pair.remoteCandidateId);return {local:local.candidateType,protocol:local.protocol,relayProtocol:local.relayProtocol,turn:local.url,remote:remote.address,port:remote.port}}),rtp:[...s.values()].filter(x=>x.type==='inbound-rtp').map(x=>({kind:x.kind,bytes:x.bytesReceived,frames:x.framesDecoded,samples:x.totalSamplesReceived}))}}))'''
def token(identity, viewer=False):
 return subprocess.check_output(['python3',ROOT+'/scripts/livekit-token.py','--kubeconfig',KUBE,'token','--room',args.room,'--identity',identity]+(['--viewer'] if viewer else []),text=True).strip()
async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--autoplay-policy=no-user-gesture-required'])
  ctx=await browser.new_context(ignore_https_errors=args.allow_private_ca,permissions=['camera','microphone'])
  await ctx.add_init_script('''window.testPCs=[];const NativePC=window.RTCPeerConnection;window.RTCPeerConnection=class extends NativePC {constructor(...args){super(...args);window.testPCs.push(this);}};''')
  player=await ctx.new_page()
  await player.goto(args.url)
  await player.wait_for_function("!document.getElementById('watch').disabled")
  await player.fill('#token',token('stream-test-viewer',True))
  await player.click('#watch')
  await player.wait_for_function("document.getElementById('status').textContent.startsWith('Connected as')",timeout=45000)
  await player.wait_for_function("[...document.querySelectorAll('video')].some(v=>v.videoWidth>0&&v.currentTime>0)",timeout=45000)
  meet=await ctx.new_page()
  await meet.goto('https://meet.livekit.io/?tab=custom')
  await meet.fill('#serverUrl',args.server_url)
  await meet.locator('textarea').fill(token('stream-test-meet'))
  await meet.get_by_role('button',name='Connect',exact=True).click()
  await meet.get_by_text('Provided video',exact=True).first.wait_for(timeout=60000)
  await asyncio.sleep(10)
  before=[await page.evaluate(STATS) for page in [player,meet]]
  await asyncio.sleep(args.observe_seconds)
  after=[await page.evaluate(STATS) for page in [player,meet]]
  for name,first,last in zip(['strict relay player','LiveKit Meet'],before,after):
   active=[pc for pc in last if pc['state']=='connected']
   assert active, name
   assert all(pair['local']=='relay' and pair['protocol']=='udp' for pc in active for pair in pc['pairs']), name
   if args.expected_turn_host:
    assert all(urlsplit(pair['turn'].replace('turn:', 'turn://', 1)).hostname==args.expected_turn_host for pc in active for pair in pc['pairs']), (name,'unexpected TURN host')
   for kind in ['audio','video']:
    def count(pcs, field):return sum((r.get(field) or 0) for pc in pcs for r in pc['rtp'] if r['kind']==kind)
    assert count(last,'bytes')>count(first,'bytes'), (name,kind)
    if kind=='video':assert count(last,'frames')>count(first,'frames'), name
   print(name, json.dumps(active))
  print('PASS: Provided file video/audio received by relay-only viewer and LiveKit Meet over TURN/UDP.')
  await player.click('#leave')
  await browser.close()
asyncio.run(main())
