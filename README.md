# WebRTC PoC — ffmpeg HLS video server behind a STUNner TURN gateway (OpenShift)

GitOps library managed with Argo CD, following the
[k8s-reference-argocd](https://github.com/rgordill/k8s-reference-argocd) pattern
and the o13y-demo cluster-domain discovery approach.

This repository is a **reusable library** of Argo CD Applications — not an
app-of-apps. Consume an overlay from your own GitOps root, or apply the azure
overlay directly onto a cluster that already runs OpenShift GitOps.

This project is not affiliated with CNCF, the Linux Foundation, l7mp, or Red Hat.

Licensed under the [Apache License 2.0](LICENSE).

## Library structure

```
applications/webrtc/
├── cluster-config/          discover ingress domain (sync-wave 0)
├── helm/                    STUNner operator (sync-wave 1)
├── video-server/            ApplicationSet → ffmpeg HLS (sync-wave 2)
├── stunner-config/          GatewayClass/Gateway/UDPRoute (sync-wave 3)
└── overlays/azure/

manifests/
├── cluster-config/          PostSync Job → ConfigMap + cluster Secret
├── stunner-operator/helm/values.yaml
├── video-server/base/       Ingress host is PLACEHOLDER (patched by AppSet)
└── stunner/
```

## Applications

| Application | Namespace | Sync wave | Description |
|---|---|---|---|
| **webrtc-cluster-config** | `openshift-gitops` | 0 | Discovers `ingresses.config.openshift.io/cluster` domain into ConfigMap `cluster-ingress-domain` and Argo CD cluster Secret |
| **stunner-operator** | `stunner-system` | 1 | STUNner Helm chart `2.0.0` |
| **webrtc-video-server** | `media` | 2 | ApplicationSet patches Ingress to `video-server.<ingress-domain>` |
| **webrtc-stunner-config** | `stunner-system` | 3 | GatewayClass, GatewayConfig, TURN-UDP Gateway, UDPRoute |

## Ingress domain (no hardcoded hosts)

Same pattern as o13y-demo / virt-loadtest:

1. `webrtc-cluster-config` PostSync Job reads the cluster ingress domain.
2. Writes:
   - ConfigMap `openshift-gitops/cluster-ingress-domain` (`data.domain`)
   - Secret `openshift-gitops/openshift-gitops-cluster-configuration` with annotation `ingress-domain` (Argo CD cluster secret for ApplicationSets)
3. `webrtc-video-server` ApplicationSet selects that cluster and patches the Ingress host to `video-server.{{ingress-domain}}`.

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

## TURN / WebRTC client config

* ICE server: `turn:<gateway-external-ip>:3478`
* Username / password: `webrtc` / `webrtc` (see `manifests/stunner/gatewayconfig.yaml`)
* Public address: `kubectl -n stunner-system get svc media-gateway`

Verified on OpenShift 5.0 (Azure): Ingress admits only when `spec.tls` is omitted
(edge termination via `route.openshift.io/*` annotations + cluster wildcard cert).

## Production notes

* No root: containers use `restricted-v2`-compatible securityContext.
* Prefer Ingress (`openshift-default`) over OpenShift Route objects.
* Pin `targetRevision` to a commit/tag in production instead of `HEAD`.
* Rotate static TURN credentials or switch `authType` to ephemeral.

## License & Attribution

* This repository: Apache-2.0
* STUNner (l7mp): MIT — https://github.com/l7mp/stunner
* ffmpeg image (`jrottenberg/ffmpeg`): see upstream image licensing
* nginx-unprivileged: BSD-2-Clause
