"""Claude Code PostToolUse hook: record which files this session edited.

Wired by the plugin to Edit, Write and NotebookEdit. It appends the edited path, relative
to the repository's top level, to ``<git-common-dir>/cotenancy/<session_id>.paths``.
The Bash rewrite hook (``bash_hook.py``) reads that list to know which changes are this
session's own. Files a session changes through Bash are not recorded: the rewrite hook
treats them as not attributed and does not guess.

Always exits 0. Problems go to stderr.
"""
from __future__ import annotations

import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from cotenancy_guard import markers  # noqa: E402
else:
    from . import markers

TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def handle(payload: dict) -> str:
    if payload.get("tool_name") not in TOOLS:
        return "ignored tool"
    session_id = str(payload.get("session_id") or "")
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not session_id or not target:
        return "nothing to record"
    cwd = payload.get("cwd") or os.getcwd()
    target = target if os.path.isabs(target) else os.path.join(cwd, target)
    try:
        toplevel, marker_dir = markers.repo_paths(os.path.dirname(target) or cwd)
    except (RuntimeError, OSError):
        return "not in a git repository"
    rel = os.path.relpath(markers.norm_path(target), toplevel)
    if rel.startswith(".."):
        return "outside the repository"
    markers.add_touched(marker_dir, session_id, [rel])
    return "recorded"


def main() -> int:
    try:
        handle(json.load(sys.stdin))
    except Exception as exc:  # never block the session
        print(f"cotenancy-guard touch hook: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
