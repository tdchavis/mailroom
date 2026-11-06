"""mailroom: Bri's hands in your inboxes.

    mailroom accounts                         which inboxes are connected
    mailroom login <account>|all           sign in once per inbox (opens a browser)
    mailroom search "query" [-a church] [-n 20]
    mailroom read  <account> <message-or-thread-id> [--thread]
    mailroom pdf   <account> <message-id> [--save DIR]
    mailroom label <account> <thread-id>... --name "Church Closing"
    mailroom archive <account> <thread-id>...
    mailroom draft <account> --to ADDR --subject S (--body TEXT | --body-file F) [--reply-to MSG-ID]
    mailroom export-env                       write the cloud key line to .secrets/cloud-env.txt
    mailroom unsubscribe <account> [--days 90] [--keep "Name"]... [--go]
                                              list mailing-list senders; --go unsubscribes via their own links
    mailroom send-self <account> --subject S (--body TEXT | --body-file F)   email yourself (nobody else)
    mailroom send-self <account> --draft ID   send a draft that is addressed only to you
    mailroom receipts [<account>|all] [--days 30] [--store Walmart] [--find "$49.99"] [--backfill] [--email personal]
                                              one list of every receipt in every inbox, with links;
                                              --email sends it to you; --backfill labels older receipts
    mailroom archive-all <account> [--older-than 1m] [--query Q] [--go]
                                              bulk-archive the inbox (dry run unless --go)
    mailroom filter <account> (--from ADDR | --query Q) --label NAME [--keep-in-inbox] [--apply]
                                              new mail from ADDR gets the label and skips the inbox;
                                              --apply also files what's already there

It never deletes, and the only address it can send to is the mailbox's own. Filters can only label
and skip the inbox (never forward or trash). `search` uses normal Gmail search words
(from:, subject:, has:attachment, filename:pdf, after:2026/10/01, in:anywhere, ...).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .auth import AuthError, interactive_login, load_credentials, project_root, save_token, secrets_dir
from .gmail import RECEIPT_QUERY, STORES, Gmail, Message, amount_in, store_of
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
    print(f"Draft saved in {g.email} (id {did}). Not sent. Travis reviews and sends it himself.")


def cmd_export_env(_):
    creds = load_credentials()
    if creds.source == "env":
        sys.exit("Already running from the cloud setting; nothing to export.")
    f = secrets_dir() / "cloud-env.txt"
    f.write_text(f"MAILROOM_CREDENTIALS='{creds.to_env_json()}'\n")
    os.chmod(f, 0o600)
    print(f"Wrote {f}\nOpen it, copy the single line, and paste it into the cloud environment's "
          "environment variables. The file stays in .secrets/, which is never uploaded to GitHub.")


def cmd_unsubscribe(a):
    from .unsubscribe import is_kept, perform, scan
    g = Gmail(load_credentials(), resolve(a.account)[0])
    senders = scan(g, a.days)
    keep = a.keep or []
    todo, kept, mail_only = [], [], []
    for s in senders:
        if is_kept(s, keep):
            kept.append(s)
        elif s.can_unsubscribe:
            todo.append(s)
        else:
            mail_only.append(s)
    print(f"[{alias(g.email)}] {len(senders)} mailing-list sender(s) in the last {a.days} days.\n")
    if kept:
        print("KEEPING (on your keep list):")
        for s in kept:
            print(f"  {s.name} <{s.email}>  ({s.count})")
        print()
    print(f"{'UNSUBSCRIBING' if a.go else 'WOULD UNSUBSCRIBE'} ({len(todo)}):")
    log = secrets_dir() / "unsubscribe-log.txt"
    for s in todo:
        line = f"  {s.name} <{s.email}>  ({s.count}, e.g. \"{s.latest_subject[:60]}\")"
        if a.go:
            result = perform(s)
            line += f"  -> {result}"
            with open(log, "a") as f:
                f.write(f"{g.email}\t{s.email}\t{result}\n")
        print(line)
    if mail_only:
        print(f"\nCAN'T (their only unsubscribe is by sending an email; mailroom never sends) ({len(mail_only)}):")
        for s in mail_only:
            print(f"  {s.name} <{s.email}>  ({s.count})")
    if not a.go:
        print("\nDry run. Add --go to do it. Nothing was changed.")
    else:
        print(f"\nDone. Log: {log}. Mail already in the inbox is untouched.")


def cmd_send_self(a):
    g = Gmail(load_credentials(), resolve(a.account)[0])
    if a.list_drafts:
        for did, subj in g.list_drafts():
            print(f"{did}  {subj}")
        return
    if a.draft:
        mid = g.send_draft_to_self(a.draft)
        print(f"Sent draft {a.draft} to {g.email} (message {mid}).")
        return
    body = Path(a.body_file).read_text() if a.body_file else (a.body or "")
    if not a.subject or not body:
        sys.exit("Need --subject and --body/--body-file (or --draft ID, or --list-drafts).")
    mid = g.send_to_self(a.subject, body)
    print(f"Emailed {g.email}: \"{a.subject}\" (message {mid}).")


def cmd_receipts(a):
    creds = load_credentials()
    rows: dict[str, list] = {}
    for acct in resolve(a.account):
        g = Gmail(creds, acct)
        if a.backfill:
            n = g.batch_modify(g.list_ids(f"{RECEIPT_QUERY} -label:receipts"), add=[g.ensure_label("Receipts")])
            print(f"[{alias(acct)}] labeled {n} older receipt(s) 'Receipts'.")
            for store, domains in STORES.items():
                lid = g.ensure_label(f"Receipts/{store}")
                q = "label:receipts (" + " OR ".join(f"from:{d}" for d in domains) + f") -label:receipts-{store.lower().replace(' ', '-')}"
                k = g.batch_modify(g.list_ids(q), add=[lid])
                if k:
                    print(f"[{alias(acct)}]   {store}: {k} labeled 'Receipts/{store}'.")
        q = f"label:receipts newer_than:{a.days}d" + (f" {a.find}" if a.find else "")
        if a.store:
            q += f" label:receipts-{a.store.lower().replace(' ', '-')}"
        for m in g.search(q, a.max):
            rows.setdefault(store_of(m.sender) or "Other", []).append((acct, m))
    lines, total = [], 0
    for store in [*STORES, "Other"]:
        if store not in rows:
            continue
        lines.append(f"\n== {store}: {len(rows[store])} ==")
        for acct, m in rows[store]:
            amt = amount_in(m.subject + " " + m.snippet)
            link = f"https://mail.google.com/mail/u/?authuser={acct}#all/{m.thread_id}"
            lines.append(f"{m.date[5:16]:12} {amt:>9}  {m.sender.split('<')[0].strip()[:28]:28}  {m.subject[:56]}  [{alias(acct)}]\n    {link}")
        total += len(rows[store])
    text = "\n".join(lines) + f"\n\n{total} receipt(s) in the last {a.days} day(s). In Gmail: label:receipts (or label:receipts-walmart, ...)."
    if a.email:
        to = resolve(a.email)[0]
        mid = Gmail(creds, to).send_to_self(f"Receipts · last {a.days} days ({total})", text)
        print(f"Emailed the receipts index to {to} (message {mid}).")
    else:
        print(text)


def cmd_archive_all(a):
    g = Gmail(load_credentials(), resolve(a.account)[0])
    q = a.query or ("in:inbox" + (f" older_than:{a.older_than}" if a.older_than else ""))
    ids = g.list_ids(q)
    if not a.go:
        print(f"[{alias(g.email)}] {len(ids)} message(s) match \"{q}\". Dry run; add --go to archive them. "
              "(Archived mail stays in All Mail and stays searchable. Nothing is deleted.)")
        return
    n = g.batch_modify(ids, remove=["INBOX"])
    print(f"[{alias(g.email)}] Archived {n} message(s) matching \"{q}\". Still searchable; not deleted.")


def cmd_filter(a):
    g = Gmail(load_credentials(), resolve(a.account)[0])
    if not (a.from_addr or a.query):
        sys.exit("Need --from ADDR or --query \"gmail search\".")
    fid = g.create_filter(a.from_addr, a.label, skip_inbox=not a.keep_in_inbox, query=a.query)
    where = "label '" + a.label + "'" + ("" if a.keep_in_inbox else ", skipping the inbox")
    what = f"mail from {a.from_addr}" if a.from_addr else f"mail matching \"{a.query}\""
    print(f"[{alias(g.email)}] Filter {fid}: {what} -> {where}.")
    if a.apply:
        ids = g.list_ids(f"from:{a.from_addr}" if a.from_addr else a.query)
        n = g.batch_modify(ids, add=[g.ensure_label(a.label)], remove=[] if a.keep_in_inbox else ["INBOX"])
        print(f"  Filed {n} existing message(s) the same way.")


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
    s = sub.add_parser("unsubscribe"); s.add_argument("account"); s.add_argument("--days", type=int, default=90)
    s.add_argument("--keep", action="append", help="sender name or address to keep (repeatable)")
    s.add_argument("--go", action="store_true", help="actually unsubscribe; without it, just list")
    s.set_defaults(fn=cmd_unsubscribe)
    s = sub.add_parser("send-self", help="email the mailbox owner (and nobody else)")
    s.add_argument("account"); s.add_argument("--subject"); s.add_argument("--body"); s.add_argument("--body-file")
    s.add_argument("--draft", help="send an existing draft that is addressed only to yourself")
    s.add_argument("--list-drafts", action="store_true"); s.set_defaults(fn=cmd_send_self)
    s = sub.add_parser("receipts", help="list receipts (label:Receipts), with amounts")
    s.add_argument("account", nargs="?", default="all"); s.add_argument("--days", type=int, default=30)
    s.add_argument("--find", help="extra Gmail search words, e.g. \"$49.99\"")
    s.add_argument("--store", help="Walmart, Amazon, \"Old Navy\", Nike")
    s.add_argument("-n", "--max", type=int, default=100)
    s.add_argument("--backfill", action="store_true", help="label older receipts it can recognise")
    s.add_argument("--email", metavar="ACCOUNT", help="email the index to this inbox's owner instead of printing")
    s.set_defaults(fn=cmd_receipts)
    s = sub.add_parser("archive-all", help="bulk-archive the inbox (dry run unless --go)")
    s.add_argument("account"); s.add_argument("--older-than", help="e.g. 1m, 30d, 1y (Gmail syntax)")
    s.add_argument("--query", help="a full Gmail query instead of in:inbox")
    s.add_argument("--go", action="store_true"); s.set_defaults(fn=cmd_archive_all)
    s = sub.add_parser("filter", help="label (and skip inbox for) mail from a sender")
    s.add_argument("account"); s.add_argument("--from", dest="from_addr")
    s.add_argument("--query", help="any Gmail search instead of a sender, e.g. '(cleaning OR janitorial) (quote OR bid)'")
    s.add_argument("--label", required=True); s.add_argument("--keep-in-inbox", action="store_true")
    s.add_argument("--apply", action="store_true", help="also file mail already in the mailbox")
    s.set_defaults(fn=cmd_filter)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except AuthError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
