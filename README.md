# mailroom

A small command-line tool that lets an AI assistant work through Gmail **without being able to send or delete anything**.

It can search, read messages and threads, pull the text out of PDF attachments, add labels, archive, save drafts, and email the mailbox owner (`send-self`, which refuses any other recipient).
Every request to Google passes one checkpoint (`ALLOWED` in `mailroom/gmail.py`) that lists only those actions;
send, delete, trash, forwarding, and filter changes are refused, and the tests check that.

```bash
pip install -e .
mailroom accounts
mailroom search "filename:pdf newer_than:7d" -a work
mailroom read work <message-id>
mailroom pdf work <message-id>
mailroom label work <thread-id> --name "Follow up"
mailroom archive work <thread-id>
mailroom draft work --reply-to <message-id> --body "..."
```

Accounts are named in an `accounts.json` next to where you run it (`{"work": "you@example.org"}`).
Keys come from your own Google Cloud OAuth client: `.secrets/client.json` plus `mailroom login <account>`,
or, in the cloud, the `MAILROOM_CREDENTIALS` environment variable (`mailroom export-env` writes it).
Never commit `.secrets/`.

`mailroom unsubscribe <account> [--days 90] [--keep "Name"]... [--go]` lists mailing-list senders (those with a List-Unsubscribe header) and, with `--go`, unsubscribes through their own https link or one-click endpoint. Senders whose only unsubscribe is by email are reported and left alone, since mailroom never sends.
