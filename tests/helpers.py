"""Shared helpers: build a small git repository with a green commit followed by a broken one."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from app.llm import LLMResponse, ToolCall

DEMO = Path(__file__).parent.parent / "demo" / "tinyshop"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(dest: Path) -> tuple[Path, str, str]:
    """Return (repo_dir, green_sha, broken_sha).

    The broken commit renames compute_total -> calc_total and updates the tests,
    but forgets the caller in tinyshop/checkout.py: a classic CI failure.
    """
    shutil.copytree(DEMO, dest)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.email", "dev@example.com")
    git(dest, "config", "user.name", "Dev")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "Initial shop")
    green = git(dest, "rev-parse", "HEAD")

    pricing = dest / "tinyshop" / "pricing.py"
    pricing.write_text(pricing.read_text().replace("def compute_total", "def calc_total"))
    tp = dest / "tests" / "test_pricing.py"
    tp.write_text(tp.read_text().replace("compute_total", "calc_total"))
    git(dest, "commit", "-qam", "Rename compute_total to calc_total")
    broken = git(dest, "rev-parse", "HEAD")
    return dest, green, broken


def tool(tool_name: str, call_id: str = "", **arguments) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=[ToolCall(call_id or f"call_{tool_name}", tool_name, arguments, json.dumps(arguments))],
                       input_tokens=1000, output_tokens=100)


FIX_SCRIPT = [
    tool("get_commit_diff"),
    tool("find_symbol", name="compute_total"),
    tool("read_file", path="tinyshop/checkout.py"),
    tool("propose_patch",
         edits=[{"path": "tinyshop/checkout.py",
                 "old_text": "from tinyshop.pricing import add_gst, apply_discount, compute_total",
                 "new_text": "from tinyshop.pricing import add_gst, apply_discount, calc_total"},
                {"path": "tinyshop/checkout.py",
                 "old_text": "subtotal = compute_total(cart)",
                 "new_text": "subtotal = calc_total(cart)"}],
         rationale="Commit renamed compute_total to calc_total in tinyshop/pricing.py:6 but "
                   "tinyshop/checkout.py:2 and :7 still use the old name, so importing checkout fails."),
]


def fix_script() -> list[LLMResponse]:
    return [LLMResponse(r.text, list(r.tool_calls), r.input_tokens, r.output_tokens) for r in FIX_SCRIPT]
