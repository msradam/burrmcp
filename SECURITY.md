# Security

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub's security advisory
form for this repository (Security tab, "Report a vulnerability") or by email to
msrahmanadam@gmail.com. Please do not open a public issue for security reports.

## Scope and trust model

BurrMCP is an adapter, not a sandbox. It enforces which Burr actions are
reachable from the current state and records every step. It does not isolate the
action bodies, the agent, or the upstream servers from each other. The following
are the responsibility of the operator deploying a mounted server.

### The agent is untrusted; the transition layer is the control

The connecting agent chooses which action to run. BurrMCP refuses actions that
are not reachable from the current state (`invalid_transition`) and surfaces
action-body failures as structured refusals. This constrains the reachable
action set; it does not make the agent's choice safe on its own. Gate
destructive actions behind explicit transitions and action-body checks, the same
way the demos do.

### Upstream MCP servers are a trust boundary

With the `upstream` feature, a Burr action calls tools on other MCP servers, so
BurrMCP acts as an MCP client. Treat upstream targets as operator-configured and
trusted:

- **No SSRF protection.** Upstream transports are not validated against private,
  loopback, or cloud-metadata address ranges in this release. Do not point
  `upstream` at addresses derived from untrusted input.
- **Tool descriptions reach the agent.** An upstream server's tool metadata is
  visible to the connecting agent and is not sanitized. A malicious upstream
  server can attempt prompt injection through it.
- **No credential forwarding.** BurrMCP does not forward the connecting client's
  credentials to upstream servers. Configure upstream auth explicitly per server.

### Tracker logs contain full state

When a `LocalTrackingClient` is wired, every step writes state and action inputs
to JSONL under `~/.burr` and mirrors them at `burr://trace`. These files are
unencrypted and readable by anyone with filesystem access. If state or inputs
carry secrets, treat the tracker directory as sensitive and restrict its
permissions.

### Sessions are isolation, not authentication

The per-session store keys on the MCP session id. Sessions separate one client's
state from another's; they are not an authentication mechanism. Put
authentication and authorization in front of the server.

### Resource limits

The session store evicts by TTL and max-size (`session_ttl_seconds`,
`max_sessions`), and actions can be bounded with `action_timeout_seconds`. Set
these for any multi-client deployment.

### Shell and subprocess demos

Demos such as `local_shell`, `unix_health`, `git_review`, and `codebase_security`
run real subprocesses. They run with the privileges of the server process and are
not sandboxed. They are examples, not hardened components.

## Durability

BurrMCP provides checkpointing and replay through Burr's persisters and tracker.
It does not provide durable execution: there is no crash supervisor, no
exactly-once guarantee, and no automatic deduplication of side effects. Actions
with external side effects should be idempotent.

## Versioning

BurrMCP is 0.x. The four-tool surface (`step`, `reset_session`, `fork_at`,
`fork_from_past`) is stable intent. Other public API may change on minor
releases.
