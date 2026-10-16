# Design notes

## Why separate create and status tools?

Generating a poster takes 5 to 20 seconds. A single tool that waits would block the agent, risk client timeouts, and lose the job if the connection dropped. With two tools, create returns a job id right away, the agent can keep talking to the user, and retries are safe because the job lives on the server.

If jobs took 5 minutes I'd keep the split and lean on `retry_after_seconds` so the agent polls rarely. At 20 minutes, like video, polling stops making sense. I'd use a push signal (a webhook or a notification), keep the job id in the task state, and show an estimate.

## Links or image data?

Links. A base64 image costs thousands of tokens, fills the context and is slow to pass through a model. A signed link is cheap, easy to open later, and can expire. Calling `get_job_status` again returns fresh ones.

## What to limit or never expose

Never the api key, other tenants' ids, or raw stack traces. Limit image count (max 4), prompt length (500), 2k resolution, active jobs per key, and creations per minute. Only accept public `https://` urls or `asset://` references, which blocks requests to internal addresses. Don't expose bulk, delete or admin operations. Idempotency keys stop a confused agent from multiplying cost.
