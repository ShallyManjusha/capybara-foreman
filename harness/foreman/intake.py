"""
Intake: normalise business requirements from external sources into Jira issues.

Three source adapters, one output shape.  Every adapter produces a Jira issue
with the `foreman` label in the configured project.  Once that issue exists the
existing intake JQL picks it up on the next Foreman run — no changes to the
state machine are needed.

Sources
-------
  jira-br    Read from a Jira Business Requirement board (different project).
             Useful when requirements already live in Jira but in a separate
             project that Foreman does not watch.

  servicenow Read incidents from a ServiceNow instance.  Only incidents whose
             state is Open/In-Progress and that carry the configured category
             label are considered.

  email      Read an IMAP mailbox.  Each unread message becomes one Jira issue.
             Processed messages are marked as read so they are not re-created.

All three adapters sanitise the incoming text before writing to Jira: control
characters, over-long fields and the injection-fence markers Foreman itself uses
are stripped.  Ticket content written by external parties is untrusted by
definition, and the Jira issue is the first place it must be contained.

Usage
-----
  python3 -m foreman ingest --source jira-br   [--dry-run]
  python3 -m foreman ingest --source servicenow [--dry-run]
  python3 -m foreman ingest --source email      [--dry-run]

Credentials come from .env (never from this file).
"""
import email as email_lib
import email.header          # noqa: F401  (email.header is not loaded by `import email`)
import email.utils           # noqa: F401
import hashlib
import imaplib
import json
import os
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# ── Jira imports are deferred to avoid circular import at module load time ──
# from .jira import JiraClient, JiraError   # used inside each function

# Characters that must not reach the model-facing pipeline from untrusted input.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Foreman's own injection-fence markers.  If external text contains them it
# would blur the boundary the prompts layer relies on.
_FENCE_RE = re.compile(r"<<<UNTRUSTED-TICKET-DATA|UNTRUSTED-TICKET-DATA>>>")

MAX_SUMMARY = 255
MAX_DESCRIPTION = 32000


def _sanitise(text: str, maxlen: int) -> str:
    """Remove control characters and fence markers; truncate to maxlen."""
    s = _CONTROL.sub(" ", str(text or ""))
    s = _FENCE_RE.sub("[REMOVED]", s)
    return s.strip()[:maxlen]


REQUIREMENT_LABELS = ["foreman", "foreman-requirement"]


def _find_existing(jira, project: str, source_tag: str) -> str:
    """Key of an issue already imported from this source item, or ''.

    The source item's id is stored as a label (`src-<hash>`), which Jira can
    search exactly. Marking the *source* as done is not enough on its own: if
    that second call fails the next run would import the same requirement again
    and the board would fill with duplicates of a ticket nobody has read yet.
    """
    try:
        hits = jira.search('project = "%s" AND labels = "%s"' % (project, source_tag),
                           fields=["summary"], limit=1)
    except Exception:
        return ""
    return (hits[0].get("key") if hits else "") or ""


def _src_label(*parts: str) -> str:
    return "src-" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _decode_header(raw: str) -> str:
    out = []
    for text, enc in email.header.decode_header(raw or ""):
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", "replace"))
            except LookupError:
                out.append(text.decode("utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out)


def _make_description(body: str, metadata: Dict[str, Any]) -> str:
    """Wrap the source body with a provenance header for the human reviewer."""
    meta_lines = "\n".join("| %s | %s |" % (k, v)
                           for k, v in metadata.items() if v)
    return textwrap.dedent("""\
        ## Source details
        | Field | Value |
        |---|---|
        %(meta)s

        ---

        %(body)s
    """) % {"meta": meta_lines, "body": body}


# ─────────────────────────────────────────── Jira Business-Requirement board ──
def ingest_jira_br(jira_target, dry: bool = False) -> List[str]:
    """Read tickets from a separate Jira project and mirror them as intake issues.

    The source project is typically a business-requirements or incident board
    that a product team maintains.  Foreman watches the *target* project; this
    adapter bridges them.

    Environment variables
    ---------------------
    INTAKE_JIRA_SOURCE_JQL   JQL for issues to import (required).
                             Example: project = BR AND issuetype = Story
                                      AND labels = ai-sdlc AND statusCategory != Done
    INTAKE_JIRA_DONE_LABEL   Label added to source issues after mirroring, to
                             prevent re-import.  Default: foreman-imported
    """
    from .jira import JiraClient, JiraError  # local import

    source_jql = os.environ.get("INTAKE_JIRA_SOURCE_JQL", "").strip()
    if not source_jql:
        print("   INTAKE_JIRA_SOURCE_JQL not set — nothing to import")
        return []

    done_label = os.environ.get("INTAKE_JIRA_DONE_LABEL", "foreman-imported").strip()
    project = os.environ.get("JIRA_PROJECT", "FORE").strip()
    created = []

    # Reuse the same credentials — both boards are on the same Jira instance.
    issues = jira_target.search(source_jql, limit=20)
    print("   jira-br: %d source issue(s) found" % len(issues))

    for issue in issues:
        f = issue.get("fields") or {}
        summary = _sanitise((f.get("summary") or ""), MAX_SUMMARY)
        if not summary:
            continue

        from .markup import to_text as body_text
        raw_desc = body_text(f.get("description"))
        metadata = {
            "Source key": issue.get("key", ""),
            "Source project": ((f.get("project") or {}).get("key") or ""),
            "Issue type": ((f.get("issuetype") or {}).get("name") or ""),
            "Reporter": ((f.get("reporter") or {}).get("displayName") or ""),
            "Priority": ((f.get("priority") or {}).get("name") or ""),
        }
        description = _make_description(
            _sanitise(raw_desc, MAX_DESCRIPTION), metadata)

        print("   [%s] %s %s" % ("dry" if dry else "creating", issue.get("key"), summary[:60]))
        if dry:
            continue
        tag = _src_label("jira", issue.get("key", ""))
        dup = _find_existing(jira_target, project, tag)
        if dup:
            print("          already imported as %s; marking the source" % dup)
            jira_target.set_labels(issue["key"], add=[done_label])
            continue
        try:
            new = jira_target.create_issue(
                project, summary, description,
                issue_type=os.environ.get("FOREMAN_INTAKE_ISSUETYPE", "Task"),
                labels=REQUIREMENT_LABELS + [tag])
            key = new.get("key", "")
            created.append(key)
            print("          → %s" % key)
            # Mark the source so we don't import it twice.
            jira_target.set_labels(issue["key"], add=[done_label])
        except JiraError as e:
            print("          ✗ %s" % e.message[:120])

    return created


# ─────────────────────────────────────────────────────────── ServiceNow ──────
def ingest_servicenow(jira_target, dry: bool = False) -> List[str]:
    """Read incidents from ServiceNow and create Jira intake issues.

    Environment variables
    ---------------------
    SNOW_INSTANCE        ServiceNow host, e.g. dev12345.service-now.com (required)
    SNOW_USER            API username (required)
    SNOW_PASSWORD        API password (required)
    SNOW_INTAKE_QUERY    Encoded query string, e.g.
                         state=1^category=integration^assignment_group=ai-sdlc
                         Default: state=1  (Open incidents)
    SNOW_DONE_STATE      Optional. State value to set on imported incidents
                         (e.g. 2 = In Progress). Default: unchanged. Never use
                         6 (Resolved) here -- importing is not fixing.
    SNOW_MAX_RECORDS     Max incidents per run. Default: 10
    """
    instance = os.environ.get("SNOW_INSTANCE", "").strip().rstrip("/")
    user = os.environ.get("SNOW_USER", "").strip()
    password = os.environ.get("SNOW_PASSWORD", "").strip()
    if not (instance and user and password):
        print("   SNOW_INSTANCE / SNOW_USER / SNOW_PASSWORD not set — skipping")
        return []

    query = os.environ.get("SNOW_INTAKE_QUERY", "state=1").strip()
    max_records = int(os.environ.get("SNOW_MAX_RECORDS", "10") or 10)
    done_state = os.environ.get("SNOW_DONE_STATE", "").strip()
    project = os.environ.get("JIRA_PROJECT", "FORE").strip()

    fields = ("number,short_description,description,priority,category,"
              "assignment_group,caller_id,sys_id")
    url = "https://%s/api/now/table/incident?%s" % (
        instance,
        urllib.parse.urlencode({
            "sysparm_query": query,
            "sysparm_limit": str(max_records),
            "sysparm_fields": fields,
        }))

    import base64
    auth = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": "Basic %s" % auth,
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("   ServiceNow HTTP %s: %s" % (e.code, e.read().decode()[:200]))
        return []
    except Exception as e:
        print("   ServiceNow error: %s" % str(e)[:200])
        return []

    records = data.get("result") or []
    print("   servicenow: %d incident(s) found" % len(records))
    created = []

    from .jira import JiraClient, JiraError  # local import

    for rec in records:
        summary = _sanitise(rec.get("short_description", ""), MAX_SUMMARY)
        if not summary:
            continue
        summary = "[SNOW %s] %s" % (rec.get("number", "?"), summary)

        metadata = {
            "ServiceNow number": rec.get("number", ""),
            "Priority": rec.get("priority", ""),
            "Category": rec.get("category", ""),
            "Assignment group": (rec.get("assignment_group") or {}).get("display_value", ""),
            "Caller": (rec.get("caller_id") or {}).get("display_value", ""),
        }
        body = _sanitise(rec.get("description") or rec.get("short_description", ""),
                         MAX_DESCRIPTION)
        description = _make_description(body, metadata)

        print("   [%s] %s %s" % ("dry" if dry else "creating",
                                  rec.get("number"), summary[:55]))
        if dry:
            continue
        tag = _src_label("snow", rec.get("number", ""), rec.get("sys_id", ""))
        dup = _find_existing(jira_target, project, tag)
        if dup:
            print("          already imported as %s" % dup)
            if done_state:
                _snow_update(instance, auth, rec.get("sys_id", ""), done_state,
                             "Imported into Jira as %s" % dup)
            continue
        try:
            new = jira_target.create_issue(
                project, summary, description,
                issue_type=os.environ.get("FOREMAN_INTAKE_ISSUETYPE", "Task"),
                labels=REQUIREMENT_LABELS + ["snow-import", tag])
            key = new.get("key", "")
            created.append(key)
            print("          → %s" % key)
            # De-duplication is handled by the `src-` label above, so ServiceNow
            # is only touched if the client asks: SNOW_DONE_STATE is empty by
            # default, because moving the incident to Resolved on import tells
            # the business it is fixed when Foreman has only just read it.
            if done_state:
                _snow_update(instance, auth, rec.get("sys_id", ""), done_state,
                             "Imported into Jira as %s" % key)
        except JiraError as e:
            print("          ✗ %s" % e.message[:120])

    return created


def _snow_update(instance: str, auth: str, sys_id: str, state: str,
                 work_note: str = "") -> None:
    """Optionally move the incident on and leave a work note. Best-effort; never raises."""
    if not sys_id:
        return
    url = "https://%s/api/now/table/incident/%s" % (instance, sys_id)
    body = {"state": state}
    if work_note:
        body["work_notes"] = work_note
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="PATCH", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": auth,
    })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────── Email/IMAP ──
def ingest_email(jira_target, dry: bool = False) -> List[str]:
    """Read unread messages from an IMAP mailbox and create Jira intake issues.

    Each unread message becomes one Jira issue.  After successful creation the
    message is marked as read (\\Seen flag) so it is not re-imported.

    Environment variables
    ---------------------
    IMAP_HOST        IMAP server hostname (required)
    IMAP_PORT        Port. Default: 993 (SSL)
    IMAP_USER        Login (required)
    IMAP_PASSWORD    Password or app-password (required)
    IMAP_FOLDER      Folder to watch. Default: INBOX
    IMAP_MAX_MSGS    Max messages per run. Default: 10
    """
    host = os.environ.get("IMAP_HOST", "").strip()
    user = os.environ.get("IMAP_USER", "").strip()
    password = os.environ.get("IMAP_PASSWORD", "").strip()
    if not (host and user and password):
        print("   IMAP_HOST / IMAP_USER / IMAP_PASSWORD not set — skipping")
        return []

    port = int(os.environ.get("IMAP_PORT", "993") or 993)
    folder = os.environ.get("IMAP_FOLDER", "INBOX").strip()
    max_msgs = int(os.environ.get("IMAP_MAX_MSGS", "10") or 10)
    project = os.environ.get("JIRA_PROJECT", "FORE").strip()

    from .jira import JiraClient, JiraError  # local import

    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)
        conn.select(folder)
    except Exception as e:
        print("   IMAP connection failed: %s" % str(e)[:200])
        return []

    try:
        _, data = conn.search(None, "UNSEEN")
        msg_ids = (data[0] or b"").split()[-max_msgs:]
    except Exception as e:
        print("   IMAP search failed: %s" % str(e)[:200])
        conn.logout()
        return []

    print("   email: %d unread message(s) found" % len(msg_ids))
    created = []

    for mid in msg_ids:
        try:
            _, raw = conn.fetch(mid, "(RFC822)")
            msg = email_lib.message_from_bytes(raw[0][1])
        except Exception:
            continue

        subject = _sanitise(_decode_header(msg.get("Subject", "")), MAX_SUMMARY)
        sender = _sanitise(msg.get("From", ""), 200)
        date_str = _sanitise(msg.get("Date", ""), 100)

        body_parts = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body_parts.append(
                            part.get_payload(decode=True).decode(charset, "replace"))
                    except Exception:
                        pass
        else:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body_parts.append(
                    msg.get_payload(decode=True).decode(charset, "replace"))
            except Exception:
                pass

        body = _sanitise("\n\n".join(body_parts), MAX_DESCRIPTION)
        summary = ("[Email] %s" % subject) if subject else "[Email] (no subject)"

        metadata = {"From": sender, "Date": date_str, "Subject": subject}
        description = _make_description(body, metadata)

        print("   [%s] %s" % ("dry" if dry else "creating", summary[:70]))
        if dry:
            continue
        tag = _src_label("mail", msg.get("Message-ID", "") or "%s|%s|%s" % (sender, date_str, subject))
        dup = _find_existing(jira_target, project, tag)
        if dup:
            print("          already imported as %s" % dup)
            conn.store(mid, "+FLAGS", "\\Seen")
            continue
        try:
            new = jira_target.create_issue(
                project, summary, description,
                issue_type=os.environ.get("FOREMAN_INTAKE_ISSUETYPE", "Task"),
                labels=REQUIREMENT_LABELS + ["email-import", tag])
            key = new.get("key", "")
            created.append(key)
            print("          → %s" % key)
            # Mark message as read so it is not re-imported.
            conn.store(mid, "+FLAGS", "\\Seen")
        except JiraError as e:
            print("          ✗ %s" % e.message[:120])

    try:
        conn.logout()
    except Exception:
        pass

    return created


# ────────────────────────────────────────────────────────────── CLI command ──
def cmd_ingest(args):
    """Entry point for `foreman ingest`."""
    from .cli import jira_from_env
    jira = jira_from_env()

    source = (args.source or "").strip().lower()
    dry = getattr(args, "dry_run", False)

    print("\n── Intake: source=%s  dry=%s" % (source, dry))

    if source == "jira-br":
        keys = ingest_jira_br(jira, dry=dry)
    elif source == "servicenow":
        keys = ingest_servicenow(jira, dry=dry)
    elif source == "email":
        keys = ingest_email(jira, dry=dry)
    elif source == "all":
        keys = (ingest_jira_br(jira, dry=dry)
                + ingest_servicenow(jira, dry=dry)
                + ingest_email(jira, dry=dry))
    else:
        print("Unknown source %r. Use: jira-br | servicenow | email | all" % source)
        return 1

    if keys:
        print("\n── Created %d intake issue(s): %s" % (len(keys), ", ".join(keys)))
    else:
        print("\n── No new intake issues created%s" % (" (dry run)" if dry else ""))
    return 0


def add_parser(sub):
    p = sub.add_parser("ingest",
                       help="pull requirements from Jira BR board / ServiceNow / Email")
    p.add_argument("--source", required=True,
                   choices=["jira-br", "servicenow", "email", "all"],
                   help="requirement source to ingest from")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be created without writing anything")
    p.set_defaults(fn=cmd_ingest)
    return p
