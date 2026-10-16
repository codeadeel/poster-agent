#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import json
import logging
import os
import re
from dataclasses import dataclass, field
import httpx

# Define Logging Configuration
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# %%
# Reply Schema Shared by All Model Backends
@dataclass
class llmReply:
    """
    One model answer.

    Arguments:
    ----------
    content : str
        The text of the answer.
    toolCalls : list[dict]
        Tool calls the model wants to make, each {"name": str, "arguments": dict}.
    """
    content: str = ""
    toolCalls: list[dict] = field(default_factory=list)

# %%
# Real Model: Ollama / Ollama Cloud
class ollamaChat:
    def __init__(self, model: str = "nemotron-3-nano:30b", host: str | None = None, apiKey: str | None = None) -> None:
        """
        Talk to Ollama's /api/chat endpoint. Defaults to Ollama Cloud (needs OLLAMA_API_KEY).

        Arguments:
        ----------
        model : str
            The model to use.
            Default : nemotron-3-nano:30b
        host : str | None
            Base URL of the Ollama server. Falls back to OLLAMA_HOST, then https://ollama.com.
        apiKey : str | None
            API key. Falls back to the OLLAMA_API_KEY environment variable.
        """
        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or "https://ollama.com").rstrip("/")
        key = apiKey or os.environ.get("OLLAMA_API_KEY")
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.tokens = 0  # total prompt + completion tokens, for cost reporting

    async def chat(self, messages: list[dict], tools: list[dict]) -> llmReply:
        """Send the conversation and the tool list to the model and return its reply."""
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.host}/api/chat", headers=self.headers,
                json={"model": self.model, "messages": messages, "tools": tools, "stream": False, "options": {"temperature": 0}},
            )
        response.raise_for_status()
        data = response.json()
        self.tokens += data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
        message = data["message"]
        toolCalls = [{"name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}} for c in message.get("tool_calls") or []]
        return llmReply(message.get("content", "").strip(), toolCalls)

# %%
# Scripted Stand-in Model
class scriptedChat:
    """
    A deterministic rule-based stand-in for a model (fast, free, repeatable).

    It is NOT smart: it follows one sensible fixed policy, so we can test the tools, the loop and the
    evaluation checks without paying for or waiting on a real model. Use ollamaChat for the real thing.
    """
    model = "scripted-policy"
    tokens = 0

    async def chat(self, messages: list[dict], tools: list[dict]) -> llmReply:
        """Decide the next step from the conversation so far."""
        brief = next(m["content"] for m in messages if m["role"] == "user")
        results = [
            (m.get("tool_name"), json.loads(m["content"]) if m["content"].startswith("{") else {"error": m["content"]})
            for m in messages if m["role"] == "tool"
        ]
        createCount = sum(1 for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", []) if c["function"]["name"] == "create_poster_job")

        if not results:
            return llmReply(toolCalls=[self.buildCreateCall(brief)])
        name, data = results[-1]
        if name == "create_poster_job" and "error" in data:
            if "Shorten" in data["error"]:
                return llmReply(toolCalls=[self.buildCreateCall(brief)])
            return llmReply("I could not start the job: " + data["error"])
        if name == "create_poster_job":
            if re.search(r"\bcancel\b", brief, re.I):
                return llmReply(toolCalls=[{"name": "cancel_job", "arguments": {"job_id": data["job_id"]}}])
            return llmReply(toolCalls=[{"name": "get_job_status", "arguments": {"job_id": data["job_id"]}}])
        if name == "cancel_job":
            return llmReply(f"Done - job {data.get('job_id')} was cancelled as requested.")
        if name == "get_job_status":
            status = data.get("status")
            if status == "processing":
                return llmReply(toolCalls=[{"name": "get_job_status", "arguments": {"job_id": data["job_id"]}}])
            if status == "completed":
                urls = "\n".join(i["url"] for i in data["images"])
                limit = " (4 is the maximum per job)" if re.search(r"\b([5-9]|\d{2,})\s+poster", brief) else ""
                return llmReply(f"Your {len(data['images'])} poster(s) are ready{limit}:\n{urls}")
            if status == "failed" and createCount < 3:
                return llmReply(toolCalls=[self.buildCreateCall(brief)])
            return llmReply(f"Sorry, the poster generation failed after {createCount} attempt(s) and I could not produce posters.")
        return llmReply("Unexpected state.")

    @staticmethod
    def buildCreateCall(brief: str) -> dict:
        """Turn the brief into a create_poster_job call (image urls, count capped at 4, prompt capped at 500)."""
        urls = re.findall(r"https://\S+", brief)
        match = re.search(r"\b(\d+)\s+poster", brief)
        count = min(int(match.group(1)), 4) if match else 1
        prompt = re.sub(r"https://\S+", "", brief).strip()[:500]
        return {"name": "create_poster_job", "arguments": {
            "product_image": urls[0], "background_image": urls[1], "prompt": prompt, "image_count": count}}
