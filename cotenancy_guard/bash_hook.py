"""Claude Code PreToolUse hook on Bash: rewrite a whole-index ``git commit`` to commit only
this session's own paths, instead of only refusing it.

When another session is live in the same working tree, ``git commit -m`` takes everything
staged, including the other session's work. This hook turns it into
``git commit --only -m MSG -- <paths this session edited that have changes>`` and returns it
as ``updatedInput``. What is "own" comes from ``touch_hook.py``, which records the files the
session edited with Edit, Write or NotebookEdit.

  commit -m MSG / commit -a -m MSG   ->  commit --only -m MSG -- <own changed paths>
  commit --include -m MSG -- p       ->  commit --only -m MSG -- p
  commit --amend [-m MSG]            ->  commit --amend --only [-m MSG]   (message only)
  commit --amend -a                  ->  commit --amend --only -- <own changed paths>

It does not guess. With no record for this session, or no own path among the changes, it
denies the call and says which command to run. A commit that already names paths
(``-- p`` or ``--only``) is left alone: git commits only those, and the pre-commit hook sees
``next-index-*.lock``. Nothing is touched when no other session is live.

Compound commands (``&&``, ``;``, ``|``, subshells) that contain a ``git commit`` are denied:
rewriting one part of a pipeline is not safe. ``git -C <dir>`` pointing at another
repository is left alone (its own pre-commit hook still applies).

Environment:
  COTENANCY_REWRITE_DECISION  ask (default) | none | allow
      ask:   Claude Code shows the rewritten command and asks. In ``claude -p`` nobody can
             answer, so the call ends denied.
      none:  no decision; normal permission rules apply to the rewritten command.
      allow: approve the rewritten command without asking.
  COTENANCY_GUARD_MODE=off    disable (shared with the pre-commit hook)

Fails open: an internal error prints to stderr and lets the command through unchanged.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from cotenancy_guard import markers  # noqa: E402
else:
    from . import markers

OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# commit options that take a value (short and long forms)
SHORT_WITH_VALUE = set("mFCct")
LONG_WITH_VALUE = {"--message", "--file", "--reuse-message", "--reedit-message", "--author",
                   "--date", "--template", "--cleanup", "--fixup", "--squash", "--trailer",
                   "--pathspec-from-file", "--untracked-files"}
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
DECISIONS = {"ask", "none", "allow"}


class Deny(Exception):
    pass


def tokenize(command: str) -> list[str] | None:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def find_commit(tokens: list[str]) -> dict | None:
    """Locate ``[ENV=..] git [global opts] commit ARGS`` in a simple command."""
    i = 0
    while i < len(tokens) and ENV_ASSIGN.match(tokens[i]):
        i += 1
    if i >= len(tokens) or os.path.basename(tokens[i]).lower() not in ("git", "git.exe"):
        return None
    git_at, i = i, i + 1
    git_dir = None
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        if opt in GIT_GLOBAL_WITH_VALUE and i + 1 < len(tokens):
            if opt == "-C":
                git_dir = tokens[i + 1] if git_dir is None else os.path.join(git_dir, tokens[i + 1])
            i += 2
        else:
            i += 1
    if i >= len(tokens) or tokens[i] != "commit":
        return None
    return {"git_at": git_at, "commit_at": i, "args": tokens[i + 1:], "git_dir": git_dir}


def parse_commit_args(args: list[str]) -> dict:
    """Split commit args into options (kept in order) and pathspecs."""
    opts, paths = [], []
    flags = {"all": False, "include": False, "only": False, "amend": False}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            paths.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name == "--all":
                flags["all"] = True
            elif name == "--include":
                flags["include"] = True
            elif name == "--only":
                flags["only"] = True
            elif name == "--amend":
                flags["amend"] = True
            opts.append(a)
            if name in LONG_WITH_VALUE and "=" not in a and i + 1 < len(args):
                opts.append(args[i + 1])
                i += 1
        elif a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            for j, ch in enumerate(cluster):
                if ch == "a":
                    flags["all"] = True
                elif ch == "i":
                    flags["include"] = True
                elif ch == "o":
                    flags["only"] = True
                if ch in SHORT_WITH_VALUE:
                    if j == len(cluster) - 1 and i + 1 < len(args):
                        opts.append(a)
                        opts.append(args[i + 1])
                        i += 1
                        break
                    opts.append(a)  # value attached, e.g. -mMSG
                    break
            else:
                opts.append(a)
        else:
            paths.append(a)
        i += 1
    return {"opts": opts, "paths": paths, **flags}


def strip_flags(opts: list[str], drop_long: set[str], drop_short: set[str]) -> list[str]:
    """Remove boolean flags such as -a/--all or -i/--include, also from short clusters."""
    out, skip_value = [], False
    for k, o in enumerate(opts):
        if skip_value:
            out.append(o)
            skip_value = False
            continue
        if o.startswith("--"):
            name = o.split("=", 1)[0]
            if name in drop_long:
                continue
            out.append(o)
            skip_value = name in LONG_WITH_VALUE and "=" not in o
        elif o.startswith("-") and len(o) > 1:
            kept, rest = "", ""
            for j, ch in enumerate(o[1:]):
                if ch in SHORT_WITH_VALUE:
                    rest = o[1 + j:]
                    break
                if ch not in drop_short:
                    kept += ch
            new = kept + rest
            if new:
                out.append("-" + new)
                skip_value = bool(rest) and len(rest) == 1
        else:
            out.append(o)
    return out


def git_lines(cwd: str, *args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return [p for p in out.stdout.split("\0") if p]


def own_changed_paths(toplevel: str, cwd: str, touched: set[str]) -> list[str]:
    """Changed paths (staged, or modified in the worktree) that this session edited,
    as pathspecs relative to ``cwd`` in git's own spelling."""
    changed = git_lines(toplevel, "diff", "--cached", "--name-only", "-z") + \
        git_lines(toplevel, "diff", "--name-only", "-z")
    mine = {os.path.normcase(p) for p in touched}
    own, seen = [], set()
    for p in changed:
        key = os.path.normcase(p)
        if key in mine and key not in seen:
            seen.add(key)
            rel = os.path.relpath(os.path.join(toplevel, p), cwd).replace("\\", "/")
            own.append(rel)
    return own


def decide(payload: dict, env: dict) -> dict | None:
    """The hook's JSON answer, or None to let the command through unchanged."""
    if env.get("COTENANCY_GUARD_MODE", "block").strip().lower() == "off":
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if "commit" not in command or "git" not in command:
        return None
    tokens = tokenize(command)
    if tokens is None:
        return None
    if any(t in OPERATORS for t in tokens):
        segments, cur = [], []
        for t in tokens:
            if t in OPERATORS:
                segments.append(cur)
                cur = []
            else:
                cur.append(t)
        segments.append(cur)
        if any(find_commit(s) for s in segments):
            raise Deny("cotenancy-guard: a git commit inside a compound command is not rewritten. "
                       "Run the commit on its own line: git commit -m MSG -- <your paths>")
        return None
    found = find_commit(tokens)
    if not found:
        return None

    cwd = payload.get("cwd") or os.getcwd()
    session_id = str(payload.get("session_id") or env.get("COTENANCY_SESSION_ID") or "")
    try:
        toplevel, marker_dir = markers.repo_paths(cwd)
    except (RuntimeError, OSError):
        return None
    if found["git_dir"] is not None:
        other = os.path.join(cwd, found["git_dir"])
        try:
            if markers.repo_paths(other)[0] != toplevel:
                return None  # another repository: its own pre-commit hook applies
        except (RuntimeError, OSError):
            return None  # not resolvable (e.g. a Windows path whose backslashes shlex consumed)
        cwd = other

    live, _ = markers.live_sessions(marker_dir, toplevel, ttl=markers.ttl_seconds(env))
    others = [m for m in live if m["session_id"] != session_id]
    if not others:
        return None

    c = parse_commit_args(found["args"])
    if c["paths"] and not c["include"]:
        return None  # already path-restricted: git commits only those
    head = tokens[: found["commit_at"] + 1]
    names = ", ".join(m["session_id"][:8] for m in others)

    if c["include"]:
        if not c["paths"]:
            raise Deny("cotenancy-guard: --include without paths takes the whole shared index "
                       f"while other sessions are live ({names}). Use: git commit -m MSG -- <your paths>")
        opts = strip_flags(c["opts"], {"--include", "--only"}, {"i", "o"})
        new_tokens = head + ["--only"] + opts + ["--"] + c["paths"]
        what = "--include P became --only -- P"
    elif c["amend"] and not c["all"]:
        opts = strip_flags(c["opts"], {"--only"}, {"o"})
        new_tokens = head + ["--only"] + opts
        what = "--amend became --amend --only: it changes the message only, nothing staged goes in"
    else:
        touched = markers.touched_paths(marker_dir, session_id) if session_id else None
        if not touched:
            raise Deny(f"cotenancy-guard: other sessions are live in this working tree ({names}) "
                       "and this session has no record of the files it edited, so a whole-index "
                       "commit cannot be narrowed safely. Commit your paths explicitly: "
                       "git commit -m MSG -- <your paths>")
        own = own_changed_paths(toplevel, cwd, touched)
        if not own:
            raise Deny(f"cotenancy-guard: other sessions are live in this working tree ({names}) "
                       "and none of the changed files were edited by this session. Commit the "
                       "paths you mean explicitly: git commit -m MSG -- <paths>")
        opts = strip_flags(c["opts"], {"--all", "--only"}, {"a", "o"})
        new_tokens = head + ["--only"] + opts + ["--"] + own
        what = f"the commit now takes only this session's paths: {', '.join(own)}"

    new_command = shlex.join(new_tokens)
    decision = env.get("COTENANCY_REWRITE_DECISION", "ask").strip().lower()
    if decision not in DECISIONS:
        decision = "ask"
    context = (f"cotenancy-guard rewrote the git commit because other sessions are live in this "
               f"working tree ({names}): {what}. Anything else staged stays staged for its "
               f"owner. Rewritten command: {new_command}")
    out = {"hookEventName": "PreToolUse",
           "updatedInput": {**tool_input, "command": new_command},
           "additionalContext": context}
    if decision != "none":
        out["permissionDecision"] = decision
        out["permissionDecisionReason"] = context
    return {"hookSpecificOutput": out}


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        try:
            answer = decide(payload, dict(os.environ))
        except Deny as exc:
            answer = deny(str(exc))
        if answer:
            sys.stdout.write(json.dumps(answer))
    except Exception as exc:  # fail open
        print(f"cotenancy-guard bash hook: skipped after an internal error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
