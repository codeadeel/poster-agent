# cost and speed

- Limits per task: 25 model steps, 8 tool calls per step, about 150k tokens, tool results cut to about 2k tokens, 6 poster jobs of at most 4 images, 2 retries per step, 15 minutes before it suspends. Going over a limit suspends the task with a report, it doesn't loop.
- Big model: understanding the brief, planning, judging creative fit, and deciding what to do after a surprise.
- Small model: formatting tool arguments, copy variants, proofreading Arabic, summarising the approval card, repairing JSON.
- No model: polling, retries, date maths, validation, permissions.
- Poster generation dominates the time, so poster jobs and the segment build run in parallel. The system prompt and tool schemas are cached. Arabic costs more tokens than English, so instructions stay in English and only the user-facing copy is Arabic.

[back to the overview](README.md)
