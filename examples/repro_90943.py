#!/usr/bin/env python3
"""Reproduction for anthropics/claude-code#90943 (stdlib only, any OS with git on PATH).

Shows two things in a throwaway repo:

1. What GIT_INDEX_FILE a pre-commit hook receives for each way of running `git commit`.
   Only a commit restricted to paths (pathspec or --only) runs the hook against the
   temporary `next-index-<pid>.lock`; every mode that commits the whole shared index
   shows `index`, `index.lock` or the private index path.
2. The "index ahead" case from the thread: session A has a file staged, session B runs
   `git commit -m` and takes A's file. With a 6-line pre-commit hook that rejects
   whole-index commits, B's plain commit is refused, B's `git commit -m msg -- b.txt`
   goes through, and A's file is still staged afterwards.

Usage: python 90943_repro.py [--package DIR]
  --package DIR   run part 2 with the pre-commit hook from cotenancy-guard (DIR is the
                  package checkout) instead of the 6-line hook, with live markers for
                  sessions A and B, committing as B.
Nothing outside a temporary directory is touched.
"""
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile

PROBE_HOOK = """#!/bin/sh
printf '%s\\n' "$GIT_INDEX_FILE" >> "$PROBE_LOG"
"""

GUARD_HOOK = """#!/bin/sh
# Allow only commits restricted to paths: git runs the hook on next-index-<pid>.lock for those.
case "$(basename "$GIT_INDEX_FILE")" in
  next-index-*.lock) exit 0 ;;
esac
echo "pre-commit: this commit would take the whole shared index; use: git commit -m MSG -- <your paths>" >&2
exit 1
"""


def git(repo, *args, env=None, check=True):
    full_env = dict(os.environ, **(env or {}))
    r = subprocess.run(["git", *args], cwd=repo, env=full_env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()}")
    return r


def write(repo, name, text):
    with open(os.path.join(repo, name), "a", encoding="utf-8") as f:
        f.write(text)


def install_hook(repo, body):
    path = os.path.join(repo, ".git", "hooks", "pre-commit")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def new_repo(base, name):
    repo = os.path.join(base, name)
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "repro@example.invalid")
    git(repo, "config", "user.name", "repro")
    git(repo, "config", "core.autocrlf", "false")
    for f in ("a.txt", "b.txt"):
        write(repo, f, "base\n")
    git(repo, "add", "a.txt", "b.txt")
    git(repo, "commit", "-q", "-m", "base", "--no-verify")
    return repo


def last_commit_files(repo):
    out = git(repo, "show", "--name-only", "--format=", "HEAD").stdout
    return sorted(l for l in out.splitlines() if l)


def staged(repo):
    return sorted(l for l in git(repo, "diff", "--cached", "--name-only").stdout.splitlines() if l)


def probe_modes(base):
    """Part 1: GIT_INDEX_FILE seen by pre-commit, per commit mode."""
    repo = new_repo(base, "probe")
    log = os.path.join(base, "probe.log")
    install_hook(repo, PROBE_HOOK)
    env = {"PROBE_LOG": log}
    rows = []

    def run(label, args, prep, extra_env=None):
        prep()
        open(log, "w").close()
        git(repo, "commit", "-q", *args, env={**env, **(extra_env or {})})
        seen = open(log, encoding="utf-8").read().strip().splitlines()
        rows.append((label, os.path.basename(seen[-1]) if seen else "(hook not run)",
                     ",".join(last_commit_files(repo))))

    def stage_both():
        write(repo, "a.txt", "x\n"); write(repo, "b.txt", "x\n")
        git(repo, "add", "a.txt", "b.txt")

    def modify_both():
        write(repo, "a.txt", "y\n"); write(repo, "b.txt", "y\n")

    def stage_a_modify_b():
        write(repo, "a.txt", "z\n"); git(repo, "add", "a.txt"); write(repo, "b.txt", "z\n")

    def clean():
        git(repo, "add", "a.txt", "b.txt")
        git(repo, "commit", "-q", "-m", "flush", "--no-verify", check=False)

    run("commit -m", ["-m", "m"], stage_both)
    run("commit --amend -m", ["--amend", "-m", "m"], lambda: (write(repo, "a.txt", "w\n"), git(repo, "add", "a.txt")))
    run("commit -a -m", ["-a", "-m", "m"], modify_both)
    run("commit --include -m m b.txt", ["--include", "-m", "m", "b.txt"], stage_a_modify_b)
    run("commit -m m -- b.txt", ["-m", "m", "--", "b.txt"], stage_both); clean()
    run("commit --only -m m b.txt", ["--only", "-m", "m", "b.txt"], stage_both); clean()

    priv = os.path.join(repo, ".git", "priv")

    def private_index():
        git(repo, "read-tree", "HEAD", env={"GIT_INDEX_FILE": priv})
        write(repo, "b.txt", "p\n")
        git(repo, "add", "b.txt", env={"GIT_INDEX_FILE": priv})

    run("GIT_INDEX_FILE=<private> commit -m", ["-m", "m"], private_index, {"GIT_INDEX_FILE": priv})
    return rows


def ahead_index(base, package=None):
    """Part 2: session A staged a.txt; session B commits b.txt, without and with the guard."""
    results = {}
    env_b = None
    for guarded in (False, True):
        repo = new_repo(base, f"ahead_{'guard' if guarded else 'plain'}")
        if guarded and package:
            sys.path.insert(0, package)
            from cotenancy_guard import install as cg_install, markers as cg_markers
            cg_install.install(repo)
            top, mdir = cg_markers.repo_paths(repo)
            for session in ("A", "B"):
                cg_markers.write_marker(mdir, session, top)
            env_b = {"COTENANCY_SESSION_ID": "B"}
        elif guarded:
            install_hook(repo, GUARD_HOOK)
        write(repo, "a.txt", "work of session A\n")
        git(repo, "add", "a.txt")                       # session A stages its file
        write(repo, "b.txt", "work of session B\n")
        git(repo, "add", "b.txt")                       # session B stages its file
        plain = git(repo, "commit", "-q", "-m", "B: plain commit", env=env_b, check=False)
        if plain.returncode == 0:
            results[guarded] = ("plain commit accepted", last_commit_files(repo), staged(repo))
            continue
        scoped = git(repo, "commit", "-q", "-m", "B: scoped commit", "--", "b.txt", env=env_b, check=False)
        results[guarded] = (f"plain commit refused (exit {plain.returncode}); "
                            f"scoped commit exit {scoped.returncode}",
                            last_commit_files(repo), staged(repo))
    return results


def main():
    package = None
    if "--package" in sys.argv:
        package = os.path.abspath(sys.argv[sys.argv.index("--package") + 1])
    ver = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
    print(f"{ver} | {platform.system()} {platform.release()} | Python {platform.python_version()}\n")
    base = tempfile.mkdtemp(prefix="repro90943_")
    try:
        print("Part 1: GIT_INDEX_FILE seen by pre-commit")
        print(f"  {'command':38} {'basename':26} files in commit")
        for label, seen, files in probe_modes(base):
            print(f"  {label:38} {seen:26} {files}")
        print("\nPart 2: session A has a.txt staged; session B commits b.txt")
        for guarded, (what, committed, still_staged) in ahead_index(base, package).items():
            tag = ("with cotenancy-guard" if package else "with guard hook") if guarded else "no hook        "
            print(f"  {tag}: {what}; commit took {committed}; still staged {still_staged}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
