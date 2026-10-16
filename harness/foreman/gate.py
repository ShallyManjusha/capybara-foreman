"""
Human-in-the-loop gates: reading a decision off the ticket, safely.

This is the most security-sensitive module in Foreman, and the reason is worth
stating plainly. Downstream of an approval, an agent writes code and commits it.
Upstream of it, *anyone with a Jira licence can leave a comment*. If "approved"
means "someone typed the word approved", then the authorisation boundary for
changing your source code is the same as the authorisation boundary for adding
a thumbs-up to a ticket.

Four independent checks, all of which must pass:

1. **Shape.** The command is the first non-space text of the comment. A comment
   that merely mentions the phrase — quoting an earlier message, or writing
   "I don't think we should /foreman approve this yet" — is not a decision.
2. **Identity.** The author's account id is on the allowlist. Email matching is
   supported for convenience but is strictly weaker: display names and emails
   can collide across directories, account ids cannot.
3. **Recency.** The comment post-dates the moment the gate opened. Without
   this, a stale `/foreman approve` from revision one silently approves
   revision three — which is the exact failure where a reviewer believes they
   examined the change they authorised, and did not.
4. **Not us.** Foreman ignores comments it wrote itself, identified by the
   marker every rendered comment carries. Otherwise a command quoted back
   inside a rendered plan — and the plan *does* quote the commands, because it
   has to tell the reviewer what to type — is a self-approval channel.

   Note what this check is **not**: "the author is the account Foreman
   authenticates as". That version is both too strict and too weak. Too strict
   because a small team may run Foreman under an account that a human also
   uses, and every approval would be silently discarded. Too weak because it
   says nothing about a Foreman comment posted under some other identity.
   Matching on the marker asks the question we actually mean: *did Foreman
   write this comment?*

   Running Foreman under its own service account is still the right
   deployment, for a reason this check cannot help with: when the bot and the
   approver share an account, the ticket's history cannot tell you who
   approved. `foreman doctor` warns about it.

Anything that fails 2, 3 or 4 while passing 1 is recorded as a *rejected
attempt* and reported on the ticket. Silently ignoring an unauthorised approval
attempt hides exactly the event you most want to see.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Reads a comment body of either format: an ADF document tree from Cloud, or
# a wiki-markup string from Data Center.
from .markup import to_text as body_text

APPROVE = "approve"
CHANGES = "changes"
REJECT = "reject"


class Decision:
    def __init__(self, verdict, author, author_id, created, feedback="",
                 comment_id=""):
        self.verdict = verdict          # approve | changes | reject
        self.author = author
        self.author_id = author_id
        self.created = created          # datetime, UTC
        self.feedback = feedback
        self.comment_id = comment_id

    def as_dict(self):
        return {"verdict": self.verdict, "author": self.author,
                "author_id": self.author_id,
                "created": self.created.isoformat() if self.created else None,
                "feedback": self.feedback, "comment_id": self.comment_id}

    def __repr__(self):
        return "<Decision %s by %s>" % (self.verdict, self.author)


class Attempt:
    """A comment that looked like a command but was not honoured."""
    def __init__(self, verdict, author, author_id, created, reason, comment_id=""):
        self.verdict = verdict
        self.author = author
        self.author_id = author_id
        self.created = created
        self.reason = reason            # not_authorised | stale | self
        self.comment_id = comment_id

    def as_dict(self):
        return {"verdict": self.verdict, "author": self.author,
                "author_id": self.author_id, "reason": self.reason,
                "created": self.created.isoformat() if self.created else None,
                "comment_id": self.comment_id}


class GateConfig:
    def __init__(self, approvers: List[str], cmd_approve: str, cmd_reject: str,
                 cmd_changes: str, bot_account_id: str = "",
                 timeout_hours: int = 72, self_marker: str = "\u27e6ai-sdlc-foreman/"):
        # Normalise once: account ids are case-sensitive, emails are not, and
        # mixing the two comparisons is how an allowlist quietly stops matching.
        self.approver_ids = {a.strip() for a in approvers if a.strip()
                             and "@" not in a}
        self.approver_emails = {a.strip().lower() for a in approvers
                                if "@" in a}
        self.cmd_approve = (cmd_approve or "/foreman approve").strip().lower()
        self.cmd_reject = (cmd_reject or "/foreman reject").strip().lower()
        self.cmd_changes = (cmd_changes or "/foreman changes").strip().lower()
        self.bot_account_id = (bot_account_id or "").strip()
        # The substring every comment Foreman renders carries. See render.MARK.
        self.self_marker = self_marker
        self.timeout_hours = max(1, int(timeout_hours or 72))

    def is_own_comment(self, text: str) -> bool:
        return bool(self.self_marker) and self.self_marker in (text or "")

    @property
    def bot_is_an_approver(self) -> bool:
        """True when Foreman authenticates as an account that can also approve.

        Not fatal, and not blocked here — but it means the ticket history
        cannot answer "who approved this?", which is most of the point of
        having a gate.
        """
        return bool(self.bot_account_id) and \
            self.bot_account_id in self.approver_ids

    def authorises(self, account_id: str, email: str) -> bool:
        if account_id and account_id in self.approver_ids:
            return True
        if email and email.strip().lower() in self.approver_emails:
            return True
        return False

    @property
    def configured(self) -> bool:
        return bool(self.approver_ids or self.approver_emails)


class GateResult:
    def __init__(self, decision: Optional[Decision], attempts: List[Attempt],
                 waited_hours: float, timed_out: bool):
        self.decision = decision
        self.attempts = attempts
        self.waited_hours = waited_hours
        self.timed_out = timed_out

    def as_dict(self):
        return {
            "decision": self.decision.as_dict() if self.decision else None,
            "rejected_attempts": [a.as_dict() for a in self.attempts],
            "waited_hours": round(self.waited_hours, 2),
            "timed_out": self.timed_out,
        }


def evaluate(comments: List[Dict[str, Any]], cfg: GateConfig,
             opened_at: Optional[datetime],
             now: Optional[datetime] = None) -> GateResult:
    """Read the ticket's comments and return the gate's outcome.

    `opened_at` is when Foreman posted the thing being approved. Comments at or
    before that instant are, by construction, not decisions about it.
    """
    now = now or datetime.now(timezone.utc)
    opened_at = opened_at or datetime.fromtimestamp(0, timezone.utc)

    decisions: List[Decision] = []
    attempts: List[Attempt] = []

    for c in comments:
        text = body_text(c.get("body")).strip()
        parsed = parse_command(text, cfg)
        if parsed is None:
            continue
        verdict, feedback = parsed

        author = c.get("author") or {}
        account_id = str(author.get("accountId") or "")
        email = str(author.get("emailAddress") or "")
        name = str(author.get("displayName") or account_id or "unknown")
        created = parse_ts(c.get("created"))
        cid = str(c.get("id") or "")

        if cfg.is_own_comment(text):
            attempts.append(Attempt(verdict, name, account_id, created,
                                    "self", cid))
            continue
        if not cfg.authorises(account_id, email):
            attempts.append(Attempt(verdict, name, account_id, created,
                                    "not_authorised", cid))
            continue
        if created is None or created <= opened_at:
            attempts.append(Attempt(verdict, name, account_id, created,
                                    "stale", cid))
            continue

        decisions.append(Decision(verdict, name, account_id, created,
                                  feedback, cid))

    # The most recent valid decision wins: a reviewer who approves and then
    # thinks better of it must be able to say so on the same ticket.
    decisions.sort(key=lambda d: d.created)
    decision = decisions[-1] if decisions else None

    waited = (now - opened_at).total_seconds() / 3600.0
    timed_out = decision is None and waited > cfg.timeout_hours
    return GateResult(decision, attempts, waited, timed_out)


def parse_command(text: str, cfg: GateConfig):
    """Return (verdict, feedback) if the comment *begins* with a command.

    Requiring the command to open the comment is what stops a quoted or
    discussed command from counting. Everything after it on the same line, plus
    any following lines, is feedback for the model.
    """
    if not text:
        return None
    stripped = text.lstrip()
    low = stripped.lower()
    for cmd, verdict in ((cfg.cmd_changes, CHANGES),
                         (cfg.cmd_reject, REJECT),
                         (cfg.cmd_approve, APPROVE)):
        if not cmd:
            continue
        if low.startswith(cmd):
            rest = stripped[len(cmd):]
            # Guard against `/foreman approved-by-someone-else` matching
            # `/foreman approve`.
            if rest[:1] and not rest[:1].isspace() and rest[:1] not in ":,.-—":
                continue
            return verdict, rest.strip(" :,.-—\n\t")
    return None


# ───────────────────────────────────────────────────────────── timestamps ────
_TS = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,6})\d*)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse Jira's timestamps into aware UTC datetimes.

    Jira emits `+0200` without a colon, which `datetime.fromisoformat` rejects
    before Python 3.11. Foreman supports 3.9, so this is parsed by hand rather
    than by trusting the runtime to be new enough.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    m = _TS.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    micro = int((frac or "0").ljust(6, "0"))
    if tz in (None, "Z", "z"):
        offset = timezone.utc
    else:
        tz = tz.replace(":", "")
        sign = 1 if tz[0] == "+" else -1
        offset = timezone(sign * timedelta(hours=int(tz[1:3]),
                                           minutes=int(tz[3:5])))
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s),
                    micro, offset).astimezone(timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
