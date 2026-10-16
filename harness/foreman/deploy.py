"""
The DevOps hand-off: trigger a non-prod deployment and wait for its verdict.

Foreman does not deploy. It asks whatever already deploys your software to do
it, and then waits. That boundary is deliberate — a delivery agent that also
holds deployment credentials is an agent whose blast radius is your estate, and
every organisation that has one already has a pipeline that does this properly.

Four modes, because a demo, a GitHub-Actions shop and a Tekton shop need
different things and none of them should need a fork:

  simulated       runs a local script; for rehearsal and for the demo board
  github-actions  workflow_dispatch, then polls the run to a conclusion
  webhook         POSTs the payload to your orchestrator and trusts its 2xx
  none            skips the stage; the ticket goes straight to verification

**Production is never one of them.** On close, Foreman sets a fixVersion and
raises a deployment ticket. A human schedules the release. That is not timidity
about automation; it is that a production deploy has a blast radius no ticket
comment can consent to on a team's behalf.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .github import GitHubClient, GitHubError


class DeployResult:
    def __init__(self, mode, ok, status, url="", detail="", duration=0.0,
                 skipped=False):
        self.mode = mode
        self.ok = ok
        self.status = status          # success | failure | timeout | skipped | dispatched
        self.url = url
        self.detail = detail
        self.duration = duration
        self.skipped = skipped

    def as_dict(self):
        return {"mode": self.mode, "ok": self.ok, "status": self.status,
                "url": self.url, "detail": self.detail[:800],
                "duration_sec": round(self.duration, 1), "skipped": self.skipped}


def trigger(cfg: Dict[str, Any], ticket: str, branch: str, sha: str,
            repo: str = "", gh: Optional[GitHubClient] = None,
            workdir: str = "") -> DeployResult:
    mode = str(cfg.get("mode", "none")).strip().lower()
    started = time.time()

    if mode in ("none", "", "off"):
        return DeployResult("none", True, "skipped",
                            detail="deployment stage disabled by configuration",
                            skipped=True)

    if mode == "simulated":
        return _simulated(cfg, ticket, branch, sha, started, workdir)

    if mode == "webhook":
        return _webhook(cfg, ticket, branch, sha, started)

    if mode == "github-actions":
        return _github(cfg, ticket, branch, sha, repo, gh, started)

    if mode == "okd":
        return _okd(cfg, ticket, branch, sha, started)

    return DeployResult(mode, False, "failure",
                        detail="unknown FOREMAN_DEPLOY_MODE: %s" % mode,
                        duration=time.time() - started)


# ────────────────────────────────────────────────────────────────── modes ────
def _simulated(cfg, ticket, branch, sha, started, workdir) -> DeployResult:
    script = cfg.get("script") or "policy/deploy-simulate.sh"
    if not os.path.exists(script):
        return DeployResult("simulated", False, "failure",
                            detail="deploy script not found: %s" % script,
                            duration=time.time() - started)
    env = dict(os.environ)
    env.update({"FOREMAN_TICKET": ticket, "FOREMAN_BRANCH": branch,
                "FOREMAN_SHA": sha, "FOREMAN_ENV": cfg.get("env", "nonprod"),
                "FOREMAN_WORKDIR": workdir or ""})
    # The deploy script is ours, not the model's, but it still runs without the
    # credentials the model tier was denied — there is no reason for it to
    # inherit them and every reason not to.
    for secret in ("BOB_API_KEY", "BOBSHELL_API_KEY", "JIRA_API_TOKEN"):
        env.pop(secret, None)
    try:
        p = subprocess.run(["bash", script], capture_output=True, text=True,
                           timeout=int(cfg.get("timeout_sec", 600)), env=env,
                           stdin=subprocess.DEVNULL)
        ok = p.returncode == 0
        return DeployResult("simulated", ok, "success" if ok else "failure",
                            url=_url_from(p.stdout),
                            detail=(p.stdout + p.stderr)[-4000:],
                            duration=time.time() - started)
    except subprocess.TimeoutExpired:
        return DeployResult("simulated", False, "timeout",
                            detail="deploy script exceeded its timeout",
                            duration=time.time() - started)


def _webhook(cfg, ticket, branch, sha, started) -> DeployResult:
    url = cfg.get("webhook") or ""
    if not url:
        return DeployResult("webhook", False, "failure",
                            detail="FOREMAN_DEPLOY_WEBHOOK is empty",
                            duration=time.time() - started)
    payload = json.dumps({"ticket": ticket, "branch": branch, "sha": sha,
                          "environment": cfg.get("env", "nonprod"),
                          "source": "ai-sdlc-foreman"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "ai-sdlc-foreman/0.1"})
    token = cfg.get("webhook_token")
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")[:2000]
        # A webhook tells us the request was accepted, not that the deploy
        # succeeded. Saying "dispatched" rather than "success" keeps the ticket
        # honest about what is actually known.
        return DeployResult("webhook", True, "dispatched", detail=body,
                            duration=time.time() - started)
    except urllib.error.HTTPError as e:
        return DeployResult("webhook", False, "failure",
                            detail="HTTP %s: %s" % (e.code,
                                                    e.read().decode("utf-8", "replace")[:400]),
                            duration=time.time() - started)
    except Exception as e:
        return DeployResult("webhook", False, "failure", detail=str(e)[:400],
                            duration=time.time() - started)


def _github(cfg, ticket, branch, sha, repo, gh, started) -> DeployResult:
    if gh is None or not repo:
        return DeployResult("github-actions", False, "failure",
                            detail="no GitHub client or repository configured",
                            duration=time.time() - started)
    workflow = cfg.get("workflow") or "deploy-nonprod.yml"
    ref = cfg.get("ref") or branch
    inputs = {"ticket": ticket, "branch": branch, "sha": sha[:40],
              "environment": str(cfg.get("env", "nonprod"))}

    before = None
    try:
        before = gh.latest_run(repo, workflow, branch=ref)
    except GitHubError:
        pass

    try:
        if not gh.dispatch_workflow(repo, workflow, ref, inputs):
            return DeployResult("github-actions", False, "failure",
                                detail="workflow_dispatch was refused — check the "
                                       "workflow exists on %s and declares "
                                       "workflow_dispatch inputs" % ref,
                                duration=time.time() - started)
    except GitHubError as e:
        return DeployResult("github-actions", False, "failure",
                            detail=str(e)[:400], duration=time.time() - started)

    timeout = int(cfg.get("timeout_sec", 1800))
    before_id = (before or {}).get("id")
    run = None
    # GitHub does not return the run id from a dispatch, so the new run has to
    # be identified by being a different one from the last. Poll gently: this
    # loop runs while a deployment is in flight and there is no prize for
    # hammering the API.
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(10)
        try:
            candidate = gh.latest_run(repo, workflow, branch=ref)
        except GitHubError:
            continue
        if candidate and candidate.get("id") != before_id:
            run = candidate
            break

    if run is None:
        return DeployResult("github-actions", False, "timeout",
                            detail="dispatched, but no new workflow run appeared "
                                   "within %ss" % timeout,
                            duration=time.time() - started)

    url = run.get("html_url", "")
    while time.time() < deadline:
        status = run.get("status")
        if status == "completed":
            concl = run.get("conclusion") or "unknown"
            return DeployResult("github-actions", concl == "success",
                                "success" if concl == "success" else "failure",
                                url=url, detail="conclusion: %s" % concl,
                                duration=time.time() - started)
        time.sleep(15)
        try:
            run = gh.get_run(repo, int(run["id"])) or run
        except GitHubError:
            continue

    return DeployResult("github-actions", False, "timeout", url=url,
                        detail="workflow run did not complete within %ss" % timeout,
                        duration=time.time() - started)




# ─────────────────────────────────────────── OKD / OpenShift pod readiness ──
def _oc() -> Optional[str]:
    import shutil
    return shutil.which(os.environ.get("FOREMAN_OC_BINARY", "oc")) or shutil.which("kubectl")


def list_pods(namespace: str, selector: str) -> tuple:
    """(items, error). Each item is the pod's name, readiness and age facts."""
    oc = _oc()
    if not oc:
        return [], ("oc/kubectl not found on PATH. Install the OpenShift CLI and "
                    "log it in with a service account that can `get pods` in "
                    "%s." % namespace)
    try:
        p = subprocess.run([oc, "get", "pods", "-n", namespace, "-l", selector,
                            "-o", "json"], capture_output=True, text=True,
                           timeout=45, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [], "oc get pods timed out"
    except OSError as e:
        return [], str(e)[:200]
    if p.returncode != 0:
        return [], (p.stderr or p.stdout or "oc failed").strip()[:300]
    try:
        raw = json.loads(p.stdout or "{}").get("items") or []
    except ValueError:
        return [], "oc returned unparseable output"
    return [_pod_facts(i) for i in raw], ""


def _pod_facts(item: Dict[str, Any]) -> Dict[str, Any]:
    meta, status = item.get("metadata") or {}, item.get("status") or {}
    cs = status.get("containerStatuses") or []
    return {"name": meta.get("name", ""), "uid": meta.get("uid", ""),
            "created": meta.get("creationTimestamp", ""),
            "terminating": bool(meta.get("deletionTimestamp")),
            "phase": status.get("phase", ""),
            "ready": bool(cs) and all(c.get("ready") for c in cs)
            and status.get("phase") == "Running"}


def rollout_state(items: List[Dict[str, Any]], since: str = "") -> tuple:
    """Decide whether a rollout has finished. Returns (done, message).

    The first version passed as soon as every matching pod was Ready -- which
    is already true *before* the pipeline has replaced anything, so a deploy
    that had not started looked finished. A rollout is finished when there is
    at least one pod, none is terminating, every one is Ready, and -- when we
    know when the change merged (`since`, UTC ISO-8601) -- every pod was
    created after that moment, i.e. nothing from the old version is left.
    """
    if any(p["terminating"] for p in items):
        return False, "old pods still terminating"
    live = list(items)
    if not live:
        return False, "no pods matched the selector"
    not_ready = [p["name"] for p in live if not p["ready"]]
    if not_ready:
        return False, "waiting for Ready: %s" % ", ".join(not_ready[:4])
    if since:
        old = [p["name"] for p in live if p.get("created") and p["created"] < since]
        if old:
            return False, "pods from before the merge still running: %s" % \
                ", ".join(old[:4])
    return True, "%d pod(s) Ready%s" % (len(live), ", all newer than the merge" if since else "")


def _wait_for_rollout(namespace: str, selector: str, since: str,
                      timeout_sec: int, poll_sec: int = 15) -> tuple:
    """Poll `oc get pods` until the rollout is finished. Returns (ok, message)."""
    deadline = time.time() + timeout_sec
    last = ""
    # Two consecutive good reads: a pod that is Ready for one poll and then
    # restarts is not a finished deploy.
    streak = 0
    while True:
        items, err = list_pods(namespace, selector)
        if err and "not found on PATH" in err:
            return False, err
        done, msg = (False, err) if err else rollout_state(items, since)
        last = msg
        streak = streak + 1 if done else 0
        if streak >= 2:
            return True, msg
        if time.time() >= deadline:
            return False, ("pods did not finish rolling out within %ds. Last: %s"
                           % (timeout_sec, last))
        time.sleep(poll_sec)


def _wait_for_pods_ready(namespace: str, label_selector: str,
                         timeout_sec: int = 600) -> tuple:
    """Backward-compatible: wait for Ready without comparing to a snapshot."""
    return _wait_for_rollout(namespace, label_selector, "", timeout_sec)


def _okd(cfg, ticket, branch, sha, started) -> DeployResult:
    """Wait for the CP4i/OKD rollout that the merge to the DEV branch triggered.

    The merge itself starts your pipeline (Tekton, Jenkins, GitHub Actions ...);
    Foreman does not. If FOREMAN_DEPLOY_WEBHOOK is set it is POSTed to as well,
    for pipelines that need an explicit trigger. Either way the verdict comes
    from the pods, not from anyone's message saying they are fine.

    Needs FOREMAN_OKD_NAMESPACE and FOREMAN_OKD_LABEL_SELECTOR. `cfg["since"]` is
    the merge time; only pods created after it count as the new version.
    """
    namespace = cfg.get("okd_namespace") or os.environ.get(
        "FOREMAN_OKD_NAMESPACE", "").strip()
    selector = cfg.get("okd_label_selector") or os.environ.get(
        "FOREMAN_OKD_LABEL_SELECTOR", "").strip()
    pod_timeout = int(os.environ.get("FOREMAN_OKD_POD_TIMEOUT_SEC", "900") or 900)

    if not namespace or not selector:
        return DeployResult("okd", False, "failure",
                            detail="FOREMAN_OKD_NAMESPACE and FOREMAN_OKD_LABEL_SELECTOR "
                                   "must both be set for the okd deploy mode.",
                            duration=time.time() - started)

    webhook_url = cfg.get("webhook") or os.environ.get("FOREMAN_DEPLOY_WEBHOOK", "").strip()
    if webhook_url:
        webhook_result = _webhook(cfg, ticket, branch, sha, started)
        if not webhook_result.ok:
            return webhook_result
        time.sleep(10)

    since = str(cfg.get("since") or "")
    if os.environ.get("FOREMAN_OKD_REQUIRE_NEW_PODS", "true").strip().lower() \
            in ("0", "false", "no"):
        since = ""
    ok, message = _wait_for_rollout(namespace, selector, since, pod_timeout)
    duration = time.time() - started
    if ok:
        return DeployResult("okd", True, "success",
                            detail="Rollout finished: %s" % message, duration=duration)
    return DeployResult("okd", False, "timeout",
                        detail="Rollout did not finish: %s" % message,
                        duration=duration)


def _url_from(text: str) -> str:
    """Let a deploy script publish its own URL by printing `DEPLOY_URL=...`."""
    for line in (text or "").splitlines():
        if line.strip().startswith("DEPLOY_URL="):
            return line.split("=", 1)[1].strip()
    return ""
