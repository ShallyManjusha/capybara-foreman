"""
GitHub / GHES REST client (stdlib urllib only).

Used exclusively by the publisher tier, after the guardrails have passed a
diff. The analysis and build tiers never import this module and never hold a
write token — the tier that reads a ticket written by a stranger must not be
one clever paragraph away from a credential that can push to main.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class GitHubError(Exception):
    def __init__(self, status, message, url=""):
        super().__init__("HTTP %s on %s: %s" % (status, url, message))
        self.status = status
        self.message = message


class GitHubClient:
    def __init__(self, api: str, token: str, timeout: int = 45, retries: int = 3):
        self.api = (api or "https://api.github.com").rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries

    def _call(self, method: str, path: str,
              body: Optional[Dict[str, Any]] = None,
              params: Optional[Dict[str, Any]] = None,
              raw_ok: Tuple[int, ...] = (404, 422)) -> Tuple[int, Any]:
        url = path if path.startswith("http") else self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer %s" % self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "capybara-foreman/0.1",
        })
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8")
                    return r.status, (json.loads(raw) if raw.strip() else {})
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:600]
                if e.code in raw_ok:
                    try:
                        return e.code, json.loads(detail)
                    except ValueError:
                        return e.code, {"message": detail}
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(e.code, detail, url)
            except urllib.error.URLError as e:
                if attempt < self.retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(0, str(e.reason), url)
        raise GitHubError(0, "exhausted retries", url)

    # ───────────────────────────────────────────────────────────────── reads ─
    def whoami(self) -> Dict[str, Any]:
        _, d = self._call("GET", "/user")
        return d

    def find_pr(self, repo: str, branch: str) -> Optional[Dict[str, Any]]:
        owner = repo.split("/")[0]
        _, d = self._call("GET", "/repos/%s/pulls" % repo,
                          params={"state": "open",
                                  "head": "%s:%s" % (owner, branch)})
        return d[0] if isinstance(d, list) and d else None

    def get_pr(self, repo: str, number: int) -> Optional[Dict[str, Any]]:
        st, d = self._call("GET", "/repos/%s/pulls/%d" % (repo, number))
        return None if st == 404 else d

    # ──────────────────────────────────────────────────────────────── writes ─
    def create_pr(self, repo: str, title: str, head: str, base: str,
                  body: str, draft: bool = False) -> Dict[str, Any]:
        st, d = self._call("POST", "/repos/%s/pulls" % repo,
                           {"title": title[:250], "head": head, "base": base,
                            "body": body, "draft": draft})
        if st >= 400:
            raise GitHubError(st, json.dumps(d)[:400], repo)
        return d

    def update_pr(self, repo: str, number: int, title: str = "",
                  body: str = "") -> Dict[str, Any]:
        payload = {}
        if title:
            payload["title"] = title[:250]
        if body:
            payload["body"] = body
        _, d = self._call("PATCH", "/repos/%s/pulls/%d" % (repo, number), payload)
        return d

    def add_labels(self, repo: str, number: int, labels: List[str]) -> bool:
        if not labels:
            return True
        st, _ = self._call("POST", "/repos/%s/issues/%d/labels" % (repo, number),
                           {"labels": labels})
        return st < 400

    def comment(self, repo: str, number: int, body: str) -> bool:
        st, _ = self._call("POST", "/repos/%s/issues/%d/comments" % (repo, number),
                           {"body": body})
        return st < 400

    def merge_pr(self, repo: str, number: int, method: str = "squash",
                 title: str = "") -> Tuple[bool, str]:
        body: Dict[str, Any] = {"merge_method": method}
        if title:
            body["commit_title"] = title[:250]
        st, d = self._call("PUT", "/repos/%s/pulls/%d/merge" % (repo, number),
                           body, raw_ok=(404, 405, 409, 422))
        if st >= 400:
            return False, str((d or {}).get("message", "merge refused"))[:300]
        return True, str((d or {}).get("sha", ""))

    # ───────────────────────────────────────────────────────────── workflows ─
    def dispatch_workflow(self, repo: str, workflow: str, ref: str,
                          inputs: Optional[Dict[str, str]] = None) -> bool:
        st, _ = self._call(
            "POST", "/repos/%s/actions/workflows/%s/dispatches" % (repo, workflow),
            {"ref": ref, "inputs": inputs or {}}, raw_ok=(404, 422))
        return st in (204, 200)

    def latest_run(self, repo: str, workflow: str,
                   branch: str = "") -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"per_page": 5}
        if branch:
            params["branch"] = branch
        st, d = self._call("GET", "/repos/%s/actions/workflows/%s/runs"
                           % (repo, workflow), params=params)
        runs = (d or {}).get("workflow_runs") or []
        return runs[0] if runs else None

    def get_run(self, repo: str, run_id: int) -> Optional[Dict[str, Any]]:
        st, d = self._call("GET", "/repos/%s/actions/runs/%d" % (repo, run_id))
        return None if st == 404 else d
