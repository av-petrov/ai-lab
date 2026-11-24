# argocd-observability: tests and demo.
# Everything except the targets marked [CHANGES THE CLUSTER] is read-only.
# All kubectl calls use your *current* kube context (no --context switching).

ifeq ($(shell command -v uv 2>/dev/null),)
  RUN := .venv/bin/
  PY  := .venv/bin/python
else
  RUN := uv run --extra dev
  PY  := uv run --extra dev python
endif
CLI    = $(if $(findstring uv,$(RUN)),$(RUN) argocd-observe,$(RUN)argocd-observe)
PYTEST = $(if $(findstring uv,$(RUN)),$(RUN) pytest,$(RUN)pytest)

DEMO_APPS := obs-demo-imagepull obs-demo-crashloop
DEMO_OUT  ?= runs/argocd-demo
MODE      ?= auto

.PHONY: help test check observe-all demo-setup demo-wait demo demo-offline demo-teardown

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

test: ## unit tests (no cluster, no LLM)
	$(PYTEST) -q tests

check: ## auth mode, permissions and data sources (read-only, no LLM)
	$(CLI) --check --mode $(MODE)

observe-all: ## LLM health overview of all apps (read-only)
	$(CLI) --all --mode $(MODE)

demo-setup: ## [CHANGES THE CLUSTER] create the two broken demo apps (asks first)
	@echo "This APPLIES deploy/demo/broken-apps.yaml to context: $$(kubectl config current-context)"
	@echo "  - Application argocd/obs-demo-imagepull (bad image tag)"
	@echo "  - Application argocd/obs-demo-crashloop (exits 1)"
	@read -p "Type 'yes' to continue: " ans && [ "$$ans" = yes ]
	kubectl apply -f deploy/demo/broken-apps.yaml

demo-wait: ## wait (read-only polling) until both demo apps show their failure
	@for i in $$(seq 1 40); do \
	  ip=$$(kubectl get pods -n obs-demo-imagepull -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason}' 2>/dev/null); \
	  cl=$$(kubectl get pods -n obs-demo-crashloop -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason}' 2>/dev/null); \
	  echo "imagepull: $${ip:--}   crashloop: $${cl:--}"; \
	  case "$$ip" in *Image*|*ErrImage*) case "$$cl" in *CrashLoop*) exit 0;; esac;; esac; sleep 6; \
	done; echo "timed out waiting (check: kubectl -n argocd get applications $(DEMO_APPS))"; exit 1

demo: ## run the LLM agent on the demo apps and verify both root causes (read-only)
	-$(CLI) $(addprefix --app ,$(DEMO_APPS)) --mode $(MODE) --out-dir $(DEMO_OUT)
	$(PY) scripts/demo_verify.py $(DEMO_OUT)/report.json

demo-offline: ## same, deterministic triage without LLM (read-only)
	-$(CLI) $(addprefix --app ,$(DEMO_APPS)) --mode $(MODE) --no-llm --out-dir $(DEMO_OUT)-offline
	$(PY) scripts/demo_verify.py $(DEMO_OUT)-offline/report.json

demo-teardown: ## [CHANGES THE CLUSTER] delete the demo apps and namespaces (asks first)
	@echo "This DELETES the demo apps and namespaces in context: $$(kubectl config current-context)"
	@read -p "Type 'yes' to continue: " ans && [ "$$ans" = yes ]
	kubectl -n argocd delete application $(DEMO_APPS) --ignore-not-found --wait=true
	kubectl delete namespace $(DEMO_APPS) --ignore-not-found
