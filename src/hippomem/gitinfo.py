"""Reading git state on the engineer's machine, for hooks and git events."""

from __future__ import annotations

import subprocess
from pathlib import Path

MAX_DIFF_CHARS = 60_000


def git(cwd: Path | str, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def toplevel(cwd: Path | str) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel"))


def repo_name(cwd: Path | str) -> str:
    """The repo's name: the last part of its origin URL, or its folder name."""
    try:
        url = git(cwd, "remote", "get-url", "origin")
    except RuntimeError:
        return toplevel(cwd).name
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def current_branch(cwd: Path | str) -> str:
    try:
        return git(cwd, "symbolic-ref", "--short", "HEAD")
    except RuntimeError:
        return "HEAD"


def default_branch(cwd: Path | str) -> str:
    try:
        return git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1]
    except RuntimeError:
        return ""


def head(cwd: Path | str) -> str:
    try:
        return git(cwd, "rev-parse", "HEAD")
    except RuntimeError:
        return ""


def relative_path(cwd: Path | str, file_path: str) -> str:
    root = toplevel(cwd).resolve()
    path = Path(file_path)
    absolute = (path if path.is_absolute() else Path(cwd) / path).resolve()
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def changed_files(cwd: Path | str, since_commit: str = "") -> list[str]:
    """Files modified in the working tree, plus any committed since `since_commit`."""
    files: set[str] = set()
    for line in git(cwd, "status", "--porcelain").splitlines():
        if line[3:]:
            files.add(line[3:].split(" -> ")[-1].strip('"'))
    current = head(cwd)
    if since_commit and current and since_commit != current:
        files.update(f for f in git(cwd, "diff", "--name-only", since_commit, current).splitlines() if f)
    return sorted(files)


def file_history(cwd: Path | str, rel_path: str, limit: int = 20) -> dict:
    """Who wrote a file and when, for asking its author why it is built that way."""
    try:
        log = git(cwd, "log", f"-{limit}", "--format=%H%x09%ae%x09%an%x09%cs%x09%s", "--", rel_path)
    except RuntimeError:
        return {"commits": 0}
    entries = [line.split("\t") for line in log.splitlines() if line]
    if not entries:
        return {"commits": 0}
    counts: dict[str, int] = {}
    for e in entries:
        counts[e[1]] = counts.get(e[1], 0) + 1
    top_email = max(counts, key=lambda k: counts[k])
    top_name = next(e[2] for e in entries if e[1] == top_email)
    return {
        "commits": len(entries),
        "top_author_email": top_email,
        "top_author_name": top_name,
        "last": {"commit": entries[0][0], "email": entries[0][1], "date": entries[0][3], "subject": entries[0][4]},
    }


def commit_files(cwd: Path | str, commit: str = "HEAD") -> list[str]:
    return [f for f in git(cwd, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit).splitlines() if f]


def commit_diff(cwd: Path | str, commit: str = "HEAD") -> str:
    return git(cwd, "show", "--format=%H %s", commit)[:MAX_DIFF_CHARS]


def merged_branch(cwd: Path | str) -> str:
    """The branch just merged into HEAD, from the reflog message
    ('merge feat/x: Merge made by ...' or 'merge feat/x: Fast-forward')."""
    message = git(cwd, "reflog", "-1", "--format=%gs")
    if not message.startswith("merge "):
        return ""
    return message[len("merge ") :].split(":", 1)[0].strip()


def merge_range(cwd: Path | str) -> tuple[list[str], str]:
    """Commits and diff brought in by the last merge (ORIG_HEAD..HEAD)."""
    commits = [c for c in git(cwd, "rev-list", "ORIG_HEAD..HEAD").splitlines() if c]
    diff = git(cwd, "diff", "ORIG_HEAD", "HEAD")[:MAX_DIFF_CHARS]
    return commits, diff
