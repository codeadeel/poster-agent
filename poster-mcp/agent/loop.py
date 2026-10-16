#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import asyncio
import json
import logging
from dataclasses import dataclass, field

# Define Logging Configuration
logging.basicConfig(level=logging.INFO)

# %%
# System Prompt of the Poster Agent
systemPrompt = """You are a poster-generation assistant for an advertising platform. You act only through the tools provided.

Rules:
- Creating posters is asynchronous: call create_poster_job, then poll get_job_status until the job is completed, failed or cancelled.
- Read tool errors and "next_step" hints and follow them. Never invent image URLs or job ids.
- If a job fails and the hint says it is retryable, retry (at most 2 retries). If it still fails, say so honestly.
- Only tell the user posters are ready if get_job_status returned status "completed", and share the image urls it returned.
- Never ask for more than 4 images in one job; if the user wants more, explain the limit.
- Keep prompts under 500 characters. Be brief in your final answer."""

# %%
# Trace Schemas (everything the agent did, kept for evaluation and transcripts)
@dataclass
class toolCall:
    """One tool call made by the agent and what came back."""
    name: str
    args: dict
    isError: bool
    text: str
    data: dict


@dataclass
class agentTrace:
    """The full record of one agent run."""
    brief: str
    model: str
    calls: list[toolCall] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    final: str = ""
    steps: int = 0
    hitStepLimit: bool = False
    tokens: int = 0

# %%
# The Agent Loop
async def runAgent(brief: str, llm, session, maxSteps: int = 20, timeScale: float = 1.0) -> agentTrace:
    """
    Run a minimal tool-calling agent on top of an MCP client session.

    Arguments:
    ----------
    brief : str
        The plain-language request from the user.
    llm : ollamaChat | scriptedChat
        The model backend.
    session : mcp.ClientSession
        A connected MCP client session.
    maxSteps : int
        Hard limit on model steps, so a confused agent cannot loop forever.
    timeScale : float
        Same factor the mock backend uses, so waiting on retry_after_seconds scales with job speed.
    """
    tools = [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}}
        for t in (await session.list_tools()).tools
    ]
    trace = agentTrace(brief=brief, model=llm.model)
    messages = [{"role": "system", "content": systemPrompt}, {"role": "user", "content": brief}]

    for step in range(1, maxSteps + 1):
        trace.steps = step
        reply = await llm.chat(messages, tools)
        assistant = {"role": "assistant", "content": reply.content}
        if reply.toolCalls:
            assistant["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in reply.toolCalls]
        messages.append(assistant)
        if not reply.toolCalls:
            trace.final = reply.content
            break

        waitSeconds = 0.0
        for call in reply.toolCalls:
            result = await session.call_tool(call["name"], call["arguments"])
            text = result.content[0].text if result.content else ""
            data = {} if result.isError else (result.structuredContent or json.loads(text))
            trace.calls.append(toolCall(call["name"], call["arguments"], result.isError, text, data))
            messages.append({"role": "tool", "tool_name": call["name"], "content": text})
            waitSeconds = max(waitSeconds, data.get("retry_after_seconds", 0) * timeScale)
        await asyncio.sleep(waitSeconds)  # honour the server's retry_after hint, like any polite API client
    else:
        trace.hitStepLimit = True
        trace.final = "(stopped: step limit reached)"

    trace.messages, trace.tokens = messages, getattr(llm, "tokens", 0)
    logging.info("Agent finished: %d steps, %d tool calls", trace.steps, len(trace.calls))
    return trace

# %%
# Transcript Rendering
def renderTranscript(trace: agentTrace, maxResultChars: int = 500) -> str:
    """Render a run as readable markdown (long tool results are shortened)."""
    def short(text: str) -> str:
        return text if len(text) <= maxResultChars else text[:maxResultChars] + " ..."

    out = [f"**User:** {trace.brief}", ""]
    callIndex = 0
    for message in trace.messages[2:]:
        if message["role"] == "assistant":
            for c in message.get("tool_calls", []):
                out.append(f"**Agent -> tool** `{c['function']['name']}({json.dumps(c['function']['arguments'], ensure_ascii=False)})`")
            if message["content"]:
                out += ["", f"**Agent:** {message['content']}"]
        else:
            call = trace.calls[callIndex]
            callIndex += 1
            out.append(f"**Tool result{' (ERROR)' if call.isError else ''}:** `{short(call.text)}`")
            out.append("")
    return "\n".join(out).strip()
