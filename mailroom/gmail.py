"""A small Gmail client that can only read, label, archive, and draft.

Safety: every request goes through `_call`, which checks it against ALLOWED below.
There is no delete, no trash, no forwarding or filter changes. The only sending allowed is
a note to the mailbox owner's own address (send_to_self / send_draft_to_self), and both
refuse any other recipient. If a future edit tries to widen that, the tests fail.
"""

from __future__ import annotations

import base64
import html
import re
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

import requests

from .auth import Credentials, access_token

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# (HTTP method, path pattern) pairs mailroom is allowed to use. Nothing else.
ALLOWED = [
    ("GET", r"/profile"),
    ("GET", r"/messages"),
    ("GET", r"/messages/[\w-]+"),
    ("GET", r"/messages/[\w-]+/attachments/[\w-]+"),
    ("GET", r"/threads"),
    ("GET", r"/threads/[\w-]+"),
    ("GET", r"/labels"),
    ("POST", r"/labels"),                      # create a label
    ("POST", r"/threads/[\w-]+/modify"),       # add/remove labels; archive = remove INBOX
    ("POST", r"/messages/[\w-]+/modify"),
    ("POST", r"/drafts"),                      # create a draft (never sent)
    ("GET", r"/drafts"),
    ("GET", r"/drafts/[\w-]+"),
    ("POST", r"/messages/send"),               # ONLY to the mailbox owner; enforced in send_to_self
    ("POST", r"/drafts/send"),                 # ONLY drafts addressed to the owner; enforced in send_draft_to_self
]


class Forbidden(RuntimeError):
    pass


def is_allowed(method: str, path: str) -> bool:
    return any(method == m and re.fullmatch(p, path) for m, p in ALLOWED)


@dataclass
class Attachment:
    filename: str
    mime_type: str
    attachment_id: str
    size: int


@dataclass
class Message:
    id: str
    thread_id: str
    date: str
    sender: str
    to: str
    cc: str
    subject: str
    snippet: str
    labels: list[str]
    body: str = ""
    message_id_header: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    list_unsubscribe: str = ""        # the sender's official unsubscribe links, if any
    list_unsubscribe_post: str = ""   # "List-Unsubscribe=One-Click" when one-click is supported


class Gmail:
    def __init__(self, creds: Credentials, email: str, session: requests.Session | None = None):
        self.creds = creds
        self.email = email.lower()
        self.session = session or requests.Session()
        self._drafts_verified_mine: set[str] = set()

    # ---- the one door to Google ----
    def _call(self, method: str, path: str, params=None, json=None):
        if not is_allowed(method, path):
            raise Forbidden(f"mailroom does not allow {method} {path}")
        if method == "POST" and path == "/messages/send":
            self._assert_only_me(recipients_of_raw((json or {}).get("raw", "")))
        if method == "POST" and path == "/drafts/send":
            if (json or {}).get("id") not in self._drafts_verified_mine:
                raise Forbidden("mailroom only sends drafts it has checked are addressed to the owner")
        for attempt in range(4):
            tok = access_token(self.creds, self.email, self.session)
            r = self.session.request(method, API + path, params=params, json=json,
                                     headers={"Authorization": f"Bearer {tok}"}, timeout=60)
            quota = r.status_code == 403 and "Quota exceeded" in r.text
            if (r.status_code in (429, 500, 502, 503) or quota) and attempt < 3:
                time.sleep(20 if quota else 2 ** attempt)   # Gmail's limit is per minute; wait it out
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Gmail error {r.status_code} on {path}: {r.text[:300]}")
            return r.json()
        raise RuntimeError("Gmail kept failing; try again later.")

    # ---- reading ----
    def search(self, query: str, max_results: int = 20) -> list[Message]:
        out: list[Message] = []
        page = None
        while len(out) < max_results:
            d = self._call("GET", "/messages", params={
                "q": query, "maxResults": min(100, max_results - len(out)), **({"pageToken": page} if page else {})})
            for m in d.get("messages", []):
                out.append(self.get_message(m["id"], body=False))
            page = d.get("nextPageToken")
            if not page:
                break
        return out

    def get_message(self, message_id: str, body: bool = True) -> Message:
        d = self._call("GET", f"/messages/{message_id}", params={"format": "full" if body else "metadata",
                       **({} if body else {"metadataHeaders": ["From", "To", "Cc", "Subject", "Date", "Message-ID",
                                                                "List-Unsubscribe", "List-Unsubscribe-Post"]})})
        return parse_message(d)

    def get_thread(self, thread_id: str) -> list[Message]:
        d = self._call("GET", f"/threads/{thread_id}", params={"format": "full"})
        return [parse_message(m) for m in d.get("messages", [])]

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        d = self._call("GET", f"/messages/{message_id}/attachments/{attachment_id}")
        return b64decode(d["data"])

    # ---- filing ----
    def labels(self) -> dict[str, str]:
        """name -> id"""
        return {l["name"]: l["id"] for l in self._call("GET", "/labels").get("labels", [])}

    def ensure_label(self, name: str) -> str:
        existing = self.labels()
        for n, i in existing.items():
            if n.lower() == name.lower():
                return i
        d = self._call("POST", "/labels", json={"name": name, "labelListVisibility": "labelShow",
                                                 "messageListVisibility": "show"})
        return d["id"]

    def modify_thread(self, thread_id: str, add: list[str] = (), remove: list[str] = ()):
        return self._call("POST", f"/threads/{thread_id}/modify",
                          json={"addLabelIds": list(add), "removeLabelIds": list(remove)})

    def archive(self, thread_id: str):
        return self.modify_thread(thread_id, remove=["INBOX"])

    def label(self, thread_id: str, name: str):
        return self.modify_thread(thread_id, add=[self.ensure_label(name)])

    # ---- drafts (never sent) ----
    def create_draft(self, to: str, subject: str, body: str, reply_to: Message | None = None) -> str:
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = self.email
        msg["Subject"] = subject
        if reply_to is not None and reply_to.message_id_header:
            msg["In-Reply-To"] = reply_to.message_id_header
            msg["References"] = reply_to.message_id_header
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = {"message": {"raw": raw}}
        if reply_to is not None:
            payload["message"]["threadId"] = reply_to.thread_id
        return self._call("POST", "/drafts", json=payload)["id"]

    # ---- the one kind of sending allowed: a note to yourself ----
    def _assert_only_me(self, addrs: list[str]):
        """Refuse unless every recipient is this mailbox's own address."""
        others = [a for a in addrs if a.lower() != self.email]
        if others or not addrs:
            raise Forbidden(f"mailroom only sends to the mailbox owner ({self.email}); refused: {others or 'no recipient'}")

    def send_to_self(self, subject: str, body: str) -> str:
        self._assert_only_me([self.email])
        msg = EmailMessage()
        msg["To"] = self.email
        msg["From"] = self.email
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self._call("POST", "/messages/send", json={"raw": raw})["id"]

    def list_drafts(self, max_results: int = 50) -> list[tuple[str, str]]:
        """[(draft_id, subject)] newest first."""
        d = self._call("GET", "/drafts", params={"maxResults": max_results})
        out = []
        for dr in d.get("drafts", []):
            m = self._call("GET", f"/drafts/{dr['id']}", params={"format": "metadata", "metadataHeaders": ["Subject", "To", "Cc", "Bcc"]})
            h = {x["name"].lower(): x["value"] for x in m.get("message", {}).get("payload", {}).get("headers", [])}
            out.append((dr["id"], h.get("subject", "(no subject)")))
        return out

    def send_draft_to_self(self, draft_id: str) -> str:
        """Send an existing draft, but only if it is addressed solely to this mailbox."""
        m = self._call("GET", f"/drafts/{draft_id}", params={"format": "metadata", "metadataHeaders": ["To", "Cc", "Bcc"]})
        headers = {x["name"].lower(): x["value"] for x in m.get("message", {}).get("payload", {}).get("headers", [])}
        recipients = [a for _, a in getaddresses([h for h in (headers.get("to"), headers.get("cc"), headers.get("bcc")) if h]) if a]
        self._assert_only_me(recipients)
        self._drafts_verified_mine.add(draft_id)
        return self._call("POST", "/drafts/send", json={"id": draft_id})["id"]


# ---------- parsing helpers (pure functions; tested) ----------

def b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _walk(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def parse_message(d: dict) -> Message:
    payload = d.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain, htmls, atts = [], [], []
    for p in _walk(payload):
        mt = p.get("mimeType", "")
        b = p.get("body", {}) or {}
        if p.get("filename"):
            if b.get("attachmentId"):
                atts.append(Attachment(p["filename"], mt, b["attachmentId"], int(b.get("size", 0))))
            continue
        if b.get("data"):
            text = b64decode(b["data"]).decode("utf-8", errors="replace")
            if mt == "text/plain":
                plain.append(text)
            elif mt == "text/html":
                htmls.append(text)
    body = "\n".join(plain).strip() or html_to_text("\n".join(htmls))
    return Message(
        id=d["id"], thread_id=d.get("threadId", ""), date=headers.get("date", ""),
        sender=headers.get("from", ""), to=headers.get("to", ""), cc=headers.get("cc", ""),
        subject=headers.get("subject", "(no subject)"), snippet=html.unescape(d.get("snippet", "")),
        labels=d.get("labelIds", []), body=body, message_id_header=headers.get("message-id", ""),
        attachments=atts,
        list_unsubscribe=headers.get("list-unsubscribe", ""),
        list_unsubscribe_post=headers.get("list-unsubscribe-post", ""),
    )


def recipients_of_raw(raw_b64: str) -> list[str]:
    """All To/Cc/Bcc addresses in a base64url MIME message."""
    from email import message_from_bytes
    try:
        msg = message_from_bytes(b64decode(raw_b64))
    except Exception:  # noqa: BLE001
        return []
    return [a for _, a in getaddresses([h for h in (msg.get("To"), msg.get("Cc"), msg.get("Bcc")) if h]) if a]


def sender_email(m: Message) -> str:
    return parseaddr(m.sender)[1].lower()
