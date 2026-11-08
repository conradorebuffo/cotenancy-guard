# cotenancy-guard

A git pre-commit hook and a Claude Code plugin for one specific failure: several Claude Code sessions working in the same git working tree, where one session's commit takes the files another session had staged.

Context: [anthropics/claude-code#90943](https://github.com/anthropics/claude-code/issues/90943). `.git/index` belongs to the repository, not to the session. `git commit -m` commits the whole index, so if session A has `a.txt` staged and session B runs `git commit -m` for its own `b.txt`, B's commit carries both files. No conflict, no warning, exit code 0.

## How it works

Git sets `GIT_INDEX_FILE` when it runs pre-commit, and the value tells you whether the commit is restricted to paths:

| command | `GIT_INDEX_FILE` basename in pre-commit | commits |
|---|---|---|
| `git commit -m` | `index` | the whole index |
| `git commit --amend -m` | `index` | the whole index |
| `git commit -a -m` | `index.lock` | the whole index |
| `git commit --include -m m p` | `index.lock` | the whole index |
| `git commit -m m -- p` | `next-index-<pid>.lock` | only `p` |
| `git commit --only -m m p` | `next-index-<pid>.lock` | only `p` |
| `GIT_INDEX_FILE=<private> git commit -m` | the private file name | that whole index |

Measured on git 2.52.0.windows.1 (Windows 11) and git 2.39.5 (Debian) with [`examples/repro_90943.py`](examples/repro_90943.py). The temporary name comes from [`builtin/commit.c`](https://github.com/git/git/blob/fb255012acbcbe81189cb8014da0280b09c66ecc/builtin/commit.c#L541).

The hook combines that with a liveness marker per session:

- The plugin's `SessionStart` hook writes `<git-common-dir>/cotenancy/<session_id>.json`, with the session's working tree (`git rev-parse --show-toplevel`). It also writes `COTENANCY_SESSION_ID` to `CLAUDE_ENV_FILE`, so the session's own Bash commands, including `git commit`, know which marker is theirs.
- `Stop` refreshes the marker after every turn, and `SessionEnd` removes it. A marker older than the TTL (2 hours by default) no longer counts, which covers sessions that died without a `SessionEnd`.
- pre-commit refuses a whole-index commit only when another live session shares the same working tree. Sessions in separate worktrees of the same repo don't count. A commit with a pathspec always passes.

It also checks one more thing, the five-line guard @capraCoder proposed in the issue: a commit whose index deletes files that are still on disk is refused. That is what a stale index looks like after another session committed new files.

### Rewriting the commit instead of refusing it

Inside Claude Code the plugin goes one step further than the pre-commit hook. Two more hooks:

- `PostToolUse` on Edit, Write and NotebookEdit appends each edited path to `<git-common-dir>/cotenancy/<session_id>.paths`. That is the session's own work.
- `PreToolUse` on Bash looks for a `git commit` that would take the whole index while another session is live, and returns the command rewritten (`updatedInput`):

| Claude runs | runs instead |
|---|---|
| `git commit -m MSG`, `git commit -a -m MSG` | `git commit --only -m MSG -- <paths this session edited that have changes>` |
| `git commit --include -m MSG -- p` | `git commit --only -m MSG -- p` |
| `git commit --amend [-m MSG]` | `git commit --amend --only [-m MSG]` (the message only; nothing staged goes in) |

The other session's staged entries stay in the index untouched; the tests compare `git ls-files -s` before and after. The command is parsed with `shlex`, so a message such as `-m "fix: a -- b"` is not mistaken for a pathspec.

It does not guess. If the session has no record of the files it edited, or none of the changed files are its own, the call is denied with the command to run instead. A commit that already names paths is left alone, and so is everything when no other session is live.

`COTENANCY_REWRITE_DECISION` sets what Claude Code does with the rewritten command: `ask` (default) shows it and asks you, `none` leaves it to your permission rules, `allow` runs it without asking.

## Install

The hook works on its own; the plugin is what lets it tell sessions apart.

```sh
git clone https://github.com/conradorebuffo/cotenancy-guard
python3 cotenancy-guard/cotenancy_guard/install.py --repo /path/to/your/repo   # "python" on Windows
```

This writes `pre-commit` into the repo's hooks directory and refuses to replace an existing hook unless you pass `--force`. It respects `core.hooksPath`: if you set that globally, the hook lands in your global hooks directory and applies to every repo. The installed hook calls `precommit.py` by absolute path inside the clone, so keep the clone where it is; if the file or Python is missing, the hook prints a warning and lets the commit through. If you already have a pre-commit hook, call `cotenancy_guard/precommit.py` from it instead.

Then, in Claude Code:

```text
/plugin marketplace add conradorebuffo/cotenancy-guard
/plugin install cotenancy-guard@cotenancy-guard
```

Requirements: git, Python 3.9 or later, and `sh` (Git for Windows provides it on Windows). No dependencies outside the standard library.

## Settings

| variable | effect |
|---|---|
| `COTENANCY_GUARD_MODE` | `block` (default), `warn` (report and let the commit through), `off` |
| `COTENANCY_TTL_SECONDS` | how long a marker counts as live; default 7200 |
| `COTENANCY_ALLOW_DELETES=1` | skip the staged-deletion check, for an intentional `git rm --cached` |
| `COTENANCY_SESSION_ID` | set by the plugin; without it every live marker counts as another session |
| `COTENANCY_REWRITE_DECISION` | what to do with a rewritten `git commit`: `ask` (default), `none` (permission rules decide) or `allow` |

When it refuses a commit, the message says which sessions are live and what to run instead: `git commit -m MSG -- <your paths>`.

## Limits

- `next-index-<pid>.lock` is an implementation detail of git, not a documented interface. If a future git renames it, every commit made while another session is live gets refused. The hook fails closed in that case, so you would notice; `COTENANCY_GUARD_MODE=warn` is the way out until it's updated.
- The session check covers the index that is ahead (one session commits what another staged). The issue describes two more routes: merges done through plumbing (`merge-tree`, `commit-tree`, `update-ref`) and an orphaned `index.lock`. The staged-deletion check catches their usual signature, files deleted in the index but still on disk; as the issue notes, it stays silent when a plumbing merge leaves the files off disk, because then index and disk agree.
- A marker records that a session is alive, not which files it owns. The pre-commit hook can tell you another session is there; it can't tell whether that session has anything staged. In a repo with two live sessions, every whole-index commit is refused, even when the other session is idle. Inside Claude Code the plugin rewrites the commit instead (see above), so this mostly affects terminals and other tools.
- The rewrite only sees the Bash tool. It knows a session's own files from Edit, Write and NotebookEdit; files a session changed through Bash (a script, `sed -i`, a generator) are not attributed, and a commit of only those is denied rather than guessed.
- A `git commit` inside a compound command (`&&`, `;`, `|`, a subshell) is denied, not rewritten: rewriting one part of a pipeline is not safe. Run the commit on its own. `git -C <dir>` pointing at another repository is left to that repository's own hook.
- With `COTENANCY_REWRITE_DECISION=ask`, a headless `claude -p` run has nobody to answer, so the rewritten commit ends denied. Use `allow` or `none` for unattended runs.
- Without the plugin, or from a terminal outside Claude Code, `COTENANCY_SESSION_ID` is unset and any live marker counts as another session. That is deliberate (a human committing next to a live agent is the same risk), but it means a session whose environment did not pick up `CLAUDE_ENV_FILE` will see its own marker as someone else's.
- Liveness is a TTL, not a heartbeat from a running process, and that cuts both ways. A session that crashes keeps counting as live until its marker expires. A session that sits idle longer than the TTL (the marker is refreshed only when a turn ends) stops counting, even if it still has files staged; raise `COTENANCY_TTL_SECONDS` if your sessions idle for hours.
- Separate worktrees per session remain the strong fix. This is for setups where several sessions share one tree on purpose.
- Internal errors (git missing, unreadable repo) let the commit through with a warning on stderr. A broken guard should not stop work.

## Tests

```sh
python -m pytest -q
```

The integration tests run real `git commit` calls in temporary repositories with the hook installed. The CI workflow runs them on Windows with Python 3.9 and 3.13, and on Ubuntu with Python 3.10 and 3.13.

## License

MIT. See [LICENSE](LICENSE).
