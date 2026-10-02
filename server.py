#!/usr/bin/env python3
"""AlphaNet MailOps — a small, dependency-free download-mail operations service."""

import base64
import datetime
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn


ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_ROOT = os.path.join(ROOT, "web")
DB_PATH = os.environ.get("MAILOPS_DB", os.path.join(ROOT, "data", "mailops.db"))
ARTIFACT_ROOT = os.environ.get(
    "MAILOPS_ARTIFACT_ROOT", os.path.join(ROOT, "data", "artifacts")
)
HOST = os.environ.get("MAILOPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("MAILOPS_PORT", "9400"))
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "gen-lang-client-0654303469")
FIREBASE_AUTH_DOMAIN = os.environ.get(
    "FIREBASE_AUTH_DOMAIN", "gen-lang-client-0654303469.firebaseapp.com"
)
ADMIN_EMAILS = set(
    value.strip().lower()
    for value in os.environ.get("ADMIN_EMAILS", "").split(",")
    if value.strip()
)
LOCAL_ADMIN_PASSWORD_HASH = os.environ.get("LOCAL_ADMIN_PASSWORD_HASH", "")
WEBHOOK_SECRET = os.environ.get("MAILOPS_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://mailops.alphanetplus.com").rstrip("/")
DOWNLOAD_BASE_URL = os.environ.get(
    "DOWNLOAD_BASE_URL", "https://download.alphanetplus.com"
).rstrip("/")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "DLSS5 Studio <downloads@alphanetplus.com>")
MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "")
TOKEN_CACHE = {}
TOKEN_CACHE_LOCK = threading.Lock()

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utcnow():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def parse_iso(value):
    if not value:
        return None
    return datetime.datetime.strptime(value.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")


def add_hours(hours):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=hours)).replace(
        microsecond=0
    ).isoformat() + "Z"


def safe_text(value, limit=4000):
    if value is None:
        return ""
    value = CONTROL_RE.sub("", str(value)).strip()
    return value[:limit]


def infer_recipient_name(email_address):
    local = (email_address or "").split("@", 1)[0]
    local = re.sub(r"[._+-]+", " ", local).strip()
    return local or email_address


def resolve_artifact(download_url, require_exists=True):
    """Resolve an artifact: target without allowing path traversal."""
    if not download_url.startswith("artifact:"):
        return None
    name = urllib.parse.unquote(download_url[len("artifact:") :]).strip()
    if not name or name != os.path.basename(name) or name in (".", ".."):
        raise ValueError("服务器文件名称无效")
    root = os.path.realpath(ARTIFACT_ROOT)
    candidate = os.path.realpath(os.path.join(root, name))
    if not candidate.startswith(root + os.sep):
        raise ValueError("服务器文件名称无效")
    if require_exists and not os.path.isfile(candidate):
        raise ValueError("服务器文件不存在")
    return candidate


def validate_download_url(download_url):
    if download_url.startswith("artifact:"):
        resolve_artifact(download_url)
        return
    parsed = urllib.parse.urlsplit(download_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("请输入有效下载 URL 或 artifact:服务器文件名")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def db_connect():
    folder = os.path.dirname(DB_PATH)
    if folder and not os.path.exists(folder):
        os.makedirs(folder)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = db_connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY,
          source_id TEXT UNIQUE,
          user_id TEXT NOT NULL DEFAULT '',
          email TEXT NOT NULL,
          recipient_name TEXT NOT NULL DEFAULT '',
          machine TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT 'dlss5',
          status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          last_sent_at TEXT,
          assigned_build_id INTEGER,
          internal_note TEXT NOT NULL DEFAULT '',
          FOREIGN KEY (assigned_build_id) REFERENCES builds(id)
        );

        CREATE INDEX IF NOT EXISTS idx_requests_status_created
          ON requests(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_requests_email ON requests(email);

        CREATE TABLE IF NOT EXISTS builds (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          version TEXT NOT NULL DEFAULT '',
          platform TEXT NOT NULL DEFAULT 'Windows',
          gpu TEXT NOT NULL DEFAULT 'NVIDIA',
          download_url TEXT NOT NULL,
          checksum TEXT NOT NULL DEFAULT '',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS templates (
          id TEXT PRIMARY KEY,
          subject TEXT NOT NULL,
          body TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS download_tokens (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          token_hash TEXT UNIQUE NOT NULL,
          request_id TEXT NOT NULL,
          build_id INTEGER NOT NULL,
          expires_at TEXT NOT NULL,
          max_downloads INTEGER NOT NULL DEFAULT 3,
          download_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          last_download_at TEXT,
          revoked_at TEXT,
          FOREIGN KEY (request_id) REFERENCES requests(id),
          FOREIGN KEY (build_id) REFERENCES builds(id)
        );

        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id TEXT,
          actor TEXT NOT NULL,
          kind TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

        CREATE TABLE IF NOT EXISTS admin_sessions (
          token_hash TEXT PRIMARY KEY,
          email TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
          ON admin_sessions(expires_at);
        """
    )
    request_columns = set(row["name"] for row in conn.execute("PRAGMA table_info(requests)"))
    if "recipient_name" not in request_columns:
        conn.execute("ALTER TABLE requests ADD COLUMN recipient_name TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """INSERT OR IGNORE INTO templates(id, subject, body, updated_at)
           VALUES (?, ?, ?, ?)""",
        (
            "delivery",
            "Your DLSS5 Studio download is ready",
            "Hi {recipient_name},\n\nYour requested build is ready.\n\nBuild: {build_name}\nMachine: {machine}\nDownload: {download_url}\n\nThis private link expires in {expires_hours} hours. Please do not share it.\n\n— DLSS5 Studio",
            utcnow(),
        ),
    )
    template = conn.execute("SELECT subject, body FROM templates WHERE id = 'delivery'").fetchone()
    if template:
        subject = html.unescape(template["subject"])
        body = html.unescape(template["body"]).replace("Hi {email},", "Hi {recipient_name},")
        if subject != template["subject"] or body != template["body"]:
            conn.execute(
                "UPDATE templates SET subject = ?, body = ?, updated_at = ? WHERE id = 'delivery'",
                (subject, body, utcnow()),
            )
    conn.commit()
    conn.close()


def row_dict(row):
    return dict(row) if row is not None else None


def audit(conn, kind, actor, request_id=None, detail=""):
    conn.execute(
        "INSERT INTO events(request_id, actor, kind, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (request_id, safe_text(actor, 320), safe_text(kind, 80), safe_text(detail, 2000), utcnow()),
    )


def http_json(url, payload, headers=None, timeout=12):
    data = json_bytes(payload)
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Resend's Cloudflare edge blocks Python's default urllib user-agent with
        # error 1010. A stable product identity also makes provider logs useful.
        "User-Agent": "AlphaNet-MailOps/1.0 (+https://mailops.alphanetplus.com)",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        response = urllib.request.urlopen(
            request, timeout=timeout, context=ssl.create_default_context()
        )
        body = response.read().decode("utf-8")
        return response.getcode(), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"message": body[:500]}
        return exc.code, parsed


def firebase_login(email_address, password):
    if not FIREBASE_API_KEY:
        raise RuntimeError("Firebase login is not configured")
    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key="
        + urllib.parse.quote(FIREBASE_API_KEY)
    )
    status, data = http_json(
        url,
        {"email": email_address, "password": password, "returnSecureToken": True},
    )
    if status >= 400:
        raise PermissionError("邮箱或密码不正确")
    if data.get("email", "").lower() not in ADMIN_EMAILS:
        raise PermissionError("该账号没有管理员权限")
    return {
        "idToken": data.get("idToken"),
        "refreshToken": data.get("refreshToken"),
        "expiresIn": int(data.get("expiresIn", "3600")),
        "email": data.get("email"),
    }


def verify_local_password(password):
    """Verify a PBKDF2-SHA256 hash without ever storing the clear-text password."""
    if not LOCAL_ADMIN_PASSWORD_HASH:
        return False
    try:
        scheme, rounds, salt_hex, expected_hex = LOCAL_ADMIN_PASSWORD_HASH.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        ).hex()
        return hmac.compare_digest(actual, expected_hex)
    except (ValueError, TypeError):
        return False


def create_local_session(email_address):
    token = "mo_" + secrets.token_urlsafe(40)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = add_hours(12)
    conn = db_connect()
    conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (utcnow(),))
    conn.execute(
        "INSERT INTO admin_sessions(token_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (digest, email_address, utcnow(), expires_at),
    )
    conn.commit()
    conn.close()
    return {"idToken": token, "refreshToken": "", "expiresIn": 43200, "email": email_address}


def verify_local_session(token):
    if not token.startswith("mo_"):
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = db_connect()
    row = conn.execute(
        "SELECT email, expires_at FROM admin_sessions WHERE token_hash = ?", (digest,)
    ).fetchone()
    if not row or row["expires_at"] <= utcnow():
        if row:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (digest,))
            conn.commit()
        conn.close()
        return None
    identity = {"email": row["email"], "localId": "mailops-local-admin"}
    conn.close()
    return identity


def verify_admin_token(token):
    return verify_local_session(token) or verify_firebase_token(token)


def verify_firebase_token(token):
    if not token or not FIREBASE_API_KEY:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now_value = time.time()
    with TOKEN_CACHE_LOCK:
        cached = TOKEN_CACHE.get(digest)
        if cached and cached[0] > now_value:
            return cached[1]
    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:lookup?key="
        + urllib.parse.quote(FIREBASE_API_KEY)
    )
    status, data = http_json(url, {"idToken": token})
    if status >= 400 or not data.get("users"):
        return None
    user = data["users"][0]
    email_address = safe_text(user.get("email"), 320).lower()
    if email_address not in ADMIN_EMAILS:
        return None
    identity = {"email": email_address, "localId": user.get("localId", "")}
    with TOKEN_CACHE_LOCK:
        TOKEN_CACHE[digest] = (now_value + 240, identity)
        if len(TOKEN_CACHE) > 300:
            for key in list(TOKEN_CACHE.keys())[:100]:
                TOKEN_CACHE.pop(key, None)
    return identity


def render_template(text, values):
    output = html.unescape(text or "")
    for key, value in values.items():
        output = output.replace("{" + key + "}", safe_text(value, 8000))
    return output


def send_resend(to_address, subject, text_body):
    if not RESEND_API_KEY:
        raise RuntimeError("邮件通道尚未配置")
    payload = {
        "from": MAIL_FROM,
        "to": [to_address],
        "subject": safe_text(subject, 200),
        "text": text_body,
        "html": "<div style=\"font-family:Inter,Arial,sans-serif;line-height:1.65;color:#111827;white-space:pre-wrap\">%s</div>"
        % html.escape(text_body),
    }
    if MAIL_REPLY_TO:
        payload["reply_to"] = MAIL_REPLY_TO
    status, data = http_json(
        "https://api.resend.com/emails",
        payload,
        headers={"Authorization": "Bearer " + RESEND_API_KEY},
    )
    if status >= 400:
        message = data.get("message") or data.get("error") or "邮件发送失败"
        raise RuntimeError(safe_text(message, 500))
    return data.get("id", "")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(SimpleHTTPRequestHandler):
    server_version = "AlphaNetMailOps/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' https://www.gstatic.com https://apis.google.com; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com; frame-src https://gen-lang-client-0654303469.firebaseapp.com https://accounts.google.com; img-src 'self' data: https://*.googleusercontent.com; style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; frame-ancestors 'none'",
        )
        SimpleHTTPRequestHandler.end_headers(self)

    def send_json(self, status, payload):
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, maximum=1024 * 1024):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if length <= 0 or length > maximum:
            raise ValueError("Invalid request body")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Invalid JSON")

    def route_path(self):
        return urllib.parse.urlsplit(self.path).path

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def bearer_identity(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return verify_admin_token(header[7:].strip())

    def require_admin(self):
        identity = self.bearer_identity()
        if not identity:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "需要管理员登录"})
        return identity

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, PUT, PATCH, OPTIONS")
        self.end_headers()

    def do_GET(self):
        try:
            path = self.route_path()
            if path == "/api/health":
                return self.api_health()
            if path == "/api/config":
                return self.api_config()
            if path.startswith("/d/"):
                return self.download_landing(path[3:])
            if path.startswith("/api/"):
                identity = self.require_admin()
                if not identity:
                    return
                return self.api_get(path, identity)
            return self.serve_static(path)
        except Exception as exc:
            traceback.print_exc()
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": safe_text(exc, 500)})

    def do_POST(self):
        try:
            path = self.route_path()
            if path == "/api/auth/login":
                return self.api_login()
            if path == "/api/intake":
                return self.api_intake()
            if path.startswith("/d/"):
                return self.download(path[3:])
            identity = self.require_admin()
            if not identity:
                return
            return self.api_post(path, identity)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": safe_text(exc, 500)})
        except PermissionError as exc:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": safe_text(exc, 500)})
        except Exception as exc:
            traceback.print_exc()
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": safe_text(exc, 500)})

    def do_PATCH(self):
        try:
            identity = self.require_admin()
            if not identity:
                return
            return self.api_patch(self.route_path(), identity)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": safe_text(exc, 500)})
        except Exception as exc:
            traceback.print_exc()
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": safe_text(exc, 500)})

    def do_PUT(self):
        try:
            identity = self.require_admin()
            if not identity:
                return
            return self.api_put(self.route_path(), identity)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": safe_text(exc, 500)})
        except Exception as exc:
            traceback.print_exc()
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": safe_text(exc, 500)})

    def serve_static(self, path):
        if path == "/":
            path = "/index.html"
        candidate = os.path.realpath(os.path.join(WEB_ROOT, path.lstrip("/")))
        if not candidate.startswith(os.path.realpath(WEB_ROOT) + os.sep):
            return self.send_error(HTTPStatus.NOT_FOUND)
        if not os.path.isfile(candidate):
            candidate = os.path.join(WEB_ROOT, "index.html")
        content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        with open(candidate, "rb") as handle:
            body = handle.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if candidate.endswith("index.html") else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def api_health(self):
        conn = db_connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "alphanet-mailops",
                "mailConfigured": bool(RESEND_API_KEY),
                "firebaseConfigured": bool(FIREBASE_API_KEY and ADMIN_EMAILS),
                "artifactStorageConfigured": os.path.isdir(ARTIFACT_ROOT),
                "time": utcnow(),
            },
        )

    def api_config(self):
        self.send_json(
            HTTPStatus.OK,
            {
                "firebase": {
                    "apiKey": FIREBASE_API_KEY,
                    "authDomain": FIREBASE_AUTH_DOMAIN,
                    "projectId": FIREBASE_PROJECT_ID,
                },
                "publicBaseUrl": PUBLIC_BASE_URL,
                "downloadBaseUrl": DOWNLOAD_BASE_URL,
            },
        )

    def api_login(self):
        payload = self.read_json(64 * 1024)
        email_address = safe_text(payload.get("email"), 320).lower()
        password = str(payload.get("password") or "")
        if not EMAIL_RE.match(email_address) or not password:
            raise ValueError("请输入有效的邮箱和密码")
        if email_address in ADMIN_EMAILS and verify_local_password(password):
            result = create_local_session(email_address)
        else:
            result = firebase_login(email_address, password)
        self.send_json(HTTPStatus.OK, result)

    def api_intake(self):
        supplied = self.headers.get("X-MailOps-Secret", "")
        if not WEBHOOK_SECRET or not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid webhook secret"})
        payload = self.read_json()
        source_id = safe_text(payload.get("requestId") or payload.get("id"), 160)
        email_address = safe_text(payload.get("email"), 320).lower()
        if not source_id or not EMAIL_RE.match(email_address):
            raise ValueError("requestId and email are required")
        now_value = utcnow()
        recipient_name = safe_text(payload.get("recipientName"), 200) or infer_recipient_name(email_address)
        request_id = "req_" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
        conn = db_connect()
        existing = conn.execute("SELECT id FROM requests WHERE source_id = ?", (source_id,)).fetchone()
        if existing:
            conn.close()
            return self.send_json(HTTPStatus.OK, {"ok": True, "id": existing["id"], "duplicate": True})
        conn.execute(
            """INSERT INTO requests(
                 id, source_id, user_id, email, recipient_name, machine, note, source, status,
                 created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                request_id,
                source_id,
                safe_text(payload.get("userId"), 200),
                email_address,
                recipient_name,
                safe_text(payload.get("machine"), 500),
                safe_text(payload.get("note"), 4000),
                safe_text(payload.get("source") or "dlss5", 80),
                safe_text(payload.get("createdAt") or now_value, 40),
                now_value,
            ),
        )
        audit(conn, "request.received", "dlss5-webhook", request_id, "New download request")
        conn.commit()
        conn.close()
        self.send_json(HTTPStatus.CREATED, {"ok": True, "id": request_id})

    def api_get(self, path, identity):
        conn = db_connect()
        try:
            if path == "/api/summary":
                counts = {}
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM requests GROUP BY status"):
                    counts[row["status"]] = row["count"]
                recent = [row_dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12")]
                builds = conn.execute("SELECT COUNT(*) AS count FROM builds WHERE active = 1").fetchone()["count"]
                return self.send_json(HTTPStatus.OK, {"counts": counts, "activeBuilds": builds, "events": recent})
            if path == "/api/requests":
                query = self.query()
                status_value = safe_text((query.get("status") or [""])[0], 40)
                search = safe_text((query.get("q") or [""])[0], 200)
                sql = "SELECT * FROM requests WHERE 1=1"
                args = []
                if status_value and status_value != "all":
                    sql += " AND status = ?"
                    args.append(status_value)
                if search:
                    sql += " AND (email LIKE ? OR machine LIKE ? OR note LIKE ?)"
                    token = "%" + search + "%"
                    args.extend([token, token, token])
                sql += " ORDER BY created_at DESC LIMIT 300"
                rows = [row_dict(row) for row in conn.execute(sql, args)]
                return self.send_json(HTTPStatus.OK, {"items": rows})
            if path.startswith("/api/requests/"):
                request_id = safe_text(path.split("/")[3], 80)
                item = row_dict(conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone())
                if not item:
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "请求不存在"})
                item["events"] = [row_dict(row) for row in conn.execute("SELECT * FROM events WHERE request_id = ? ORDER BY id DESC", (request_id,))]
                item["tokens"] = [row_dict(row) for row in conn.execute("SELECT id, build_id, expires_at, max_downloads, download_count, created_at, last_download_at, revoked_at FROM download_tokens WHERE request_id = ? ORDER BY id DESC", (request_id,))]
                return self.send_json(HTTPStatus.OK, item)
            if path == "/api/builds":
                rows = [row_dict(row) for row in conn.execute("SELECT * FROM builds ORDER BY active DESC, created_at DESC")]
                return self.send_json(HTTPStatus.OK, {"items": rows})
            if path == "/api/template":
                item = row_dict(conn.execute("SELECT * FROM templates WHERE id = 'delivery'").fetchone())
                item["subject"] = html.unescape(item["subject"])
                item["body"] = html.unescape(item["body"])
                return self.send_json(HTTPStatus.OK, item)
            if path == "/api/activity":
                rows = [row_dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200")]
                return self.send_json(HTTPStatus.OK, {"items": rows})
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "API not found"})
        finally:
            conn.close()

    def api_post(self, path, identity):
        if path == "/api/builds":
            payload = self.read_json()
            name = safe_text(payload.get("name"), 200)
            download_url = safe_text(payload.get("download_url"), 2000)
            if not name or not download_url:
                raise ValueError("构建名称和下载目标为必填项")
            validate_download_url(download_url)
            now_value = utcnow()
            conn = db_connect()
            cursor = conn.execute(
                """INSERT INTO builds(name, version, platform, gpu, download_url, checksum, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    safe_text(payload.get("version"), 100),
                    safe_text(payload.get("platform") or "Windows", 100),
                    safe_text(payload.get("gpu") or "NVIDIA", 200),
                    download_url,
                    safe_text(payload.get("checksum"), 300),
                    1 if payload.get("active", True) else 0,
                    now_value,
                    now_value,
                ),
            )
            audit(conn, "build.created", identity["email"], None, name)
            conn.commit()
            item = row_dict(conn.execute("SELECT * FROM builds WHERE id = ?", (cursor.lastrowid,)).fetchone())
            conn.close()
            return self.send_json(HTTPStatus.CREATED, item)
        match = re.match(r"^/api/requests/([^/]+)/(send|reject)$", path)
        if match:
            request_id = safe_text(match.group(1), 80)
            action = match.group(2)
            payload = self.read_json()
            if action == "reject":
                reason = safe_text(payload.get("reason") or "Rejected by administrator", 1000)
                conn = db_connect()
                if not conn.execute("SELECT 1 FROM requests WHERE id = ?", (request_id,)).fetchone():
                    conn.close()
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "请求不存在"})
                conn.execute("UPDATE requests SET status = 'rejected', internal_note = ?, updated_at = ? WHERE id = ?", (reason, utcnow(), request_id))
                audit(conn, "request.rejected", identity["email"], request_id, reason)
                conn.commit()
                conn.close()
                return self.send_json(HTTPStatus.OK, {"ok": True})
            if not RESEND_API_KEY:
                return self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "邮件通道尚未配置，暂时不能发送"})
            try:
                build_id = int(payload.get("build_id"))
                expires_hours = max(1, min(720, int(payload.get("expires_hours", 72))))
                max_downloads = max(1, min(100, int(payload.get("max_downloads", 3))))
            except (TypeError, ValueError):
                raise ValueError("构建、有效期或下载次数无效")
            conn = db_connect()
            item = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            build = conn.execute("SELECT * FROM builds WHERE id = ? AND active = 1", (build_id,)).fetchone()
            template = conn.execute("SELECT * FROM templates WHERE id = 'delivery'").fetchone()
            if not item or not build:
                conn.close()
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "请求或构建不存在"})
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            download_link = DOWNLOAD_BASE_URL + "/d/" + raw_token
            values = {
                "email": item["email"],
                "recipient_name": item["recipient_name"] or infer_recipient_name(item["email"]),
                "machine": item["machine"] or "Not specified",
                "note": item["note"] or "",
                "build_name": "%s %s" % (build["name"], build["version"]),
                "download_url": download_link,
                "expires_hours": str(expires_hours),
            }
            subject = render_template(payload.get("subject") or template["subject"], values)
            body = render_template(payload.get("body") or template["body"], values)
            try:
                provider_id = send_resend(item["email"], subject, body)
            except Exception:
                audit(conn, "mail.failed", identity["email"], request_id, "Delivery provider rejected the message")
                conn.commit()
                conn.close()
                raise
            now_value = utcnow()
            conn.execute(
                """INSERT INTO download_tokens(token_hash, request_id, build_id, expires_at, max_downloads, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (token_hash, request_id, build_id, add_hours(expires_hours), max_downloads, now_value),
            )
            conn.execute("UPDATE requests SET status = 'sent', assigned_build_id = ?, last_sent_at = ?, updated_at = ? WHERE id = ?", (build_id, now_value, now_value, request_id))
            audit(conn, "mail.sent", identity["email"], request_id, "Provider id: " + provider_id)
            conn.commit()
            conn.close()
            return self.send_json(HTTPStatus.OK, {"ok": True, "providerId": provider_id})
        return self.send_json(HTTPStatus.NOT_FOUND, {"error": "API not found"})

    def api_patch(self, path, identity):
        match = re.match(r"^/api/(requests|builds)/([^/]+)$", path)
        if not match:
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "API not found"})
        kind, raw_id = match.groups()
        payload = self.read_json()
        conn = db_connect()
        if kind == "requests":
            request_id = safe_text(raw_id, 80)
            status_value = safe_text(payload.get("status"), 40)
            allowed = ("pending", "reviewing", "approved", "sent", "rejected", "archived")
            if status_value and status_value not in allowed:
                conn.close()
                raise ValueError("无效状态")
            current = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            if not current:
                conn.close()
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "请求不存在"})
            new_status = status_value or current["status"]
            note = safe_text(payload.get("internal_note", current["internal_note"]), 4000)
            conn.execute("UPDATE requests SET status = ?, internal_note = ?, updated_at = ? WHERE id = ?", (new_status, note, utcnow(), request_id))
            audit(conn, "request.updated", identity["email"], request_id, "Status: " + new_status)
            conn.commit()
            item = row_dict(conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone())
            conn.close()
            return self.send_json(HTTPStatus.OK, item)
        try:
            build_id = int(raw_id)
        except ValueError:
            conn.close()
            raise ValueError("无效构建 ID")
        current = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not current:
            conn.close()
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "构建不存在"})
        values = {
            "name": safe_text(payload.get("name", current["name"]), 200),
            "version": safe_text(payload.get("version", current["version"]), 100),
            "platform": safe_text(payload.get("platform", current["platform"]), 100),
            "gpu": safe_text(payload.get("gpu", current["gpu"]), 200),
            "download_url": safe_text(payload.get("download_url", current["download_url"]), 2000),
            "checksum": safe_text(payload.get("checksum", current["checksum"]), 300),
            "active": 1 if payload.get("active", bool(current["active"])) else 0,
        }
        validate_download_url(values["download_url"])
        conn.execute("""UPDATE builds SET name = :name, version = :version, platform = :platform, gpu = :gpu,
                        download_url = :download_url, checksum = :checksum, active = :active, updated_at = :updated_at
                        WHERE id = :id""", dict(values, updated_at=utcnow(), id=build_id))
        audit(conn, "build.updated", identity["email"], None, values["name"])
        conn.commit()
        item = row_dict(conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone())
        conn.close()
        return self.send_json(HTTPStatus.OK, item)

    def api_put(self, path, identity):
        if path != "/api/template":
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "API not found"})
        payload = self.read_json()
        subject = html.unescape(safe_text(payload.get("subject"), 240))
        body = html.unescape(safe_text(payload.get("body"), 12000))
        if not subject or not body:
            raise ValueError("主题和正文不能为空")
        conn = db_connect()
        conn.execute("UPDATE templates SET subject = ?, body = ?, updated_at = ? WHERE id = 'delivery'", (subject, body, utcnow()))
        audit(conn, "template.updated", identity["email"], None, subject)
        conn.commit()
        item = row_dict(conn.execute("SELECT * FROM templates WHERE id = 'delivery'").fetchone())
        conn.close()
        self.send_json(HTTPStatus.OK, item)

    def download(self, raw_token):
        if not raw_token or len(raw_token) > 200:
            return self.send_error(HTTPStatus.NOT_FOUND)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        conn = db_connect()
        row = conn.execute(
            """SELECT t.*, b.download_url, b.name AS build_name
               FROM download_tokens t JOIN builds b ON b.id = t.build_id
               WHERE t.token_hash = ?""",
            (token_hash,),
        ).fetchone()
        if not row or row["revoked_at"]:
            conn.close()
            return self.download_error("链接无效", "这个下载链接不存在或已被撤销。", HTTPStatus.NOT_FOUND)
        if parse_iso(row["expires_at"]) <= datetime.datetime.utcnow():
            conn.close()
            return self.download_error("链接已过期", "请联系 DLSS5 Studio 重新申请下载。", HTTPStatus.GONE)
        if row["download_count"] >= row["max_downloads"]:
            conn.close()
            return self.download_error("下载次数已用完", "请联系 DLSS5 Studio 重新获取链接。", HTTPStatus.GONE)
        if row["download_url"].startswith("artifact:"):
            try:
                resolve_artifact(row["download_url"])
            except ValueError:
                conn.close()
                return self.download_error(
                    "文件不可用", "交付文件暂时不存在，请联系 DLSS5 Studio。", HTTPStatus.NOT_FOUND
                )
        now_value = utcnow()
        conn.execute("UPDATE download_tokens SET download_count = download_count + 1, last_download_at = ? WHERE id = ?", (now_value, row["id"]))
        audit(conn, "download.opened", "recipient", row["request_id"], row["build_name"])
        conn.commit()
        conn.close()
        if row["download_url"].startswith("artifact:"):
            return self.serve_artifact(row["download_url"])
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", row["download_url"])
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def download_landing(self, raw_token):
        if not raw_token or len(raw_token) > 200:
            return self.send_error(HTTPStatus.NOT_FOUND)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        conn = db_connect()
        row = conn.execute(
            """SELECT t.*, b.name AS build_name, b.version AS build_version, b.checksum
               FROM download_tokens t JOIN builds b ON b.id = t.build_id
               WHERE t.token_hash = ?""",
            (token_hash,),
        ).fetchone()
        conn.close()
        if not row or row["revoked_at"]:
            return self.download_error("链接无效", "这个下载链接不存在或已被撤销。", HTTPStatus.NOT_FOUND)
        if parse_iso(row["expires_at"]) <= datetime.datetime.utcnow():
            return self.download_error("链接已过期", "请联系 DLSS5 Studio 重新申请下载。", HTTPStatus.GONE)
        remaining = row["max_downloads"] - row["download_count"]
        if remaining <= 0:
            return self.download_error("下载次数已用完", "请联系 DLSS5 Studio 重新获取链接。", HTTPStatus.GONE)
        build_label = (row["build_name"] + " " + (row["build_version"] or "")).strip()
        checksum = row["checksum"] or "未提供"
        action = "/d/" + urllib.parse.quote(raw_token)
        body = ("<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>准备下载</title><style>body{margin:0;background:#080b0d;color:#eef4ef;font:15px/1.65 system-ui;display:grid;place-items:center;min-height:100vh}"
                "main{width:min(560px,calc(100%% - 48px));padding:42px;border:1px solid #26302b;background:#0e1311;border-radius:18px}"
                "b{color:#49e995;font-size:12px;letter-spacing:.14em}h1{font-size:30px;margin:.45em 0}.meta{padding:16px;background:#0a0e0c;border-radius:10px;color:#9eaaa3;overflow-wrap:anywhere}"
                "button{margin-top:22px;border:0;border-radius:9px;background:#49e995;color:#06110b;padding:13px 20px;font-weight:800;cursor:pointer}</style>"
                "<main><b>ALPHANET · PRIVATE DELIVERY</b><h1>%s</h1><p>文件已经准备好。点击按钮后才会计入一次下载。</p>"
                "<div class=meta>剩余下载次数：%s<br>有效期至：%s<br>校验值：%s</div>"
                "<form method=post action='%s'><button type=submit>开始下载 ↗</button></form></main></html>") % (
                    html.escape(build_label), remaining, html.escape(row["expires_at"]),
                    html.escape(checksum), html.escape(action)
                )
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def serve_artifact(self, download_url):
        try:
            artifact_path = resolve_artifact(download_url)
        except ValueError:
            return self.download_error(
                "文件不可用", "交付文件暂时不存在，请联系 DLSS5 Studio。", HTTPStatus.NOT_FOUND
            )
        size = os.path.getsize(artifact_path)
        filename = os.path.basename(artifact_path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(filename),
        )
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        with open(artifact_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break

    def download_error(self, title, message, status):
        body = ("<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>%s</title><style>body{margin:0;background:#080b0d;color:#eef4ef;font:16px/1.6 system-ui;display:grid;place-items:center;min-height:100vh}"
                "main{max-width:520px;padding:44px;border:1px solid #26302b;background:#0e1311;border-radius:18px}b{color:#49e995;font-size:13px;letter-spacing:.14em}h1{font-size:34px;margin:.4em 0}</style>"
                "<main><b>ALPHANET · MAILOPS</b><h1>%s</h1><p>%s</p></main></html>") % (html.escape(title), html.escape(title), html.escape(message))
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("AlphaNet MailOps listening on http://%s:%s" % (HOST, PORT))
    server.serve_forever()


if __name__ == "__main__":
    main()
