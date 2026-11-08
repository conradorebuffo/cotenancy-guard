"""Claude Code hook: keep a liveness marker for this session.

Wired by the plugin to three events (it reads the hook JSON from stdin):

  SessionStart  write the marker and export COTENANCY_SESSION_ID through
                CLAUDE_ENV_FILE, so the pre-commit hook can tell "me" from "another"
  Stop          refresh the marker at the end of every turn
  SessionEnd    remove the marker

Always exits 0: a liveness marker must never block a session. Problems go to stderr.
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


def handle(payload: dict, env: dict) -> str:
    event = payload.get("hook_event_name", "")
    session_id = str(payload.get("session_id") or "")
    cwd = payload.get("cwd") or os.getcwd()
    if not session_id:
        return "no session_id in hook input"
    try:
        toplevel, marker_dir = markers.repo_paths(cwd)
    except RuntimeError:
        return "not a git repository"
    if event == "SessionEnd":
        markers.remove_marker(marker_dir, session_id)
        return "removed"
    markers.write_marker(marker_dir, session_id, toplevel)
    if event == "SessionStart":
        env_file = env.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f"export COTENANCY_SESSION_ID={session_id}\n")
    return "written"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        handle(payload, dict(os.environ))
    except Exception as exc:  # never block the session
        print(f"cotenancy-guard session hook: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
