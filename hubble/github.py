"""GitHub integration: run Hubble from GitHub Actions on pull requests and @hubble mentions.

`hubble -p --github` reads the Actions event ($GITHUB_EVENT_PATH), turns it into a prompt, runs
the agent, and posts the answer back as a comment using $GITHUB_TOKEN:

  pull_request (opened / synchronize / ready_for_review)   -> code review comment on the PR
  issue_comment / pull_request_review_comment with @hubble -> answers the request; on a PR the
                                                              workflow can commit its edits
  issues (opened) with @hubble in the body                 -> answers on the issue

`/install-github` writes a ready-to-use workflow into .github/workflows/hubble.yml.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

MENTION = re.compile(r"(?<![\w-])@hubble\b", re.IGNORECASE)
TRUSTED_AUTHORS = {"OWNER", "MEMBER", "COLLABORATOR"}
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")

REVIEW_PROMPT = """You are reviewing pull request #{number} in {repo}: "{title}".

PR description:
{body}

The PR branch is checked out. Run `git diff {base}...HEAD` (and read files as needed) to see the
change. Review it for correctness bugs, security problems, missing tests, and unclear code. Be
specific: cite path:line, explain the problem and suggest the fix. Skip style nitpicks a linter
would catch. If the change looks good, say so briefly. Do not modify any files.
Reply in GitHub-flavored Markdown; this text is posted as the review comment."""

MENTION_PROMPT = """You were mentioned in {kind} #{number} in {repo}: "{title}".

{context}

Request from @{author}:
{request}

{instructions}
Reply in GitHub-flavored Markdown; your final answer is posted as a comment."""


class GitHubError(Exception):
    pass


def load_event(path: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = path or os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not Path(path).is_file():
        raise GitHubError("--github needs GITHUB_EVENT_PATH (run it inside GitHub Actions)")
    return name, json.loads(Path(path).read_text(encoding="utf-8"))


def build_task(event_name: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{prompt, number, kind, can_edit} for events Hubble should act on; None to skip."""
    repo = (event.get("repository") or {}).get("full_name", "")
    if event_name == "pull_request":
        pr = event.get("pull_request") or {}
        if event.get("action") not in ("opened", "synchronize", "reopened", "ready_for_review") or pr.get("draft"):
            return None
        return {"kind": "pr", "number": pr.get("number"), "can_edit": False,
                "prompt": REVIEW_PROMPT.format(number=pr.get("number"), repo=repo, title=pr.get("title", ""),
                                               body=(pr.get("body") or "(none)")[:4000],
                                               base="origin/" + (pr.get("base") or {}).get("ref", "main"))}
    if event_name in ("issue_comment", "pull_request_review_comment"):
        comment = event.get("comment") or {}
        if event.get("action") != "created" or not MENTION.search(comment.get("body") or ""):
            return None
        if (comment.get("user") or {}).get("type") == "Bot":
            return None  # never answer bots, including our own replies
        if comment.get("author_association") not in TRUSTED_AUTHORS:
            return None  # same gate as the workflow's `if:`, in case a custom workflow skips it
        issue = event.get("issue") or event.get("pull_request") or {}
        is_pr = event_name == "pull_request_review_comment" or bool(issue.get("pull_request"))
        context = f"Description:\n{(issue.get('body') or '(none)')[:4000]}"
        if event_name == "pull_request_review_comment":
            context += f"\n\nThe comment is on {comment.get('path')} line {comment.get('line') or comment.get('original_line')}:\n```diff\n{(comment.get('diff_hunk') or '')[-2000:]}\n```"
        instructions = ("The PR branch is checked out. If the request asks for a change, make it with your "
                        "tools and verify it (run the tests); the workflow commits your edits to the PR. "
                        "Summarize what you changed." if is_pr else
                        "Investigate the repository to answer. Do not modify files; describe the fix instead.")
        return {"kind": "pr" if is_pr else "issue", "number": issue.get("number"), "can_edit": is_pr,
                "prompt": MENTION_PROMPT.format(kind="pull request" if is_pr else "issue", number=issue.get("number"),
                                                repo=repo, title=issue.get("title", ""), context=context,
                                                author=(comment.get("user") or {}).get("login", "someone"),
                                                request=MENTION.sub("", comment.get("body") or "").strip(),
                                                instructions=instructions)}
    if event_name == "issues":
        issue = event.get("issue") or {}
        if event.get("action") != "opened" or not MENTION.search(issue.get("body") or ""):
            return None
        if issue.get("author_association") not in TRUSTED_AUTHORS:
            return None
        return {"kind": "issue", "number": issue.get("number"), "can_edit": False,
                "prompt": MENTION_PROMPT.format(kind="issue", number=issue.get("number"), repo=repo,
                                                title=issue.get("title", ""), context="",
                                                author=(issue.get("user") or {}).get("login", "someone"),
                                                request=MENTION.sub("", issue.get("body") or "").strip(),
                                                instructions="Investigate the repository to answer. Do not "
                                                             "modify files; describe the fix instead.")}
    return None


def post_comment(repo: str, number: int, body: str, token: Optional[str] = None,
                 http: Optional[httpx.Client] = None) -> str:
    token = token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise GitHubError("GITHUB_TOKEN is not set; cannot post the comment")
    client = http or httpx.Client(timeout=30)
    resp = client.post(f"{API}/repos/{repo}/issues/{number}/comments",
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28"},
                       json={"body": body[:65000]})
    if resp.status_code >= 300:
        raise GitHubError(f"posting the comment failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json().get("html_url", "")


def format_reply(result: str, stats, model: str, is_error: bool) -> str:
    if is_error:
        body = f"Hubble could not finish: {stats.error or 'interrupted'}"
    else:
        body = result.strip() or "(no answer)"
    return (f"{body}\n\n<sub>🔭 Hubble · {model} · {stats.model_calls} model calls, {stats.tool_calls} tool "
            f"calls</sub>")


WORKFLOW = """name: Hubble

on:
  pull_request:
    types: [opened, synchronize, ready_for_review]
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]
  issues:
    types: [opened]

permissions:
  contents: write        # lets Hubble push its edits to a PR branch when asked in a comment
  pull-requests: write
  issues: write

jobs:
  hubble:
    # Reviews every PR; otherwise only runs when someone writes @hubble.
    # Only people with write access can trigger it: a mention runs an agent with your API key
    # and push access, so it must not be triggerable by any passer-by on a public repo.
    if: >-
      (github.event_name == 'pull_request' &&
       github.event.pull_request.head.repo.full_name == github.repository) ||
      (contains(github.event.comment.body, '@hubble') &&
       contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association)) ||
      (contains(github.event.issue.body, '@hubble') && github.event_name == 'issues' &&
       contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.issue.author_association))
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Check out the PR branch for PR comments
        if: github.event.issue.pull_request || github.event_name == 'pull_request_review_comment'
        run: gh pr checkout ${{ github.event.issue.number || github.event.pull_request.number }}
        env:
          GH_TOKEN: ${{ github.token }}
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install hubble-cli
      - name: Run Hubble
        run: hubble -p --github --permission-mode accept-edits --allow "shell(*)" --no-session
        env:
          HUBBLE_API_KEY: ${{ secrets.HUBBLE_API_KEY }}
          HUBBLE_BASE_URL: ${{ vars.HUBBLE_BASE_URL }}       # optional; default: the AIHub gateway
          HUBBLE_MODEL: ${{ vars.HUBBLE_MODEL }}             # optional
          GITHUB_TOKEN: ${{ github.token }}
      - name: Push Hubble's edits to the PR
        if: github.event.issue.pull_request || github.event_name == 'pull_request_review_comment'
        # Never interpolate ${{ github.event.* }} text into this script: it is attacker-controlled.
        run: |
          if [ -n "$(git status --porcelain)" ]; then
            git config user.name "hubble[bot]"
            git config user.email "hubble[bot]@users.noreply.github.com"
            git add -A
            git commit -q -m "Hubble: apply requested change" -m "Requested in a comment by @$COMMENT_AUTHOR"
            git push
          fi
        env:
          COMMENT_AUTHOR: ${{ github.event.comment.user.login }}
"""


def install_workflow(root: Path) -> Path:
    path = root / ".github" / "workflows" / "hubble.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKFLOW, encoding="utf-8")
    return path
