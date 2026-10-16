#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
# Run the agent on one brief:
#   uv run python -m agent.run --mock                  # scripted stand-in model, no LLM needed
#   uv run python -m agent.run --fail-first 1          # real model, the first poster job fails
import argparse
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv
from .harness import posterStack
from .llm import ollamaChat, scriptedChat
from .loop import renderTranscript, runAgent

# Define Logging Configuration
logging.basicConfig(level=logging.INFO)

# %%
# Default Brief
defaultBrief = (
    "Make 2 Arabic posters for our weekend coffee sale, warm cafe mood, 20% off all lattes. "
    "Product photo: https://cdn.example.com/latte.png Background: https://cdn.example.com/cafe.png"
)

# %%
# Command Line Entry Point
async def main() -> None:
    """Parse the arguments, start the stack, run the agent and print the transcript."""
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("brief", nargs="?", default=defaultBrief)
    parser.add_argument("--model", default="nemotron-3-nano:30b")
    parser.add_argument("--mock", action="store_true", help="use the scripted stand-in model instead of a real LLM")
    parser.add_argument("--fail-first", type=int, default=0, help="force the first N poster jobs to fail")
    parser.add_argument("--time-scale", type=float, default=0.2, help="1.0 = real 5-20 s jobs; 0.2 = 1-4 s")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--save", type=Path, help="write the transcript to this markdown file")
    args = parser.parse_args()

    llm = scriptedChat() if args.mock else ollamaChat(args.model)
    async with posterStack(MOCK_TIME_SCALE=args.time_scale, MOCK_FAIL_FIRST_N=args.fail_first, MOCK_FAIL_RATE=0) as session:
        trace = await runAgent(args.brief, llm, session, args.max_steps, args.time_scale)

    transcript = renderTranscript(trace)
    summary = f"[{trace.model}] {trace.steps} model steps, {len(trace.calls)} tool calls, {trace.tokens} tokens"
    print(transcript)
    print(f"\n{summary}")
    if args.save:
        args.save.write_text(f"_{summary}_\n\n{transcript}\n")


if __name__ == "__main__":
    asyncio.run(main())
