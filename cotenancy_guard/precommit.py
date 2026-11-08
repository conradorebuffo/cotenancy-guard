"""pre-commit guard for several sessions sharing one git working tree.

Two checks, both cheap:

1. Whole-index commit while another session is live. Git runs pre-commit with
   GIT_INDEX_FILE pointing at the index it is about to commit. Only a commit restricted
   to paths (``git commit -- <paths>`` or ``--only``) uses the temporary
   ``next-index-<pid>.lock``; ``commit -m``, ``--amend``, ``-a``, ``--include`` and a
   private index all commit a whole index, which includes whatever another session
   staged. When another session is live in the same working tree, those are refused.

2. Staged deletions of files that still exist on disk. A stale index can delete files
   another session committed; the file is still in the working tree, but the index
   records it as removed. Idea from @capraCoder in anthropics/claude-code#90943.

Environment:
  COTENANCY_GUARD_MODE   block (default) | warn | off
  COTENANCY_SESSION_ID   the current session; set by the plugin's SessionStart hook
  COTENANCY_ALLOW_DELETES=1   skip check 2 (e.g. an intentional ``git rm --cached``)
  COTENANCY_TTL_SECONDS  how long a marker counts as live (default 7200)

Fails open: an internal error prints a warning and lets the commit through, so a broken
guard never blocks work. It never edits the index.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from cotenancy_guard import markers  # noqa: E402
else:
    from . import markers

PREFIX = "cotenancy-guard"


def is_scoped(index_file: str | None) -> bool | None:
    """True if git is committing a path-restricted temporary index.

    None when GIT_INDEX_FILE is absent (not running under ``git commit``).
    """
    if not index_file:
        return None
    return fnmatch.fnmatch(os.path.basename(index_file).lower(), "next-index-*.lock")


def staged_deletions_on_disk(toplevel: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=D", "-z"],
                         cwd=toplevel, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git diff --cached failed: {out.stderr.strip()}")
    paths = [p for p in out.stdout.split("\0") if p]
    return [p for p in paths if os.path.lexists(os.path.join(toplevel, p))]


def evaluate(cwd: str, env: dict) -> tuple[list[str], list[str]]:
    """Return (violations, warnings). Pure apart from git and marker reads."""
    violations: list[str] = []
    warnings: list[str] = []
    toplevel, marker_dir = markers.repo_paths(cwd)

    scoped = is_scoped(env.get("GIT_INDEX_FILE"))
    if scoped is False:
        me = env.get("COTENANCY_SESSION_ID", "")
        live, problems = markers.live_sessions(marker_dir, toplevel)
        warnings.extend(problems)
        others = [m for m in live if m["session_id"] != me]
        if others:
            names = ", ".join(m["session_id"][:8] for m in others)
            violations.append(
                f"this commit takes the whole shared index, and {len(others)} other session(s) "
                f"are live in this working tree ({names}). Anything they staged would go into "
                f"your commit. Commit only your paths: git commit -m MSG -- <paths>")

    if env.get("COTENANCY_ALLOW_DELETES") != "1":
        gone = staged_deletions_on_disk(toplevel)
        if gone:
            shown = ", ".join(gone[:5]) + (" ..." if len(gone) > 5 else "")
            violations.append(
                f"the index deletes {len(gone)} file(s) that still exist on disk ({shown}). "
                f"That is what a stale index looks like when another session committed them. "
                f"If you meant it (git rm --cached), set COTENANCY_ALLOW_DELETES=1")
    return violations, warnings


def main(argv: list[str] | None = None, env: dict | None = None, cwd: str | None = None) -> int:
    env = dict(os.environ) if env is None else env
    cwd = os.getcwd() if cwd is None else cwd
    mode = env.get("COTENANCY_GUARD_MODE", "block").strip().lower()
    if mode == "off":
        return 0
    try:
        violations, warnings = evaluate(cwd, env)
    except Exception as exc:  # fail open, but say so
        print(f"{PREFIX}: skipped after an internal error: {exc}", file=sys.stderr)
        return 0
    for w in warnings:
        print(f"{PREFIX}: warning: {w}", file=sys.stderr)
    if not violations:
        return 0
    for v in violations:
        print(f"{PREFIX}: {v}", file=sys.stderr)
    if mode == "warn":
        print(f"{PREFIX}: warn mode, commit allowed", file=sys.stderr)
        return 0
    print(f"{PREFIX}: commit refused. To bypass once: COTENANCY_GUARD_MODE=off", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
