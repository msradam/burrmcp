# examples

Runnable Burr Applications wired through Theodosia's `mount()`. Every
file in this directory is an FSM that drives an MCP server when served
with `theodosia serve <module>:build_application --app-dir examples`.

**New here? Start with [CURATED.md](CURATED.md).** It picks six demos
that cover Theodosia's breadth without overlap and gives you a learning
path.

The rest of the directory keeps the full catalog. Each `.py` file's
module docstring explains what it shows. Group by what they exercise:

## Pure FSM, no external dependencies

`coffee_order` (canonical), `triage`, `adventure`, `chargen`,
`local_shell`, `incident_response`, `subgraphs`, `ml_training`,
`streaming_narrate`, `security_audit`, `differential_review`,
`fp_check`, `webapp_testing`, `typed_state_loan`, `pipeline_hooks`,
`async_hooks`, `streaming_hooks`, `custom_telemetry`, `state_forking`,
`subgraph_composition`, `class_action`, `pydantic_actions`,
`partition_key_tenants`.

## Shellout / deterministic tooling

`unix_health`, `codebase_security`, `git_review`, `codebase_audit`.

## LLM in the graph (need a local model runtime)

`granite_oncall`, `adaptive_crag`, `mellea_qiskit_migration`,
`granite_guardian`.

## External-library coexistence

`hamilton_features`, `burr_map_parallel`, `sqlite_persister`,
`async_persister`.

## Composed-use showcases

`combinatoric_testing`, `combo_testing`, `multi_graph`.

## RAG / parallel

`parallel_research`.

## Caller-LLM / user-in-the-loop (FastMCP Context-driven)

`caller_sample`, `elicit_confirm`, `doc_coauthoring`.

## Observability

`with_otel`, `with_middleware`, `trace_decorator`, `full_logger`.

## Transport examples (not narrative demos)

`http_serve`, `sse_serve`.

## Other directories

- `data/` — sample inputs shipped with the demos that read real data.
- `scripts/` — `demo_walk.py`, `demo_play.py`, and the recorded trace
  the README's `demo.gif` plays back.
- `skills/` — seven Claude Code SKILLs pulled verbatim with attribution.
  Four are converted to FSM demos (`security_audit`,
  `differential_review`, `fp_check`, `webapp_testing`).
- `*.example.json` — canonical `.mcp.json` configs for Claude Code,
  IBM Bob, and mcphost.
