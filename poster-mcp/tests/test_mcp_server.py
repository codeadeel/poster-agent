#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import logging
import httpx
import pytest
from mcp_server.server import mcpSettings
from .conftest import testApiKey, validArgs

# %%
# Helper: create a job and return its data (fails the test if the tool returns an error)
async def createJob(env, **overrides):
    result = await env.call("create_poster_job", **{**validArgs, **overrides})
    assert not result.isError, result.text
    return result.data

# %%
# Schemas and Discovery
async def test_tools_are_discoverable_with_clear_schemas(openEnv):
    async with openEnv() as env:
        tools = {t.name: t for t in (await env.session.list_tools()).tools}
    assert set(tools) == {"list_templates", "create_poster_job", "get_job_status", "cancel_job"}
    for tool in tools.values():
        assert len(tool.description) > 80
    props = tools["create_poster_job"].inputSchema["properties"]
    assert props["image_count"]["minimum"] == 1 and props["image_count"]["maximum"] == 4
    assert props["resolution"]["enum"] == ["1k", "2k"]
    assert props["prompt"]["maxLength"] == 500
    assert set(tools["create_poster_job"].inputSchema["required"]) == {"product_image", "background_image", "prompt"}


async def test_list_templates_filters_by_category_and_search(openEnv):
    async with openEnv() as env:
        everything = (await env.call("list_templates")).data["templates"]
        sale = (await env.call("list_templates", category="sale")).data["templates"]
        coffee = (await env.call("list_templates", search="coffee")).data["templates"]
        nothing = (await env.call("list_templates", category="nope")).data
    assert len(everything) > len(sale) > 0
    assert {t["category"] for t in sale} == {"sale"}
    assert [t["id"] for t in coffee] == ["tpl_coffee_steam"]
    assert nothing["templates"] == [] and "fewer filters" in nothing["next_step"]

# %%
# Validation and Errors an Agent Can Act On
async def test_create_returns_processing_job(openEnv):
    async with openEnv() as env:
        data = await createJob(env, template_id="tpl_sale_bold", image_count=2, resolution="2k")
    assert data["status"] == "processing" and data["job_id"] and "get_job_status" in data["next_step"]


async def test_prompt_over_500_chars_is_rejected_with_next_step(openEnv):
    async with openEnv() as env:
        result = await env.call("create_poster_job", **{**validArgs, "prompt": "x" * 501})
        assert result.isError
        assert "501" in result.text and "Shorten" in result.text
        assert not env.app.state.jobs  # rejected before reaching the backend


@pytest.mark.parametrize("count", [0, 5, -1])
async def test_image_count_outside_1_to_4_is_rejected(openEnv, count):
    async with openEnv() as env:
        result = await env.call("create_poster_job", **{**validArgs, "image_count": count})
    assert result.isError and "1 to 4" in result.text and "Next step" in result.text


async def test_backend_rejects_invalid_input_too(openEnv):
    """The mock backend is the real gatekeeper; the MCP checks are just a fast first line."""
    async with openEnv() as env:
        for bad in ({"prompt": "x" * 501}, {"image_count": 9}, {"resolution": "4k"}, {"product_image": "ftp://x"}):
            response = await env.backend.post("/jobs", json={**validArgs, **bad})
            assert response.status_code == 400, bad
        response = await env.backend.post("/jobs", json={**validArgs, "template_id": "nope"})
        assert response.status_code == 404


@pytest.mark.parametrize(
    "url",
    ["http://cdn.example.com/a.png", "https://localhost/a.png", "https://169.254.169.254/latest", "https://10.0.0.5/a.png", "https://user:pw@cdn.example.com/a.png"],
)
async def test_unsafe_image_urls_are_rejected(openEnv, url):
    async with openEnv() as env:
        result = await env.call("create_poster_job", **{**validArgs, "product_image": url})
    assert result.isError and "https://" in result.text and "asset://" in result.text


async def test_unknown_job_and_template_errors_say_what_to_do(openEnv):
    async with openEnv() as env:
        job = await env.call("get_job_status", job_id="doesnotexist")
        template = await env.call("create_poster_job", **{**validArgs, "template_id": "tpl_missing"})
    assert job.isError and "create_poster_job" in job.text
    assert template.isError and "list_templates" in template.text

# %%
# Job Lifecycle
async def test_job_goes_from_processing_to_completed_with_image_links(openEnv, clock):
    async with openEnv() as env:
        job = await createJob(env, image_count=3)
        early = (await env.call("get_job_status", job_id=job["job_id"])).data
        assert early["status"] == "processing" and early["retry_after_seconds"] == 5
        clock.advance(25)
        done = (await env.call("get_job_status", job_id=job["job_id"])).data
        assert done["status"] == "completed" and len(done["images"]) == 3
        link = done["images"][0]["url"].replace("http://localhost:8000", "")
        assert (await env.backend.get(link)).headers["content-type"] == "image/svg+xml"
        assert (await env.backend.get(link[:-3] + "bad")).status_code == 403  # tampered signature


async def test_failed_job_reports_error_and_a_retry_creates_a_new_job(openEnv, clock):
    async with openEnv(failFirstN=1) as env:
        first = await createJob(env)
        clock.advance(25)
        status = (await env.call("get_job_status", job_id=first["job_id"])).data
        assert status["status"] == "failed" and status["error"]["retryable"]
        assert "create_poster_job again" in status["next_step"]
        second = await createJob(env)  # same inputs: a failed job must not block the retry
        assert second["job_id"] != first["job_id"]
        clock.advance(25)
        assert (await env.call("get_job_status", job_id=second["job_id"])).data["status"] == "completed"


async def test_stuck_job_is_flagged_so_the_agent_can_cancel_and_retry(openEnv, clock):
    async with openEnv(minSeconds=500, maxSeconds=500) as env:
        job = await createJob(env)
        clock.advance(90)
        status = (await env.call("get_job_status", job_id=job["job_id"])).data
    assert status["status"] == "processing" and "warning" in status and "cancel_job" in status["next_step"]


async def test_cancel_running_job_then_cancel_again_is_harmless(openEnv):
    async with openEnv() as env:
        job = await createJob(env)
        assert (await env.call("cancel_job", job_id=job["job_id"])).data["status"] == "cancelled"
        assert (await env.call("cancel_job", job_id=job["job_id"])).data["status"] == "cancelled"
        assert (await env.call("get_job_status", job_id=job["job_id"])).data["status"] == "cancelled"


async def test_cancel_finished_job_is_an_error_with_guidance(openEnv, clock):
    async with openEnv() as env:
        job = await createJob(env)
        clock.advance(25)
        result = await env.call("cancel_job", job_id=job["job_id"])
    assert result.isError and "already finished" in result.text and "get_job_status" in result.text

# %%
# Idempotency
async def test_retrying_identical_request_does_not_create_a_second_job(openEnv):
    async with openEnv() as env:
        first, second = await createJob(env), await createJob(env)
        assert first["job_id"] == second["job_id"] and second["duplicate"] is True
        assert len(env.app.state.jobs) == 1


async def test_explicit_idempotency_key_dedupes_conflicts_and_can_be_renewed(openEnv):
    async with openEnv() as env:
        first = await createJob(env, idempotency_key="retry-key-0001")
        second = await createJob(env, idempotency_key="retry-key-0001")
        assert first["job_id"] == second["job_id"]
        clash = await env.call("create_poster_job", **{**validArgs, "prompt": "something else", "idempotency_key": "retry-key-0001"})
        assert clash.isError and "different" in clash.text
        third = await createJob(env, idempotency_key="retry-key-0002")  # new key = deliberate new batch
        assert third["job_id"] != first["job_id"]

# %%
# Timeouts, Outages and Limits
class flakyTransport(httpx.AsyncBaseTransport):
    """Fails the first N requests like a network timeout, then forwards to the real app."""
    def __init__(self, inner, failures: int) -> None:
        self.inner, self.failures, self.calls = inner, failures, 0

    async def handle_async_request(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectTimeout("timed out", request=request)
        return await self.inner.handle_async_request(request)


async def test_timeout_is_retried_and_cannot_duplicate_the_job(openEnv):
    created = {}

    def wrap(inner):
        created["transport"] = flakyTransport(inner, failures=2)
        return created["transport"]

    async with openEnv(transport=wrap) as env:
        data = await createJob(env)
        assert created["transport"].calls == 3 and data["status"] == "processing"
        assert len(env.app.state.jobs) == 1


async def test_persistent_timeout_tells_agent_to_retry_with_same_key(openEnv):
    async with openEnv(transport=lambda inner: flakyTransport(inner, failures=99)) as env:
        result = await env.call("create_poster_job", **validArgs)
    assert result.isError and "did not respond" in result.text
    assert "SAME" in result.text and 'idempotency_key="auto-' in result.text


async def test_active_job_limit_protects_cost(openEnv):
    async with openEnv(maxActiveJobs=2) as env:
        await createJob(env, prompt="one")
        await createJob(env, prompt="two")
        result = await env.call("create_poster_job", **{**validArgs, "prompt": "three"})
    assert result.isError and "limit 2" in result.text and "cancel" in result.text

# %%
# Secrets
async def test_wrong_api_key_gives_config_error_and_leaks_nothing(openEnv):
    async with openEnv(headerKey="wrong-key-value") as env:
        result = await env.call("list_templates")
    assert result.isError and "POSTER_API_KEY" in result.text and "Do not retry" in result.text
    assert "wrong-key-value" not in result.text and testApiKey not in result.text


async def test_api_key_never_appears_in_tool_output_or_logs(openEnv, caplog, clock):
    caplog.set_level(logging.DEBUG)
    async with openEnv(serverSettings=mcpSettings(apiKey=testApiKey, retryBackoff=0)) as env:
        outputs = [(await env.call("list_templates")).text]
        job = await createJob(env)
        clock.advance(25)
        outputs.append((await env.call("get_job_status", job_id=job["job_id"])).text)
        outputs.append((await env.call("get_job_status", job_id="missing")).text)
    assert testApiKey not in "".join(outputs)
    assert testApiKey not in caplog.text
