#!/usr/bin/env bash
# Write a SEPARATE kubeconfig for the read-only ServiceAccount (your ~/.kube/config is not touched).
# Uses the cluster/CA of your *current* context. Requires deploy/kube-readonly/rbac.yaml applied.
#   deploy/kube-readonly/make-kubeconfig.sh  [out-file]   then:  ARGOCD_OBS_KUBECONFIG=<out-file>
set -euo pipefail
OUT="${1:-.kube/argocd-observer.kubeconfig}"
CTX="$(kubectl config current-context)"
CLUSTER="$(kubectl config view --minify -o jsonpath='{.contexts[0].context.cluster}')"
SERVER="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
CA="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
TOKEN="$(kubectl -n argocd create token argocd-observer --duration 24h)"
mkdir -p "$(dirname "$OUT")"
umask 077
cat > "$OUT" <<KCFG
apiVersion: v1
kind: Config
clusters:
- name: ${CLUSTER}
  cluster: {server: "${SERVER}", certificate-authority-data: "${CA}"}
users:
- name: argocd-observer
  user: {token: "${TOKEN}"}
contexts:
- name: argocd-observer@${CLUSTER}
  context: {cluster: "${CLUSTER}", user: argocd-observer}
current-context: argocd-observer@${CLUSTER}
KCFG
echo "wrote $OUT (from context $CTX, token valid 24h)"
echo "check: KUBECONFIG=$OUT kubectl auth can-i get secrets -A   # expect: no"
