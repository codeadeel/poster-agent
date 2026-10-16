#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp_server.server import createMcp, mcpSettings
from mock_backend.app import backendSettings, createApp

# %%
# Shared Test Data
testApiKey = "sk-test-SECRET-123"

validArgs = {
    "product_image": "https://cdn.example.com/latte.png",
    "background_image": "https://cdn.example.com/cafe.png",
    "prompt": "Weekend sale on all lattes, warm cafe mood, Arabic text",
}

# %%
# Test Helpers
class fakeClock:
    """A clock the tests can move forward, so 5-20 s jobs finish without waiting."""
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class toolResult:
    """What an agent sees after calling a tool."""
    isError: bool
    text: str
    data: dict[str, Any]


@dataclass
class testEnv:
    """A connected MCP client plus direct access to the mock backend."""
    session: Any
    app: Any
    clock: fakeClock
    backend: httpx.AsyncClient  # talks straight to the backend, bypassing MCP

    async def call(self, tool: str, **args) -> toolResult:
        """Call an MCP tool through the real client session."""
        result = await self.session.call_tool(tool, args)
        text = result.content[0].text if result.content else ""
        data = {} if result.isError else (result.structuredContent or json.loads(text))
        return toolResult(result.isError, text, data)

# %%
# Fixtures
@pytest.fixture
def clock() -> fakeClock:
    return fakeClock()


@pytest.fixture
def openEnv(clock):
    @asynccontextmanager
    async def _open(*, transport=None, headerKey=testApiKey, serverSettings=None, **backendOverrides):
        defaults = dict(apiKey=testApiKey, minSeconds=5, maxSeconds=20, failRate=0.0, seed=1)
        app = createApp(backendSettings(**{**defaults, **backendOverrides}), clock=clock)
        asgi = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {headerKey}"}
        httpClient = httpx.AsyncClient(transport=transport(asgi) if transport else asgi, base_url="http://backend", headers=headers)
        direct = httpx.AsyncClient(transport=asgi, base_url="http://backend", headers=headers)
        mcp = createMcp(serverSettings or mcpSettings(apiKey=testApiKey, retryBackoff=0), httpClient=httpClient)
        async with create_connected_server_and_client_session(mcp._mcp_server) as session:
            yield testEnv(session, app, clock, direct)

    return _open
