#!/usr/bin/env python3
"""Check a two-participant synthetic A/V call over relay/UDP (requires Playwright)."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', required=True, help='HTTPS URL of the deployed livekit-test page')
parser.add_argument('--kubeconfig', required=True)
parser.add_argument('--allow-private-ca', action='store_true', help='Accept the lab certificate in this temporary test context only')
args = parser.parse_args()
from playwright.async_api import async_playwright
ROOT=str(Path(__file__).resolve().parents[1])
KUBE=args.kubeconfig
async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--autoplay-policy=no-user-gesture-required'])
  ctx=await browser.new_context(ignore_https_errors=args.allow_private_ca, permissions=['camera','microphone'])
  await ctx.add_init_script('''window.testPCs=[];const NativePC=window.RTCPeerConnection;window.RTCPeerConnection=class extends NativePC {constructor(...args){super(...args);window.testPCs.push(this);}};''')
  pages=[]
  for who in ['test-alice','test-bob']:
   page=await ctx.new_page(); pages.append(page)
   page.on('pageerror',lambda e: print('PAGE ERROR:',str(e)))
   await page.goto(args.url)
   await page.wait_for_function("!document.getElementById('join').disabled", timeout=30000)
   token=subprocess.check_output(['python3',ROOT+'/scripts/livekit-token.py','--kubeconfig',KUBE,'token','--room','turn-e2e','--identity',who],text=True).strip()
   await page.fill('#token',token);await page.click('#join')
   try: await page.wait_for_function("document.getElementById('status').textContent.startsWith('Connected as')", timeout=45000)
   except Exception:
    print('STATUS:',await page.locator('#status').inner_text());raise
  await asyncio.sleep(8)
  async def stats(page):
   return await page.evaluate('''async()=>Promise.all(window.testPCs.map(async pc=>{const s=await pc.getStats(); const pairs=[...s.values()].filter(x=>x.type==='transport'&&x.selectedCandidatePairId).map(t=>{const pair=s.get(t.selectedCandidatePairId);const local=s.get(pair.localCandidateId);const remote=s.get(pair.remoteCandidateId);return {local:local.candidateType,protocol:local.protocol,relayProtocol:local.relayProtocol,turn:local.url,remote:remote.address,port:remote.port}});const rtp=[...s.values()].filter(x=>(x.type==='inbound-rtp'||x.type==='outbound-rtp')&&!x.isRemote).map(x=>({type:x.type,kind:x.kind,bytes:x.bytesReceived??x.bytesSent,frames:x.framesDecoded??x.framesEncoded}));return {state:pc.connectionState,policy:pc.getConfiguration().iceTransportPolicy,pairs,rtp}}))''')
  baseline=[await stats(page) for page in pages]
  await asyncio.sleep(3)
  results=[await stats(page) for page in pages]
  for before, after in zip(baseline,results):
   for kind in ['audio','video']:
    for direction in ['inbound-rtp','outbound-rtp']:
     total=lambda pcs: sum(r['bytes'] or 0 for pc in pcs for r in pc['rtp'] if r['kind']==kind and r['type']==direction)
     assert total(after)>total(before), (kind,direction,'bytes did not increase')
  print(json.dumps(results,indent=2))
  for pcs in results:
   active=[pc for pc in pcs if pc['state']=='connected']
   assert active, 'Expected an active media transport'
   for pc in active:
    assert pc['policy']=='relay'
    assert pc['pairs'] and all(pair['local']=='relay' and pair['protocol']=='udp' for pair in pc['pairs'])
   for kind in ['audio','video']:
    for direction in ['inbound-rtp','outbound-rtp']:
     assert any(r['kind']==kind and r['type']==direction and r['bytes']>0 and (kind=='audio' or r['frames']>0) for pc in active for r in pc['rtp']), (kind,direction)
  print('PASS: Both participants publish and receive audio/video over relay/UDP.')
  for page in pages: await page.click('#leave')
  await browser.close()
asyncio.run(main())
