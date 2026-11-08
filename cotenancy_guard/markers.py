"""Per-session liveness markers shared by every session of one repository.

A marker is a small JSON file under ``<git-common-dir>/cotenancy/<session_id>.json``.
The common dir is shared by all worktrees of a repo, so each marker also records the
working tree (``git rev-parse --show-toplevel``) it belongs to; sessions in separate
worktrees do not count as co-tenants.

Markers are keyed by the Claude Code ``session_id``, never by machine or user: two
sessions on the same machine are exactly the case this guards against.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

DEFAULT_TTL_SECONDS = 2 * 60 * 60
MARKER_SUBDIR = "cotenancy"
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def ttl_seconds(env: dict | None = None) -> int:
    raw = (os.environ if env is None else env).get("COTENANCY_TTL_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


def _git(cwd: str, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def norm_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def repo_paths(cwd: str) -> tuple[str, str]:
    """Return (toplevel, marker_dir) for the repository that contains ``cwd``."""
    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    common = _git(cwd, "rev-parse", "--git-common-dir")
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    return norm_path(toplevel), os.path.join(norm_path(common), MARKER_SUBDIR)


def marker_file(marker_dir: str, session_id: str) -> str:
    safe = _SAFE_ID.sub("_", session_id)[:128] or "unknown"
    return os.path.join(marker_dir, safe + ".json")


def write_marker(marker_dir: str, session_id: str, toplevel: str, now: float | None = None) -> str:
    """Create or refresh the marker of ``session_id``. Written atomically."""
    now = time.time() if now is None else now
    os.makedirs(marker_dir, exist_ok=True)
    path = marker_file(marker_dir, session_id)
    created = now
    try:
        with open(path, encoding="utf-8") as fh:
            created = float(json.load(fh).get("created_at", now))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    data = {"session_id": session_id, "toplevel": toplevel,
            "created_at": created, "updated_at": now}
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return path


def remove_marker(marker_dir: str, session_id: str) -> bool:
    removed = False
    for path in (marker_file(marker_dir, session_id), paths_file(marker_dir, session_id)):
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    return removed


def paths_file(marker_dir: str, session_id: str) -> str:
    """Where a session's touched paths are recorded (one repo-relative path per line)."""
    return marker_file(marker_dir, session_id)[: -len(".json")] + ".paths"


def add_touched(marker_dir: str, session_id: str, rel_paths: list[str]) -> None:
    """Record paths this session edited. Append-only: one write per call, so parallel
    tool calls of the same session cannot lose each other's entries."""
    lines = "".join(p.replace("\\", "/") + "\n" for p in rel_paths if p and "\n" not in p)
    if not lines:
        return
    os.makedirs(marker_dir, exist_ok=True)
    fd = os.open(paths_file(marker_dir, session_id), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, lines.encode("utf-8"))
    finally:
        os.close(fd)


def touched_paths(marker_dir: str, session_id: str) -> set[str] | None:
    """Repo-relative paths this session edited, or None when it has no record at all."""
    try:
        with open(paths_file(marker_dir, session_id), encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except FileNotFoundError:
        return None


def live_sessions(marker_dir: str, toplevel: str, now: float | None = None,
                  ttl: int | None = None) -> tuple[list[dict], list[str]]:
    """Live markers for ``toplevel`` and a list of problems found while reading.

    A marker is live when it was refreshed less than ``ttl`` seconds ago. Unreadable
    markers are reported, not silently dropped.
    """
    now = time.time() if now is None else now
    ttl = ttl_seconds() if ttl is None else ttl
    live, problems = [], []
    try:
        names = sorted(os.listdir(marker_dir))
    except FileNotFoundError:
        return live, problems
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(marker_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            updated = float(data["updated_at"])
            marker_top = str(data["toplevel"])
            session = str(data["session_id"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"unreadable marker {name}: {exc}")
            continue
        if now - updated >= ttl:
            continue
        if marker_top != toplevel:
            continue
        live.append({"session_id": session, "updated_at": updated, "path": path})
    return live, problems
