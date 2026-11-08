"""Install the pre-commit hook into a repository.

  python -m cotenancy_guard.install [--repo PATH] [--force]

Writes ``pre-commit`` into the repo's hooks directory (``git rev-parse --git-path hooks``,
so ``core.hooksPath`` is respected). The hook is a short sh script that runs this
package's ``precommit.py`` with python3 or python. An existing hook is left alone unless
``--force`` is given.
"""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

HOOK_TEMPLATE = """#!/bin/sh
# cotenancy-guard pre-commit (https://github.com/conradorebuffo/cotenancy-guard)
GUARD="{script}"
if python3 -c "" >/dev/null 2>&1; then PY=python3; else PY=python; fi
exec "$PY" "$GUARD"
"""


def hooks_dir(repo: str) -> str:
    out = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=repo,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"not a git repository: {repo} ({out.stderr.strip()})")
    path = out.stdout.strip()
    return path if os.path.isabs(path) else os.path.join(repo, path)


def install(repo: str, force: bool = False) -> str:
    target_dir = hooks_dir(repo)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, "pre-commit")
    if os.path.exists(target) and not force:
        raise SystemExit(f"{target} already exists; rerun with --force to replace it")
    script = os.path.join(PACKAGE_DIR, "precommit.py").replace("\\", "/")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(HOOK_TEMPLATE.format(script=script))
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    print(f"installed {install(args.repo, args.force)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
