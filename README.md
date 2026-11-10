# WebRTC PoC — LiveKit A/V through STUNner TURN (Azure OpenShift)

GitOps library managed with Argo CD, following the
[k8s-reference-argocd](https://github.com/rgordill/k8s-reference-argocd) pattern
and the o13y-demo cluster-domain discovery approach.

This repository is a **reusable library** of Argo CD Applications — not an
app-of-apps. Consume an overlay from your own GitOps root, or apply the azure
overlay directly onto a cluster that already runs OpenShift GitOps.

This project is not affiliated with CNCF, the Linux Foundation, l7mp, or Red Hat.

Licensed under the [Apache License 2.0](LICENSE).

The WebRTC path is browser → STUNner TURN/UDP → LiveKit pod media (UDP 7882).
Signaling uses WSS through the OpenShift ingress. The existing nginx/ffmpeg
HLS player remains available independently: HTTP HLS traffic does not traverse TURN.
This uses TURN relay, rather than STUN-only NAT discovery.

## Library structure

```
applications/webrtc/
├── cluster-config/          discover ingress domain (sync-wave 0)
├── helm/                    STUNner operator (sync-wave 1)
├── video-server/            ApplicationSet → ffmpeg HLS (sync-wave 2)
├── livekit/                 ApplicationSet → SFU + relay-only test client (wave 2)
├── stunner-config/          GatewayClass/Gateway/UDPRoute (sync-wave 3)
└── overlays/azure/

manifests/
├── cluster-config/          PostSync Job → ConfigMap + cluster Secret
├── stunner-operator/helm/values.yaml
├── video-server/base/       Ingress host is PLACEHOLDER (patched by AppSet)
├── livekit/base/            Single-node SFU, test page, signaling ingress
└── stunner/
```

## Applications

| Application | Namespace | Sync wave | Description |
|---|---|---|---|
| **webrtc-cluster-config** | `openshift-gitops` | 0 | Discovers `ingresses.config.openshift.io/cluster` domain into ConfigMap `cluster-ingress-domain` and Argo CD cluster Secret |
| **stunner-operator** | `stunner-system` | 1 | STUNner Helm chart `2.0.0` |
| **webrtc-video-server** | `media` | 2 | ApplicationSet patches Ingress to `video-server.<ingress-domain>` |
| **webrtc-livekit** | `media` | 2 | ApplicationSet patches `livekit` and `livekit-test` ingress hosts |
| **webrtc-stunner-config** | `stunner-system` | 3 | GatewayClass, GatewayConfig, TURN-UDP Gateway, UDPRoute |

## Ingress domain (no hardcoded hosts)

Same pattern as o13y-demo / virt-loadtest:

1. `webrtc-cluster-config` PostSync Job reads the cluster ingress domain.
2. Writes:
   - ConfigMap `openshift-gitops/cluster-ingress-domain` (`data.domain`)
   - Secret `openshift-gitops/openshift-gitops-cluster-configuration` with annotation `ingress-domain` (Argo CD cluster secret for ApplicationSets)
3. The video-server and LiveKit ApplicationSets select that cluster and patch their ingress hosts using `{{ingress-domain}}`.

## Prerequisites

* OpenShift cluster with **OpenShift GitOps** installed (`openshift-gitops`).
* Cluster Gateway API CRDs present (OpenShift 4.19+/5.x ships them). The
  STUNner chart is configured with `standardCrds.enabled=false` so it does not
  fight those CRDs.
* Azure LoadBalancer integration (OpenShift IPI / ARO). UDP TURN uses a
  Standard LB with an HTTP health probe on STUNner's `/live` port `8086`.
* `kubectl` aligned with the cluster.

## Deploy (Azure OpenShift)

```bash
export KUBECONFIG=~/PoC/azure/demo/auth/kubeconfig

# Create API signing keys once; the helper preserves existing keys.
# Secret is intentionally outside Git/Argo and must be backed up separately.
python3 scripts/livekit-token.py bootstrap

# Admin bootstrap: scoped controller permissions for workload reconciliation.
kubectl apply -k manifests/gitops-rbac

kubectl kustomize applications/webrtc/overlays/azure
kubectl apply -k applications/webrtc/overlays/azure

# Domain discovery (after wave 0 sync)
kubectl -n openshift-gitops get cm cluster-ingress-domain
kubectl -n openshift-gitops get secret openshift-gitops-cluster-configuration \
  -o jsonpath='{.metadata.annotations.ingress-domain}{"\n"}'

kubectl -n openshift-gitops get applications.argoproj.io,applicationsets.argoproj.io
kubectl -n media get ingress,deploy,pvc
kubectl -n stunner-system get gateway,udproute,gatewayconfig
```

Player URL is always `https://video-server.<ingress-domain>/` — never baked into git.

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

## Join a real A/V call over TURN/UDP

1. Open `https://livekit-test.<ingress-domain>/` in two tabs, ideally on two
   devices with headphones. The SDK loads from a pinned jsDelivr URL; clients
   need access to that CDN. Camera/mic need a secure context and permission.
2. Mint a separate one-hour token for each tab, using the same room name:

   ```bash
   python3 scripts/livekit-token.py token --room turn-demo --identity alice
   python3 scripts/livekit-token.py token --room turn-demo --identity bob
   ```

3. Paste the tokens into the corresponding tabs. The signaling URL defaults to
   `wss://livekit.<ingress-domain>`. TURN defaults to
   `turn:135.237.110.232:3478?transport=udp`, username/password `webrtc` / `webrtc`.
   If the Azure LB address changes, update `manifests/livekit/base/livekit.yaml`
   and the default in `index.html`, then sync. Confirm the address with
   `kubectl -n stunner-system get svc media-gateway`.
4. Click **Join with camera/mic** in each tab. The client passes
   `rtcConfig.iceTransportPolicy: 'relay'` to `Room.connect`, with only the
   STUNner TURN/UDP server. Both publishing and subscribing use that policy.
   Click **Enable audio playback** if the browser blocks remote audio.
5. In `chrome://webrtc-internals` or Firefox `about:webrtc`, inspect **all**
   active media transports (LiveKit may use a single PeerConnection for both directions): the selected local candidate must be
   `relay`, use `udp` (`relayProtocol` where available), and point at the
   STUNner TURN server. Confirm increasing outbound/inbound RTP bytes and
   encoded/decoded frames while the other tab plays video/audio. A successful
   ICE allocation or WSS connection alone is not an A/V test.

The SFU advertises its private pod IP and multiplexes media on UDP 7882.
STUNner resolves the `media/livekit-server` Service endpoints and relays to
those pods; a public media LoadBalancer or host networking is unnecessary.
`rtc.turn_servers` also advertises STUNner to standard LiveKit clients, including
[LiveKit Meet custom connections](https://meet.livekit.io/?tab=custom).
Use this repository's test page for an explicit relay-only configuration.

### TLS trust on the Azure lab

The test page and WSS signaling use the existing OpenShift wildcard certificate.
The current Azure lab presents a private/self-signed CA chain. Trust the cluster
router CA in your browser/OS, or configure a publicly trusted ingress certificate,
before testing. Check both HTTPS hosts without certificate errors. Merely
clicking through a warning may not make camera access or WSS work. This repository
does not disable TLS verification for clients.

### GitOps controller permissions

`manifests/gitops-rbac` is a one-time admin bootstrap, outside the applications it
permits. It grants the standard OpenShift GitOps controller CRUD on ConfigMaps,
Services, Deployments and Ingresses in `media`, and Gateway/GatewayConfig/UDPRoute
resources in `stunner-system`. Cluster-scoped permissions allow GatewayClass
creation and updates/deletion of `stunner-gatewayclass` only (Kubernetes cannot
restrict create by resource name). It does not grant access to LiveKit API-key
Secrets. Apply this bootstrap with an admin identity before syncing. Adjust the
ServiceAccount subject if using a different Argo CD instance.

### GitOps sync and migration

After pushing this repository, apply the Azure overlay to create the new
ApplicationSet, then sync `webrtc-livekit` and `webrtc-stunner-config` in Argo CD.
The library retains the existing `selfHeal` policy without automatic pruning.
For an upgrade, remove the obsolete HTTP backend route after the new route is
accepted (already done on this Azure cluster):

```bash
kubectl -n stunner-system get udproute livekit-server -o yaml
kubectl -n stunner-system delete udproute video-server --ignore-not-found
kubectl -n media rollout status deployment/livekit-server
kubectl -n media rollout status deployment/livekit-test
```

Kustomize hashes config names so configuration changes roll pods. Argo CD may
show old generated ConfigMaps as extraneous until explicitly pruned.

### Repeatable media verification

The optional Playwright check uses synthetic camera/mic sources, joins two
participants, and asserts relay/UDP candidates, increasing inbound/outbound
RTP bytes for audio and video, and encoded/decoded video frames:

```bash
# In a temporary virtual environment with playwright + Chromium installed:
python3 scripts/verify-turn.py --kubeconfig "$KUBECONFIG" \
  --url "https://livekit-test.<ingress-domain>/"
```

For this lab's private certificate only, add `--allow-private-ca`. This accepts
certificate errors in the temporary test browser; normal clients still need
proper CA trust. Test tokens are fetched via the local helper and never logged.

### Optional file publishing

The existing `/media/video.webm` remains an HLS source only. Publishing that file
into the LiveKit room requires an additional LiveKit Ingress deployment with
Redis and WHIP/RTMP configuration; it is not included in this single-node slice.
The camera/mic call above is the full TURN A/V loop implemented here.

Sources: [STUNner LiveKit example](https://github.com/l7mp/stunner/tree/main/docs/examples/livekit),
[LiveKit server configuration](https://github.com/livekit/livekit/blob/v1.13.11/config-sample.yaml),
[JavaScript connection options](https://github.com/livekit/client-sdk-js/blob/v2.23.0/src/options.ts).

## Production notes

* No root: containers use `restricted-v2`-compatible securityContext.
* Prefer Ingress (`openshift-default`) over OpenShift Route objects.
* Pin `targetRevision` to a commit/tag in production instead of `HEAD`.
* Rotate static TURN credentials or switch `authType` to ephemeral.
* LiveKit is a single-replica PoC without Redis, persistence, or HA; Recreate
  rollouts interrupt active calls. Add Redis and a distributed design before scaling.
* API signing keys live only in `media/livekit-keys`; restrict access to that
  Secret. A production token service must authenticate users before issuing tokens.
* LiveKit may warn about the node UDP receive buffer. Tune node socket buffers
  for production throughput; this PoC does not modify node sysctls.

## License & Attribution

* This repository: Apache-2.0
* STUNner (l7mp): MIT — https://github.com/l7mp/stunner
* LiveKit server and JS client: Apache-2.0 — https://github.com/livekit
* ffmpeg image (`jrottenberg/ffmpeg`): see upstream image licensing
* nginx-unprivileged: BSD-2-Clause
