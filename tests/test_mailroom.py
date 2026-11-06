import base64, json
import pytest

from mailroom import auth
from mailroom.auth import AuthError, Credentials, load_credentials
from mailroom.gmail import Forbidden, Gmail, is_allowed, parse_message, html_to_text
from mailroom.pdf import pdf_text


def b64(s): return base64.urlsafe_b64encode(s.encode() if isinstance(s, str) else s).decode().rstrip("=")


# ---------- safety: no send, no delete ----------
@pytest.mark.parametrize("method,path", [
    ("POST", "/messages/send"), ("POST", "/drafts/send"), ("DELETE", "/messages/abc"),
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


def test_gmail_refuses_send_even_if_code_tries():
    g = Gmail(creds(), "me@x.com", FakeSession())
    with pytest.raises(Forbidden):
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
