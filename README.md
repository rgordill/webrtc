# WebRTC PoC — Envoy Gateway + LiveKit embedded TURN (Azure OpenShift)

`main` uses Envoy Gateway v1.8.6 as the UDP/HTTP proxy and LiveKit v1.13.11 as
both SFU and TURN server. The original implementation is preserved on
[`codex/stunner-implementation`](https://github.com/rgordill/webrtc/tree/codex/stunner-implementation),
whose Argo CD source revisions track that branch independently.

```text
video-server HLS → LiveKit Ingress → LiveKit SFU (internal pod networking)
Browser WSS/HTTPS → OpenShift TLS edge → Envoy HTTPRoute → LiveKit / player / HLS
Browser TURN/UDP → Azure LB → Envoy UDPRoute :3478 → LiveKit embedded TURN → SFU :7882
```

OpenShift's existing DNS and labmad certificates remain the TLS edge. All three
web endpoints route through Envoy; Envoy's public UDP listener exposes LiveKit
TURN. No STUNner controller, dataplane, custom UDPRoute or credentials are used
by the `main` configuration. The standard-group `gateway.networking.k8s.io/v1alpha2`
UDPRoute selects the LiveKit TURN port, **not** its raw media port.

This is a single-node PoC: one LiveKit pod and one Envoy replica. Do not scale
these independently without redesigning TURN allocation and room affinity.

## Version alignment

All existing Gateway API CRDs are bundle **v1.5.1**. Envoy Gateway **v1.8.6**
uses that exact Gateway API version and Envoy Proxy **v1.38.x**. The vendored
TCPRoute/UDPRoute CRDs are from Gateway API v1.5.1 experimental, not Envoy's
latest chart. The bootstrap verifies the bundle version and fingerprints all
existing Gateway API schemas before/after creation. The v1.8 line reached EOL
on 8 November 2026; it is pinned here to satisfy the cluster's exact schema match.
A supported newer Envoy release requires an OpenShift-coordinated API upgrade.

## Bootstrap and GitOps

Prerequisites: OpenShift GitOps, cert-manager/labmad bootstrap from the prior
setup, kubectl, Helm, Python 3 and PyYAML. API and CA keys remain outside Git.

```bash
export KUBECONFIG=~/PoC/azure/demo/auth/kubeconfig
python3 scripts/livekit-token.py bootstrap
kubectl apply -k manifests/gitops-rbac

# Install pinned Envoy controller and extension CRDs, preserve existing GW API CRDs.
python3 scripts/bootstrap-envoy.py --kubeconfig "$KUBECONFIG"
```

On this OpenShift cluster, its admission policy protects even missing Gateway
API CRDs. If TCPRoute/UDPRoute are absent, review
`manifests/envoy-operator/route-crd-bootstrap-exception.json` and obtain explicit
admin approval, then add `--allow-missing-route-crds` to the bootstrap command.
The exception permits only CREATE for those two names and is removed in a
`finally` block immediately afterward. Existing OpenShift CRDs are never updated.
OpenShift upgrades and future upgrades to these added CRDs require compatibility
review. The controller's Helm release is an admin bootstrap, with version and
values tracked here; Argo CD manages the application Gateway, HTTPRoutes,
UDPRoute and EnvoyProxy in the `webrtc-livekit` application.

For an initial install, first apply `manifests/envoy` with its HTTPRoute hosts
patched to the discovered ingress domain. Once Azure assigns a public IP:

```bash
python3 scripts/envoy-address.py --kubeconfig "$KUBECONFIG"
kubectl apply -k applications/webrtc/overlays/azure
```

`media/livekit-public-endpoint` is discovered runtime configuration outside
Argo CD. If the Envoy LoadBalancer IP changes, run the address helper with
`--restart` so LiveKit advertises the new endpoint.

## UDP packet sizing and source pacing

The admin bootstrap enables EnvoyPatchPolicy and applies only
`manifests/envoy-operator/udp-buffer-policy.yaml`, targeted at this Gateway's
TURN listener. Application RBAC does not grant EnvoyPatchPolicy access. The
policy raises downstream/upstream UDP receive datagram limits from the Envoy
1500-byte default to 65535; larger TURN-wrapped DTLS packets otherwise get
dropped and can make Meet fail after ICE connects. This low-level policy is
version-pinned with the controller and should be revalidated during upgrades.
The HLS encoder is paced with `-re` so Ingress can consume segments before they
are deleted; the stream manager still retries source failures.

## TURN authentication and private networking

LiveKit's native embedded-TURN advertisement uses its private node IP. The init
container adds a public Envoy alias in `rtc.turn_servers` using credentials
accepted by that same embedded server. This is not an external TURN service.
The client consumes the ICE server list from signaling; Meet needs no manual
TURN fields. The test player enforces `iceTransportPolicy: relay`.

The alias uses a randomly generated shared participant identifier and a one-year
credential lifetime, signed using LiveKit's API secret at pod startup. Refresh
it before expiry with `kubectl -n media rollout restart deployment/livekit-server`.
This interrupts active rooms. The shared alias has a 128-allocation quota;
production needs per-participant public-address advertising rather than shared
credentials. Nothing secret is printed or committed: the rendered config lives
in the pod's emptyDir. The API Secret remains mounted read-only.

TURN relay sockets use pod-private ports 40000–40511. Only :3478 is public; the
embedded relay's peer is the co-located SFU. Permissions allow only this pod's
private IP and LiveKit's embedded local media-port policy. RTC candidates remain
private, avoiding Azure public-IP hairpinning.

## Certificate management

The existing namespace-scoped `Issuer/media/livekit-labmad` and Certificate
`media/livekit-labmad` are unchanged. Leaf certificates renew after 60 days of
their 90-day lifetime. The constrained labmad intermediate and root remain as
previously configured; the root signing key stays local. New deployments can
use `manifests/cert-manager-operator` and `scripts/livekit-ca.py` as documented
in those files. The intermediate expires in November 2027 and needs admin renewal.

## Providing the video

The ffmpeg container waits for `/media/video.webm` on the `video-source` PVC
(RWO, Azure Disk). Until the file is present the pod stays Ready (nginx
`/health`) while ffmpeg retries.

**From a URL** (helper Job, not synced by Argo CD):

```bash
sed 's|VIDEO_URL_PLACEHOLDER|https://example.com/my/video.webm|' \
  manifests/video-server/base/81-upload-job-template.yaml | \
  kubectl -n media apply -f -
kubectl -n media wait --for=condition=complete job/video-upload --timeout=300s
```

**Direct copy** of a local file into the PVC: scale the Deployment to 0, `kubectl cp`
into a filler pod mounting the PVC, then scale back to 1 (see prior runbook).

Default ffmpeg args transcode vp9/opus → h264/aac HLS. For h264/aac sources,
edit `manifests/video-server/base/40-configmap-video.yaml` to use `-c copy`.

## Watch the provided video in LiveKit

The Azure deployment continuously publishes the existing file into room
**`video-stream`**, as participant **`video-source`**, displayed as **Provided video**.
The file loops in the existing ffmpeg deployment. New viewers join the current
stream position; this is a shared live broadcast, not independent on-demand playback.

Internal connections (no external DNS, public ingress, or TURN required for the publisher):

```text
video-server.media.svc.cluster.local:8080/hls/index.m3u8
  → LiveKit Ingress (HTTP HLS fetch + transcoding)
  → livekit-server.media.svc.cluster.local:7880 (WS signaling)
  → LiveKit pod UDP media

LiveKit embedded TURN → Envoy UDPRoute :3478 → TURN/UDP → viewers
```

`livekit-redis` provides the internal control bus for the SFU and Ingress.
`livekit-stream` maintains exactly one URL ingress for its configured identity,
recreating it after a completed/failed source. Source HTTP, signaling and Redis
all use internal ClusterIP Services. Ingress joins using direct pod networking;
viewer transport is determined by the viewer's ICE configuration.
No public RTMP/WHIP endpoint, extra LoadBalancer or second PVC mount is used.

### Watch without camera/microphone

From this repository with your admin kubeconfig:

```bash
export KUBECONFIG=~/PoC/azure/demo/auth/kubeconfig
python3 scripts/livekit-token.py token --room video-stream --identity viewer-alice --viewer
```

1. Open https://livekit-test.apps.demo.p225x.azure.redhatworkshops.io/ .
2. Paste that token into **Room token**. Keep the default signaling and TURN fields.
3. Click **Watch room**, then **Enable audio playback** if needed.
4. The video track is the **Provided video** publisher's stream. This viewer
   forces relay-only TURN/UDP and does not request camera/microphone access.

Generate a different token with a unique identity for each viewer; reusing an
identity disconnects the earlier connection. Tokens expire after one hour for
new connections. Do not share your API signing Secret or kubeconfig with viewers.
They only need their room token, URLs, and browser CA trust.

### Invite people using LiveKit Meet

Generate a regular participant token for each person (omit `--viewer` so they
can use camera/mic and chat in Meet):

```bash
python3 scripts/livekit-token.py token --room video-stream --identity guest-bob
python3 scripts/livekit-token.py token --room video-stream --identity guest-carol
```

Send each person their individual token and these instructions:

1. Ensure their browser trusts labmad (see TLS instructions below).
2. Open [LiveKit Meet → Custom](https://meet.livekit.io/?tab=custom).
3. Enter **LiveKit URL**: `wss://livekit.apps.demo.p225x.azure.redhatworkshops.io`.
4. Enter their token in **Token**, then connect. The token selects `video-stream`;
   no LiveKit Cloud account/API key or separate room-name field is needed.
5. Locate **Provided video** in the room. Guests can enable their own mic/camera
   to speak with other participants; use headphones to avoid feedback.

The SFU supplies the public Envoy TURN alias and credentials to Meet. Meet's hosted UI does
not expose an explicit relay-only policy switch; use the test client for a
strict forced-TURN test. Inspect the actual selected candidates in browser
WebRTC diagnostics when using Meet, especially if a viewer can reach cluster
pod addresses through a VPN. ICE may choose a direct path when one is reachable.

To change the source URL or room, edit `manifests/livekit/base/stream.env` and
sync `webrtc-livekit`; for a different room also update the test-page hint.
The manager owns only ingresses with its name/identity in that configured room;
remove the old ingress before changing rooms/identities. Scaling its Deployment
to zero alone does not terminate an already-running ingress. To stop the stream,
scale the manager to zero and delete its ingress using the LiveKit Ingress API.

### Browser trust for labmad

Both LiveKit hosts now present certificates issued by **Brno labmad CA**. Devices
that already trust labmad should connect normally. Other viewers need that
public CA certificate imported into their browser/OS as an authority for websites.
Use the public file `~/PoC/labmad/cert-manager/ca.crt`; never share `ca.key`.

CA SHA-256 fingerprint:
`91:CB:F2:F1:EF:47:F0:79:0B:88:6E:FC:86:09:90:1B:86:6B:BB:A9:BC:E0:FA:7F:30:C2:DD:86:48:37:46:43`

Verify these pages open without certificate warnings before connecting from Meet:

- https://livekit.apps.demo.p225x.azure.redhatworkshops.io/ (plain `OK` response)
- https://livekit-test.apps.demo.p225x.azure.redhatworkshops.io/

Keep **Enable end-to-end encryption** unchecked in Meet; the file ingress is
not an E2EE publisher. HTTPS/WSS and WebRTC transport encryption remain enabled.

### Streaming health

```bash
kubectl -n media get deploy livekit-server livekit-redis livekit-ingress livekit-stream
kubectl -n media logs deploy/livekit-stream --tail=10
kubectl -n media logs deploy/livekit-ingress --tail=30
```

Expect `ENDPOINT_PUBLISHING` in manager logs and both audio/video tracks in the
room. For a repeatable browser test (temporary Playwright environment with
Chromium installed):

```bash
python3 scripts/verify-stream.py --kubeconfig "$KUBECONFIG" \
  --url https://livekit-test.apps.demo.p225x.azure.redhatworkshops.io/ \
  --server-url wss://livekit.apps.demo.p225x.azure.redhatworkshops.io \
  --expected-turn-host "$(kubectl -n media get cm livekit-public-endpoint -o jsonpath='{.data.host}')"
```

Run without `--allow-private-ca` to check the labmad trust chain as well.
The test joins the room from both the player and hosted Meet, verifies selected
TURN/UDP candidates and increasing audio/video RTP bytes and decoded frames,
then closes its sessions. It never logs API keys or room tokens.

 Verify browser inbound RTP bytes and decoded video frames increase over
time, with a relay/UDP selected candidate. HLS input adds segment buffering
latency; the LiveKit output is WebRTC but does not remove that input latency.


## Verification and operations

```bash
kubectl -n media get gateway,httproute,udproute,envoyproxy,svc
kubectl -n media logs deploy/livekit-server --tail=20
kubectl -n media logs deploy/livekit-stream --tail=10
python3 scripts/verify-stream.py --kubeconfig "$KUBECONFIG" \
  --url https://livekit-test.apps.demo.p225x.azure.redhatworkshops.io/ \
  --server-url wss://livekit.apps.demo.p225x.azure.redhatworkshops.io \
  --expected-turn-host "$(kubectl -n media get cm livekit-public-endpoint -o jsonpath='{.data.host}')"
```

The browser check verifies that both the relay-only player and hosted Meet receive
audio/video with increasing RTP bytes and decoded frames, using a UDP relay
candidate whose TURN URL is the Envoy public address. Trust labmad; do not use
`--allow-private-ca` for a certificate-trust test.

## Rollback

The preserved branch has all STUNner manifests and branch-pinned Argo CD sources.
Restoring it requires applying its overlay, restoring its RBAC bootstrap, and
syncing its applications. Disable/prune Envoy resources explicitly after the old
TURN path is healthy. Argo CD automatic pruning remains disabled; removing an
application from the overlay alone does not remove its cluster resources.

## License

Apache-2.0; see LICENSE. Envoy Gateway/Envoy and LiveKit are Apache-2.0.
The TURN credential encoding is compatible with jxskiss/base62 (MIT).
Sources: [Envoy Gateway](https://gateway.envoyproxy.io/),
[Envoy UDP proxy](https://www.envoyproxy.io/docs/envoy/latest/configuration/listeners/udp_filters/udp_proxy.html),
[LiveKit TURN implementation](https://github.com/livekit/livekit/blob/v1.13.11/pkg/service/turn.go).
