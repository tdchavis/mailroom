"""mailroom: Bri's hands in your inboxes.

    mailroom accounts                         which inboxes are connected
    mailroom login <account>|all          sign in once per inbox (opens a browser)
    mailroom search "query" [-a work] [-n 20]
    mailroom read  <account> <message-or-thread-id> [--thread]
    mailroom pdf   <account> <message-id> [--save DIR]
    mailroom label <account> <thread-id>... --name "Follow up"
    mailroom archive <account> <thread-id>...
    mailroom draft <account> --to ADDR --subject S (--body TEXT | --body-file F) [--reply-to MSG-ID]
    mailroom export-env                       write the cloud key line to .secrets/cloud-env.txt

It never sends and never deletes. `search` uses normal Gmail search words
(from:, subject:, has:attachment, filename:pdf, after:2026/10/01, in:anywhere, ...).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .auth import AuthError, interactive_login, load_credentials, project_root, save_token, secrets_dir
from .gmail import Gmail, Message
from .pdf import pdf_text


def load_accounts() -> dict[str, str]:
    f = project_root() / "accounts.json"
    return {k: v.lower() for k, v in json.loads(f.read_text()).items()} if f.exists() else {}


def resolve(name: str) -> list[str]:
    accts = load_accounts()
    if name == "all":
        return list(accts.values())
    if name in accts:
        return [accts[name]]
    if "@" in name:
        return [name.lower()]
    sys.exit(f"Unknown account '{name}'. Use one of: {', '.join(accts)} or all")


def alias(email: str) -> str:
    for k, v in load_accounts().items():
        if v == email:
            return k
    return email


def fmt_row(acct: str, m: Message) -> str:
    return (f"[{alias(acct)}] {m.date[:31]}\n  from: {m.sender}\n  subject: {m.subject}\n"
            f"  message: {m.id}  thread: {m.thread_id}  labels: {','.join(m.labels)}\n"
            f"  {m.snippet[:200]}")


def fmt_full(m: Message) -> str:
    lines = [f"Date: {m.date}", f"From: {m.sender}", f"To: {m.to}"]
    if m.cc:
        lines.append(f"Cc: {m.cc}")
    lines += [f"Subject: {m.subject}", f"Message: {m.id}  Thread: {m.thread_id}"]
    if m.attachments:
        lines.append("Attachments: " + "; ".join(f"{a.filename} ({a.mime_type}, {a.size // 1024} KB)"
                                                for a in m.attachments))
    lines += ["", m.body or m.snippet]
    return "\n".join(lines)


def cmd_accounts(_):
    try:
        creds = load_credentials()
    except AuthError as e:
        print(e)
        creds = None
    for k, v in load_accounts().items():
        ok = creds is not None and v in creds.tokens
        print(f"{k:9} {v:28} {'connected' if ok else 'NOT connected'}")
    if creds:
        print(f"(keys from {'cloud setting ' + 'MAILROOM_CREDENTIALS' if creds.source == 'env' else '.secrets/ on this Mac'})")


def cmd_login(a):
    creds = load_credentials()
    for want in resolve(a.account):
        email, rt = interactive_login(creds, login_hint=want, open_browser=not a.no_browser)
        if email != want:
            print(f"Heads up: you signed in as {email}, not {want}. Saved it under {email}.")
        save_token(email, rt)
        creds.tokens[email] = rt
        print(f"Connected {email}.")


def cmd_search(a):
    creds = load_credentials()
    found = 0
    for acct in resolve(a.account):
        try:
            msgs = Gmail(creds, acct).search(a.query, a.max)
        except AuthError as e:
            print(f"[{alias(acct)}] skipped: {e}")
            continue
        for m in msgs:
            print(fmt_row(acct, m) + "\n")
        found += len(msgs)
    print(f"{found} message(s).")


def cmd_read(a):
    creds = load_credentials()
    g = Gmail(creds, resolve(a.account)[0])
    if a.thread:
        msgs = g.get_thread(a.id)
        for i, m in enumerate(msgs, 1):
            print(f"===== message {i} of {len(msgs)} =====")
            print(fmt_full(m) + "\n")
    else:
        print(fmt_full(g.get_message(a.id)))


def cmd_pdf(a):
    creds = load_credentials()
    g = Gmail(creds, resolve(a.account)[0])
    m = g.get_message(a.id)
    pdfs = [x for x in m.attachments if x.mime_type == "application/pdf" or x.filename.lower().endswith(".pdf")]
    if not pdfs:
        print(f"No PDFs on this message. Attachments: {[x.filename for x in m.attachments] or 'none'}")
        return
    for att in pdfs:
        data = g.get_attachment(m.id, att.attachment_id)
        if a.save:
            d = Path(a.save)
            d.mkdir(parents=True, exist_ok=True)
            (d / Path(att.filename).name).write_bytes(data)
        print(f"===== {att.filename} (from: {m.sender}, subject: {m.subject}) =====")
        print(pdf_text(data) + "\n")


def cmd_label(a):
    g = Gmail(load_credentials(), resolve(a.account)[0])
    for t in a.thread_ids:
        g.label(t, a.name)
        print(f"Labeled thread {t} '{a.name}'.")


def cmd_archive(a):
    g = Gmail(load_credentials(), resolve(a.account)[0])
    for t in a.thread_ids:
        g.archive(t)
        print(f"Archived thread {t} (still searchable; not deleted).")


def cmd_draft(a):
    g = Gmail(load_credentials(), resolve(a.account)[0])
    body = Path(a.body_file).read_text() if a.body_file else (a.body or "")
    reply = g.get_message(a.reply_to) if a.reply_to else None
    to = a.to or (reply.sender if reply else None)
    if not to:
        sys.exit("Need --to (or --reply-to).")
    did = g.create_draft(to, a.subject or (("Re: " + reply.subject) if reply else "(no subject)"), body, reply)
    print(f"Draft saved in {g.email} (id {did}). Not sent. The account owner reviews and sends it.")


def cmd_export_env(_):
    creds = load_credentials()
    if creds.source == "env":
        sys.exit("Already running from the cloud setting; nothing to export.")
    f = secrets_dir() / "cloud-env.txt"
    f.write_text(f"MAILROOM_CREDENTIALS='{creds.to_env_json()}'\n")
    os.chmod(f, 0o600)
    print(f"Wrote {f}\nOpen it, copy the single line, and paste it into the cloud environment's "
          "environment variables. The file stays in .secrets/, which is never uploaded to GitHub.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="mailroom", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("accounts").set_defaults(fn=cmd_accounts)
    s = sub.add_parser("login"); s.add_argument("account", default="all", nargs="?")
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser here")
    s.set_defaults(fn=cmd_login)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-a", "--account", default="all")
    s.add_argument("-n", "--max", type=int, default=20); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("read"); s.add_argument("account"); s.add_argument("id")
    s.add_argument("--thread", action="store_true"); s.set_defaults(fn=cmd_read)
    s = sub.add_parser("pdf"); s.add_argument("account"); s.add_argument("id")
    s.add_argument("--save"); s.set_defaults(fn=cmd_pdf)
    s = sub.add_parser("label"); s.add_argument("account"); s.add_argument("thread_ids", nargs="+")
    s.add_argument("--name", required=True); s.set_defaults(fn=cmd_label)
    s = sub.add_parser("archive"); s.add_argument("account"); s.add_argument("thread_ids", nargs="+")
    s.set_defaults(fn=cmd_archive)
    s = sub.add_parser("draft"); s.add_argument("account"); s.add_argument("--to"); s.add_argument("--subject")
    s.add_argument("--body"); s.add_argument("--body-file"); s.add_argument("--reply-to"); s.set_defaults(fn=cmd_draft)
    sub.add_parser("export-env").set_defaults(fn=cmd_export_env)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except AuthError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
