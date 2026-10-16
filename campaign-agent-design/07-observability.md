# observability

One trace per task, in an append-only store. Secrets are never logged and personal data is redacted.

- every model step: model, prompt version, tokens, latency, the plan step it belongs to, and a one-line reason the model gives with each tool call
- every tool call: tool, redacted arguments, idempotency key, result summary, latency, retries
- guardrail hits (blocked tool, tenant mismatch, suspected injection)
- approvals: who, when, the token hash, and a snapshot of the card they saw

People see this as a timeline ("created 3 posters because the brief asked for Arabic, built the segment: 12,400 people"). It's also what gets replayed against mocked tools for evals. Worth alerting on: any cross-tenant guard hit, retry storms, tasks hitting their budget.

[back to the overview](README.md)
