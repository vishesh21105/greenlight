from pathlib import Path

import pytest

from app.agent.loop import Budget, build_initial_messages, run_agent
from app.agent.tools import ToolContext, call_tool
from app.llm import LLMResponse, ScriptedProvider
from app.patches import apply_diff
from app.policy import load_policy
from app.sandbox import LocalSandbox
from tests.helpers import fix_script, make_repo, tool

TARGETS = ["tests/test_checkout.py::test_checkout_total"]


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    repo, green, broken = make_repo(tmp_path / "repo")
    return ToolContext(repo_dir=repo, deps_dir=tmp_path / "deps", policy=load_policy(repo),
                       sandbox=LocalSandbox(install_deps=False), py_version="3.12",
                       failing_targets=TARGETS, failing_sha=broken, base_sha=green)


def test_commit_diff_shows_the_rename(ctx):
    out = call_tool(ctx, "get_commit_diff", {})
    assert "-def compute_total" in out and "+def calc_total" in out


def test_find_symbol_and_search(ctx):
    out = call_tool(ctx, "find_symbol", {"name": "calc_total"})
    assert "tinyshop/pricing.py:6: function calc_total" in out
    out = call_tool(ctx, "search_code", {"query": "compute_total"})
    assert "tinyshop/checkout.py" in out
    assert "Error" in call_tool(ctx, "find_symbol", {"name": "x; rm -rf /"})
    assert "Error" in call_tool(ctx, "search_code", {"query": "a", "path_glob": "../../etc"})


def test_read_file_is_bounded_and_safe(ctx):
    (ctx.repo_dir / ".env").write_text("SECRET=1")
    assert "Error" in call_tool(ctx, "read_file", {"path": "../../etc/passwd"})
    assert "secrets" in call_tool(ctx, "read_file", {"path": ".env"})
    out = call_tool(ctx, "read_file", {"path": "tinyshop/checkout.py", "start_line": 1, "end_line": 3})
    assert "    1  from tinyshop.models import Item" in out and "def checkout" not in out


def test_run_failing_tests_has_a_budget(ctx):
    ctx.max_test_runs = 1
    first = call_tool(ctx, "run_failing_tests", {})
    assert "FAILED" in first
    assert "budget" in call_tool(ctx, "run_failing_tests", {})


def test_propose_patch_rejects_unsafe_edits(ctx):
    bad = call_tool(ctx, "propose_patch", {"edits": [{"path": ".github/workflows/ci.yml", "old_text": "pytest -q",
                                                      "new_text": "pytest -q || true"}],
                                           "rationale": "make CI always green no matter what"})
    assert "NOT accepted" in bad and ctx.proposed is None
    skip = call_tool(ctx, "propose_patch", {"edits": [{"path": "tests/test_checkout.py",
                                                       "old_text": "def test_checkout_total():",
                                                       "new_text": "@pytest.mark.skip\ndef test_checkout_total():"}],
                                            "rationale": "skip the failing test so the build is green"})
    assert "NOT accepted" in skip and ctx.proposed is None


def test_unknown_tool_and_bad_args(ctx):
    assert "unknown tool" in call_tool(ctx, "run_shell", {"command": "ls"})
    assert "not valid JSON" in call_tool(ctx, "read_file", {"_invalid_arguments": "{oops"})


def test_agent_loop_fixes_the_bug(ctx):
    llm = ScriptedProvider(fix_script())
    messages = build_initial_messages("code_bug", "ImportError", "ImportError: cannot import name 'compute_total'",
                                      TARGETS, "max 3 files")
    events = []
    outcome = run_agent(llm, ctx, messages, Budget(), on_event=lambda *e: events.append(e))
    assert outcome.kind == "patch"
    assert "tinyshop/checkout.py" in ctx.proposed.files
    # The patch really fixes the build.
    ok, msg = apply_diff(ctx.repo_dir, ctx.proposed.diff)
    assert ok, msg
    res = ctx.sandbox.run_tests(ctx.repo_dir, ctx.deps_dir, ctx.policy, "3.12")
    assert res.passed, res.output
    # Every tool call got a matching tool result in the conversation.
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert len(tool_msgs) == 4
    assert [e[1] for e in events if e[0] == "tool_call"] == ["get_commit_diff", "find_symbol", "read_file", "propose_patch"]


def test_agent_loop_stops_on_step_budget(ctx):
    llm = ScriptedProvider(lambda messages, tools: tool("list_files"))
    outcome = run_agent(llm, ctx, build_initial_messages("code_bug", "", "", TARGETS, ""), Budget(max_steps=4))
    assert outcome.kind == "budget" and outcome.steps == 4


def test_agent_loop_stops_on_cost_budget(ctx):
    llm = ScriptedProvider(lambda messages, tools: tool("list_files"))
    outcome = run_agent(llm, ctx, build_initial_messages("code_bug", "", "", TARGETS, ""),
                        Budget(max_cost_usd=0.05), on_usage=lambda r: 0.02)
    assert outcome.kind == "budget" and "cost" in outcome.message


def test_agent_that_never_decides_is_treated_as_give_up(ctx):
    llm = ScriptedProvider([LLMResponse(text="I think it is fine."), LLMResponse(text="Really, it is fine.")])
    outcome = run_agent(llm, ctx, build_initial_messages("code_bug", "", "", TARGETS, ""), Budget())
    assert outcome.kind == "give_up"


def test_model_error_ends_cleanly(ctx):
    class Broken:
        name, model = "x", "y"

        def chat(self, *a, **k):
            raise TimeoutError("model down")

    outcome = run_agent(Broken(), ctx, build_initial_messages("code_bug", "", "", TARGETS, ""), Budget())
    assert outcome.kind == "error"
