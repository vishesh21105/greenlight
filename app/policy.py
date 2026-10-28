"""The `.greenlight.yml` policy file: the house rules a repository sets for GreenLight.

If a repository has no policy file, safe defaults apply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

POLICY_FILENAME = ".greenlight.yml"

# Paths GreenLight may never change, whatever the policy says.
ALWAYS_BLOCKED = [POLICY_FILENAME, ".git/**", ".github/**", "**/.env", "**/*.env", "**/*.pem", "**/*.key"]


@dataclass
class Policy:
    mode: str = "pull_request"                     # pull_request / comment_only
    languages: list[str] = field(default_factory=lambda: ["python"])
    test_command: list[str] = field(default_factory=lambda: ["python", "-m", "pytest", "-q"])
    install_command: list[str] = field(default_factory=lambda: ["pip", "install", "-r", "requirements.txt"])
    allowed_paths: list[str] = field(default_factory=lambda: ["**"])
    blocked_paths: list[str] = field(default_factory=list)
    max_files_changed: int = 3
    max_lines_changed: int = 40
    allow_test_edits: str = "flag"                 # no / flag / yes
    cost_limit_usd_per_day: float = 5.0

    def all_blocked(self) -> list[str]:
        return ALWAYS_BLOCKED + list(self.blocked_paths)


class PolicyError(ValueError):
    pass


_ALLOWED_COMMANDS = {"python", "python3", "pytest", "pip", "pip3"}


def _check_command(cmd, name: str) -> list[str]:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise PolicyError(f"{name} must be a list of strings, e.g. [\"python\", \"-m\", \"pytest\", \"-q\"]")
    if cmd[0] not in _ALLOWED_COMMANDS:
        raise PolicyError(f"{name} must start with one of {sorted(_ALLOWED_COMMANDS)}")
    if any(c in {";", "&&", "||", "|", "`"} or "$(" in c for c in cmd):
        raise PolicyError(f"{name} may not contain shell operators")
    return cmd


def parse_policy(text: str) -> Policy:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise PolicyError("policy file must be a mapping")
    p = Policy()
    known = set(p.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
    for key, value in data.items():
        setattr(p, key, value)
    if p.mode not in {"pull_request", "comment_only"}:
        raise PolicyError("mode must be pull_request or comment_only")
    if p.allow_test_edits not in {"no", "flag", "yes"}:
        raise PolicyError("allow_test_edits must be no, flag or yes")
    p.test_command = _check_command(p.test_command, "test_command")
    p.install_command = _check_command(p.install_command, "install_command")
    p.max_files_changed = int(p.max_files_changed)
    p.max_lines_changed = int(p.max_lines_changed)
    return p


def load_policy(repo_dir: str | Path) -> Policy:
    path = Path(repo_dir) / POLICY_FILENAME
    if not path.exists():
        return Policy()
    return parse_policy(path.read_text())


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern:
    """Turn a glob like 'src/**/*.py' into a regex. '**' crosses folders, '*' does not."""
    i, out = 0, []
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)
