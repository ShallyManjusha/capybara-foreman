"""
Change-budget and path enforcement, applied to the diff.

The build stage runs with write tools in a disposable worktree. This module is
what stands between that worktree and your main branch. It does not ask the
model what it changed — it reads `git diff` and decides.

The rules exist because each one corresponds to a way an autonomous build stage
fails in practice:

* **Path allowlist / denylist.** An agent that can edit `.github/workflows/`
  can rewrite the pipeline that is supposed to gate it. An agent that can edit
  `.foreman/` can rewrite its own guardrails. Those paths are denied outright,
  and a denial is not something a plan can grant.
* **No touching existing tests unless the plan said so.** This is the single
  most valuable rule here. The cheapest way to make a failing test pass is to
  change the test, and a model under instruction to "make the tests pass" will
  find that route. Modifying or deleting a test that the approved plan did not
  name is a blocking violation.
* **File and line budget.** A change an order of magnitude larger than the
  approved plan is not the change that was approved. The reviewer looked at a
  plan for three files; they did not consent to forty.
* **Plan adherence.** Files the plan never mentioned are reported. New test
  files are tolerated — writing tests is the point of a later stage — but
  production code outside the plan blocks.
* **No secrets.** A committed credential is the one mistake here that cannot be
  undone by reverting, because the moment it reaches a remote it is burned.
* **No binaries.** Nothing legitimate in an automated bug-fix adds a binary,
  and a binary is the one thing a human reviewer genuinely cannot review.

Every violation carries a `blocking` flag. Blocking violations abort the commit
and send the ticket back to a human with the reason. Non-blocking ones are
reported on the ticket so the reviewer sees them at the sign-off gate.
"""
import fnmatch
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Set

# Paths no plan and no approval can authorise. These are the levers that
# control Foreman itself and the pipeline around it; a change here is a change
# to the controls, and must be made by a person, in a normal pull request.
HARD_DENY = [
    ".github/workflows/*", ".github/actions/*", ".gitlab-ci.yml", "Jenkinsfile",
    ".foreman/*", ".bob/*", "AGENTS.md", ".bobignore",
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.keystore",
    ".git/*", ".ssh/*",
]

TEST_PATTERNS = ["test_*.py", "*_test.py", "*test*.py", "tests/*", "test/*",
                 "*.test.js", "*.test.ts", "*.spec.js", "*.spec.ts",
                 "spec/*", "__tests__/*", "*Test.java", "*Tests.cs"]

BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz",
              ".jar", ".war", ".class", ".so", ".dylib", ".dll", ".exe",
              ".bin", ".pyc", ".woff", ".woff2", ".ttf", ".ico", ".mp4"}

# Deliberately narrow. A broad entropy heuristic on source code produces a
# false positive on every base64 fixture and every hash constant, and a
# guardrail that cries wolf gets switched off.
SECRET_PATTERNS = [
    (r"(?i)\b(?:aws_secret_access_key|aws_access_key_id)\b\s*[:=]\s*\S{8,}", "AWS key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "GitHub token"),
    (r"\bATATT[A-Za-z0-9_\-=]{20,}", "Atlassian API token"),
    (r"\bbob_(?:prod|dev|test)_[A-Za-z0-9_\-]{20,}", "Bob Shell API key"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "private key"),
    (r"(?i)\b(?:password|passwd|secret|api_key|apikey|token)\b\s*[:=]\s*"
     r"[\"'][^\"'\s]{8,}[\"']", "hardcoded credential"),
]


class Violation:
    def __init__(self, code, detail, blocking=True, path=""):
        self.code = code
        self.detail = detail
        self.blocking = blocking
        self.path = path

    def as_dict(self):
        return {"code": self.code, "detail": self.detail,
                "blocking": self.blocking, "path": self.path}

    def __repr__(self):
        return "<%s %s %s>" % ("BLOCK" if self.blocking else "warn",
                               self.code, self.path)


class Guardrails:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        c = cfg or {}
        self.allow: List[str] = list(c.get("allow_paths") or ["**"])
        self.deny: List[str] = list(HARD_DENY) + list(c.get("deny_paths") or [])
        self.max_files = int(c.get("max_files", 15))
        self.max_lines = int(c.get("max_lines", 600))
        self.max_file_lines = int(c.get("max_lines_per_file", 400))
        self.allow_test_edits = bool(c.get("allow_test_edits", False))
        self.allow_new_files = bool(c.get("allow_new_files", True))
        self.require_plan_adherence = bool(c.get("require_plan_adherence", True))
        self.forbid_binaries = bool(c.get("forbid_binaries", True))
        self.scan_secrets = bool(c.get("scan_secrets", True))
        self.test_patterns = list(c.get("test_patterns") or TEST_PATTERNS)
        # Deletions get their own, much tighter cap. Removing code is how a
        # change that "makes the problem go away" differs from a fix.
        self.max_deleted_lines = int(c.get("max_deleted_lines", 200))


class DiffReport:
    def __init__(self):
        self.files: List[Dict[str, Any]] = []
        self.added = 0
        self.removed = 0
        self.violations: List[Violation] = []
        self.patch = ""

    @property
    def blocked(self) -> bool:
        return any(v.blocking for v in self.violations)

    @property
    def paths(self) -> List[str]:
        return [f["path"] for f in self.files]

    def as_dict(self):
        return {
            "files": self.files,
            "file_count": len(self.files),
            "added": self.added, "removed": self.removed,
            "blocked": self.blocked,
            "violations": [v.as_dict() for v in self.violations],
        }

    def summary(self) -> str:
        return "%d file(s), +%d/-%d, %d violation(s)%s" % (
            len(self.files), self.added, self.removed, len(self.violations),
            " — BLOCKED" if self.blocked else "")


# ──────────────────────────────────────────────────────────────── git reads ──
def _git(repo: str, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", repo] + list(args),
                           capture_output=True, text=True, timeout=120)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def collect_diff(repo: str, base_ref: str = "HEAD") -> DiffReport:
    """Everything that changed in the worktree, staged or not, tracked or not.

    Untracked files are included deliberately. A build stage that creates a new
    file has changed the repository just as surely as one that edits an old
    one, and a guardrail that only inspects tracked changes is a guardrail with
    an obvious hole in it.
    """
    report = DiffReport()

    # Stage everything first so one `git diff --cached` sees new files too.
    subprocess.run(["git", "-C", repo, "add", "-A"],
                   capture_output=True, text=True, timeout=120)

    numstat = _git(repo, "diff", "--cached", "--numstat", base_ref)
    status = _git(repo, "diff", "--cached", "--name-status", base_ref)

    actions: Dict[str, str] = {}
    for line in status.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            code = parts[0][:1]
            path = parts[-1]
            actions[path] = {"A": "create", "M": "modify", "D": "delete",
                             "R": "rename", "C": "create"}.get(code, "modify")

    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a_raw, d_raw, path = parts[0], parts[1], parts[-1]
        binary = (a_raw == "-" or d_raw == "-")
        added = 0 if binary else int(a_raw or 0)
        removed = 0 if binary else int(d_raw or 0)
        report.files.append({
            "path": path, "action": actions.get(path, "modify"),
            "added": added, "removed": removed, "binary": binary,
        })
        report.added += added
        report.removed += removed

    report.patch = _git(repo, "diff", "--cached", base_ref)
    return report


# ──────────────────────────────────────────────────────────────── the rules ──
def _matches(path: str, patterns: List[str]) -> bool:
    p = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat):
            return True
        # Treat "dir/*" as covering the whole subtree, which is what everyone
        # means when they write it in a policy file.
        if pat.endswith("/*") and p.startswith(pat[:-1]):
            return True
        if pat == "**":
            return True
    return False


def is_test(path: str, patterns: List[str]) -> bool:
    return _matches(path, patterns)


def inspect(report: DiffReport, rules: Guardrails,
            planned_paths: Optional[List[str]] = None) -> DiffReport:
    """Apply every rule to a collected diff. Mutates and returns the report."""
    planned: Set[str] = {p.replace("\\", "/").lstrip("./")
                         for p in (planned_paths or [])}

    if not report.files:
        report.violations.append(Violation(
            "empty_diff", "the build stage produced no changes at all", True))
        return report

    if len(report.files) > rules.max_files:
        report.violations.append(Violation(
            "too_many_files",
            "%d files changed; the budget is %d" % (len(report.files),
                                                    rules.max_files), True))

    total = report.added + report.removed
    if total > rules.max_lines:
        report.violations.append(Violation(
            "too_many_lines",
            "%d lines changed; the budget is %d" % (total, rules.max_lines), True))

    if report.removed > rules.max_deleted_lines:
        report.violations.append(Violation(
            "too_much_deleted",
            "%d lines removed; the deletion budget is %d. Deleting code is how "
            "a change that suppresses a problem differs from one that fixes it."
            % (report.removed, rules.max_deleted_lines), True))

    for f in report.files:
        path, action = f["path"], f["action"]

        if _matches(path, rules.deny):
            report.violations.append(Violation(
                "denied_path",
                "this path controls the pipeline or holds credentials; no plan "
                "and no approval can authorise a change to it", True, path))
            continue

        if not _matches(path, rules.allow):
            report.violations.append(Violation(
                "outside_allowlist",
                "not covered by any allow_paths pattern in policy/guardrails.yaml",
                True, path))
            continue

        if rules.forbid_binaries and (f["binary"] or
                                      os.path.splitext(path)[1].lower() in BINARY_EXT):
            report.violations.append(Violation(
                "binary_file",
                "binary content cannot be reviewed by the human at the gate",
                True, path))

        if f["added"] + f["removed"] > rules.max_file_lines:
            report.violations.append(Violation(
                "file_too_large",
                "%d lines changed in one file; the per-file budget is %d"
                % (f["added"] + f["removed"], rules.max_file_lines), True, path))

        touching_test = is_test(path, rules.test_patterns)
        if touching_test and action in ("modify", "delete") and \
                not rules.allow_test_edits and path not in planned:
            report.violations.append(Violation(
                "test_modified",
                "an existing test was %s but the approved plan did not name it. "
                "Changing the test is the cheapest way to make a failing test "
                "pass, and it is never the fix."
                % ("deleted" if action == "delete" else "modified"), True, path))

        if action == "create" and not rules.allow_new_files and path not in planned:
            report.violations.append(Violation(
                "unplanned_new_file", "new files are disabled by policy", True, path))

        if rules.require_plan_adherence and planned and path not in planned:
            # New tests are expected: the verify stage exists to add them.
            blocking = not (touching_test and action == "create")
            report.violations.append(Violation(
                "not_in_plan",
                "changed but not named in the approved plan%s"
                % ("" if blocking else " (new test file — allowed)"),
                blocking, path))

    if rules.scan_secrets:
        report.violations.extend(scan_patch_for_secrets(report.patch))

    return report


def scan_patch_for_secrets(patch: str) -> List[Violation]:
    """Look for credentials in *added* lines only.

    Scanning removed lines would flag the very commit that takes a secret out
    of the codebase, which is exactly the commit you want to encourage.
    """
    out: List[Violation] = []
    path = ""
    seen: Set[str] = set()
    for line in (patch or "").splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        for pattern, label in SECRET_PATTERNS:
            if re.search(pattern, body):
                key = "%s:%s" % (path, label)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Violation(
                    "secret_in_diff",
                    "%s detected in an added line. A credential that reaches a "
                    "remote is burned; reverting does not un-publish it." % label,
                    True, path))
    return out


def revert(repo: str) -> bool:
    """Throw the worktree away. Called whenever a diff is rejected.

    Leaving a rejected change on disk invites a later stage to find it and
    commit it, which is how a blocked change reaches main anyway.
    """
    ok = True
    for args in (("reset", "--hard", "HEAD"), ("clean", "-fdx")):
        p = subprocess.run(["git", "-C", repo] + list(args),
                           capture_output=True, text=True, timeout=120)
        ok = ok and p.returncode == 0
    return ok
