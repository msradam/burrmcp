# Deployment FAQ

## How long does a deploy take?

A typical deploy: 5 minutes for the GitHub Action plus 30 minutes for
the canary plus 10 minutes for the full-fleet promotion. End-to-end:
about 45 minutes.

## Can I deploy on a Friday?

Officially yes; informally we discourage it. Friday deploys mean a
weekend rollback risk if the canary catches something on Monday.
Discuss with the on-call before merging anything bigger than a hotfix
on a Friday afternoon.

## How do I deploy a hotfix?

Hotfixes use the same pipeline but skip the canary clock: pass
`--canary-minutes=0` to `deploy-cli` to promote immediately after the
canary metrics check. Requires approval from a senior engineer or the
on-call.

## What if I need to deploy multiple services at once?

Deploy them serially, not in parallel. The deploy pipeline isn't
designed for parallel rollouts across services and you'll lose the
ability to bisect if something breaks.
