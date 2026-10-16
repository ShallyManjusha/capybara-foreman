"""
Worktree preparation, commit and publish.

The build stage never runs against your repository. It runs against a
disposable clone in `var/work/<TICKET>`, on a branch of its own, with the
credentialed remote stripped out the moment the clone finishes. That last
detail matters more than it looks: the clone URL carries a token, and `git
remote -v` inside the worktree would hand that token to anything the build
stage later runs. The worktree the model touches holds no credential at all.

Publishing is a separate step, performed by the caller after `guardrails`
has passed the diff. The two are not merged into one convenient function on
purpose — "commit and push" as a single call is how an unreviewed diff reaches
a remote during a refactor.
"""
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

BRANCH_SAFE = re.compile(r"[^A-Za-z0-9._/-]+")


class GitError(Exception):
    pass


def run(repo: Optional[str], *args: str, timeout: int = 300,
        env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    cmd = ["git"] + (["-C", repo] if repo else []) + list(args)
    child = dict(os.environ)
    # Never let git stop for a credential prompt: in a scheduled run that is a
    # hang, not a question.
    child.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
                  "GCM_INTERACTIVE": "never"})
    if env:
        child.update(env)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=child, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out after %ss: %s" % (timeout, " ".join(args))
    except OSError as e:
        return 127, "", str(e)


def branch_name(prefix: str, ticket: str, suffix: str = "") -> str:
    raw = "%s%s%s" % (prefix or "foreman/", ticket, ("-" + suffix) if suffix else "")
    name = BRANCH_SAFE.sub("-", raw).strip("-/")
    return name[:200]


def clone(source: str, dest: str, base_branch: str = "main",
          token: str = "", depth: int = 1) -> str:
    """Clone `source` into `dest` and return the worktree path.

    `source` is either a local path (demo and rehearsal) or an https URL. The
    token, when present, is injected into the URL for the clone and removed
    from the remote immediately afterwards.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    local = os.path.isdir(source)
    url = os.path.abspath(source) if local else _with_token(source, token)

    args = ["clone", "--quiet", "--no-tags"]
    if not local:
        # A shallow clone is enough to change a file and cheap over a WAN, but
        # it cannot be pushed from without unshallowing, so depth is applied
        # only where we will not need history.
        args += ["--depth", str(depth)]
    args += ["--branch", base_branch, url, dest]

    rc, _, err = run(None, *args, timeout=600)
    if rc != 0:
        # A wrong base branch is the most common cause and produces a cryptic
        # message, so retry against the remote's default before giving up.
        rc2, _, err2 = run(None, "clone", "--quiet", "--no-tags", url, dest,
                           timeout=600)
        if rc2 != 0:
            raise GitError("clone failed: %s" % (err or err2).strip()[:400])

    # Strip the credentialed remote. Re-added, briefly, only by `push`.
    run(dest, "remote", "remove", "origin")
    run(dest, "config", "user.name", os.environ.get("FOREMAN_GIT_NAME",
                                                    "Capybara Foreman"))
    run(dest, "config", "user.email", os.environ.get("FOREMAN_GIT_EMAIL",
                                                     "foreman@capybara.invalid"))
    return dest


def _with_token(url: str, token: str) -> str:
    if not token or not url.startswith("https://"):
        return url
    return url.replace("https://", "https://x-access-token:%s@" % token, 1)


def head(repo: str) -> str:
    rc, out, _ = run(repo, "rev-parse", "HEAD")
    return out.strip() if rc == 0 else ""


def current_branch(repo: str) -> str:
    rc, out, _ = run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else ""


def checkout_branch(repo: str, branch: str) -> bool:
    rc, _, _ = run(repo, "checkout", "-q", "-B", branch)
    return rc == 0


def commit(repo: str, subject: str, body: str = "",
           trailers: Optional[List[str]] = None) -> Optional[str]:
    """Stage everything and commit. Returns the sha, or None if nothing changed."""
    run(repo, "add", "-A")
    rc, out, _ = run(repo, "diff", "--cached", "--name-only")
    if rc != 0 or not out.strip():
        return None
    message = subject.strip()
    if body.strip():
        message += "\n\n" + body.strip()
    for t in (trailers or []):
        message += "\n" + t
    rc, _, err = run(repo, "commit", "-q", "--no-verify", "-m", message)
    if rc != 0:
        raise GitError("commit failed: %s" % err.strip()[:300])
    return head(repo)


def push(repo: str, remote_url: str, branch: str, token: str = "",
         force: bool = True) -> Tuple[bool, str]:
    """Push one branch to a remote, then remove the credentialed remote again.

    Force is the default and is safe *because* the branch namespace is
    Foreman's own (`foreman/<TICKET>`): re-running the build stage after a
    rework must replace that branch, not stack a divergent history on it.
    Nothing else is ever pushed.
    """
    url = os.path.abspath(remote_url) if os.path.isdir(remote_url) \
        else _with_token(remote_url, token)
    run(repo, "remote", "remove", "foreman-origin")
    rc, _, err = run(repo, "remote", "add", "foreman-origin", url)
    if rc != 0:
        return False, err.strip()[:300]
    args = ["push", "--quiet"]
    if force:
        args.append("--force-with-lease")
    args += ["foreman-origin", "%s:%s" % (branch, branch)]
    rc, _, err = run(repo, *args, timeout=600)
    run(repo, "remote", "remove", "foreman-origin")
    if rc != 0:
        # --force-with-lease refuses when the remote branch moved under us,
        # which is the right answer: something else is writing here.
        return False, err.strip()[:400] or "push failed"
    return True, branch


def diff_stat(repo: str, base: str = "HEAD~1") -> str:
    rc, out, _ = run(repo, "diff", "--stat", base)
    return out.strip() if rc == 0 else ""


def file_list(repo: str) -> List[str]:
    rc, out, _ = run(repo, "ls-files")
    return out.splitlines() if rc == 0 else []


def ensure_repo(path: str) -> bool:
    """True if `path` is inside a git work tree."""
    rc, out, _ = run(path, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out.strip() == "true"


def run_command(repo: str, command: str, timeout: int = 900
                ) -> Dict[str, Any]:
    """Run a build or test command in the worktree and capture the result.

    This is how the harness establishes whether the tests pass. The build stage
    is also able to run commands, and it will report that everything is fine.
    Only this result is recorded, because only this one was produced by a
    process the model does not control.
    """
    started = __import__("time").time()
    child = dict(os.environ)
    child.update({"CI": "1", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1",
                  "GIT_TERMINAL_PROMPT": "0"})
    # The test command must not inherit anything the model tier should not see.
    for secret in ("JIRA_API_TOKEN", "GITHUB_WRITE_TOKEN", "GITHUB_READ_TOKEN",
                   "BOB_API_KEY", "BOBSHELL_API_KEY"):
        child.pop(secret, None)
    try:
        p = subprocess.run(command, cwd=repo, shell=True, capture_output=True,
                           text=True, timeout=timeout, env=child,
                           stdin=subprocess.DEVNULL)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "timed out after %ss" % timeout
    except OSError as e:
        rc, out, err = 127, "", str(e)
    return {
        "command": command,
        "exit_code": rc,
        "passed": rc == 0,
        "duration_sec": round(__import__("time").time() - started, 2),
        # Keep the tail: a failing suite puts the useful part at the end.
        "stdout": out[-20000:],
        "stderr": err[-8000:],
    }
