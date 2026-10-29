"""Fake `claude -p PROMPT ...`: records argv, cwd and the context file, exits $FAKE_CLAUDE_RC."""
import json
import os
import sys
from pathlib import Path

fake = Path(os.environ["FAKE_GLAB_DIR"])
ctx = os.environ.get("CODE_REVIEW_AUTOMATION_CONTEXT")
with (fake / "claude_calls.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                        "context": json.loads(Path(ctx).read_text()) if ctx else None}) + "\n")
# FAKE_DRAFTS="10:1,10:1,10:2" makes the fake pass log helper drafts like a real one would.
for item in filter(None, os.environ.get("FAKE_DRAFTS", "").split(",")):
    pid, iid = item.split(":")
    with (Path(os.environ["CODE_REVIEW_AUTOMATION_HOME"]) / "code-review-automation.log").open("a") as f:
        f.write(f"2026-01-01 00:00:00 DRAFT - project {pid} !{iid}\n")
print(json.dumps({"result": "!1: 2 comments posted", "total_cost_usd": 0.42, "num_turns": 7,
                  "duration_ms": 61000, "usage": {"output_tokens": 900}}))
sys.exit(int(os.environ.get("FAKE_CLAUDE_RC", "0")))
