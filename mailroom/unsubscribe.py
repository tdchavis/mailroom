"""Unsubscribe from mailing lists using the sender's own official unsubscribe link.

How it works: bulk senders put a `List-Unsubscribe` header on their mail (it's what Gmail's
own "Unsubscribe" button uses). We collect those per sender, skip anyone on the keep list,
and then either POST the one-click form (RFC 8058) or open the https link once.

Rules that keep this safe:
- Only https links are used. `mailto:` unsubscribes would mean sending email, which mailroom
  never does, so those senders are reported as "needs a sent email" and left alone.
- Senders with no List-Unsubscribe header are never touched (that's bank alerts, people, etc.).
- Nothing is deleted. Old mail stays where it is.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from email.utils import parseaddr

import requests

from .gmail import Gmail, Message, sender_email

UA = "mailroom/0.1 (unsubscribe helper for the mailbox owner)"


@dataclass
class ListSender:
    email: str
    name: str
    count: int = 0
    latest_subject: str = ""
    https_links: list[str] = field(default_factory=list)
    mailto_links: list[str] = field(default_factory=list)
    one_click: bool = False
    thread_ids: list[str] = field(default_factory=list)

    @property
    def can_unsubscribe(self) -> bool:
        return bool(self.https_links)


def parse_list_unsubscribe(header: str) -> tuple[list[str], list[str]]:
    """'<https://a/x>, <mailto:u@b>' -> (['https://a/x'], ['u@b'])"""
    https, mailto = [], []
    for link in re.findall(r"<([^>]+)>", header or ""):
        link = link.strip()
        if link.lower().startswith("https://"):
            https.append(link)
        elif link.lower().startswith("mailto:"):
            mailto.append(link[7:].split("?")[0])
    return https, mailto


def is_kept(s: ListSender, keep: list[str]) -> bool:
    hay = f"{s.name} {s.email}".lower()
    return any(k.strip().lower() in hay for k in keep if k.strip())


def scan(g: Gmail, days: int = 90, max_messages: int = 500) -> list[ListSender]:
    """Find every sender in the last `days` days whose mail carries an unsubscribe link."""
    msgs = g.search(f"newer_than:{days}d -in:sent -in:spam -in:trash", max_results=max_messages)
    by: dict[str, ListSender] = {}
    for m in msgs:
        if not m.list_unsubscribe:
            continue
        em = sender_email(m)
        if not em:
            continue
        s = by.setdefault(em, ListSender(email=em, name=parseaddr(m.sender)[0] or em))
        s.count += 1
        if not s.latest_subject:
            s.latest_subject = m.subject
        https, mailto = parse_list_unsubscribe(m.list_unsubscribe)
        for u in https:
            if u not in s.https_links:
                s.https_links.append(u)
        for u in mailto:
            if u not in s.mailto_links:
                s.mailto_links.append(u)
        if "one-click" in m.list_unsubscribe_post.lower():
            s.one_click = True
        if m.thread_id not in s.thread_ids:
            s.thread_ids.append(m.thread_id)
    return sorted(by.values(), key=lambda s: -s.count)


def perform(s: ListSender, session: requests.Session | None = None, pause: float = 1.0) -> str:
    """Try the sender's unsubscribe link. Returns a short human-readable result."""
    if not s.https_links:
        return "skipped: only a mailto: unsubscribe (would require sending email)"
    sess = session or requests.Session()
    url = s.https_links[0]
    try:
        if s.one_click:
            r = sess.post(url, data="List-Unsubscribe=One-Click", timeout=20, allow_redirects=True,
                          headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
        else:
            r = sess.get(url, timeout=20, allow_redirects=True, headers={"User-Agent": UA})
        ok = r.status_code < 400
        return f"{'done' if ok else 'failed'}: HTTP {r.status_code} via {'one-click' if s.one_click else 'link'}"
    except requests.RequestException as e:
        return f"failed: {type(e).__name__}"
    finally:
        time.sleep(pause)
