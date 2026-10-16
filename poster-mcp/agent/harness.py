#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Define Logging Configuration
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# %%
# Start the Mock Backend and the MCP Server for an Agent Run
rootDir = Path(__file__).resolve().parent.parent
demoApiKey = "agent-demo-key"


def getFreePort() -> int:
    """Ask the OS for a free TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def posterStack(**backendEnv):
    """
    Start the mock backend and the MCP server (stdio) and yield a connected MCP client session.

    Arguments:
    ----------
    backendEnv : dict
        MOCK_* settings for the backend, e.g. MOCK_FAIL_FIRST_N=1 or MOCK_TIME_SCALE=0.1.
    """
    port = getFreePort()
    env = {
        **os.environ,
        "POSTER_API_KEY": demoApiKey,
        "MOCK_PUBLIC_URL": f"http://127.0.0.1:{port}",
        **{k: str(v) for k, v in backendEnv.items()},
    }
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_backend.app:createApp", "--factory", "--port", str(port), "--log-level", "warning"],
        cwd=rootDir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=1).raise_for_status()
                break
            except httpx.HTTPError:
                if time.time() > deadline or backend.poll() is not None:
                    raise RuntimeError("mock backend did not start")
                await asyncio.sleep(0.2)

        serverParams = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server"],
            cwd=str(rootDir),
            env={"POSTER_API_KEY": demoApiKey, "BACKEND_URL": f"http://127.0.0.1:{port}", "JOB_STALE_AFTER": "60"},
        )
        with open(os.devnull, "w") as errLog:  # MCP server logs go to stderr; hide them from the demo output
            async with stdio_client(serverParams, errlog=errLog) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        backend.terminate()
        backend.wait(timeout=5)
