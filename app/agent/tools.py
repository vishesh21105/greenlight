"""The agent's tools. Each one is narrow, checks its own arguments, and returns text.

A tool call from the model is a REQUEST, not permission: every argument is checked
here before anything runs. There is deliberately no "run any shell command" tool.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.patches import Edit, build_diff_from_edits, path_problem, validate_diff
from app.policy import Policy, path_matches
from app.sandbox import Sandbox
from app.scrub import scrub_text

MAX_TOOL_OUTPUT = 6000
SECRET_FILES = ["**/.env", "**/*.env", "**/*.pem", "**/*.key", "**/id_rsa*", ".git/**"]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,80}$")
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


@dataclass
class ProposedPatch:
    diff: str
    rationale: str
    files: list[str]
    lines_changed: int
    touches_tests: bool


@dataclass
class ToolContext:
    repo_dir: Path
    deps_dir: Path
    policy: Policy
    sandbox: Sandbox
    py_version: str
    failing_targets: list[str]
    failing_sha: str = "HEAD"
    base_sha: Optional[str] = None          # last green commit, if known
    max_test_runs: int = 3
    test_runs_used: int = 0
    proposed: Optional[ProposedPatch] = None
    gave_up: Optional[dict] = None
    on_test_run: Optional[Callable] = None   # lets the pipeline record sandbox runs


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    text = scrub_text(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters cut; ask for a narrower range ...]"


def _git(ctx: ToolContext, args: list[str]) -> str:
    proc = subprocess.run(["git", *args], cwd=str(ctx.repo_dir), capture_output=True, text=True,
                          timeout=30, env=_GIT_ENV)
    return proc.stdout if proc.returncode in (0, 1) else proc.stderr


# --------------------------------------------------------------------------- tools

def get_commit_diff(ctx: ToolContext, **_) -> str:
    if ctx.base_sha:
        out = _git(ctx, ["diff", "--no-color", "--no-ext-diff", f"{ctx.base_sha}..{ctx.failing_sha}"])
        label = f"Changes from the last green commit {ctx.base_sha[:8]} to the failing commit {ctx.failing_sha[:8]}"
    else:
        out = _git(ctx, ["show", "--no-color", "--no-ext-diff", "--format=commit %H%n%s%n", ctx.failing_sha])
        label = f"Changes in the failing commit {ctx.failing_sha[:8]}"
    return _clip(f"{label}:\n{out or '(no changes found)'}")


def search_code(ctx: ToolContext, query: str = "", path_glob: str = "", **_) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 200 or "\n" in query:
        return "Error: query must be a single line of 1 to 200 characters."
    args = ["grep", "-n", "-I", "--no-color", "--fixed-strings", "-e", query, "--"]
    if path_glob:
        if not isinstance(path_glob, str) or path_glob.startswith(("-", "/")) or ".." in path_glob:
            return "Error: path_glob must be a relative pattern such as 'tinyshop/*.py'."
        args.append(path_glob)
    out = _git(ctx, args)
    lines = [l for l in out.splitlines() if not path_matches(l.split(":", 1)[0], SECRET_FILES)]
    if not lines:
        return f"No matches for {query!r}."
    shown = lines[:60]
    more = f"\n[... {len(lines) - 60} more matches ...]" if len(lines) > 60 else ""
    return _clip("\n".join(shown) + more)


def find_symbol(ctx: ToolContext, name: str = "", **_) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        return "Error: name must be a Python identifier like compute_total."
    definitions = []
    for rel in _git(ctx, ["ls-files", "*.py"]).splitlines():
        path = ctx.repo_dir / rel
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (SyntaxError, OSError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                definitions.append(f"{rel}:{node.lineno}: {kind} {name}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        definitions.append(f"{rel}:{node.lineno}: variable {name}")
    usages = _git(ctx, ["grep", "-n", "-I", "--no-color", "-w", "--fixed-strings", "-e", name, "--", "*.py"])
    usage_lines = usages.splitlines()[:40]
    text = "Definitions:\n" + ("\n".join(definitions) if definitions else f"(no definition of {name} found)")
    text += "\n\nAll mentions:\n" + ("\n".join(usage_lines) if usage_lines else "(none)")
    return _clip(text)


def read_file(ctx: ToolContext, path: str = "", start_line: int = 1, end_line: Optional[int] = None, **_) -> str:
    if not isinstance(path, str):
        return "Error: path must be a string."
    problem = path_problem(path, ctx.repo_dir)
    if problem:
        return f"Error: {problem}"
    if path_matches(path, SECRET_FILES):
        return "Error: this file may contain secrets and cannot be read."
    target = ctx.repo_dir / path
    if not target.is_file():
        return f"Error: {path} does not exist. Use search_code or find_symbol to locate files."
    try:
        start = max(1, int(start_line))
        end = int(end_line) if end_line else start + 199
    except (TypeError, ValueError):
        return "Error: start_line and end_line must be numbers."
    end = min(end, start + 299)
    lines = target.read_text(errors="replace").splitlines()
    chunk = lines[start - 1:end]
    body = "\n".join(f"{n:>5}  {line}" for n, line in enumerate(chunk, start))
    tail = f"\n[file has {len(lines)} lines]" if end < len(lines) else ""
    return _clip(f"{path} (lines {start}-{min(end, len(lines))}):\n{body}{tail}")


def list_files(ctx: ToolContext, directory: str = ".", **_) -> str:
    if not isinstance(directory, str) or directory.startswith(("/", "-")) or ".." in directory:
        return "Error: directory must be relative to the repository root."
    files = _git(ctx, ["ls-files", "--", directory]).splitlines()
    files = [f for f in files if not path_matches(f, SECRET_FILES)]
    return _clip("\n".join(files[:200]) + (f"\n[... {len(files) - 200} more ...]" if len(files) > 200 else ""))


def run_failing_tests(ctx: ToolContext, **_) -> str:
    if ctx.test_runs_used >= ctx.max_test_runs:
        return f"Error: test-run budget used up ({ctx.max_test_runs} runs). Decide with what you have."
    ctx.test_runs_used += 1
    result = ctx.sandbox.run_tests(ctx.repo_dir, ctx.deps_dir, ctx.policy, ctx.py_version, ctx.failing_targets)
    if ctx.on_test_run:
        ctx.on_test_run("agent_rerun", result)
    status = "PASSED" if result.passed else ("TIMED OUT" if result.timed_out else f"FAILED (exit code {result.exit_code})")
    return _clip(f"Ran the failing tests on the ORIGINAL code: {status}\n{result.output[-4000:]}")


def propose_patch(ctx: ToolContext, edits: list | None = None, rationale: str = "", **_) -> str:
    if not isinstance(edits, list) or not edits:
        return "Error: edits must be a non-empty list of {path, old_text, new_text}."
    parsed = []
    for e in edits:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("path", "old_text", "new_text")):
            return "Error: every edit needs string fields path, old_text and new_text."
        parsed.append(Edit(e["path"], e["old_text"], e["new_text"]))
    if not isinstance(rationale, str) or len(rationale.strip()) < 10:
        return "Error: explain the root cause and why the fix is correct in the rationale."

    diff, errors = build_diff_from_edits(ctx.repo_dir, parsed)
    if errors:
        return "Patch NOT accepted:\n- " + "\n- ".join(errors)
    result = validate_diff(diff, ctx.policy, ctx.repo_dir)
    if not result.valid:
        return "Patch NOT accepted by the safety rules:\n- " + "\n- ".join(result.errors)
    ctx.proposed = ProposedPatch(diff, rationale.strip(), result.files, result.lines_changed, result.touches_tests)
    return "Patch accepted for verification. GreenLight will now test it in a fresh sandbox."


def give_up(ctx: ToolContext, diagnosis: str = "", reason: str = "", **_) -> str:
    ctx.gave_up = {"diagnosis": str(diagnosis)[:3000], "reason": str(reason)[:500]}
    return "Understood. GreenLight will post your diagnosis without a patch."


# --------------------------------------------------------------------------- registry

TOOLS: dict[str, Callable[..., str]] = {
    "get_commit_diff": get_commit_diff,
    "search_code": search_code,
    "find_symbol": find_symbol,
    "read_file": read_file,
    "list_files": list_files,
    "run_failing_tests": run_failing_tests,
    "propose_patch": propose_patch,
    "give_up": give_up,
}

TOOL_SCHEMAS: list[dict] = [
    {"name": "get_commit_diff", "description": "Show what changed between the last green commit and the failing commit.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "search_code", "description": "Search the repository for an exact piece of text. Returns file:line:text matches.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Exact text to find, one line"},
         "path_glob": {"type": "string", "description": "Optional, e.g. 'tinyshop/*.py'"}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "find_symbol", "description": "Find where a Python function, class or variable is defined and every place it is mentioned.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                    "required": ["name"], "additionalProperties": False}},
    {"name": "read_file", "description": "Read part of a file, with line numbers. At most 300 lines per call.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
         "required": ["path"], "additionalProperties": False}},
    {"name": "list_files", "description": "List the files tracked in a directory of the repository.",
     "parameters": {"type": "object", "properties": {"directory": {"type": "string"}}, "additionalProperties": False}},
    {"name": "run_failing_tests", "description": "Re-run only the failing tests on the original code in the sandbox. Limited uses.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "propose_patch",
     "description": "Propose the fix as exact text replacements. old_text must appear exactly once in the file; copy it exactly from read_file output, without the line numbers.",
     "parameters": {"type": "object", "properties": {
         "edits": {"type": "array", "items": {"type": "object", "properties": {
             "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
             "required": ["path", "old_text", "new_text"], "additionalProperties": False}},
         "rationale": {"type": "string", "description": "Root cause and why this fix is correct, citing file:line"}},
         "required": ["edits", "rationale"], "additionalProperties": False}},
    {"name": "give_up", "description": "Stop without a patch, and report your diagnosis. Use this when you are not confident.",
     "parameters": {"type": "object", "properties": {
         "diagnosis": {"type": "string", "description": "What you believe is wrong, citing file:line"},
         "reason": {"type": "string", "description": "Why you are not proposing a patch"}},
         "required": ["diagnosis", "reason"], "additionalProperties": False}},
]


def call_tool(ctx: ToolContext, name: str, arguments: dict) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        return f"Error: unknown tool {name!r}. Available tools: {', '.join(TOOLS)}."
    if "_invalid_arguments" in arguments:
        return "Error: the tool arguments were not valid JSON. Try again."
    try:
        return fn(ctx, **arguments)
    except TypeError as exc:
        return f"Error: bad arguments for {name}: {exc}"
    except subprocess.TimeoutExpired:
        return f"Error: {name} took too long."
