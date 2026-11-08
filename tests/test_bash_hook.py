"""Tests for the Bash rewrite hook (bash_hook.py) and the attribution hook (touch_hook.py).

Each rewrite is run for real: the hook's command goes through `sh -c` in a temp repo with the
pre-commit hook installed, and the test checks what the commit took, that the other session's
staged entry is byte for byte the same (`git ls-files -s`), and that the message is intact."""
from __future__ import annotations

import os
import subprocess

import pytest

from test_guard import BASE_ENV, files_in_head, git, live, repo, staged, write  # noqa: F401

from cotenancy_guard import bash_hook, markers, touch_hook

ME, OTHER = "session-me", "session-other"


def payload(repo, command, session=ME):
    return {"session_id": session, "cwd": repo, "hook_event_name": "PreToolUse",
            "tool_name": "Bash", "tool_input": {"command": command, "description": "commit"}}


def decide(repo, command, env=None, session=ME):
    try:
        return bash_hook.decide(payload(repo, command, session), {**BASE_ENV, **(env or {})})
    except bash_hook.Deny as exc:
        return bash_hook.deny(str(exc))


def touch(repo, *names, session=ME):
    for n in names:
        touch_hook.handle({"tool_name": "Edit", "session_id": session, "cwd": repo,
                           "tool_input": {"file_path": os.path.join(repo, n)}})


def ls_stage(repo, name):
    return git(repo, "ls-files", "-s", "--", name).stdout


def run(repo, command):
    r = subprocess.run(["sh", "-c", command], cwd=repo, capture_output=True, text=True,
                       env={**BASE_ENV, "COTENANCY_SESSION_ID": ME})
    assert r.returncode == 0, r.stderr
    return r


def message(repo):
    return git(repo, "log", "-1", "--format=%B").stdout.strip()


@pytest.fixture
def shared(repo):
    """Two live sessions: OTHER staged a.txt; ME edited and staged b.txt."""
    live(repo, ME, OTHER)
    write(repo, "a.txt", "work of the other session\n")
    git(repo, "add", "a.txt")
    write(repo, "b.txt", "my work\n")
    touch(repo, "b.txt")
    git(repo, "add", "b.txt")
    return repo


def rewritten(answer):
    out = answer["hookSpecificOutput"]
    assert "updatedInput" in out, out
    return out["updatedInput"]["command"], out


# --- attribution -----------------------------------------------------------------------

def test_touch_hook_records_repo_relative_paths(repo):
    os.makedirs(os.path.join(repo, "sub"))
    touch(repo, "b.txt", os.path.join("sub", "x.txt"))
    _, marker_dir = markers.repo_paths(repo)
    assert {p.lower() for p in markers.touched_paths(marker_dir, ME)} == {"b.txt", "sub/x.txt"}
    assert markers.touched_paths(marker_dir, OTHER) is None


def test_touch_hook_ignores_other_tools_and_outside_paths(repo, tmp_path):
    assert touch_hook.handle({"tool_name": "Read", "session_id": ME, "cwd": repo,
                              "tool_input": {"file_path": os.path.join(repo, "b.txt")}}) == "ignored tool"
    outside = str(tmp_path / "elsewhere.txt")
    assert touch_hook.handle({"tool_name": "Write", "session_id": ME, "cwd": repo,
                              "tool_input": {"file_path": outside}}) == "not in a git repository"


def test_session_end_removes_the_paths_record(repo):
    touch(repo, "b.txt")
    _, marker_dir = markers.repo_paths(repo)
    assert markers.remove_marker(marker_dir, ME) is True
    assert markers.touched_paths(marker_dir, ME) is None


# --- rewrites --------------------------------------------------------------------------

@pytest.mark.parametrize("msg", ["mine", "fix: a -- b is not a pathspec", "it's -- \"quoted\""])
def test_commit_m_takes_only_own_paths(shared, msg):
    import shlex
    before = ls_stage(shared, "a.txt")
    cmd, out = rewritten(decide(shared, f"git commit -m {shlex.quote(msg)}"))
    assert "--only" in cmd and out["permissionDecision"] == "ask"
    assert "b.txt" in out["additionalContext"]
    run(shared, cmd)
    assert files_in_head(shared) == ["b.txt"]
    assert message(shared) == msg
    assert staged(shared) == ["a.txt"] and ls_stage(shared, "a.txt") == before


def test_commit_a_takes_own_modified_paths(shared):
    before = ls_stage(shared, "a.txt")
    write(shared, "c.txt", "my unstaged edit\n")
    touch(shared, "c.txt")
    cmd, _ = rewritten(decide(shared, "git commit -a -m both"))
    assert " -a " not in f" {cmd} "
    run(shared, cmd)
    assert files_in_head(shared) == ["b.txt", "c.txt"]
    assert ls_stage(shared, "a.txt") == before and staged(shared) == ["a.txt"]


def test_combined_short_flags_am(shared):
    cmd, _ = rewritten(decide(shared, "git commit -am 'short cluster'"))
    run(shared, cmd)
    assert files_in_head(shared) == ["b.txt"] and message(shared) == "short cluster"


def test_include_becomes_only(shared):
    before = ls_stage(shared, "a.txt")
    write(shared, "c.txt", "c\n")
    cmd, _ = rewritten(decide(shared, "git commit --include -m inc -- c.txt"))
    assert "--include" not in cmd and "--only" in cmd
    run(shared, cmd)
    assert files_in_head(shared) == ["c.txt"]
    assert ls_stage(shared, "a.txt") == before


def test_amend_becomes_amend_only(shared):
    git(shared, "commit", "-q", "--only", "-m", "first", "--", "b.txt",
        env={"COTENANCY_SESSION_ID": ME})
    before = ls_stage(shared, "a.txt")
    cmd, _ = rewritten(decide(shared, "git commit --amend -m 'better message'"))
    assert "--only" in cmd
    run(shared, cmd)
    assert message(shared) == "better message" and files_in_head(shared) == ["b.txt"]
    assert ls_stage(shared, "a.txt") == before


def test_rewrite_from_a_subdirectory_uses_cwd_relative_paths(shared):
    sub = os.path.join(shared, "sub")
    os.makedirs(sub)
    write(sub, "s.txt", "x\n")
    touch(shared, os.path.join("sub", "s.txt"))
    git(shared, "add", "sub/s.txt")
    cmd, _ = rewritten(decide(sub, "git commit -m fromsub"))
    r = subprocess.run(["sh", "-c", cmd], cwd=sub, capture_output=True, text=True, env=BASE_ENV)
    assert r.returncode == 0, r.stderr
    assert files_in_head(shared) == ["b.txt", "sub/s.txt"]


@pytest.mark.parametrize("decision, expected", [("allow", "allow"), ("ask", "ask"), ("bogus", "ask")])
def test_decision_comes_from_the_env(shared, decision, expected):
    _, out = rewritten(decide(shared, "git commit -m x", {"COTENANCY_REWRITE_DECISION": decision}))
    assert out["permissionDecision"] == expected


def test_decision_none_leaves_permissions_alone(shared):
    _, out = rewritten(decide(shared, "git commit -m x", {"COTENANCY_REWRITE_DECISION": "none"}))
    assert "permissionDecision" not in out and out["additionalContext"]


def test_updated_input_keeps_other_fields(shared):
    _, out = rewritten(decide(shared, "git commit -m x"))
    assert out["updatedInput"]["description"] == "commit"


# --- left alone ------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git commit -m mine -- b.txt",
    "git commit --only -m mine b.txt",
    "git status",
    "echo git commit",
    "git log --grep commit",
])
def test_scoped_or_unrelated_commands_pass(shared, command):
    assert decide(shared, command) is None


def test_nothing_happens_without_another_live_session(repo):
    live(repo, ME)
    write(repo, "b.txt", "x\n")
    touch(repo, "b.txt")
    git(repo, "add", "b.txt")
    assert decide(repo, "git commit -m solo") is None


def test_git_dash_c_to_another_repo_passes(shared, tmp_path):
    other = str(tmp_path / "other")
    os.makedirs(other)
    git(other, "init", "-q")
    assert decide(shared, f"git -C {other.replace(os.sep, '/')} commit -m elsewhere") is None
    assert decide(shared, "git -C C:\no\such commit -m x") is None  # unresolvable: passes


def test_mode_off_passes(shared):
    assert decide(shared, "git commit -m x", {"COTENANCY_GUARD_MODE": "off"}) is None


# --- denials ---------------------------------------------------------------------------

def deny_reason(answer):
    out = answer["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "updatedInput" not in out, out
    return out["permissionDecisionReason"]


def test_no_attribution_denies(repo):
    live(repo, ME, OTHER)
    write(repo, "b.txt", "x\n")
    git(repo, "add", "b.txt")
    assert "no record" in deny_reason(decide(repo, "git commit -m x"))


def test_no_own_changed_path_denies(shared):
    git(shared, "reset", "-q", "--", "b.txt")
    git(shared, "checkout", "--", "b.txt")
    assert "none of the changed files" in deny_reason(decide(shared, "git commit -m x"))


@pytest.mark.parametrize("command", [
    "git add -A && git commit -m x",
    "git commit -m x; echo done",
    "true | git commit -m x",
    "(git commit -m x)",
])
def test_compound_commands_with_a_commit_are_denied(shared, command):
    assert "compound command" in deny_reason(decide(shared, command))


def test_compound_without_commit_passes(shared):
    assert decide(shared, "git status && git diff") is None


def test_include_without_paths_denies(shared):
    assert "--include without paths" in deny_reason(decide(shared, "git commit --include -m x"))


def test_main_fails_open_on_bad_input(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert bash_hook.main() == 0
    assert capsys.readouterr().out == ""
