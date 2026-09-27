import json
from types import SimpleNamespace

import httpx

from hubble import github
from hubble.main import run_headless
from hubble.permissions import Permissions
from hubble.provider import TurnResult
from hubble.session import SessionStore
from hubble.tools import ToolContext

REPO = {"full_name": "me/app"}


def comment_event(body, assoc="OWNER", on_pr=True, user_type="User"):
    issue = {"number": 7, "title": "Fix login", "body": "Login breaks on Safari"}
    if on_pr:
        issue["pull_request"] = {"url": "x"}
    return {"action": "created", "repository": REPO, "issue": issue,
            "comment": {"body": body, "author_association": assoc, "user": {"login": "alice", "type": user_type}}}


def test_pr_opened_builds_review_task():
    task = github.build_task("pull_request", {"action": "opened", "repository": REPO, "pull_request": {
        "number": 3, "title": "Add cache", "body": "Adds an LRU", "base": {"ref": "main"}, "draft": False}})
    assert task["number"] == 3 and task["can_edit"] is False
    assert "git diff origin/main...HEAD" in task["prompt"] and "Add cache" in task["prompt"]


def test_draft_or_closed_pr_is_skipped():
    base = {"repository": REPO, "pull_request": {"number": 3, "draft": True, "base": {"ref": "main"}}}
    assert github.build_task("pull_request", {**base, "action": "opened"}) is None
    assert github.build_task("pull_request", {**base, "action": "closed"}) is None


def test_mention_on_pr_can_edit_and_strips_mention():
    task = github.build_task("issue_comment", comment_event("@hubble please fix the Safari bug"))
    assert task["kind"] == "pr" and task["can_edit"] is True and task["number"] == 7
    assert "please fix the Safari bug" in task["prompt"] and "@hubble" not in task["prompt"].split("Request")[1]


def test_mention_on_issue_cannot_edit():
    task = github.build_task("issue_comment", comment_event("@hubble why does this happen?", on_pr=False))
    assert task["kind"] == "issue" and task["can_edit"] is False


def test_untrusted_commenters_bots_and_no_mention_are_ignored():
    assert github.build_task("issue_comment", comment_event("@hubble rm -rf", assoc="NONE")) is None
    assert github.build_task("issue_comment", comment_event("@hubble hi", assoc="CONTRIBUTOR")) is None
    assert github.build_task("issue_comment", comment_event("@hubble hi", user_type="Bot")) is None
    assert github.build_task("issue_comment", comment_event("thanks all")) is None
    assert github.build_task("issue_comment", comment_event("mail me at x@hubble.dev")) is None


def test_workflow_template_never_interpolates_event_text_into_shell():
    wf = github.WORKFLOW
    for line in wf.splitlines():
        if "${{" in line and ("comment.body" in line or "issue.body" in line or "issue.title" in line):
            # only allowed inside the `if:` expression, never in a run: script
            assert "contains(" in line, line
    assert "author_association" in wf and "HUBBLE_API_KEY" in wf


def test_install_workflow(tmp_path):
    path = github.install_workflow(tmp_path)
    assert path == tmp_path / ".github" / "workflows" / "hubble.yml"
    assert "hubble -p --github" in path.read_text(encoding="utf-8")


class Provider:
    def __init__(self):
        self.prompts = []

    def stream(self, model, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        return TurnResult(text="Looks good, one bug on line 3.")


def test_headless_github_run_posts_comment(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(comment_event("@hubble review this", on_pr=False)), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/app")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    posted = {}

    def handler(req):
        posted["url"] = str(req.url)
        posted["auth"] = req.headers["authorization"]
        posted["body"] = json.loads(req.content)["body"]
        return httpx.Response(201, json={"html_url": "https://github.com/me/app/issues/7#c1"})

    real_client = httpx.Client
    monkeypatch.setattr(github.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    provider = Provider()
    perms = Permissions("accept-edits")
    args = SimpleNamespace(output_format="text", quiet=True, continue_=False, resume=None, no_session=True,
                           _providers={}, github=True, image=[])
    code = run_headless(args, {"model": "m", "max_turns": 3, "max_tokens": 10, "context_window": 1000,
                               "auto_compact_ratio": 0, "persona": "code", "web_tools": False},
                        provider, ToolContext(root=tmp_path), perms, SessionStore(tmp_path), "")
    assert code == 0
    assert "review this" in provider.prompts[0]
    assert posted["url"].endswith("/repos/me/app/issues/7/comments") and posted["auth"] == "Bearer ghs_test"
    assert posted["body"].startswith("Looks good") and "Hubble" in posted["body"]
    assert "write_file" in perms.deny  # issue answers never edit files
