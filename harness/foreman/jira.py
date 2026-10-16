"""
Jira Cloud REST client (stdlib urllib only).

Two things about this module are design decisions rather than plumbing.

**The board is the source of truth.** Foreman's per-ticket state lives in a Jira
*issue property* (`capybara.foreman.state`), not in the local SQLite file. The
database is a cache and an audit trail. That means you can delete `var/`, move
Foreman to another machine, or run two of them, and every ticket resumes from
exactly where it was. A workflow engine whose state lives on one laptop is a
workflow engine that loses a release.

**Labels are the human signal, the property is the machine record.** A reviewer
should be able to look at the board and see `foreman:awaiting-approval` without
opening anything. They should never have to read JSON to know what is happening,
and Foreman should never have to parse a human-editable field to know what it
did.

**Two deployments, one client.** Jira Cloud and Jira Data Center are different
products wearing the same name. They differ in the REST version (`/rest/api/3`
vs `/rest/api/2`), the comment body format (ADF, a JSON document tree, vs wiki
markup, a string), the search endpoint and its pagination, and the auth scheme
(an email + API token vs a personal access token as a bearer). A client that
speaks only Cloud works at roughly half the enterprises you will meet.

The deployment is detected from `/serverInfo` rather than configured, because
an operator who has to know the answer will eventually get it wrong, and the
failure mode — a 404 on every call, or comments posted as literal JSON — does
not point at the cause. `JIRA_DEPLOYMENT` overrides it when detection is not
possible, for instance behind a proxy that rewrites `serverInfo`.

Everything Foreman says is rendered to **markdown** by `render.py` and
converted at the last moment: to ADF for Cloud, to wiki markup for DC. Adding
a third tracker is one more converter, not one more set of templates.

Used by BOTH tiers, but with different credentials: the analysis tier gets a
client only if `readonly=True`, which refuses every mutating call outright.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import adf, markup

STATE_PROPERTY = "capybara.foreman.state"
USER_AGENT = "ai-sdlc-foreman/0.1"

CLOUD = "cloud"
DATACENTER = "datacenter"


class JiraError(Exception):
    def __init__(self, status, message, url=""):
        super().__init__("HTTP %s on %s: %s" % (status, url, message))
        self.status = status
        self.message = message
        self.url = url


class ReadOnlyViolation(JiraError):
    """Raised when the analysis tier attempts a write.

    This is a bug-catcher, not a defence in depth: the analysis tier should not
    be holding a client that *could* write. It exists so that mistake fails
    loudly in a unit test rather than quietly on someone's board.
    """
    def __init__(self, method, path):
        super().__init__(0, "read-only client attempted %s %s" % (method, path))


class JiraClient:
    def __init__(self, site: str, email: str, token: str,
                 timeout: int = 45, retries: int = 4, readonly: bool = False,
                 deployment: str = "auto", scheme: str = ""):
        site = (site or "").strip()
        # A DC instance is often on a path, behind a port, and on plain http
        # inside a VPN. Preserve whatever the operator gave us.
        detected_scheme = "https"
        for prefix in ("https://", "http://"):
            if site.startswith(prefix):
                detected_scheme = prefix[:-3]
                site = site[len(prefix):]
                break
        self.site = site.strip("/")
        self.base = "%s://%s" % (scheme or detected_scheme, self.site)
        self.email = email
        self.token = token
        self.readonly = readonly
        self.timeout = timeout
        self.retries = retries
        self.calls = 0

        # An email plus a token is Cloud's Basic scheme. A token alone is a DC
        # personal access token, which is a bearer. Inferring from what the
        # operator supplied is more reliable than another env var to get wrong.
        if email:
            self._auth_header = "Basic " + base64.b64encode(
                ("%s:%s" % (email, token)).encode("utf-8")).decode("ascii")
        else:
            self._auth_header = "Bearer %s" % token

        self._deployment = (deployment or "auto").strip().lower()
        if self._deployment not in (CLOUD, DATACENTER):
            self._deployment = ""          # resolved lazily on first use

    # ────────────────────────────────────────────────────────── deployment ──
    @property
    def deployment(self) -> str:
        if not self._deployment:
            self._deployment = self._detect_deployment()
        return self._deployment

    def _detect_deployment(self) -> str:
        """Ask the instance what it is.

        `/serverInfo` exists at the same path on both and needs no auth on most
        instances, so this works before anything else does. If it cannot be
        reached we assume Cloud — the more common case, and the failure is a
        clear 404 rather than silently malformed comment bodies.
        """
        for version in ("2", "3"):
            try:
                _, d = self._call("GET", "/rest/api/%s/serverInfo" % version,
                                  raw_ok=(404, 401))
            except JiraError:
                continue
            kind = str((d or {}).get("deploymentType", "")).strip().lower()
            if kind == "cloud":
                return CLOUD
            if kind in ("server", "datacenter", "data center"):
                return DATACENTER
        return CLOUD

    @property
    def api_version(self) -> str:
        return "2" if self.deployment == DATACENTER else "3"

    def _api(self, path: str) -> str:
        """Insert the REST version this deployment speaks."""
        return "/rest/api/%s%s" % (self.api_version, path)

    def _body(self, body_md: str):
        """Render markdown into whatever this deployment accepts."""
        if self.deployment == DATACENTER:
            return markup.truncate(markup.from_markdown(body_md))
        return adf.truncate(adf.from_markdown(body_md))

    def text_of(self, body) -> str:
        """Flatten a comment or description body of either format."""
        return markup.to_text(body) if isinstance(body, str) else adf.to_text(body)

    # ───────────────────────────────────────────────────────── transport ────
    def _call(self, method: str, path: str,
              body: Optional[Dict[str, Any]] = None,
              params: Optional[Dict[str, Any]] = None,
              raw_ok: Tuple[int, ...] = (404,)) -> Tuple[int, Any]:
        if self.readonly and method not in ("GET", "HEAD"):
            raise ReadOnlyViolation(method, path)

        url = path if path.startswith("http") else self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        })

        last = None
        for attempt in range(self.retries):
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = r.read().decode("utf-8")
                    return r.status, (json.loads(payload) if payload.strip() else {})
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:800]
                # Jira answers "no such thing" with 404 and "you asked for
                # something impossible" with 400. Both are answers, not
                # transport failures; the caller decides what they mean.
                if e.code in raw_ok:
                    return e.code, _maybe_json(detail)
                if e.code == 429:
                    # Jira Cloud is aggressive about rate limits on search and
                    # comment endpoints. Honour Retry-After; guessing produces
                    # a thundering herd across a backlog sweep.
                    wait = _retry_after(e) or (2 ** attempt)
                    if attempt < self.retries - 1:
                        time.sleep(min(wait, 60))
                        last = e
                        continue
                if e.code in (500, 502, 503, 504) and attempt < self.retries - 1:
                    time.sleep(2 ** attempt)
                    last = e
                    continue
                raise JiraError(e.code, _error_text(detail), url)
            except urllib.error.URLError as e:
                if attempt < self.retries - 1:
                    time.sleep(2 ** attempt)
                    last = e
                    continue
                raise JiraError(0, str(e.reason), url)
        raise JiraError(0, str(last), url)

    # ───────────────────────────────────────────────────────────── identity ──
    def myself(self) -> Dict[str, Any]:
        _, d = self._call("GET", self._api("/myself"))
        return d

    def server_info(self) -> Dict[str, Any]:
        _, d = self._call("GET", self._api("/serverInfo"))
        return d

    def find_users(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        key = "username" if self.deployment == DATACENTER else "query"
        st, d = self._call("GET", self._api("/user/search"),
                           params={key: query, "maxResults": limit})
        return d if isinstance(d, list) else []

    # ─────────────────────────────────────────────────────────────── search ──
    def search(self, jql: str, fields: Optional[List[str]] = None,
               limit: int = 50, expand: str = "") -> List[Dict[str, Any]]:
        """Run JQL. Uses the token-paginated endpoint, falls back to the old one.

        Atlassian replaced `/search` with `/search/jql` and changed pagination
        from offsets to an opaque token. Sites are migrated at different times,
        so both are supported; a harness that works only on the newest API is a
        harness that stops working on a customer's instance.
        """
        fields = fields or ["summary", "status", "labels", "issuetype",
                            "priority", "assignee", "reporter", "created",
                            "updated", "description", "parent", "fixVersions"]
        if self.deployment == DATACENTER:
            # DC never had the token-paginated endpoint. Going straight there
            # avoids a guaranteed 404 on the first page of every search.
            return self._search_legacy(jql, fields, limit, expand)

        out: List[Dict[str, Any]] = []
        token = None
        while len(out) < limit:
            body: Dict[str, Any] = {
                "jql": jql,
                "maxResults": min(100, limit - len(out)),
                "fields": fields,
            }
            if expand:
                body["expand"] = expand.split(",")
            if token:
                body["nextPageToken"] = token
            st, d = self._call("POST", self._api("/search/jql"), body,
                               raw_ok=(400, 404, 410))
            if st in (404, 410) or (st == 400 and not isinstance(d, dict)):
                return self._search_legacy(jql, fields, limit, expand)
            if st == 400:
                raise JiraError(400, _error_text(json.dumps(d)),
                                self._api("/search/jql"))
            issues = d.get("issues") or []
            out.extend(issues)
            token = d.get("nextPageToken")
            if not token or not issues:
                break
        return out[:limit]

    def _search_legacy(self, jql, fields, limit, expand) -> List[Dict[str, Any]]:
        out, start = [], 0
        while len(out) < limit:
            body = {"jql": jql, "startAt": start,
                    "maxResults": min(100, limit - len(out)), "fields": fields}
            if expand:
                body["expand"] = expand.split(",")
            st, d = self._call("POST", self._api("/search"), body)
            issues = d.get("issues") or []
            out.extend(issues)
            start += len(issues)
            if not issues or start >= int(d.get("total", 0)):
                break
        return out[:limit]

    # ──────────────────────────────────────────────────────────────── issues ─
    def get_issue(self, key: str, fields: Optional[List[str]] = None,
                  expand: str = "") -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = expand
        st, d = self._call("GET", self._api("/issue/%s") % _q(key), params=params)
        return None if st == 404 else d

    def create_issue(self, project_key: str, summary: str, description_md: str,
                     issue_type: str = "Task",
                     labels: Optional[List[str]] = None,
                     priority: str = "",
                     parent: str = "",
                     extra_fields: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
        fields: Dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary[:250],
            "issuetype": {"name": issue_type},
            "description": self._body(description_md),
        }
        if labels:
            fields["labels"] = labels
        if priority:
            fields["priority"] = {"name": priority}
        if parent:
            fields["parent"] = {"key": parent}
        if extra_fields:
            fields.update(extra_fields)
        st, d = self._call("POST", self._api("/issue"), {"fields": fields},
                           raw_ok=(400,))
        if st >= 400:
            raise JiraError(st, _error_text(json.dumps(d)), "create_issue")
        return d

    def set_labels(self, key: str, add: Optional[List[str]] = None,
                   remove: Optional[List[str]] = None) -> bool:
        """Add and remove labels in one call, without read-modify-write.

        Using `update` rather than `fields` means two Foreman processes (or a
        human editing the ticket at the same moment) cannot clobber each
        other's labels. A read-modify-write here would silently drop whatever
        changed in between.
        """
        ops = [{"add": l} for l in (add or [])] + \
              [{"remove": l} for l in (remove or [])]
        if not ops:
            return True
        st, d = self._call("PUT", self._api("/issue/%s") % _q(key),
                           {"update": {"labels": ops}}, raw_ok=(400, 404))
        return st < 400

    def assign(self, key: str, account_id: Optional[str]) -> bool:
        # DC predates account ids and still identifies users by username.
        field = "name" if self.deployment == DATACENTER else "accountId"
        st, _ = self._call("PUT", self._api("/issue/%s/assignee") % _q(key),
                           {field: account_id}, raw_ok=(400, 404))
        return st < 400

    def set_fix_version(self, key: str, version_name: str) -> bool:
        st, _ = self._call("PUT", self._api("/issue/%s") % _q(key),
                           {"update": {"fixVersions": [{"add": {"name": version_name}}]}},
                           raw_ok=(400, 404))
        return st < 400

    # ─────────────────────────────────────────────────────────── transitions ─
    def transitions(self, key: str) -> List[Dict[str, Any]]:
        st, d = self._call("GET", self._api("/issue/%s/transitions") % _q(key))
        return (d or {}).get("transitions", []) if st < 400 else []

    def transition_to(self, key: str, target_status: str) -> Optional[str]:
        """Move the issue to a named status if that transition is available.

        Returns the transition name applied, or None. Deliberately forgiving:
        Foreman's own state lives in labels and an issue property, so a board
        whose workflow has no matching status still runs correctly — it just
        does not mirror. Requiring a bespoke workflow would make this
        undeployable on most real projects.
        """
        want = (target_status or "").strip().lower()
        if not want:
            return None
        for t in self.transitions(key):
            to = ((t.get("to") or {}).get("name") or "").strip().lower()
            if to == want or (t.get("name") or "").strip().lower() == want:
                st, _ = self._call("POST", self._api("/issue/%s/transitions") % _q(key),
                                   {"transition": {"id": t["id"]}}, raw_ok=(400, 404))
                return t.get("name") if st < 400 else None
        return None

    # ───────────────────────────────────────────────────────────── comments ──
    def comments(self, key: str, limit: int = 200,
                 newest_first: bool = False) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        start = 0
        order = "-created" if newest_first else "created"
        while len(out) < limit:
            st, d = self._call("GET", self._api("/issue/%s/comment") % _q(key),
                               params={"startAt": start, "maxResults": 100,
                                       "orderBy": order})
            if st >= 400:
                break
            batch = (d or {}).get("comments") or []
            out.extend(batch)
            start += len(batch)
            if not batch or start >= int((d or {}).get("total", 0)):
                break
        return out[:limit]

    def add_comment(self, key: str, body_md: str,
                    internal: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"body": self._body(body_md)}
        if internal:
            # Jira Service Management only; harmless elsewhere.
            payload["properties"] = [{"key": "sd.public.comment",
                                      "value": {"internal": True}}]
        st, d = self._call("POST", self._api("/issue/%s/comment") % _q(key),
                           payload, raw_ok=(400,))
        if st >= 400:
            raise JiraError(st, _error_text(json.dumps(d)), "add_comment")
        return d

    def update_comment(self, key: str, comment_id: str,
                       body_md: str) -> Dict[str, Any]:
        st, d = self._call("PUT", self._api("/issue/%s/comment/%s")
                           % (_q(key), _q(str(comment_id))),
                           {"body": self._body(body_md)},
                           raw_ok=(400, 404))
        if st >= 400:
            raise JiraError(st, _error_text(json.dumps(d)), "update_comment")
        return d

    def find_marked_comment(self, key: str, marker: str) -> Optional[Dict[str, Any]]:
        """Foreman's own comment, located by an invisible marker in its text.

        Matching on a marker rather than on authorship means a second Foreman,
        or a token swap, still finds and updates the right comment instead of
        starting a parallel conversation on the ticket.

        Newest first: a ticket that ran in append mode carries one marked
        comment per revision, and the one to update is the latest, not the
        plan that was superseded two revisions ago.
        """
        for c in self.comments(key, newest_first=True):
            if marker in self.text_of(c.get("body")):
                return c
        return None

    # ─────────────────────────────────────────────────────── issue property ──
    def get_state(self, key: str) -> Dict[str, Any]:
        st, d = self._call("GET", self._api("/issue/%s/properties/%s")
                           % (_q(key), STATE_PROPERTY))
        if st == 404 or not isinstance(d, dict):
            return {}
        value = d.get("value")
        return value if isinstance(value, dict) else {}

    def put_state(self, key: str, value: Dict[str, Any]) -> bool:
        st, _ = self._call("PUT", self._api("/issue/%s/properties/%s")
                           % (_q(key), STATE_PROPERTY), value, raw_ok=(400, 404))
        return st < 400

    # ───────────────────────────────────────────────────────── remote links ──
    def add_remote_link(self, key: str, url: str, title: str,
                        summary: str = "", relationship: str = "") -> bool:
        """Attach a PR, pipeline run or report to the ticket.

        `globalId` keyed on the URL makes this idempotent: re-running a stage
        updates the existing link instead of stacking duplicates on the ticket.
        """
        body = {
            "globalId": "ai-sdlc-foreman:%s" % url,
            "application": {"type": "capybara.foreman", "name": "AI-SDLC Foreman"},
            "object": {"url": url, "title": title[:250], "summary": summary[:500]},
        }
        if relationship:
            body["relationship"] = relationship
        st, _ = self._call("POST", self._api("/issue/%s/remotelink") % _q(key),
                           body, raw_ok=(400, 404))
        return st < 400

    # ───────────────────────────────────────────────────────── status / files ──
    def status_of(self, key: str) -> Optional[Dict[str, str]]:
        """{'name': 'Done', 'category': 'done'} or None if the issue is not visible."""
        issue = self.get_issue(key, fields=["status"])
        if not issue:
            return None
        status = ((issue.get("fields") or {}).get("status") or {})
        return {"name": status.get("name") or "",
                "category": ((status.get("statusCategory") or {}).get("key") or "")}

    def is_done(self, key: str) -> Optional[bool]:
        """True/False by Jira's status *category*, None if it cannot be read.

        The category, not the status name: every workflow calls its last column
        something different, but all of them put it in the `done` category.
        None is deliberately not False -- "I could not look" must not be
        mistaken for "somebody reopened it".
        """
        st = self.status_of(key)
        if st is None:
            return None
        return st["category"] == "done"

    def add_attachment(self, key: str, filename: str, content: bytes,
                       mime: str = "text/plain") -> bool:
        """Attach a file (the mapping diff a person applies by hand)."""
        if self.readonly:
            raise ReadOnlyViolation("POST", "attachments")
        boundary = "----foreman%x" % int(time.time() * 1000)
        safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "file.txt"
        body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                 "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                 % (boundary, safe, mime)).encode("utf-8")
                + content + ("\r\n--%s--\r\n" % boundary).encode("utf-8"))
        req = urllib.request.Request(
            self.base + self._api("/issue/%s/attachments") % _q(key),
            data=body, method="POST", headers={
                "Authorization": self._auth_header,
                "X-Atlassian-Token": "no-check",
                "Content-Type": "multipart/form-data; boundary=%s" % boundary,
                "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status < 400
        except urllib.error.HTTPError:
            return False
        except urllib.error.URLError:
            return False

    # ───────────────────────────────────────────────────────────── projects ──
    def get_project(self, key: str) -> Optional[Dict[str, Any]]:
        st, d = self._call("GET", self._api("/project/%s") % _q(key))
        return None if st == 404 else d

    def list_projects(self, limit: int = 50) -> List[Dict[str, Any]]:
        if self.deployment == DATACENTER:
            # DC has no /project/search; /project returns every visible
            # project as a bare list, unpaginated.
            st, d = self._call("GET", self._api("/project"))
            return (d if isinstance(d, list) else [])[:limit]
        st, d = self._call("GET", self._api("/project/search"),
                           params={"maxResults": limit})
        return (d or {}).get("values", []) if st < 400 else []

    def create_project(self, key: str, name: str, lead_account_id: str,
                       description: str = "",
                       template: str = "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
                       project_type: str = "software") -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "key": key, "name": name,
            "projectTypeKey": project_type, "projectTemplateKey": template,
            "description": description, "assigneeType": "PROJECT_LEAD",
        }
        # DC identifies the lead by username; Cloud by account id. Sending the
        # wrong one yields a 400 naming a field the caller never set.
        if self.deployment == DATACENTER:
            payload["lead"] = lead_account_id
        else:
            payload["leadAccountId"] = lead_account_id
        st, d = self._call("POST", self._api("/project"), payload,
                           raw_ok=(400, 401, 403))
        if st >= 400:
            raise JiraError(st, _error_text(json.dumps(d)), "create_project")
        return d

    def project_statuses(self, key: str) -> List[Dict[str, Any]]:
        st, d = self._call("GET", self._api("/project/%s/statuses") % _q(key))
        return d if isinstance(d, list) and st < 400 else []

    def create_version(self, project_id: str, name: str,
                       description: str = "") -> Optional[Dict[str, Any]]:
        st, d = self._call("POST", self._api("/version"),
                           {"projectId": int(project_id), "name": name,
                            "description": description, "released": False},
                           raw_ok=(400, 404))
        return d if st < 400 else None

    def project_versions(self, key: str) -> List[Dict[str, Any]]:
        st, d = self._call("GET", self._api("/project/%s/versions") % _q(key))
        return d if isinstance(d, list) else []

    def delete_issue(self, key: str) -> bool:
        """Only ever used by `foreman provision --reset` against a demo board."""
        st, _ = self._call("DELETE", self._api("/issue/%s") % _q(key),
                           raw_ok=(400, 404))
        return st < 400


# ────────────────────────────────────────────────────────────────── helpers ──
def _q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return {"message": text}


def _retry_after(err) -> Optional[int]:
    try:
        raw = err.headers.get("Retry-After")
        return int(float(raw)) if raw else None
    except Exception:
        return None


def _error_text(detail: str) -> str:
    """Turn Jira's error envelope into something readable in a log line."""
    try:
        d = json.loads(detail)
    except ValueError:
        return detail[:400]
    if isinstance(d, dict):
        msgs = list(d.get("errorMessages") or [])
        errs = d.get("errors") or {}
        if isinstance(errs, dict):
            msgs += ["%s: %s" % (k, v) for k, v in errs.items()]
        if msgs:
            return " | ".join(str(m) for m in msgs)[:400]
    return detail[:400]
