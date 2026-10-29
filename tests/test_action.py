"""GitHub Actions mode, run against a local repository instead of GitHub."""
import json

from app import github_client
from app.demo import simulate_ci_log
from tests.helpers import make_repo


def test_action_mode_end_to_end(tmp_path, monkeypatch, capsys):
    repo, green, broken = make_repo(tmp_path / "src")
    log = simulate_ci_log(repo, broken, tmp_path)

    class FakeGitHub(github_client.LocalCodeHost):
        def __init__(self, token=None):
            assert token == "ghs_test_token"
            super().__init__()

        def failing_log(self, repo_row, incident):
            return log

    monkeypatch.setattr(github_client, "GitHubCodeHost", FakeGitHub)

    event = {
        "action": "completed",
        "workflow_run": {"id": 777, "workflow_id": 5, "name": "CI", "conclusion": "failure", "head_branch": "main",
                         "head_sha": broken, "head_repository": {"full_name": "acme/shop"}},
        "repository": {"full_name": "acme/shop", "clone_url": str(repo), "default_branch": "main"},
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_token")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    monkeypatch.setenv("GREENLIGHT_DATABASE_URL", f"sqlite:///{tmp_path / 'action.db'}")
    monkeypatch.setenv("SANDBOX_BACKEND", "local-noinstall")

    # The last green commit is normally looked up on GitHub; here we hand it over.
    monkeypatch.setattr(FakeGitHub, "last_green_sha", lambda self, r, i: green)

    from app import db
    db.reset_engine()
    from app.action import main
    assert main() == 0
    out = capsys.readouterr().out
    assert "::notice title=GreenLight result::diagnosed_no_fix" in out      # no AI key in tests
    text = summary.read_text()
    assert "GreenLight: diagnosed no fix" in text and "Reproduced" in text
    assert (tmp_path / "runner" / "greenlight" / "report.html").exists()
    db.reset_engine()


def test_action_ignores_success(tmp_path, monkeypatch, capsys):
    event = {"action": "completed", "workflow_run": {"id": 1, "conclusion": "success"}, "repository": {"full_name": "a/b"}}
    p = tmp_path / "e.json"
    p.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(p))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GREENLIGHT_DATABASE_URL", f"sqlite:///{tmp_path / 'a.db'}")
    from app import db
    db.reset_engine()
    from app.action import main
    assert main() == 0
    assert "Nothing to do" in capsys.readouterr().out
    db.reset_engine()
