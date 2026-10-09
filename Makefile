# Taltempla: the single entry point. `make` (= make help) lists the targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help
.ONESHELL:
.SILENT:

# Overridable on the command line, e.g. make run CAP_RUN=1 GATE=auto
MODEL       ?= deepseek-v4-pro
EFFORT      ?= high
CAP_BUILD   ?= 0.60
CAP_RUN     ?= 2.00
CAP_SESSION ?= 5.00
CAP_TOTAL   ?= 20.00
GATE        ?= ask
SHED_URL    ?= http://127.0.0.1:7700

export TALTEMPLA_MODEL       := $(MODEL)
export TALTEMPLA_EFFORT      := $(EFFORT)
export TALTEMPLA_CAP_BUILD   := $(CAP_BUILD)
export TALTEMPLA_CAP_RUN     := $(CAP_RUN)
export TALTEMPLA_CAP_SESSION := $(CAP_SESSION)
export TALTEMPLA_CAP_TOTAL   := $(CAP_TOTAL)
export TALTEMPLA_GATE        := $(GATE)
export TALTEMPLA_SHED_URL    := $(SHED_URL)

IMAGE     := localhost/taltempla-shed:latest
CONTAINER := taltempla-shed
VOLUME    := taltempla-data
FRANK     := $(CURDIR)/.frank
WORKSPACE := $(CURDIR)/workspace
N         ?= 200
# Recipes call sub-makes only through SUBMAKE. With .ONESHELL, a recipe that names the MAKE variable directly
# executes in full even under make -n (a dry run of toolshed-up or demo-reset would really run).
SUBMAKE    = $(MAKE) --no-print-directory
# Dev mounts: the server, the SDK and the shared LLM client run from the host tree, so a restart picks up
# new code without an image build. llm.py goes OUTSIDE the read-only server mount: a nested file mount
# would make an empty toolshed/server/shed/llmclient.py on the host (it breaks the host import).
# CHEF_* and SHED_TOOL_* (role model/effort overrides) are the only host variables that go in.
SHED_RUN  := -p 127.0.0.1:7700:7700 -v $(VOLUME):/data -v $(WORKSPACE):/work -v $(FRANK)/run:/run/meter \
	-v $(CURDIR)/toolshed/server:/opt/shed/server:ro -v $(CURDIR)/toolshed/sdk:/opt/shed/sdk:ro \
	-v $(CURDIR)/cli/taltempla/llm.py:/opt/shed/cli/taltempla/llm.py:ro \
	-e PYTHONPATH=/opt/shed/server:/opt/shed/sdk:/opt/shed/cli -e 'CHEF_*' -e 'SHED_TOOL_*' \
	--pids-limit 2048 --memory 4g

.PHONY: help deps doctor image toolshed-up toolshed-restart toolshed-down toolshed-reset toolshed-shell toolshed-logs \
	    run shed cost \
	    rollback history test m1 lint fmt lock spike-shed demo-reset clean

help: ## List the targets (default)
	echo "Taltempla targets (variables: MODEL EFFORT CAP_BUILD CAP_RUN CAP_SESSION CAP_TOTAL GATE, e.g. make run CAP_RUN=1):"
	grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "} {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

deps: ## Print the apt line for the system packages; install uv if missing; uv sync
	echo "System packages (run once): sudo apt install -y podman passt uidmap"
	command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
	uv sync

doctor: ## Run the preflight checks (each failure prints the fix)
	fail=0
	ok()  { printf '  \033[32mok\033[0m   %s\n' "$$1"; }
	bad() { printf '  \033[31mFAIL\033[0m %s\n         fix: %s\n' "$$1" "$$2"; fail=1; }
	command -v uv >/dev/null && ok "uv $$(uv --version | cut -d' ' -f2)" \
	    || bad "uv is missing" "make deps (or: curl -LsSf https://astral.sh/uv/install.sh | sh)"
	if command -v podman >/dev/null; then
	    [ "$$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] && ok "podman rootless" \
	        || bad "podman is not rootless" "run make as your normal user, not root"
	else bad "podman is missing" "sudo apt install -y podman passt uidmap"; fi
	grep -q "^$$(id -un):" /etc/subuid 2>/dev/null && ok "subuid entry for $$(id -un)" \
	    || bad "no /etc/subuid entry for $$(id -un)" "sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $$(id -un)"
	grep -qE '^[[:space:]]*(export[[:space:]]+)?OAI_COMPATIBLE_KEY=.+' .env 2>/dev/null && ok ".env has OAI_COMPATIBLE_KEY" \
	    || bad ".env has no OAI_COMPATIBLE_KEY" "ask the operator for .env (OAI_COMPATIBLE_KEY=...) in $(CURDIR)"
	podman image exists $(IMAGE) 2>/dev/null && ok "image $(IMAGE)" || bad "image $(IMAGE) is missing" "make image"
	curl -fsS --max-time 3 $(SHED_URL)/health >/dev/null 2>&1 && ok "toolshed answers $(SHED_URL)/health" \
	    || bad "toolshed does not answer $(SHED_URL)/health" "make toolshed-up (logs: make toolshed-logs)"
	exit $$fail

image: ## Build the toolshed image (tiers + uv inside)
	podman build -f toolshed/Containerfile -t $(IMAGE) .

toolshed-up: ## Start the toolshed (idempotent; builds the image if missing; recreates it on a new image, mounts or role env; keeps data)
	podman image exists $(IMAGE) || $(SUBMAKE) image || exit 1
	mkdir -p $(FRANK)/run $(WORKSPACE)/in $(WORKSPACE)/out && chmod 700 $(FRANK) $(FRANK)/run
	[ -s $(FRANK)/admin.token ] || (umask 077; head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' > $(FRANK)/admin.token)
	chmod 600 $(FRANK)/admin.token
	fp=$$( { printf '%s\n' "$(SHED_RUN)"; podman image inspect -f '{{.Id}}' $(IMAGE); env | grep -E '^(CHEF|SHED_TOOL)_' | sort; } \
	    | sha256sum | cut -c1-16)
	if podman container exists $(CONTAINER) && [ "$$(podman inspect -f '{{index .Config.Labels "taltempla.fp"}}' $(CONTAINER) 2>/dev/null)" != "$$fp" ]; then
	    echo "toolshed: new image, mounts or role env; recreating the container (the data volume stays)"
	    podman rm -f -t 5 $(CONTAINER) >/dev/null
	fi
	if podman container exists $(CONTAINER); then
	    [ "$$(podman inspect -f '{{.State.Running}}' $(CONTAINER))" = true ] || podman start $(CONTAINER) >/dev/null
	else
	    SHED_ADMIN_TOKEN="$$(cat $(FRANK)/admin.token)" podman run -d --name $(CONTAINER) --label taltempla.fp=$$fp \
	        $(SHED_RUN) -e SHED_ADMIN_TOKEN $(IMAGE) >/dev/null || exit 1
	fi
	for i in $$(seq 1 40); do curl -fsS --max-time 2 $(SHED_URL)/health >/dev/null 2>&1 && break; sleep 0.5; done
	curl -fsS --max-time 3 $(SHED_URL)/health && echo || { echo "toolshed did not answer; see make toolshed-logs"; exit 1; }

toolshed-restart: ## Restart the toolshed to load new server/SDK code from the dev mounts (keeps data)
	podman container exists $(CONTAINER) || { echo "no toolshed container; run: make toolshed-up"; exit 1; }
	podman stop -t 5 $(CONTAINER) >/dev/null || exit 1
	# pasta releases the published port a moment after stop; retry the start until it binds
	for i in $$(seq 1 10); do podman start $(CONTAINER) >/dev/null 2>&1 && break; sleep 0.5; done
	for i in $$(seq 1 40); do curl -fsS --max-time 2 $(SHED_URL)/health >/dev/null 2>&1 && break; sleep 0.5; done
	curl -fsS --max-time 3 $(SHED_URL)/health && echo || { echo "toolshed did not answer; see make toolshed-logs"; exit 1; }

toolshed-down: ## Stop the toolshed (keeps the data)
	podman container exists $(CONTAINER) && podman stop -t 5 $(CONTAINER) >/dev/null; echo "toolshed stopped"

toolshed-reset: ## Delete the toolshed container and ALL its data (registry volume)
	podman rm -f $(CONTAINER) >/dev/null 2>&1; podman volume rm -f $(VOLUME) >/dev/null 2>&1; echo "toolshed reset (container and volume removed)"

toolshed-shell: ## Open a shell in the toolshed as the tool user (clean env)
	podman exec -it -u tool -w /tmp $(CONTAINER) env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp TERM=$${TERM:-xterm} LANG=C.UTF-8 PYTHONPATH=/opt/shed/sdk bash

toolshed-logs: ## Show the toolshed logs (N=200 lines)
	podman logs --tail $(N) $(CONTAINER)

run: ## Start the CLI (starts the toolshed if down, runs doctor first)
	$(SUBMAKE) toolshed-up >/dev/null || exit 1
	$(SUBMAKE) doctor || exit 1
	uv run taltempla

shed: toolshed-up ## Print the registry
	uv run taltempla shed

cost: ## Print the ledger by run, role and tool
	uv run taltempla cost

history: toolshed-up ## Print the version and event history of a tool (T=name)
	[ -n "$(T)" ] || { echo "usage: make history T=<tool>"; exit 1; }
	uv run taltempla history "$(T)"

rollback: toolshed-up ## Roll back one tool (T=name [V=version], default: the previous version)
	[ -n "$(T)" ] || { echo "usage: make rollback T=<tool> [V=<version>]"; exit 1; }
	uv run taltempla rollback "$(T)" $(V)

test: ## uv run pytest (unit + contract tests), then the in-container selftest if the toolshed runs
	uv run pytest || exit 1
	if [ "$$(podman inspect -f '{{.State.Running}}' $(CONTAINER) 2>/dev/null)" = true ]; then
	    podman exec $(CONTAINER) python3 -m shed.selftest
	else echo "toolshed not running: skipped the selftest (make toolshed-up && make test)"; fi

m1: toolshed-up ## M1: a chained fixture run (temp DB) whose sub-tool calls shed.llm through the meter
	uv run python spikes/m1_chain.py

lint: ## uv run ruff check
	uv run ruff check .

fmt: ## uv run ruff format
	uv run ruff format .

lock: ## uv lock
	uv lock

spike-shed: ## The container networking and setpriv spike
	bash spikes/spike_shed.sh $(IMAGE)

demo-reset: ## Back up, then clear the registry, ledger and permissions for a clean recording
	ts=$$(date +%Y%m%d-%H%M%S); dir=$(FRANK)/backup-$$ts; mkdir -p "$$dir" && chmod 700 $(FRANK) "$$dir"
	for f in ledger.db ledger.db-wal ledger.db-shm permissions.json history; do [ -e $(FRANK)/$$f ] && cp -a $(FRANK)/$$f "$$dir"/; done
	if [ -n "$$(ls -A $(WORKSPACE)/out 2>/dev/null)" ]; then  # a fresh session must not read old outputs
	    mkdir -p "$$dir/out" && podman unshare find $(WORKSPACE)/out -mindepth 1 -maxdepth 1 -exec mv -t "$$dir/out" {} + \
	        || echo "warning: some files in workspace/out did not move"
	fi
	if podman container exists $(CONTAINER); then
	    [ "$$(podman inspect -f '{{.State.Running}}' $(CONTAINER))" = true ] || podman start $(CONTAINER) >/dev/null
	    podman exec $(CONTAINER) sqlite3 /data/shed.db .dump > "$$dir/shed.sql" || echo "warning: DB dump failed"
	fi
	podman volume exists $(VOLUME) && { podman stop -t 5 $(CONTAINER) >/dev/null 2>&1; podman volume export $(VOLUME) -o "$$dir/shed-data.tar"; }
	$(SUBMAKE) toolshed-reset
	rm -f $(FRANK)/ledger.db $(FRANK)/ledger.db-wal $(FRANK)/ledger.db-shm $(FRANK)/permissions.json
	echo "backup in $$dir; registry, ledger, permissions and workspace/out cleared (make toolshed-up to start fresh)"

clean: ## Remove .venv, caches and build output (keeps the data)
	rm -rf .venv .pytest_cache .ruff_cache build dist
	find . -path ./.git -prune -o -path ./.frank -prune -o -path ./workspace -prune -o -name __pycache__ -type d -print0 | xargs -0 rm -rf
	echo "clean (data in .frank/, workspace/ and the $(VOLUME) volume kept)"
