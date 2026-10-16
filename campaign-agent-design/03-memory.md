# memory

- **During a task:** a record per task and tenant with the spec, the plan and step statuses, the artifacts (job ids, asset ids, draft ids, idempotency keys), approvals and an event log. The model's context only holds a short summary and the last few tool results. Images never go in the context. After a crash the task resumes from the record.
- **Across tasks, per tenant only:** a brand profile (names in Arabic and English, colours, logo, tone, dialect, default timezone and screens, the loyalty segment id) and short notes on what people edited or rejected before. Only approved outcomes are written, customers can see and delete them, and no personal data is stored, only ids and counts.

[back to the overview](README.md)
