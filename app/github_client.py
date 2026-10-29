"""Talking to the code host (GitHub), plus a local stand-in for offline development.

GitHubCodeHost  - the real thing: a GitHub App with least-privilege permissions.
LocalCodeHost   - uses local git folders instead of GitHub; for development and the
                  offline demo. Same interface, so the pipeline never knows the difference.
"""
from __future__ import annotations

import io
import itertools
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Optional, Protocol

from app import config
from app.models import Incident, Repository
from app.scrub import scrub_text

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def run_git(args: list[str], cwd: Path | None = None, timeout: int = 120) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, timeout=timeout, env=_GIT_ENV)
    if proc.returncode != 0:
        raise RuntimeError(scrub_text(f"git {args[0]} failed: {proc.stderr.strip()[:500]}"))
    return proc.stdout.strip()


class CodeHost(Protocol):
    def checkout(self, repo: Repository, incident: Incident, dest: Path) -> None: ...
    def failing_log(self, repo: Repository, incident: Incident) -> str: ...
    def last_green_sha(self, repo: Repository, incident: Incident) -> Optional[str]: ...
    def open_pull_request(self, repo: Repository, incident: Incident, patched_dir: Path,
                          branch: str, title: str, body: str) -> str: ...
    def comment(self, repo: Repository, incident: Incident, body: str) -> Optional[str]: ...


def _commit_patch(patched_dir: Path, branch: str, title: str) -> None:
    run_git(["checkout", "-q", "-b", branch], patched_dir)
    run_git(["add", "-A"], patched_dir)
    run_git(["-c", "user.name=GreenLight", "-c", "user.email=greenlight@users.noreply.github.com",
             "commit", "-q", "-m", title], patched_dir)


class LocalCodeHost:
    """Offline stand-in. `repo.clone_url` is a local folder; the CI log is a file."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace or config.settings.workspace_dir)

    def log_path(self, incident: Incident) -> Path:
        return self.workspace / incident.id / "ci_log.txt"

    def checkout(self, repo, incident, dest):
        run_git(["clone", "-q", "--no-hardlinks", repo.clone_url, str(dest)])
        run_git(["checkout", "-q", incident.failing_sha], dest)

    def failing_log(self, repo, incident):
        return self.log_path(incident).read_text()

    def last_green_sha(self, repo, incident):
        return incident.last_green_sha

    def open_pull_request(self, repo, incident, patched_dir, branch, title, body):
        _commit_patch(patched_dir, branch, title)
        # "Push" the branch back to the source folder, like pushing to GitHub.
        run_git(["push", "-q", repo.clone_url, f"{branch}:{branch}"], patched_dir)
        (self.workspace / incident.id / "pull_request.md").write_text(f"# {title}\n\n{body}\n")
        return f"local://{repo.full_name}/tree/{branch}"

    def comment(self, repo, incident, body):
        path = self.workspace / incident.id / "comment.md"
        path.write_text(body)
        return None


class GitHubCodeHost:
    """The real GitHub.

    Two ways to log in:
    - GitHub App (server mode): GITHUB_APP_ID + GITHUB_PRIVATE_KEY_PATH; a short-lived token per repository.
    - A ready token (GitHub Actions mode): the GITHUB_TOKEN that Actions gives every workflow run.
    """

    def __init__(self, token: Optional[str] = None) -> None:
        self._static_token = token
        self._integration = None
        if token:
            return
        from github import Auth, GithubIntegration
        s = config.settings
        if not (s.github_app_id and s.github_private_key_path):
            raise RuntimeError("Set GITHUB_APP_ID and GITHUB_PRIVATE_KEY_PATH, or pass a token")
        key = Path(s.github_private_key_path).read_text()
        self._integration = GithubIntegration(auth=Auth.AppAuth(int(s.github_app_id), key))

    def _token(self, repo: Repository) -> str:
        if self._static_token:
            return self._static_token
        return self._integration.get_access_token(repo.github_installation_id).token

    def _gh_repo(self, repo: Repository):
        from github import Auth, Github
        return Github(auth=Auth.Token(self._token(repo))).get_repo(repo.full_name)

    def _authed_url(self, repo: Repository) -> str:
        return f"https://x-access-token:{self._token(repo)}@github.com/{repo.full_name}.git"

    def checkout(self, repo, incident, dest):
        run_git(["clone", "-q", self._authed_url(repo), str(dest)], timeout=600)
        # Remove the token from the saved remote so it never sits on disk.
        run_git(["remote", "set-url", "origin", f"https://github.com/{repo.full_name}.git"], dest)
        run_git(["checkout", "-q", incident.failing_sha], dest)

    def failing_log(self, repo, incident):
        import httpx
        token = self._token(repo)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        base = f"https://api.github.com/repos/{repo.full_name}/actions/runs/{incident.run_id}"
        with httpx.Client(headers=headers, timeout=60, follow_redirects=True) as client:
            resp = client.get(f"{base}/jobs", params={"per_page": 50})
            resp.raise_for_status()
            parts = []
            for job in resp.json().get("jobs", []):
                if job.get("conclusion") != "failure":
                    continue
                r = client.get(f"https://api.github.com/repos/{repo.full_name}/actions/jobs/{job['id']}/logs")
                if r.status_code == 200:
                    parts.append(f"===== job: {job.get('name')} =====\n{r.text}")
            if not parts:   # fall back to the whole run's log archive
                r = client.get(f"{base}/logs")
                if r.status_code == 200:
                    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                        for name in zf.namelist():
                            parts.append(zf.read(name).decode(errors="replace"))
        if not parts:
            raise RuntimeError("could not download the failing run's logs (does the token have actions: read?)")
        return "\n".join(parts)

    def last_green_sha(self, repo, incident):
        """The newest earlier commit where the SAME workflow passed.

        Looks on the failing branch first, then on the default branch (a new branch usually
        starts from there and has no passing runs of its own yet).
        """
        gh = self._gh_repo(repo)
        branches = [incident.branch] if incident.branch else []
        if repo.default_branch and repo.default_branch not in branches:
            branches.append(repo.default_branch)
        for branch in branches:
            if incident.workflow_id:
                runs = gh.get_workflow(incident.workflow_id).get_runs(branch=branch, status="success")
            else:
                runs = gh.get_workflow_runs(branch=branch, status="success")
            for run in itertools.islice(runs, 30):      # islice: safe even when there are no runs
                if run.head_sha == incident.failing_sha:
                    continue
                try:
                    # Only a commit that comes before the failing one is a fair comparison.
                    if gh.compare(run.head_sha, incident.failing_sha).status in ("ahead", "identical"):
                        return run.head_sha
                except Exception:
                    continue
        return None

    def open_pull_request(self, repo, incident, patched_dir, branch, title, body):
        from github import GithubException
        _commit_patch(patched_dir, branch, title)
        run_git(["push", "-q", self._authed_url(repo), f"{branch}:refs/heads/{branch}"], patched_dir, timeout=180)
        base = incident.branch or repo.default_branch
        try:
            pr = self._gh_repo(repo).create_pull(title=title, body=body, head=branch, base=base, draft=True)
            return pr.html_url
        except GithubException as exc:
            # Some repositories do not let GitHub Actions open pull requests (a repository setting).
            # The verified fix branch is pushed anyway; give the human a one-click link to open the PR.
            if exc.status in (403, 422):
                return f"https://github.com/{repo.full_name}/compare/{base}...{branch}?expand=1"
            raise

    def comment(self, repo, incident, body):
        commit = self._gh_repo(repo).get_commit(incident.failing_sha)
        return commit.create_comment(body).html_url


def get_code_host(kind: str = "github") -> CodeHost:
    return LocalCodeHost() if kind == "local" else GitHubCodeHost()
