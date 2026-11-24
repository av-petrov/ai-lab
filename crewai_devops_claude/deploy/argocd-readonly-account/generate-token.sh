#!/usr/bin/env bash
# Generate an API token for the read-only `sre-observer` account. Run it yourself: it logs in as admin
# (password prompt) and creates a token, which is a change to Argo CD state.
# On kind-dev01 the GitOps equivalent is `make argocd-observer-token` in k8s-lab/kind.
#
#   terminal 1:  kubectl -n argocd port-forward svc/argocd-server 18081:80   (or: make argocd-ui in k8s-lab/kind)
#   terminal 2:  deploy/argocd-readonly-account/generate-token.sh
#
# server.insecure=true -> plain HTTP behind the port-forward, hence --plaintext. --skip-test-tls is required:
# without it `argocd login` first sends a TLS probe, the plain-HTTP server resets it, and kubectl drops the
# whole port-forward ("lost connection to pod").
set -euo pipefail
SERVER="${ARGOCD_SERVER:-localhost:18081}"
ACCOUNT="${ACCOUNT:-sre-observer}"
argocd login "$SERVER" --plaintext --skip-test-tls --username admin     # prompts for the admin password
argocd account get --account "$ACCOUNT" --server "$SERVER" --plaintext >/dev/null
TOKEN="$(argocd account generate-token --account "$ACCOUNT" --id "argocd-observability-$(date +%Y%m%d)" \
  --expires-in 720h --server "$SERVER" --plaintext)"
# verify with the new token: the account must be able to read, and must NOT be able to sync
argocd account can-i get applications '*/*' --auth-token "$TOKEN" --server "$SERVER" --plaintext
argocd account can-i sync applications '*/*' --auth-token "$TOKEN" --server "$SERVER" --plaintext
echo
echo "Add to .env (never commit it):"
echo "ARGOCD_OBS_AUTH_MODE=argocd"
echo "ARGOCD_SERVER=$SERVER"
echo "ARGOCD_PLAINTEXT=true"
echo "ARGOCD_AUTH_TOKEN=<the token printed below>"
echo "$TOKEN"
