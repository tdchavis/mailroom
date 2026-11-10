"""Keys for the inboxes.

Where keys come from, in order:
1. The MAILROOM_CREDENTIALS environment variable (used in the cloud).
   JSON: {"client_id": ..., "client_secret": ..., "tokens": {"email": "refresh_token", ...}}
2. Files on this Mac: .secrets/client.json (from Google) and .secrets/tokens.json (from `mailroom login`).

A "refresh token" is the long-lived key Google gives after you click Allow.
Mailroom trades it for a short-lived access key (about 1 hour) whenever it needs one.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import requests

# gmail.modify = read/label/archive/draft; gmail.settings.basic = create label+skip-inbox filters.
# (Filters need a fresh `mailroom login <acct>` for inboxes signed in before this was added.)
# documents.readonly = read Google Docs (Bri's rulebook lives in one). Read-only; no Drive browsing.
SCOPE = ("https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.settings.basic "
         "https://www.googleapis.com/auth/documents.readonly")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ENV_VAR = "MAILROOM_CREDENTIALS"


class AuthError(RuntimeError):
    pass


def project_root() -> Path:
    env = os.environ.get("MAILROOM_HOME")
    if env:
        return Path(env)
    here = Path.cwd()
    for p in [here, *here.parents]:
        if (p / "accounts.json").exists():
            return p
    return Path(__file__).resolve().parent.parent


def secrets_dir() -> Path:
    return project_root() / ".secrets"


@dataclass
class Credentials:
    client_id: str
    client_secret: str
    tokens: dict[str, str] = field(default_factory=dict)  # email (lowercase) -> refresh token
    source: str = "files"

    def to_env_json(self) -> str:
        return json.dumps(
            {"client_id": self.client_id, "client_secret": self.client_secret, "tokens": self.tokens},
            separators=(",", ":"),
        )


def load_credentials() -> Credentials:
    raw = os.environ.get(ENV_VAR)
    if raw:
        raw = raw.strip().strip("'").strip('"') if raw.strip()[:1] in "'\"" else raw.strip()
        try:
            d = json.loads(raw)
            return Credentials(d["client_id"], d["client_secret"],
                               {k.lower(): v for k, v in d.get("tokens", {}).items()}, source="env")
        except (ValueError, KeyError) as e:
            raise AuthError(f"{ENV_VAR} is set but isn't valid ({e}).") from e

    client_file = secrets_dir() / "client.json"
    if not client_file.exists():
        raise AuthError(
            "No Google client keys found. Put the JSON you downloaded from Google Cloud at "
            f"{client_file} (see docs/GOOGLE_SETUP.md)."
        )
    c = json.loads(client_file.read_text())
    c = c.get("installed") or c.get("web") or c
    tokens_file = secrets_dir() / "tokens.json"
    tokens = json.loads(tokens_file.read_text()) if tokens_file.exists() else {}
    return Credentials(c["client_id"], c["client_secret"], {k.lower(): v for k, v in tokens.items()})


def save_token(email: str, refresh_token: str) -> Path:
    d = secrets_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / "tokens.json"
    tokens = json.loads(f.read_text()) if f.exists() else {}
    tokens[email.lower()] = refresh_token
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(f)
    return f


# ---------- access tokens ----------

_cache: dict[str, tuple[str, float]] = {}


def access_token(creds: Credentials, email: str, session: requests.Session | None = None) -> str:
    email = email.lower()
    hit = _cache.get(email)
    if hit and hit[1] > time.time() + 60:
        return hit[0]
    rt = creds.tokens.get(email)
    if not rt:
        raise AuthError(f"No key for {email}. Run: mailroom login {email}")
    s = session or requests
    r = s.post(TOKEN_URL, data={
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": rt,
        "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code != 200:
        err = r.json().get("error", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
        if err == "invalid_grant":
            raise AuthError(f"The key for {email} was revoked or expired. Run: mailroom login {email}")
        raise AuthError(f"Google refused the key for {email}: {err}")
    d = r.json()
    _cache[email] = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
    return d["access_token"]


# ---------- first-time sign-in (runs on your Mac, opens a browser) ----------

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def interactive_login(creds: Credentials, login_hint: str | None = None, timeout: int = 900,
                      open_browser: bool = True) -> tuple[str, str]:
    """Open the Google sign-in page, wait for Allow, return (email, refresh_token)."""
    result: dict[str, str] = {}
    state = secrets.token_urlsafe(16)
    verifier, challenge = _pkce()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") != state:
                self.send_response(400); self.end_headers(); return
            result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Done. You can close this tab and go back to Claude." if "code" in q else f"Not allowed: {q.get('error')}"
            self.wfile.write(f"<h2>Mailroom</h2><p>{msg}</p>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    params = {
        "client_id": creds.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent select_account",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"Opening Google sign-in{' for ' + login_hint if login_hint else ''}...")
    print(f"If no browser opens, paste this link into one:\n{url}\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    else:
        print("Signing in from another computer? After you click Allow, that browser shows "
              "'site can't be reached'. Copy its full address and, on this Mac, run:\n"
              "  curl '<that address>'\n", flush=True)

    t = threading.Thread(target=lambda: _serve_until(server, result, timeout), daemon=True)
    t.start()
    t.join(timeout + 5)
    server.server_close()
    if "code" not in result:
        raise AuthError(f"Sign-in didn't finish: {result.get('error', 'timed out')}")

    r = requests.post(TOKEN_URL, data={
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }, timeout=30)
    if r.status_code != 200:
        raise AuthError(f"Google refused the sign-in: {r.text}")
    d = r.json()
    if "refresh_token" not in d:
        raise AuthError("Google didn't return a long-lived key. Remove the app at "
                        "myaccount.google.com/permissions and run login again.")
    prof = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                        headers={"Authorization": f"Bearer {d['access_token']}"}, timeout=30)
    prof.raise_for_status()
    return prof.json()["emailAddress"].lower(), d["refresh_token"]


def _serve_until(server, result, timeout):
    end = time.time() + timeout
    server.timeout = 1
    while "code" not in result and "error" not in result and time.time() < end:
        server.handle_request()
