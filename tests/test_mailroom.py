import base64, json
from email.message import EmailMessage
import pytest

from mailroom import auth
from mailroom.auth import AuthError, Credentials, load_credentials
from mailroom.gmail import Forbidden, Gmail, is_allowed, parse_message, html_to_text
from mailroom.pdf import pdf_text


def b64(s): return base64.urlsafe_b64encode(s.encode() if isinstance(s, str) else s).decode().rstrip("=")


# ---------- safety: no send, no delete ----------
@pytest.mark.parametrize("method,path", [
    ("DELETE", "/messages/abc"),
    ("POST", "/messages/abc/trash"), ("POST", "/threads/abc/trash"), ("DELETE", "/threads/abc"),
    ("POST", "/messages/batchDelete"), ("PUT", "/settings/autoForwarding"), ("POST", "/settings/filters"),
    ("POST", "/messages/import"), ("POST", "/messages"),
])
def test_dangerous_calls_refused(method, path):
    assert not is_allowed(method, path)


@pytest.mark.parametrize("method,path", [
    ("GET", "/messages"), ("GET", "/messages/18f0a"), ("GET", "/messages/18f0a/attachments/ANG-x_1"),
    ("POST", "/threads/18f0a/modify"), ("POST", "/drafts"), ("POST", "/labels"),
])
def test_needed_calls_allowed(method, path):
    assert is_allowed(method, path)


class FakeResp:
    def __init__(self, data, status=200): self._d, self.status_code, self.text = data, status, json.dumps(data)
    headers = {"content-type": "application/json"}
    def json(self): return self._d


class FakeSession:
    def __init__(self): self.calls = []
    def post(self, url, data=None, timeout=None):
        return FakeResp({"access_token": "AT", "expires_in": 3600})
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json)); return FakeResp({"id": "x"})


def creds():
    auth._cache.clear()
    return Credentials("cid", "secret", {"me@x.com": "RT"})


def test_gmail_refuses_send_to_anyone_else_even_if_code_tries():
    g = Gmail(creds(), "me@x.com", FakeSession())
    msg = EmailMessage(); msg["To"] = "friend@y.com"; msg["Subject"] = "hi"; msg.set_content("x")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    with pytest.raises(Forbidden):
        g._call("POST", "/messages/send", json={"raw": raw})
    with pytest.raises(Forbidden):          # unchecked draft id
        g._call("POST", "/drafts/send", json={"id": "D9"})
    with pytest.raises(Forbidden):          # no recipient at all
        g._call("POST", "/messages/send", json={})


def test_archive_only_removes_inbox_label():
    s = FakeSession(); g = Gmail(creds(), "me@x.com", s)
    g.archive("T1")
    method, url, body = s.calls[-1]
    assert method == "POST" and url.endswith("/threads/T1/modify")
    assert body == {"addLabelIds": [], "removeLabelIds": ["INBOX"]}


def test_draft_is_a_draft_not_a_send():
    s = FakeSession(); g = Gmail(creds(), "me@x.com", s)
    g.create_draft("me@x.com", "Morning brief", "hello")
    method, url, body = s.calls[-1]
    assert url.endswith("/drafts") and "raw" in body["message"]
    assert all("send" not in c[1] for c in s.calls)


# ---------- parsing ----------
def test_parse_multipart_with_pdf():
    d = {"id": "M1", "threadId": "T1", "snippet": "Please sign &amp; return", "labelIds": ["INBOX"],
         "payload": {"mimeType": "multipart/mixed", "headers": [
             {"name": "From", "value": "Lawyer <l@firm.com>"}, {"name": "Subject", "value": "Articles"},
             {"name": "Message-ID", "value": "<abc@firm.com>"}],
             "parts": [
                 {"mimeType": "multipart/alternative", "parts": [
                     {"mimeType": "text/plain", "body": {"data": b64("Please sign by Nov 20.")}},
                     {"mimeType": "text/html", "body": {"data": b64("<p>Please sign</p>")}}]},
                 {"mimeType": "application/pdf", "filename": "Articles.pdf",
                  "body": {"attachmentId": "ATT1", "size": 20480}}]}}
    m = parse_message(d)
    assert m.body == "Please sign by Nov 20."
    assert m.snippet == "Please sign & return"
    assert [a.filename for a in m.attachments] == ["Articles.pdf"]
    assert m.message_id_header == "<abc@firm.com>"


def test_html_only_body_becomes_text():
    d = {"id": "M2", "payload": {"mimeType": "text/html", "headers": [],
         "body": {"data": b64("<div>Hi<br>Sam</div><style>x{}</style><p>Meeting &amp; notes</p>")}}}
    assert parse_message(d).body == "Hi\nSam\nMeeting & notes"


# ---------- keys ----------
def test_cloud_env_keys(monkeypatch):
    monkeypatch.setenv("MAILROOM_CREDENTIALS", json.dumps(
        {"client_id": "c", "client_secret": "s", "tokens": {"Sam@Example.org": "r"}}))
    c = load_credentials()
    assert c.source == "env" and c.tokens == {"sam@example.org": "r"}


def test_revoked_key_gives_plain_message():
    auth._cache.clear()
    class S:
        def post(self, *a, **k): return FakeResp({"error": "invalid_grant"}, 400)
    with pytest.raises(AuthError, match="revoked or expired"):
        auth.access_token(Credentials("c", "s", {"me@x.com": "RT"}), "me@x.com", S())


# ---------- PDFs ----------
def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, x)
    return out


def test_pdf_text_extracted():
    t = pdf_text(make_pdf("Articles of Dissolution due Nov 20"))
    assert "Articles of Dissolution due Nov 20" in t and "page 1 of 1" in t


def test_bad_pdf_reported_not_crashed():
    assert pdf_text(b"not a pdf").startswith("[Could not open")


# ---------- unsubscribe ----------
from mailroom.gmail import Message
from mailroom.unsubscribe import ListSender, is_kept, parse_list_unsubscribe, perform, scan


def test_parse_list_unsubscribe_header():
    https, mailto = parse_list_unsubscribe("<mailto:unsub@news.example.com?subject=x>, <https://news.example.com/u/123>")
    assert https == ["https://news.example.com/u/123"] and mailto == ["unsub@news.example.com"]
    assert parse_list_unsubscribe("<http://insecure.example.com/u>") == ([], [])  # http (not https) is ignored


def test_keep_list_matches_name_or_address():
    s = ListSender(email="news@example-author.com", name="Example Author")
    assert is_kept(s, ["example author"]) and is_kept(s, ["Example-Author.com"]) and not is_kept(s, ["Other School"])


def test_scan_groups_by_sender_and_skips_mail_without_header():
    msgs = [
        Message(id="1", thread_id="t1", date="", sender="Deals <deals@shop.com>", to="", cc="", subject="50% off",
                snippet="", labels=[], list_unsubscribe="<https://shop.com/u/1>", list_unsubscribe_post="List-Unsubscribe=One-Click"),
        Message(id="2", thread_id="t2", date="", sender="Deals <deals@shop.com>", to="", cc="", subject="Sale",
                snippet="", labels=[], list_unsubscribe="<https://shop.com/u/1>"),
        Message(id="3", thread_id="t3", date="", sender="Bank <alerts@bank.com>", to="", cc="", subject="NSF", snippet="", labels=[]),
        Message(id="4", thread_id="t4", date="", sender="Old List <x@old.org>", to="", cc="", subject="Hi",
                snippet="", labels=[], list_unsubscribe="<mailto:leave@old.org>"),
    ]
    class G:
        def search(self, q, max_results): return msgs
    out = scan(G(), 30)
    assert [s.email for s in out] == ["deals@shop.com", "x@old.org"]
    assert out[0].count == 2 and out[0].one_click and out[0].can_unsubscribe
    assert not out[1].can_unsubscribe and out[1].mailto_links == ["leave@old.org"]


def test_perform_one_click_posts_and_mailto_only_is_skipped():
    calls = []
    class S:
        def post(self, url, data=None, **k): calls.append(("POST", url, data)); return FakeResp({}, 200)
        def get(self, url, **k): calls.append(("GET", url, None)); return FakeResp({}, 200)
    s = ListSender(email="d@shop.com", name="Deals", https_links=["https://shop.com/u/1"], one_click=True)
    assert perform(s, S(), pause=0).startswith("done")
    assert calls == [("POST", "https://shop.com/u/1", "List-Unsubscribe=One-Click")]
    plain = ListSender(email="p@x.com", name="P", https_links=["https://x.com/u"])
    assert perform(plain, S(), pause=0).startswith("done") and calls[-1][0] == "GET"
    only_mail = ListSender(email="m@x.com", name="M", mailto_links=["leave@x.com"])
    assert perform(only_mail, S(), pause=0).startswith("skipped") and len(calls) == 2


# ---------- send-to-self: the only sending allowed ----------
class DraftSession(FakeSession):
    def __init__(self, to): super().__init__(); self.to = to
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json))
        if "/drafts/D1" in url:
            return FakeResp({"message": {"payload": {"headers": [{"name": "To", "value": self.to}]}}})
        return FakeResp({"id": "sent1"})


def test_send_to_self_posts_to_send_with_only_me():
    s = FakeSession(); g = Gmail(creds(), "me@x.com", s)
    assert g.send_to_self("Bri · Fri", "hello") == "x"
    method, url, body = s.calls[-1]
    assert method == "POST" and url.endswith("/messages/send")
    raw = base64.urlsafe_b64decode(body["raw"] + "==").decode()
    assert "To: me@x.com" in raw and "Subject: Bri" in raw


def test_send_draft_refuses_other_recipients():
    g = Gmail(creds(), "me@x.com", DraftSession("Lawyer <law@firm.com>, me@x.com"))
    with pytest.raises(Forbidden):
        g.send_draft_to_self("D1")


def test_send_draft_to_self_only():
    s = DraftSession("Travis <me@x.com>"); g = Gmail(creds(), "me@x.com", s)
    assert g.send_draft_to_self("D1") == "sent1"
    assert s.calls[-1][0:2] == ("POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send")


def test_assert_only_me_blocks_everyone_else():
    g = Gmail(creds(), "me@x.com", FakeSession())
    with pytest.raises(Forbidden):
        g._assert_only_me(["me@x.com", "friend@y.com"])
    with pytest.raises(Forbidden):
        g._assert_only_me([])
