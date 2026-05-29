---
title: 'Sessions and forking'
description: 'Per-session state isolation, and the tools that reset, roll back, and resume a run.'
---

State lives on the server, and it is isolated per MCP session. How that state is
created depends on what you pass to `mount`.

## Factory mode vs shared mode

`mount` accepts either a callable factory or a built `Application`.

- **Factory mode (recommended).** Pass a zero-argument function that builds an
  `Application`. Theodosia builds a fresh one per MCP session, lazily on first
  touch, and keeps them isolated. Two agents connected at once each get their
  own state.
- **Shared mode.** Pass an `Application` instance directly and every session
  shares the one state object. Useful for a single-tenant tool; not what you
  want when multiple agents connect.

```python
mount(build_application, name="incident")   # factory: per-session isolation
mount(build_application(), name="incident")  # shared: one state for all sessions
```

Sessions are evicted lazily, on access, by TTL (`session_ttl_seconds`, default
3600) and count (`max_sessions`, default 100). There is no background timer.

## `reset_session`

Rebuilds this session's `Application` from the factory, discarding its state and
history. It refuses in shared mode, where there is no factory to rebuild from.

## `fork_at(sequence_id)`

Rolls this session back to a prior point in its own history and continues from
there. Use it to retry from before a wrong turn without losing the record of
what happened. Forking to a refused sequence id raises a structured
`cannot_fork_to_refusal` response; pick a successful prior step.

The forked run starts a new `app_id`, but the tracker for that new run is
only written on the next `step` call. Right after `fork_at` returns,
`theodosia sessions show <new-app-id>` may say "No steps recorded yet";
take one more step and the audit trail materializes.

## `fork_from_past(app_id, sequence_id)`

Resumes a *different* session's state, by its tracker `app_id` and a sequence
number, through the persister. This is how you pick up a run that a previous
process started.

`fork_from_past` is hidden from the tool listing unless a `state_loader` or a
`LocalTrackingClient` is wired, because without one of those it could only ever
return a `no_tracker` refusal. Wire a tracker (see [Observability](observability.md))
and it appears.

## What the agent sees

The current state is always readable at `theodosia://state`, the per-session
attempt timeline (including refusals and forks) at `theodosia://history`, and
the tracker coordinates (project, `app_id`, partition key) at
`theodosia://session`. Each fork is its own tracked run, so the audit trail
stays complete across rollbacks and resumes.

## Don't reuse `app_id` across runs

Burr's `app_id` is the row key for everything on disk: the tracker log, the
refusal sidecar, and the hash-chained ledger. Each fresh `mount()` of a
factory generates a new `app_id` automatically. If you instead pin a
factory's `with_identifiers(app_id="my-fixed-id")` and rerun it in a fresh
process, the new session opens for append on the same
`~/.<tracker>/<project>/<app_id>/log.jsonl` but starts its sequence at
`seq=0`. You then see `seq=0` appear multiple times in the same file. The
hash chain still verifies (the binding covers `app_id` and each run's chain
is self-consistent), but reading the timeline is confusing. Pin `app_id`
only when you genuinely want one logical session that survives restarts via
`fork_from_past`; otherwise let Burr generate one per run.
