#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import asyncio
import html
import json
import logging
import os
from contextlib import asynccontextmanager
from IPython.display import HTML, display
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Keep notebook output clean: only warnings from the libraries
logging.getLogger().setLevel(logging.WARNING)

# %%
# Where the MCP server lives (inside docker-compose it is the `mcp` service)
mcpUrl = os.environ.get("MCP_URL", "http://localhost:8001/mcp")


@asynccontextmanager
async def connectMcp(url: str = mcpUrl):
    """
    Open an MCP client session to the running MCP server (streamable HTTP).

    Arguments:
    ----------
    url : str
        The MCP endpoint. Defaults to the MCP_URL environment variable.
    """
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def callTool(session, name: str, **args) -> dict:
    """
    Call an MCP tool and return what an agent would see.

    Returns:
    --------
    dict
        {"ok": bool, "data": parsed result when ok, "error": the error text when not ok}
    """
    result = await session.call_tool(name, args)
    text = result.content[0].text if result.content else ""
    if result.isError:
        return {"ok": False, "error": text}
    return {"ok": True, "data": result.structuredContent or json.loads(text)}


def show(value) -> None:
    """Print a result as indented JSON (or plain text for errors)."""
    print(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))


async def waitForJob(session, jobId: str, pollSeconds: float = 1.5, maxPolls: int = 40) -> dict:
    """
    Poll get_job_status until the job reaches a final state, printing progress.

    Real agents should wait for the `retry_after_seconds` the server suggests; the demo uses a shorter
    interval because the demo backend runs jobs faster than the real 5-20 s.
    """
    for attempt in range(1, maxPolls + 1):
        status = await callTool(session, "get_job_status", job_id=jobId)
        if not status["ok"]:
            print(status["error"])
            return status
        data = status["data"]
        print(f"poll {attempt}: {data['status']} (elapsed {data['elapsed_seconds']}s)")
        if data["status"] != "processing":
            return status
        await asyncio.sleep(pollSeconds)
    raise TimeoutError("job did not finish")


def showImages(images: list) -> None:
    """Display the poster images returned by a completed job (links are opened by your browser)."""
    cards = "".join(
        f'<figure style="margin:0 12px 0 0"><img src="{html.escape(i["url"])}" width="220" '
        f'style="border:1px solid #ccc"/><figcaption>variant {i["index"]} ({i["resolution"]})</figcaption></figure>'
        for i in images
    )
    display(HTML(f'<div style="display:flex">{cards}</div>'))
