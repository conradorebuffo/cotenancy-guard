"""Tests for cotenancy-guard. Each integration test runs real `git commit` calls in a temp
repo with the installed pre-commit hook, so they exercise what git actually passes in
GIT_INDEX_FILE rather than a mocked value."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cotenancy_guard import install, markers, precommit, session_hook  # noqa: E402

BASE_ENV = {k: v for k, v in os.environ.items()
            if not k.startswith(("COTENANCY_", "GIT_"))}


def git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                       env={**BASE_ENV, **(env or {})})
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r


def write(repo, name, text):
    with open(os.path.join(repo, name), "a", encoding="utf-8") as fh:
        fh.write(text)


@pytest.fixture
def repo(tmp_path):
    path = str(tmp_path / "repo")
    os.makedirs(path)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "test")
    git(path, "config", "core.autocrlf", "false")
    for f in ("a.txt", "b.txt", "c.txt"):
        write(path, f, "base\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "base")
    install.install(path)
    return path


def live(repo, *sessions, age=0.0):
    toplevel, marker_dir = markers.repo_paths(repo)
    for s in sessions:
        markers.write_marker(marker_dir, s, toplevel, now=time.time() - age)
    return marker_dir


def files_in_head(repo):
    out = git(repo, "show", "--name-only", "--format=", "HEAD").stdout
    return sorted(l for l in out.splitlines() if l)


def staged(repo):
    return sorted(l for l in git(repo, "diff", "--cached", "--name-only").stdout.splitlines() if l)


# --- unit -------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (".git/index", False),
    ("/repo/.git/index.lock", False),
    (r"C:\repo\.git\priv", False),
    ("/repo/.git/next-index-24512.lock", True),
    (r"C:\repo\.git\NEXT-INDEX-1.LOCK", True),
    ("", None),
    (None, None),
])
def test_is_scoped(value, expected):
    assert precommit.is_scoped(value) is expected


def test_marker_ttl_and_toplevel(tmp_path):
    d = str(tmp_path / "m")
    now = 1_000_000.0
    markers.write_marker(d, "fresh", "/top/a", now=now - 10)
    markers.write_marker(d, "old", "/top/a", now=now - 9999)
    markers.write_marker(d, "elsewhere", "/top/b", now=now - 10)
    found, problems = markers.live_sessions(d, "/top/a", now=now, ttl=3600)
    assert [m["session_id"] for m in found] == ["fresh"] and problems == []


def test_marker_refresh_keeps_created_at(tmp_path):
    d = str(tmp_path / "m")
    path = markers.write_marker(d, "s1", "/top", now=100.0)
    markers.write_marker(d, "s1", "/top", now=200.0)
    data = json.load(open(path, encoding="utf-8"))
    assert data["created_at"] == 100.0 and data["updated_at"] == 200.0


def test_unreadable_marker_is_reported(tmp_path):
    d = str(tmp_path / "m")
    os.makedirs(d)
    open(os.path.join(d, "bad.json"), "w").write("{not json")
    found, problems = markers.live_sessions(d, "/top")
    assert found == [] and problems and "bad.json" in problems[0]


def test_marker_file_sanitizes_session_id(tmp_path):
    path = markers.marker_file(str(tmp_path), "../../etc/passwd")
    assert os.path.dirname(path) == str(tmp_path)


# --- integration: whole-index commits --------------------------------------------------

def test_alone_plain_commit_passes(repo):
    live(repo, "me")
    write(repo, "a.txt", "x\n")
    git(repo, "add", "a.txt")
    assert git(repo, "commit", "-q", "-m", "m", env={"COTENANCY_SESSION_ID": "me"}, check=False).returncode == 0


def test_other_live_session_blocks_plain_commit_and_keeps_its_staged_file(repo):
    live(repo, "A", "B")
    write(repo, "a.txt", "work of A\n")
    git(repo, "add", "a.txt")                       # A staged its file
    write(repo, "b.txt", "work of B\n")
    git(repo, "add", "b.txt")                       # B staged its file
    env_b = {"COTENANCY_SESSION_ID": "B"}
    plain = git(repo, "commit", "-q", "-m", "B plain", env=env_b, check=False)
    assert plain.returncode != 0 and "other session" in plain.stderr
    scoped = git(repo, "commit", "-q", "-m", "B scoped", "--", "b.txt", env=env_b, check=False)
    assert scoped.returncode == 0, scoped.stderr
    assert files_in_head(repo) == ["b.txt"]
    assert staged(repo) == ["a.txt"]                # A's work is still staged


@pytest.mark.parametrize("args", [
    ["-a", "-m", "m"],
    ["--amend", "-m", "m"],
    ["--include", "-m", "m", "b.txt"],
])
def test_other_whole_index_modes_are_blocked(repo, args):
    live(repo, "A", "B")
    write(repo, "a.txt", "x\n")
    git(repo, "add", "a.txt")
    write(repo, "b.txt", "x\n")
    r = git(repo, "commit", "-q", *args, env={"COTENANCY_SESSION_ID": "B"}, check=False)
    assert r.returncode != 0 and "whole shared index" in r.stderr


def test_only_commit_passes_with_other_session(repo):
    live(repo, "A", "B")
    write(repo, "a.txt", "x\n")
    git(repo, "add", "a.txt")
    write(repo, "b.txt", "x\n")
    r = git(repo, "commit", "-q", "--only", "-m", "m", "b.txt",
            env={"COTENANCY_SESSION_ID": "B"}, check=False)
    assert r.returncode == 0, r.stderr
    assert files_in_head(repo) == ["b.txt"] and staged(repo) == ["a.txt"]


def test_private_index_is_whole_index(repo):
    live(repo, "A", "B")
    priv = os.path.join(repo, ".git", "priv")
    git(repo, "read-tree", "HEAD", env={"GIT_INDEX_FILE": priv})
    write(repo, "b.txt", "p\n")
    git(repo, "add", "b.txt", env={"GIT_INDEX_FILE": priv})
    r = git(repo, "commit", "-q", "-m", "m",
            env={"GIT_INDEX_FILE": priv, "COTENANCY_SESSION_ID": "B"}, check=False)
    assert r.returncode != 0 and "whole shared index" in r.stderr


def test_stale_marker_does_not_count(repo):
    live(repo, "A", age=markers.DEFAULT_TTL_SECONDS + 60)
    live(repo, "B")
    write(repo, "b.txt", "x\n")
    git(repo, "add", "b.txt")
    assert git(repo, "commit", "-q", "-m", "m", env={"COTENANCY_SESSION_ID": "B"}, check=False).returncode == 0


def test_unknown_self_counts_every_live_marker(repo):
    """Without COTENANCY_SESSION_ID (a human terminal), any live session is another one."""
    live(repo, "A")
    write(repo, "b.txt", "x\n")
    git(repo, "add", "b.txt")
    assert git(repo, "commit", "-q", "-m", "m", check=False).returncode != 0


def test_session_in_another_worktree_does_not_count(repo, tmp_path):
    wt = str(tmp_path / "wt")
    git(repo, "worktree", "add", "-q", wt, "-b", "side")
    live(wt, "A")                                    # A works in the other worktree
    live(repo, "B")
    write(repo, "b.txt", "x\n")
    git(repo, "add", "b.txt")
    assert git(repo, "commit", "-q", "-m", "m", env={"COTENANCY_SESSION_ID": "B"}, check=False).returncode == 0


@pytest.mark.parametrize("mode, expected_rc", [("warn", 0), ("off", 0), ("block", 1)])
def test_modes(repo, mode, expected_rc):
    live(repo, "A", "B")
    write(repo, "b.txt", "x\n")
    git(repo, "add", "b.txt")
    r = git(repo, "commit", "-q", "-m", "m",
            env={"COTENANCY_SESSION_ID": "B", "COTENANCY_GUARD_MODE": mode}, check=False)
    assert (r.returncode != 0) == bool(expected_rc)


# --- integration: staged deletions of files still on disk ------------------------------

def test_staged_deletion_of_file_on_disk_is_blocked(repo):
    git(repo, "rm", "-q", "--cached", "c.txt")       # index drops it, file stays on disk
    r = git(repo, "commit", "-q", "-m", "m", check=False)
    assert r.returncode != 0 and "still exist on disk" in r.stderr


def test_real_deletion_passes(repo):
    git(repo, "rm", "-q", "c.txt")                   # gone from disk too
    assert git(repo, "commit", "-q", "-m", "m", check=False).returncode == 0


def test_intentional_untrack_with_escape(repo):
    git(repo, "rm", "-q", "--cached", "c.txt")
    r = git(repo, "commit", "-q", "-m", "m", env={"COTENANCY_ALLOW_DELETES": "1"}, check=False)
    assert r.returncode == 0, r.stderr


# --- fail open -------------------------------------------------------------------------

def test_internal_error_fails_open(tmp_path, capsys):
    rc = precommit.main(env={"GIT_INDEX_FILE": ".git/index"}, cwd=str(tmp_path))  # not a repo
    assert rc == 0 and "internal error" in capsys.readouterr().err


# --- session hook ----------------------------------------------------------------------

def test_session_hook_lifecycle(repo, tmp_path):
    env_file = str(tmp_path / "env.sh")
    env = {"CLAUDE_ENV_FILE": env_file}
    assert session_hook.handle({"hook_event_name": "SessionStart", "session_id": "s-1", "cwd": repo}, env) == "written"
    toplevel, marker_dir = markers.repo_paths(repo)
    assert [m["session_id"] for m in markers.live_sessions(marker_dir, toplevel)[0]] == ["s-1"]
    assert "export COTENANCY_SESSION_ID=s-1" in open(env_file, encoding="utf-8").read()
    assert session_hook.handle({"hook_event_name": "Stop", "session_id": "s-1", "cwd": repo}, env) == "written"
    assert session_hook.handle({"hook_event_name": "SessionEnd", "session_id": "s-1", "cwd": repo}, env) == "removed"
    assert markers.live_sessions(marker_dir, toplevel)[0] == []


def test_session_hook_outside_repo_is_harmless(tmp_path):
    assert session_hook.handle({"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(tmp_path)}, {}) == "not a git repository"


def test_session_hook_main_never_fails(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert session_hook.main() == 0
    assert "session hook" in capsys.readouterr().err


def test_install_refuses_to_overwrite(repo):
    with pytest.raises(SystemExit):
        install.install(repo)
    assert install.install(repo, force=True).endswith("pre-commit")


def test_plugin_manifests_are_valid_json():
    for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "hooks/hooks.json"):
        json.load(open(os.path.join(ROOT, rel), encoding="utf-8"))
    hooks = json.load(open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "SessionEnd"}


def test_installed_hook_fails_open_when_guard_file_is_missing(repo):
    hook = os.path.join(repo, ".git", "hooks", "pre-commit")
    text = open(hook, encoding="utf-8").read().replace("precommit.py", "missing_precommit.py")
    open(hook, "w", encoding="utf-8", newline="\n").write(text)
    live(repo, "A", "B")
    write(repo, "b.txt", "x\n")
    git(repo, "add", "b.txt")
    r = git(repo, "commit", "-q", "-m", "m", env={"COTENANCY_SESSION_ID": "B"}, check=False)
    assert r.returncode == 0 and "not found" in r.stderr


def test_ttl_comes_from_the_env_passed_in(repo):
    live(repo, "A", age=120)                       # A refreshed two minutes ago
    write(repo, "b.txt", "x\n")
    git(repo, "add", "b.txt")
    base = {"GIT_INDEX_FILE": ".git/index", "COTENANCY_SESSION_ID": "B"}
    assert precommit.evaluate(repo, {**base, "COTENANCY_TTL_SECONDS": "60"})[0] == []
    assert precommit.evaluate(repo, {**base, "COTENANCY_TTL_SECONDS": "3600"})[0] != []
