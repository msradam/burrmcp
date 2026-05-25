# Spike: tamper-evident ledger + `.app` single-file format

Exploratory prototypes on `spike/ledger-and-app-format`. Not wired into the
shipped package; run `uv run --no-sync python spike/smoke.py` to exercise both.

## 1. Hash-chained tamper-evident ledger (`ledger.py`)

Each event is a JSONL line carrying `prev` (previous line's hash) and `hash`
(sha256 over `prev` + the entry's canonical encoding). `verify_ledger` recomputes
the chain and names the exact line where a recorded hash or prev-link diverges.

**Verdict: works, small, honest.** The smoke test forges a recorded refusal into
an "allowed" and verification flags `line 1: hash mismatch`. This is the real
version of the "tamper-evident audit log" the brand mockup claimed.

**To productionize:**
- Call `HashChainedLedger.append` from the adapter's recording path (next to
  `_record_history` / `_append_refusal_sidecar`), so every step and refusal is
  chained, durable, and verifiable, not just the in-memory history.
- Add `theodosia verify <session>` to the CLI (wraps `verify_ledger`).
- Decide the chain's scope (per-session vs per-project) and whether to sign the
  head with a key for non-repudiation (chain proves integrity, a signature
  proves origin).
- Only then make the claim on the site; until merged, the docs stay silent on it.

## 2. `.app` single-file format (`appfile.py`, `incident.app`)

A `.app` is YAML frontmatter (name, description, optional `upstream`) + a Python
body defining `build_application`. `load_app` splits and execs it; `serve_appfile`
mounts it with the frontmatter as mount config. The smoke test loads
`incident.app`, builds the FSM, and mounts it as MCP server `incident`.

**Verdict: works, nice authoring story.** One portable artifact carries the
workflow and how to serve it, matching the landing page's `incident.app` panel.

**To productionize:**
- CLI: detect a `.app` target in `theodosia serve` / `doctor` and route to the
  loader (alongside the existing `module:attr`).
- Security: loading execs arbitrary Python, so it is a trusted-local-file
  feature. Document that plainly; do not fetch-and-run remote `.app`s.
- Frontmatter schema: settle the keys (`name`, `description`, `upstream`,
  `session_ttl`, ...) and validate them.
- Editor support: a `.app` is Python with a YAML header; a syntax-highlight hint
  (`# python` after the frontmatter) helps.

## Recommendation

Both are feasible and self-contained. The ledger is the higher-value one (it
turns the audit trail into something verifiable and retires the one place the
mockup overclaimed). The `.app` format is a strong DX/marketing artifact. Each
can graduate to `src/theodosia/` behind its own PR with tests; neither is on
`main` yet.
