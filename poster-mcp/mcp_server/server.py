#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlparse
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

# Define Logging Configuration (stderr, so it never mixes with the stdio protocol on stdout)
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# %%
# MCP Server for the Poster Service
# Design notes:
#   - Four tools mirror the service contract. Create and status are separate because jobs are asynchronous.
#   - Every error is raised as ToolError: "what happened" + "Next step: what to do".
#   - Create is idempotent: the same inputs (or the same idempotency_key) never create two jobs.
#   - The API key comes from the environment and is never logged or returned.
maxPromptChars = 500
pollSeconds = 5

# What the agent should do next, keyed by error code (from our own checks or from the backend)
errorHints = {
    "invalid_prompt": "Provide a short, non-empty prompt describing the poster (product, offer, language, mood).",
    "prompt_too_long": "Shorten the prompt to 500 characters or fewer, keeping the key details, then call create_poster_job again.",
    "invalid_image_count": "Use an integer from 1 to 4. If the user wants more, create several jobs of up to 4 each (confirm with the user first).",
    "invalid_resolution": 'Use "1k" (fast, default) or "2k" (print quality).',
    "invalid_image": "Pass an https:// image URL on a public host, or an asset:// reference from an upload. Ask the user for the image if you do not have one.",
    "invalid_request": "Check the arguments against the tool's input schema and try again.",
    "invalid_idempotency_key": "Use a string of 8-128 characters, or omit idempotency_key.",
    "template_not_found": "Call list_templates to get valid template ids, or omit template_id.",
    "job_not_found": "Use the exact job_id returned by create_poster_job. If you lost it, create the job again (same inputs return the same job).",
    "job_already_finished": "There is nothing to cancel. Call get_job_status to see the final result.",
    "too_many_active_jobs": "Wait for running jobs to finish (poll get_job_status) or cancel one, then retry in about 10 seconds.",
    "idempotency_conflict": "Use a different idempotency_key to start a new job, or repeat the original inputs to get the existing job.",
    "unauthorized": "The server's POSTER_API_KEY is missing or wrong. You cannot fix this: tell the user to check the server configuration. Do not retry.",
    "link_invalid_or_expired": "Call get_job_status again to get fresh image links.",
}

# %%
# Settings and Error Helpers
@dataclass
class mcpSettings:
    """
    Configuration of the MCP server.

    Arguments:
    ----------
    backendUrl : str
        Base URL of the poster service.
    apiKey : str
        Credential for the poster service. Read from POSTER_API_KEY, never logged or returned.
    timeout : float
        Timeout in seconds for one request to the backend.
    maxAttempts : int
        How many times a request is tried on network errors / 5xx responses.
    retryBackoff : float
        Seconds to wait between attempts (multiplied by the attempt number).
    staleAfter : float
        A job still processing after this many seconds is reported as taking too long.
    """
    backendUrl: str = "http://localhost:8000"
    apiKey: str = ""
    timeout: float = 10.0
    maxAttempts: int = 3
    retryBackoff: float = 0.5
    staleAfter: float = 60.0

    @classmethod
    def fromEnv(cls) -> "mcpSettings":
        """Build the settings from environment variables."""
        env = os.environ.get
        return cls(
            backendUrl=env("BACKEND_URL", "http://localhost:8000"),
            apiKey=env("POSTER_API_KEY", ""),
            timeout=float(env("REQUEST_TIMEOUT", 10)),
            maxAttempts=int(env("MAX_ATTEMPTS", 3)),
            staleAfter=float(env("JOB_STALE_AFTER", 60)),
        )


def buildToolError(code: str, message: str) -> ToolError:
    """Return a ToolError that says what happened and what the agent should do next."""
    hint = errorHints.get(code, "Check the inputs and try again; if it keeps failing, tell the user.")
    return ToolError(f"{message} Next step: {hint}")


def checkImageRef(name: str, value: str) -> None:
    """Reject anything that is not a public https URL or an asset:// reference (SSRF / abuse guard)."""
    error = buildToolError("invalid_image", f"`{name}` ({value[:60]!r}) is not an allowed image reference.")
    if value.startswith("asset://") and len(value) > len("asset://"):
        return
    url = urlparse(value)
    host = (url.hostname or "").lower()
    if url.scheme != "https" or not host or url.username or url.password:
        raise error
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        raise error
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
        raise error

# %%
# MCP Server Factory
def createMcp(settings: mcpSettings | None = None, httpClient: httpx.AsyncClient | None = None, **fastmcpKwargs) -> FastMCP:
    """
    Create the MCP server with its four tools.

    Arguments:
    ----------
    settings : mcpSettings | None
        Server configuration. Read from environment variables when omitted.
    httpClient : httpx.AsyncClient | None
        HTTP client used to reach the backend. Tests pass one wired straight to the mock backend.
    fastmcpKwargs : dict
        Extra FastMCP options such as host and port for the streamable-http transport.
    """
    settings = settings or mcpSettings.fromEnv()
    client = httpClient or httpx.AsyncClient(
        base_url=settings.backendUrl,
        headers={"Authorization": f"Bearer {settings.apiKey}"},
        timeout=settings.timeout,
    )

    def scrub(text: str) -> str:
        return text.replace(settings.apiKey, "***") if settings.apiKey else text

    async def callBackend(method: str, path: str, *, retryNote: str, **kwargs) -> dict:
        """Call the backend with retries on network errors / 5xx; turn failures into actionable ToolErrors."""
        lastProblem = ""
        for attempt in range(1, settings.maxAttempts + 1):
            try:
                response = await client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                lastProblem = type(exc).__name__
            else:
                logging.info("%s %s -> %s (attempt %d)", method, path.split("?")[0], response.status_code, attempt)
                if response.status_code < 500:
                    break
                lastProblem = f"HTTP {response.status_code}"
            if attempt < settings.maxAttempts:
                await asyncio.sleep(settings.retryBackoff * attempt)
        else:
            logging.warning("%s %s gave up after %d attempts (%s)", method, path.split("?")[0], settings.maxAttempts, lastProblem)
            raise ToolError(
                f"The poster service did not respond ({lastProblem}) after {settings.maxAttempts} attempts. "
                f"Next step: {retryNote} If it keeps failing, tell the user the service is unavailable."
            )
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise buildToolError(err.get("code", "invalid_request"), scrub(err.get("message", f"Request rejected (HTTP {response.status_code}).")))
        return data

    mcp = FastMCP("poster-service", instructions=(
        "Tools for generating advertising posters. Workflow: (optional) list_templates -> create_poster_job -> "
        "poll get_job_status every ~5 seconds until status is completed, failed or cancelled. "
        "Jobs normally take 5-20 seconds."
    ), **fastmcpKwargs)

    # %%
    # Tool 1: list_templates
    @mcp.tool()
    async def list_templates(
        category: Annotated[str | None, Field(description='Optional exact category, e.g. "sale", "food-and-beverage", "occasion", "new-product", "minimal".')] = None,
        search: Annotated[str | None, Field(description="Optional text to look for in template names (case-insensitive).")] = None,
    ) -> dict[str, Any]:
        """Browse ready-made poster templates. Use this before create_poster_job when the user wants a specific style
        or occasion. Returns templates (id, name, category) and the list of valid categories. An empty list means
        nothing matched: retry with fewer filters. Read-only and free."""
        params = {k: v for k, v in {"category": category, "search": search}.items() if v}
        data = await callBackend("GET", "/templates", params=params, retryNote="Call list_templates again in a few seconds.")
        if not data["templates"]:
            data["next_step"] = "No template matched. Retry with fewer filters, or create a poster without template_id."
        return data

    # %%
    # Tool 2: create_poster_job
    @mcp.tool()
    async def create_poster_job(
        product_image: Annotated[str, Field(description="The product photo: a public https:// URL or an asset:// reference.")],
        background_image: Annotated[str, Field(description="The background / scene image: a public https:// URL or an asset:// reference.")],
        prompt: Annotated[str, Field(description="What the poster should show and say (language, offer, mood). Maximum 500 characters.", json_schema_extra={"maxLength": maxPromptChars})],
        template_id: Annotated[str | None, Field(description="Optional template id from list_templates. Omit for a free-form poster.")] = None,
        image_count: Annotated[int, Field(description="How many poster variations to generate, 1 to 4. Costs scale with this number; default 1.", json_schema_extra={"minimum": 1, "maximum": 4})] = 1,
        resolution: Annotated[str, Field(description='"1k" (default, fast) or "2k" (higher quality, use only when the user asks for print/large format).', json_schema_extra={"enum": ["1k", "2k"]})] = "1k",
        idempotency_key: Annotated[str | None, Field(description="Optional. Reuse the SAME key when retrying after a timeout or error so no duplicate job (and no extra cost) is created. If omitted, one is derived from the inputs, so repeating identical inputs returns the existing job. Pass a NEW key to deliberately get another batch with identical inputs.")] = None,
    ) -> dict[str, Any]:
        """Start generating poster images. This is asynchronous: it returns immediately with a job_id and
        status "processing". It does NOT return images. Next, call get_job_status with the job_id every ~5 seconds
        until the job is completed. Safe to retry: identical inputs / the same idempotency_key never create
        a second job. Costs money per image, so only request what the user asked for."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise buildToolError("invalid_prompt", "`prompt` is empty.")
        if len(prompt) > maxPromptChars:
            raise buildToolError("prompt_too_long", f"`prompt` is {len(prompt)} characters; the maximum is {maxPromptChars}.")
        if not 1 <= image_count <= 4:
            raise buildToolError("invalid_image_count", f"`image_count` is {image_count}; it must be from 1 to 4.")
        if resolution not in ("1k", "2k"):
            raise buildToolError("invalid_resolution", f'`resolution` is "{resolution}"; it must be "1k" or "2k".')
        checkImageRef("product_image", product_image)
        checkImageRef("background_image", background_image)
        if idempotency_key is not None and not 8 <= len(idempotency_key) <= 128:
            raise buildToolError("invalid_idempotency_key", "`idempotency_key` has the wrong length.")

        body = {
            "template_id": template_id, "product_image": product_image, "background_image": background_image,
            "prompt": prompt, "image_count": image_count, "resolution": resolution,
        }
        key = idempotency_key or "auto-" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
        data = await callBackend(
            "POST", "/jobs", json=body, headers={"Idempotency-Key": key},
            retryNote=(
                "The job may or may not have been created. Call create_poster_job again with the SAME arguments "
                f'and idempotency_key="{key}": this is safe and cannot create a duplicate.'
            ),
        )
        out = {
            "job_id": data["job_id"], "status": data["status"], "idempotency_key": key,
            "duplicate": data.get("duplicate", False),
            "next_step": f"Call get_job_status with job_id={data['job_id']} in about {pollSeconds} seconds.",
        }
        if out["duplicate"]:
            out["note"] = "An identical request already exists, so no new job was created."
        return out

    # %%
    # Tool 3: get_job_status
    @mcp.tool()
    async def get_job_status(
        job_id: Annotated[str, Field(description="The job_id returned by create_poster_job.")],
    ) -> dict[str, Any]:
        """Check a poster job. Status is one of: processing, completed, failed, cancelled.
        completed -> images[] contains links (url) to show to the user; links expire, call again for fresh ones.
        failed -> error explains why and next_step says whether to retry. processing -> poll again after
        retry_after_seconds. Read-only and free; do not poll more often than every ~5 seconds."""
        data = await callBackend("GET", f"/jobs/{job_id}", retryNote="Call get_job_status again in a few seconds.")
        status = data["status"]
        if status == "processing":
            data["retry_after_seconds"] = pollSeconds
            data["next_step"] = f"Still working (jobs usually take 5-20 s). Call get_job_status again in {pollSeconds} seconds."
            if data["elapsed_seconds"] > settings.staleAfter:
                data["warning"] = f"Job has been running {data['elapsed_seconds']:.0f}s, much longer than normal."
                data["next_step"] = (
                    "Taking far too long. Call cancel_job once, then create_poster_job again with the same inputs "
                    "and a NEW idempotency_key. If that is also slow, tell the user the service is degraded."
                )
        elif status == "completed":
            data["next_step"] = "Give the image urls to the user. Do not call this tool again unless links expired."
        elif status == "failed":
            data["next_step"] = (
                "Generation failed. It is safe to call create_poster_job again with the same inputs (a failed job "
                "does not block a retry). Retry at most 2 times, then tell the user it failed."
                if data["error"]["retryable"] else
                "Generation failed and retrying will not help. Change the inputs or tell the user."
            )
        elif status == "cancelled":
            data["next_step"] = "The job was cancelled. Create a new job if the user still wants posters."
        return data

    # %%
    # Tool 4: cancel_job
    @mcp.tool()
    async def cancel_job(
        job_id: Annotated[str, Field(description="The job_id returned by create_poster_job.")],
    ) -> dict[str, Any]:
        """Stop a job that is still processing, e.g. the user changed their mind or the job is stuck.
        Returns status "cancelled". If the job already finished (completed or failed) it returns an error and
        nothing is changed; call get_job_status to see the outcome. Cancelling twice is harmless."""
        data = await callBackend("POST", f"/jobs/{job_id}/cancel", retryNote="Call cancel_job again in a few seconds; cancelling twice is harmless.")
        data["next_step"] = "Job cancelled. Create a new job if the user still wants posters."
        return data

    return mcp

# %%
# Entry Point
def main() -> None:
    """Start the MCP server over stdio (default) or streamable HTTP."""
    import argparse
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Poster service MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    settings = mcpSettings.fromEnv()
    if not settings.apiKey:
        sys.exit("POSTER_API_KEY is not set. Export it (see .env.example) and start the server again.")
    extra = {"host": args.host, "port": args.port} if args.transport == "streamable-http" else {}
    createMcp(settings, **extra).run(transport=args.transport)
