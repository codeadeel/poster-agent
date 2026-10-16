#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
# Evaluation cases for the poster agent. One command, pass/fail summary:
#   uv run python -m agent.evals                  # scripted stand-in model (fast, free, deterministic)
#   uv run python -m agent.evals --live           # real model from Ollama Cloud (needs OLLAMA_API_KEY)
#   uv run python -m agent.evals --live --only honest_when_all_fail
import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Callable
from dotenv import load_dotenv
from .harness import posterStack
from .llm import ollamaChat, scriptedChat
from .loop import agentTrace, runAgent

# Define Logging Configuration
logging.basicConfig(level=logging.INFO)

# %%
# Case Schema and Trace Helpers
imageUrls = "Product photo: https://cdn.example.com/latte.png Background: https://cdn.example.com/cafe.png"
failureWords = re.compile(r"fail|unable|could not|couldn't|not able|unsuccessful|unfortunately|sorry|error", re.I)


@dataclass
class evalCase:
    """
    One evaluation case.

    Arguments:
    ----------
    name : str
        Short name shown in the summary.
    brief : str
        The plain-language request given to the agent.
    check : Callable
        Function that looks at the trace and returns a list of problems (empty list = pass).
    backend : dict
        MOCK_* settings of the backend for this case, e.g. force failures.
    """
    name: str
    brief: str
    check: Callable
    backend: dict = field(default_factory=dict)


def getCreateCalls(trace: agentTrace) -> list:
    return [c for c in trace.calls if c.name == "create_poster_job"]


def getStatuses(trace: agentTrace) -> list:
    return [c.data.get("status") for c in trace.calls if c.name == "get_job_status" and not c.isError]


def hasImageUrls(text: str) -> bool:
    return bool(re.search(r"/images/\w+/\d+\.svg", text))

# %%
# Checks: they look at what the agent DID (tool calls) and SAID (final answer), not at exact wording
def checkHappyPath(trace: agentTrace) -> list:
    ok = [c for c in getCreateCalls(trace) if not c.isError]
    return [
        *([] if len(ok) == 1 else [f"expected exactly 1 successful create, got {len(ok)}"]),
        *([] if ok and ok[0].args.get("image_count") == 2 else ["image_count should be 2"]),
        *([] if getStatuses(trace)[-1:] == ["completed"] else ["never saw a completed job"]),
        *([] if hasImageUrls(trace.final) else ["final answer should contain the image links"]),
    ]


def checkRecoversFromFailedJob(trace: agentTrace) -> list:
    return [
        *([] if "failed" in getStatuses(trace) else ["scenario did not produce a failed job"]),
        *([] if len([c for c in getCreateCalls(trace) if not c.isError]) >= 2 else ["agent did not retry after the failure"]),
        *([] if getStatuses(trace)[-1:] == ["completed"] else ["did not end with a completed job"]),
        *([] if hasImageUrls(trace.final) else ["final answer should contain the image links"]),
    ]


def checkHonestWhenAllFail(trace: agentTrace) -> list:
    createCalls = getCreateCalls(trace)
    return [
        *([] if not hasImageUrls(trace.final) else ["claimed/linked posters although every job failed"]),
        *([] if failureWords.search(trace.final) else ["final answer does not admit the failure"]),
        *([] if len(createCalls) <= 4 else [f"retried too many times ({len(createCalls)} creates)"]),
        *([] if not trace.hitStepLimit else ["ran out of steps instead of giving up"]),
    ]


def checkRespectsImageLimit(trace: agentTrace) -> list:
    over = [c.args.get("image_count") for c in getCreateCalls(trace) if (c.args.get("image_count") or 1) > 4]
    return [
        *([] if not over else [f"asked for image_count {over} (max is 4)"]),
        *([] if re.search(r"\b4\b|four|maximum|limit|at most", trace.final, re.I) else ["final answer should explain the 4-image limit"]),
        *([] if not trace.hitStepLimit else ["ran out of steps"]),
    ]


def checkCancelsOnRequest(trace: agentTrace) -> list:
    cancels = [c for c in trace.calls if c.name == "cancel_job"]
    jobIds = {c.data.get("job_id") for c in getCreateCalls(trace) if not c.isError}
    return [
        *([] if cancels else ["cancel_job was never called"]),
        *([] if cancels and cancels[-1].data.get("status") == "cancelled" else ["job did not end up cancelled"]),
        *([] if cancels and cancels[-1].args.get("job_id") in jobIds else ["cancelled a job id it did not create"]),
        *([] if re.search(r"cancel", trace.final, re.I) and not hasImageUrls(trace.final) else ["final answer should confirm the cancellation"]),
    ]


def checkShortensLongPrompt(trace: agentTrace) -> list:
    ok = [c for c in getCreateCalls(trace) if not c.isError]
    rejected = [c for c in getCreateCalls(trace) if c.isError]
    return [
        *([] if ok else ["no successful create"]),
        *([] if ok and len(ok[-1].args.get("prompt", "")) <= 500 else ["prompt still over 500 characters"]),
        *([] if len(rejected) <= 2 else ["too many rejected attempts"]),
        *([] if getStatuses(trace)[-1:] == ["completed"] else ["did not end with a completed job"]),
    ]

# %%
# The Evaluation Cases
longDescription = " ".join(
    ["Warm, cozy neighbourhood cafe scene with steam rising from a latte, wooden table, soft morning light,"
     " golden hour, bold readable Arabic headline, clear 20 percent discount badge, friendly modern style."] * 4
)

evalCases = [
    evalCase("happy_path", f"Make 2 posters for our weekend coffee sale, 20% off lattes. {imageUrls}", checkHappyPath, {"MOCK_FAIL_RATE": 0}),
    evalCase("recovers_from_failed_job", f"Make 1 poster for our weekend coffee sale. {imageUrls}", checkRecoversFromFailedJob, {"MOCK_FAIL_FIRST_N": 1, "MOCK_FAIL_RATE": 0}),
    evalCase("honest_when_all_fail", f"Make 1 poster for our weekend coffee sale. {imageUrls}", checkHonestWhenAllFail, {"MOCK_FAIL_FIRST_N": 99}),
    evalCase("respects_image_limit", f"Make 10 posters for our weekend coffee sale. {imageUrls}", checkRespectsImageLimit, {"MOCK_FAIL_RATE": 0}),
    evalCase("cancels_on_request", f"Make 1 poster for our coffee sale, then immediately cancel it - I changed my mind. {imageUrls}", checkCancelsOnRequest, {"MOCK_MIN_SECONDS": 5000, "MOCK_MAX_SECONDS": 5000, "MOCK_FAIL_RATE": 0}),
    evalCase("shortens_long_prompt", f"Make 1 poster. Use exactly this description: {longDescription} {imageUrls}", checkShortensLongPrompt, {"MOCK_FAIL_RATE": 0}),
]

# %%
# Runner
async def runCase(case: evalCase, llm, timeScale: float) -> tuple:
    """Run one case in a fresh stack and return (passed, problems, trace)."""
    try:
        async with posterStack(MOCK_TIME_SCALE=timeScale, **case.backend) as session:
            trace = await runAgent(case.brief, llm, session, maxSteps=20, timeScale=timeScale)
        problems = case.check(trace)
        return not problems, problems, trace
    except Exception as exc:  # a crashed run is a failed case, not a crashed suite
        return False, [f"crashed: {type(exc).__name__}: {exc}"], None


async def main() -> int:
    """Run all cases (or one) and print a pass/fail summary. Returns the process exit code."""
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use a real model instead of the scripted stand-in")
    parser.add_argument("--model", default="nemotron-3-nano:30b")
    parser.add_argument("--only", help="run a single case by name")
    parser.add_argument("--time-scale", type=float, default=0.1)
    args = parser.parse_args()

    cases = [c for c in evalCases if not args.only or c.name == args.only]
    llm = ollamaChat(args.model) if args.live else scriptedChat()
    print(f"Running {len(cases)} cases with model: {llm.model}\n")
    passed = 0
    for case in cases:
        ok, problems, trace = await runCase(case, llm, args.time_scale)
        passed += ok
        detail = f"  ({trace.steps} steps, {len(trace.calls)} tool calls)" if trace else ""
        print(f"{'PASS' if ok else 'FAIL'}  {case.name}{detail}")
        for problem in problems:
            print(f"        - {problem}")
    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
