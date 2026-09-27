"""Git worktrees: an isolated checkout of the repo on its own branch, so risky or parallel work
never touches the main working tree until you merge it.

Worktrees live in <repo>/.hubble/worktrees/<name> on branch hubble/<name>; that folder is added
to .git/info/exclude so it never shows up as untracked files.
"""

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")


class WorktreeError(Exception):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorktreeError(f"git {args[0]} failed: {e}") from None
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args[:2])}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout.strip()


def repo_root(path: Path) -> Optional[Path]:
    try:
        out = _git(path, "rev-parse", "--show-toplevel")
    except WorktreeError:
        return None
    return Path(out).resolve() if out else None


def main_repo(path: Path) -> Optional[Path]:
    """The main checkout, even when `path` is inside one of its worktrees."""
    try:
        common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except WorktreeError:
        return repo_root(path)
    p = Path(common).resolve()
    return p.parent if p.name == ".git" else repo_root(path)


def _exclude(repo: Path):
    try:
        info = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info"
    except WorktreeError:
        return
    info.mkdir(parents=True, exist_ok=True)
    f = info / "exclude"
    existing = f.read_text(encoding="utf-8") if f.exists() else ""
    if "/.hubble/worktrees/" not in existing:
        f.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + "/.hubble/worktrees/\n",
                     encoding="utf-8")


def create(path: Path, name: str, base: str = "HEAD") -> Dict[str, str]:
    if not NAME_RX.match(name):
        raise WorktreeError("worktree name: letters, digits, . _ - (max 61 chars)")
    repo = main_repo(path)
    if repo is None:
        raise WorktreeError(f"{path} is not inside a git repository")
    dest = repo / ".hubble" / "worktrees" / name
    if dest.exists():
        raise WorktreeError(f"worktree '{name}' already exists at {dest}")
    branch = f"hubble/{name}"
    _exclude(repo)
    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if exists:
        _git(repo, "worktree", "add", str(dest), branch)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(dest), base)
    return {"name": name, "path": str(dest), "branch": branch, "base": _git(repo, "rev-parse", "--short", base)}


def list_worktrees(path: Path) -> List[Dict[str, str]]:
    repo = main_repo(path)
    if repo is None:
        return []
    out, cur = [], {}
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = {}
        elif " " in line:
            k, v = line.split(" ", 1)
            cur[k] = v
        else:
            cur[line] = "true"
    hubble_dir = (repo / ".hubble" / "worktrees").resolve()
    items = []
    for w in out:
        p = Path(w.get("worktree", "")).resolve()
        items.append({"path": str(p), "branch": w.get("branch", "").removeprefix("refs/heads/"),
                      "head": w.get("HEAD", "")[:8], "hubble": str(p.parent) == str(hubble_dir),
                      "name": p.name if str(p.parent) == str(hubble_dir) else ""})
    return items


def status(path: Path, base_branch: str = "") -> Dict[str, str]:
    """Uncommitted changes and commits ahead of the branch it was made from."""
    dirty = _git(path, "status", "--porcelain", check=False)
    ahead = ""
    if base_branch:
        ahead = _git(path, "log", "--oneline", f"{base_branch}..HEAD", check=False)
    return {"dirty": dirty, "ahead": ahead,
            "diffstat": _git(path, "diff", "--stat", "HEAD", check=False)}


def commit_all(path: Path, message: str) -> str:
    """Commit everything in the worktree (new files included). Returns the short sha, or '' if
    there was nothing to commit or git refused (e.g. no user.name configured)."""
    if not _git(path, "status", "--porcelain", check=False):
        return ""
    _git(path, "add", "-A", check=False)
    proc = subprocess.run(["git", "-c", "user.name=Hubble", "-c", "user.email=hubble@localhost",
                           "commit", "-q", "--no-verify", "-m", message], cwd=path, capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    return _git(path, "rev-parse", "--short", "HEAD", check=False)


def remove(path: Path, name: str, force: bool = False, delete_branch: bool = False) -> str:
    repo = main_repo(path)
    if repo is None:
        raise WorktreeError("not inside a git repository")
    dest = repo / ".hubble" / "worktrees" / name
    if not dest.exists():
        raise WorktreeError(f"no worktree named '{name}'")
    if not force and _git(dest, "status", "--porcelain", check=False):
        raise WorktreeError(f"worktree '{name}' has uncommitted changes; commit them or remove with --force")
    _git(repo, "worktree", "remove", *(["--force"] if force else []), str(dest))
    if delete_branch:
        _git(repo, "branch", "-D" if force else "-d", f"hubble/{name}", check=False)
    return str(dest)


def current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
