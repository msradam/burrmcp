#!/usr/bin/env bash
# Bootstrap a clean Docker-capable box (GitHub Codespace or a CPU VM) to run the
# full o11y-bench investigation category (11 tasks) with Phoebe on Theodosia,
# Kimi K2.6 (FSM) and the raw-tools base.
#
# Prereqs on the box: Docker daemon running, git, curl, Python 3.12+.
# Pass the two keys in the environment before running:
#   TOGETHER_API_KEY=...  ANTHROPIC_API_KEY=...  bash bench_bootstrap.sh
set -euo pipefail

WORK="${WORK:-$HOME/bench}"
NCONCURRENT="${NCONCURRENT:-2}"   # 2 for a 16GB box; 6-8 on a 32GB+ box
mkdir -p "$WORK" && cd "$WORK"

# 1) Tooling: uv + mise
command -v uv   >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
command -v mise >/dev/null || curl -fsSL https://mise.run | sh
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
eval "$(mise activate bash)" 2>/dev/null || true

# 2) Repos: the harness (public), phoebe (the agent), theodosia from PyPI
[ -d o11y-bench ] || git clone --depth 1 https://github.com/grafana/o11y-bench
[ -d phoebe ]     || git clone --depth 1 https://github.com/msradam/phoebe

cd o11y-bench
mise trust -y 2>/dev/null || true
mise install -y 2>/dev/null || true
uv sync
# make phoebe + theodosia importable by the harness venv
uv pip install -e ../phoebe theodosia

# 3) Sanity: docker + the agent import + a one-shot live model check (cheap)
docker info >/dev/null 2>&1 || { echo "FATAL: docker daemon not reachable"; exit 1; }
uv run --no-sync python -c "import phoebe.harbor.agent as a; print('phoebe agent import OK:', a.PhoebeAgent.name())"

# 4) The full investigation category (11 tasks), Pass^3
TASKS=(cache-incident-blast-radius cache-refresh-lag-handoff cache-rollout-trigger-check \
       dependency-outage-false-lead deployment-blast-radius-check incident-triage \
       payment-error-blast-radius payments-path-root-cause retry-backlog-incident \
       service-degradation-rca slow-path-hotspot-correlation)
TASK_FLAGS=(); for t in "${TASKS[@]}"; do TASK_FLAGS+=(--task-name "$t"); done

echo "================ KIMI FSM (Phoebe + Theodosia) full investigation $(date) ================"
OPENAI_API_BASE=https://api.together.xyz/v1 OPENAI_API_KEY="$TOGETHER_API_KEY" \
mise run bench:job -- --model openai/moonshotai/Kimi-K2.6 \
  --agent-import-path phoebe.harbor:PhoebeAgent --reasoning-effort off \
  --n-attempts 3 --n-concurrent "$NCONCURRENT" --job-name kimi-inv-full-fsm "${TASK_FLAGS[@]}"

echo "================ KIMI base (raw tools) full investigation $(date) ================"
OPENAI_API_BASE=https://api.together.xyz/v1 OPENAI_API_KEY="$TOGETHER_API_KEY" \
mise run bench:job -- --model openai/moonshotai/Kimi-K2.6 \
  --agent-import-path agents.o11y_agent:O11yBenchAgent --reasoning-effort off \
  --n-attempts 3 --n-concurrent "$NCONCURRENT" --job-name kimi-inv-full-base "${TASK_FLAGS[@]}"

echo "================ DONE $(date) ================"
python3 - <<'PY'
import json, glob
for j in ("kimi-inv-full-fsm", "kimi-inv-full-base"):
    try:
        d = json.load(open(f"jobs/{j}/result.json"))
        v = list(d["stats"]["evals"].values())[0]
        print(f"{j}: mean {round(v['metrics'][0]['mean'],4)}  ({v['n_trials']} trials, {v['n_errors']} errors)")
    except Exception as e:
        print(j, "no result:", e)
PY
