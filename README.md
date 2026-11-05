# WebRTC PoC — ffmpeg HLS video server behind a STUNner TURN gateway (OpenShift)

GitOps library managed with Argo CD, following the
[k8s-reference-argocd](https://github.com/rgordill/k8s-reference-argocd) pattern.

This repository is a **reusable library** of Argo CD Applications — not an
app-of-apps. Consume an overlay from your own GitOps root, or apply the azure
overlay directly onto a cluster that already runs OpenShift GitOps.

This project is not affiliated with CNCF, the Linux Foundation, l7mp, or Red Hat.

Licensed under the [Apache License 2.0](LICENSE).

## Library structure

```
applications/webrtc/                 reusable Argo CD Applications
├── helm/                            STUNner operator (Helm, sync-wave 1)
├── video-server/                    ffmpeg + nginx HLS (sync-wave 2)
├── stunner-config/                  GatewayClass/Gateway/UDPRoute (sync-wave 3)
└── overlays/
    └── azure/                       OpenShift on Azure target

manifests/
├── stunner-operator/helm/values.yaml
├── video-server/                    (+ overlays/azure for Ingress host)
└── stunner/                         Gateway API + STUNner CRs
```

## Applications

| Application | Namespace | Sync wave | Description |
|---|---|---|---|
| **stunner-operator** | `stunner-system` | 1 | STUNner Helm chart `2.0.0` (operator + auth + dataplane CR) |
| **webrtc-video-server** | `media` | 2 | ffmpeg → looping HLS; nginx-unprivileged; OpenShift Ingress |
| **webrtc-stunner-config** | `stunner-system` | 3 | GatewayClass, GatewayConfig, TURN-UDP Gateway, UDPRoute → `media/video-server` |

## Prerequisites

* OpenShift cluster with **OpenShift GitOps** installed (`openshift-gitops`).
* Cluster Gateway API CRDs present (OpenShift 4.19+/5.x ships them). The
  STUNner chart is configured with `standardCrds.enabled=false` so it does not
  fight those CRDs.
* Azure LoadBalancer integration (OpenShift IPI / ARO). UDP TURN uses a
  Standard LB with an HTTP health probe on STUNner's `/live` port `8086`.
* `kubectl` aligned with the cluster (prefer kubectl over `oc` for vanilla APIs).

## Deploy (Azure OpenShift)

```bash
export KUBECONFIG=~/PoC/azure/demo/auth/kubeconfig

# Preview
kubectl kustomize applications/webrtc/overlays/azure

# Register the Applications in openshift-gitops
kubectl apply -k applications/webrtc/overlays/azure

kubectl -n openshift-gitops get applications.argoproj.io
kubectl -n media get ingress,deploy,pvc
kubectl -n stunner-system get gateway,udproute,gatewayconfig
```

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

**Direct copy** of a local file into the PVC:

```bash
kubectl -n media run video-filler --rm -i --restart=Never \
  --image=docker.io/curlimages/curl:8.11.0 \
  --overrides='{"spec":{"containers":[{"name":"video-filler","image":"docker.io/curlimages/curl:8.11.0","command":["sh","-c","cat > /data/video.webm && ls -l /data/video.webm"],"volumeMounts":[{"name":"media","mountPath":"/data"}],"stdin":true}],"volumes":[{"name":"media","persistentVolumeClaim":{"claimName":"video-source"}}]}}' \
  < video/free-video.webm
```

Default ffmpeg args transcode vp9/opus → h264/aac HLS. For h264/aac sources,
edit `manifests/video-server/base/40-configmap-video.yaml` to use `-c copy`.

## TURN / WebRTC client config

* ICE server: `turn:<gateway-external-ip>:3478`
* Username / password: `webrtc` / `webrtc` (see `manifests/stunner/gatewayconfig.yaml`)
* Public address: `kubectl -n stunner-system get svc media-gateway`

Player URL (Azure demo):
`https://video-server.apps.demo.pqbt8.azure.redhatworkshops.io/`

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
