"""Collabora Online file manager + WOPI host + reverse proxy.

A single Quart process that:

1. Serves a tiny file-management UI at ``/``.
2. Acts as the WOPI host coolwsd talks to during edits (``/wopi/files/...``).
3. Reverse-proxies the Collabora editor surface (``/browser/``, ``/cool/``,
   ``/lool/``, ``/hosting/``) — including WebSockets — to the loopback
   coolwsd backend on port 9980.

Why one process:
    The container needs both the UI ("here are your files") and Collabora's
    editor available under the same hostname so the iframe can talk back.
    Splitting at the OpenHost router level would mean two apps, which
    doubles deploy footprint for what is logically one product.  Splitting
    inside the container with nginx + supervisord adds two more processes
    and a config language to maintain.  A single Quart with httpx +
    websockets reverse-proxying a few paths is short, easy to reason about,
    and matches the patterns OpenHost's own router uses.

Persistent state lives under OPENHOST_APP_DATA_DIR:
    files/<uuid>     — raw document bytes
    index.db         — sqlite metadata (filename, size, mtime, ext)

The whole thing intentionally avoids folders, sharing, multi-user, sorting,
search.  This is "barebones": list, upload, open, save, delete, new-doc.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from quart import (
    Quart,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
    websocket,
)
from werkzeug.datastructures import Headers

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

APP_DATA_DIR = Path(os.environ.get("OPENHOST_APP_DATA_DIR", "/tmp/collabora-data"))
FILES_DIR = APP_DATA_DIR / "files"
DB_PATH = APP_DATA_DIR / "index.db"
TEMPLATES_DIR = Path(__file__).parent / "blank_templates"

# coolwsd is started by the entrypoint script on loopback only.
COOLWSD_HOST = "127.0.0.1"
COOLWSD_PORT = 9980

# Public URL the browser uses to talk back to us.  Constructed from
# OpenHost-injected env vars; coolwsd embeds this in the editor iframe
# config (``WOPISrc``).
ZONE_DOMAIN = os.environ.get("OPENHOST_ZONE_DOMAIN", "localhost")
APP_NAME = os.environ.get("OPENHOST_APP_NAME", "collabora")
PUBLIC_HOST = f"{APP_NAME}.{ZONE_DOMAIN}"
PUBLIC_BASE = f"https://{PUBLIC_HOST}"

# An opaque shared secret so coolwsd can prove to us it's a real WOPI
# request when calling back.  Not a security boundary — both processes
# are inside the same container — but rejects accidental cross-app calls.
WOPI_ACCESS_TOKEN = secrets.token_urlsafe(32)

# File-extension allowlist, derived from Collabora's own discovery output.
# Anything outside this list won't load in the editor; we still let it be
# uploaded and downloaded so the user can stash other files alongside.
EDITABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Writer
        "odt", "fodt", "doc", "docx", "docm", "dot", "dotx", "dotm",
        "rtf", "txt", "html", "htm",
        # Calc
        "ods", "fods", "xls", "xlsx", "xlsm", "xlt", "xltx", "xltm", "csv",
        # Impress
        "odp", "fodp", "ppt", "pptx", "pptm", "pot", "potx", "potm", "pps", "ppsx",
        # Draw
        "odg", "fodg",
    }
)

# Map "new document" template → on-disk seed file.  Pre-baked into the
# image so the user doesn't have to upload an empty .odt to get started.
TEMPLATE_SEEDS = {
    "writer": ("New Document.odt", TEMPLATES_DIR / "blank.odt"),
    "calc": ("New Spreadsheet.ods", TEMPLATES_DIR / "blank.ods"),
    "impress": ("New Presentation.odp", TEMPLATES_DIR / "blank.odp"),
}

# ---------------------------------------------------------------------------
# Sqlite — file index
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Sqlite ships with foreign-key enforcement OFF by default — the FK in
    # the shares table is just documentary unless we flip this on per
    # connection.  We rely on it for cascading delete (deleting a file row
    # auto-removes its share rows).
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                ext TEXT NOT NULL,
                mtime REAL NOT NULL
            )
            """
        )
        # Per-document share tokens.  Each row is one shareable link.
        # ``mode`` controls what the recipient can do:
        #   view     — open in the editor read-only
        #   edit     — open in the editor with write access
        #   download — fetch the raw bytes, no editor involved
        # ``expires_at`` is a unix timestamp; NULL means "never expires"
        # but the UI defaults to 30 days from creation.  ``revoked`` is a
        # tombstone — we keep the row so revoked tokens can be listed
        # historically (and so a future "see who's currently editing"
        # feature has stable UserIds to attribute to).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shares (
                token TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('view', 'edit', 'download')),
                created_at REAL NOT NULL,
                expires_at REAL,
                revoked INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_shares_file_id ON shares (file_id)"
        )


def list_files() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, size, ext, mtime FROM files ORDER BY mtime DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_file_row(file_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, size, ext, mtime FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
    return dict(row) if row else None


def insert_file(file_id: str, name: str, size: int, ext: str, mtime: float) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO files (id, name, size, ext, mtime) VALUES (?, ?, ?, ?, ?)",
            (file_id, name, size, ext, mtime),
        )


def update_file_size(file_id: str, size: int, mtime: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE files SET size = ?, mtime = ? WHERE id = ?",
            (size, mtime, file_id),
        )


def rename_file_row(file_id: str, name: str, mtime: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE files SET name = ?, mtime = ? WHERE id = ?",
            (name, mtime, file_id),
        )


def delete_file_row(file_id: str) -> None:
    with _connect() as conn:
        # Cascading delete (foreign_keys=ON) drops the share rows too.
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


# ---------------------------------------------------------------------------
# Sqlite — share tokens
# ---------------------------------------------------------------------------

# Default lifetime of a freshly-minted share link.  Operators can extend
# individual links from the manage-shares UI.  30 days is a middle ground
# between "share with one person, never revoke" and "set in stone forever".
DEFAULT_SHARE_TTL_SECONDS = 30 * 24 * 3600

# Mode → directory letter used in the public URL prefix.  Keeping the URL
# short matters when these get pasted into chat / email.
MODE_TO_LETTER = {"view": "v", "edit": "e", "download": "d"}
LETTER_TO_MODE = {v: k for k, v in MODE_TO_LETTER.items()}


def create_share(file_id: str, mode: str, ttl_seconds: int | None) -> str:
    """Mint a new share token.  ttl_seconds=None means "never expire"."""
    if mode not in MODE_TO_LETTER:
        raise ValueError(f"invalid share mode: {mode}")
    token = secrets.token_urlsafe(24)
    now = time.time()
    expires = now + ttl_seconds if ttl_seconds else None
    with _connect() as conn:
        conn.execute(
            "INSERT INTO shares (token, file_id, mode, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token, file_id, mode, now, expires),
        )
    return token


def get_share(token: str) -> dict[str, Any] | None:
    """Return the share row if the token is well-formed, exists, has not
    been revoked, and has not expired.  Otherwise return None.

    All four checks live here so callers can't accidentally trust a
    revoked-but-not-expired or expired-but-not-revoked row.
    """
    # Token shape check before hitting the DB — secrets.token_urlsafe(24)
    # produces 32 chars from the [A-Za-z0-9_-] alphabet.
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT token, file_id, mode, created_at, expires_at, revoked "
            "FROM shares WHERE token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    row = dict(row)
    if row["revoked"]:
        return None
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        return None
    return row


def list_shares_for_file(file_id: str) -> list[dict[str, Any]]:
    """List all shares (active and inactive) for a file, newest first.

    Inactive ones are still surfaced in the manage-shares UI so the owner
    can see "I revoked this last Tuesday" rather than have it silently
    disappear.
    """
    now = time.time()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token, file_id, mode, created_at, expires_at, revoked "
            "FROM shares WHERE file_id = ? ORDER BY created_at DESC",
            (file_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["expired"] = d["expires_at"] is not None and d["expires_at"] < now
        d["active"] = not d["revoked"] and not d["expired"]
        out.append(d)
    return out


def revoke_share(token: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE shares SET revoked = 1 WHERE token = ?", (token,))


def extend_share(token: str, ttl_seconds: int | None) -> None:
    """Push the expiry forward by ``ttl_seconds`` from now, or remove the
    expiry entirely if ttl_seconds is None.
    """
    new_expiry = (time.time() + ttl_seconds) if ttl_seconds else None
    with _connect() as conn:
        conn.execute(
            "UPDATE shares SET expires_at = ?, revoked = 0 WHERE token = ?",
            (new_expiry, token),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Filename validation: keep it strict so the UI doesn't have to deal with
# weird shell-escaping or path-traversal during downloads.
_SAFE_NAME_RE = re.compile(r"^[\w\-. ()\[\]]+$")


def _validated_name(name: str) -> str:
    name = name.strip()
    if not name or not _SAFE_NAME_RE.match(name):
        abort(400, description="invalid filename")
    if len(name) > 240:
        abort(400, description="filename too long")
    return name


def _ext_of(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return ext.lower() if dot else ""


def _file_path(file_id: str) -> Path:
    # ``file_id`` is generated server-side as a UUID; never trust user input.
    return FILES_DIR / file_id


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0  # type: ignore[assignment]
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Quart(__name__)
# 5 GB body cap.  Most documents are tiny; the cap exists to keep a bad
# upload from filling the disk or hanging the event loop.
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024


@app.before_serving
async def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# UI routes
# ---------------------------------------------------------------------------


@app.route("/")
async def index_page():
    files = list_files()
    rendered = []
    for f in files:
        rendered.append(
            {
                **f,
                "size_human": _format_size(f["size"]),
                "editable": f["ext"] in EDITABLE_EXTENSIONS,
            }
        )
    return await render_template(
        "index.html",
        files=rendered,
        new_doc_kinds=list(TEMPLATE_SEEDS.keys()),
    )


@app.route("/upload", methods=["POST"])
async def upload():
    form = await request.files
    uploaded = form.get("file")
    if uploaded is None or not uploaded.filename:
        abort(400, description="no file in request")

    name = _validated_name(uploaded.filename)
    ext = _ext_of(name)
    file_id = uuid.uuid4().hex
    dest = _file_path(file_id)

    # Stream to disk in chunks to avoid loading the whole document into RAM.
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = uploaded.stream.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)

    insert_file(file_id, name, size, ext, time.time())
    return redirect(url_for("index_page"))


@app.route("/new/<kind>", methods=["POST"])
async def new_doc(kind: str):
    seed = TEMPLATE_SEEDS.get(kind)
    if seed is None:
        abort(404, description="unknown document type")
    template_name, template_path = seed
    if not template_path.exists():
        # Should be impossible: templates are baked into the image.
        abort(500, description=f"missing seed template {template_path}")
    file_id = uuid.uuid4().hex
    dest = _file_path(file_id)
    shutil.copyfile(template_path, dest)
    size = dest.stat().st_size
    ext = _ext_of(template_name)
    insert_file(file_id, template_name, size, ext, time.time())
    return redirect(url_for("editor_page", file_id=file_id))


@app.route("/delete/<file_id>", methods=["POST"])
async def delete(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        abort(400, description="invalid id")
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    dest = _file_path(file_id)
    try:
        dest.unlink()
    except FileNotFoundError:
        pass
    delete_file_row(file_id)
    return redirect(url_for("index_page"))


@app.route("/download/<file_id>")
async def download(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        abort(400, description="invalid id")
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    # Read the whole file into memory.  Documents in this app are bounded
    # to a few MB in practice; if/when that becomes false, swap to
    # response.send() with an iterator.
    body = _file_path(file_id).read_bytes()
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    # RFC 5987-style Content-Disposition handles UTF-8 filenames;
    # ASCII-only fallback covers older browsers.
    safe_name = row["name"].encode("ascii", errors="replace").decode("ascii")
    return Response(
        body,
        status=200,
        headers={
            "Content-Type": mime,
            "Content-Disposition": (
                f'attachment; filename="{safe_name}"; '
                f"filename*=UTF-8''{row['name'].replace(' ', '%20')}"
            ),
        },
    )


@app.route("/open/<file_id>")
async def editor_page(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        abort(400, description="invalid id")
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    if row["ext"] not in EDITABLE_EXTENSIONS:
        abort(400, description="this file type is not editable in Collabora")

    # The editor iframe needs:
    #   1. The Collabora cool.html URL with WOPISrc pointing at our WOPI
    #      endpoint for this file.
    #   2. A POST form (constructed below in JS) carrying the access_token
    #      so coolwsd loads the doc.  GET-with-token is also supported but
    #      POST is recommended by Collabora's docs (keeps the token out of
    #      logs).
    #
    # WOPISrc points at loopback rather than the public URL.  The
    # iframe's <form action> is loaded by the browser via the public URL,
    # which is fine — the browser only ever sees the public origin.
    # WOPISrc, by contrast, is dereferenced by coolwsd from *inside* the
    # container; using loopback avoids a hairpin trip out through the
    # OpenHost router and back to ourselves, plus it sidesteps any
    # outbound DNS/firewall surprises.
    # WOPISrc must be percent-encoded in the cool.html query string; coolwsd
    # rejects (older builds) or warns on (current) an unencoded value.
    wopi_src = f"http://127.0.0.1:8080/wopi/files/{file_id}"
    cool_url = (
        f"{PUBLIC_BASE}/browser/dist/cool.html"
        f"?WOPISrc={quote(wopi_src, safe='')}"
        f"&closebutton=true"
    )
    return await render_template(
        "editor.html",
        file=row,
        cool_url=cool_url,
        access_token=WOPI_ACCESS_TOKEN,
        permission="edit",
    )


# ---------------------------------------------------------------------------
# WOPI host
# ---------------------------------------------------------------------------
#
# coolwsd calls these endpoints during an edit session.  WOPI is a closed,
# fully-specified protocol (Microsoft CSPP) that lives in exactly two URL
# shapes — ``/wopi/files/<id>`` and ``/wopi/files/<id>/contents`` — with the
# non-GET operations multiplexed onto ``POST /wopi/files/<id>`` and keyed by
# the ``X-WOPI-Override`` header.  We implement the complete set coolwsd can
# emit:
#
#   GET  /wopi/files/<id>            → CheckFileInfo (metadata + capabilities)
#   GET  /wopi/files/<id>/contents   → GetFile  (raw bytes)
#   POST /wopi/files/<id>/contents   → PutFile  (overwrite raw bytes)
#   POST /wopi/files/<id>            → dispatched on X-WOPI-Override:
#       RENAME_FILE                    rename (keeps the extension)
#       LOCK / UNLOCK / REFRESH_LOCK   advisory per-file lock
#       GET_LOCK                       report the current lock
#       DELETE                         delete the file
#       PUT_RELATIVE                   Save-As — declined (see below)
#
# Anything outside that set returns a *loud* 501 with the override name
# logged, so a protocol addition (or a caller bug) announces itself in the
# logs instead of surfacing to the user as a mysterious "expired session"
# (which is what a bare 404 on a save-path call renders as).
#
# We accept either ``access_token`` query param or ``Authorization: Bearer``.
# coolwsd uses the query param.
#
# PutRelativeFile ("Save As" → a new stored file) is implemented for the
# OWNER only.  For share-link callers it is refused (501) and CheckFileInfo
# advertises ``UserCanNotWriteRelative=True`` for them, so a guest can never
# create an owner-owned file — the two checks are belt-and-suspenders.
# ---------------------------------------------------------------------------

# In-memory advisory locks, keyed by file_id → opaque lock string chosen by
# the client (coolwsd).  A WOPI lock's lifetime is a single edit session; if
# the container restarts every session is torn down anyway, so there is
# nothing to persist.  hypercorn runs one worker on one event loop and no
# lock handler awaits between reading and mutating this dict, so plain dict
# operations are atomic — no mutex needed.
_locks: dict[str, str] = {}


def _lock_header() -> str:
    return request.headers.get("X-WOPI-Lock", "")


def _lock_conflict(file_id: str) -> Response:
    """WOPI lock-conflict response: 409 carrying the *current* lock so the
    caller can see who holds it.  An empty header means the file is unlocked.
    """
    resp = Response(status=409)
    resp.headers["X-WOPI-Lock"] = _locks.get(file_id, "")
    return resp


def _resolve_wopi_caller(file_id: str) -> dict[str, Any]:
    """Validate the WOPI access_token query param and return the caller's
    effective permissions and identity for ``file_id``.

    The caller is one of:
      - the zone owner (token == WOPI_ACCESS_TOKEN, full read+write access)
      - a share-link recipient (token matches an active row in `shares`)

    Returns a dict ``{can_write, user_id, user_friendly_name}`` or aborts
    401/403 if the token is unknown / scoped to a different file / the
    share-mode forbids what the caller is trying to do (the caller checks
    the mode against the request method they're handling).
    """
    token = request.args.get("access_token", "")
    if not token:
        abort(401, description="missing wopi access_token")

    if token == WOPI_ACCESS_TOKEN:
        return {
            "can_write": True,
            "user_id": "owner",
            "user_friendly_name": "Owner",
            "share_mode": None,  # owner is implicitly all-modes
        }

    share = get_share(token)
    if share is None:
        abort(401, description="invalid or expired wopi access_token")
    if share["file_id"] != file_id:
        # A share token is bound to a single file; reject cross-file misuse.
        abort(403, description="share token is for a different document")

    # Stable per-share UserId so co-editors get distinct cursor colors.
    # We hash the token rather than expose it directly so the UserId
    # itself isn't a credential equivalent.  First 12 hex chars is enough
    # entropy that two share-recipient cursors won't collide.
    user_id = "guest-" + hashlib.sha256(token.encode("ascii")).hexdigest()[:12]
    return {
        "can_write": share["mode"] == "edit",
        "user_id": user_id,
        "user_friendly_name": "Guest",
        "share_mode": share["mode"],
    }


@app.route("/wopi/files/<file_id>")
async def wopi_check_file_info(file_id: str):
    caller = _resolve_wopi_caller(file_id)
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    # The fields below are the ones coolwsd cares about.  Anything else in
    # the WOPI spec we leave unset (sensible defaults assumed).
    #
    # ``DisableExport`` / ``DisablePrint`` only apply meaningfully to view
    # shares — the file's bytes are recoverable from the editor anyway via
    # copy-paste, so view shares aren't a confidentiality boundary.  We
    # leave them False (default) to keep the editor experience clean.
    return jsonify(
        {
            "BaseFileName": row["name"],
            "Size": row["size"],
            "OwnerId": "owner",
            "UserId": caller["user_id"],
            "UserFriendlyName": caller["user_friendly_name"],
            "UserCanWrite": caller["can_write"],
            # Capability advertisement — this is how the host tells coolwsd
            # which write-family operations it may attempt.  Only advertise
            # what POST /wopi/files/<id> actually implements: rename, locks,
            # in-place update.  Save-As (PutRelativeFile) is NOT implemented,
            # so UserCanNotWriteRelative=True suppresses that affordance.
            "UserCanRename": caller["can_write"],
            "SupportsRename": True,
            "SupportsLocks": True,
            "SupportsGetLock": True,
            "SupportsUpdate": True,
            # Owner may Save-As (PutRelativeFile); share recipients may not,
            # which also keeps coolwsd from offering them the affordance.
            "UserCanNotWriteRelative": caller.get("share_mode") is not None,
            "DisableCopy": False,
            "DisableExport": False,
            "DisablePrint": False,
            "Version": str(int(row["mtime"] * 1000)),
        }
    )


@app.route("/wopi/files/<file_id>/contents")
async def wopi_get_file(file_id: str):
    # Reading is allowed for any caller that authenticates — a 'view'
    # share, an 'edit' share, and the owner all need to fetch the bytes.
    _resolve_wopi_caller(file_id)
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    body = _file_path(file_id).read_bytes()
    return Response(
        body,
        status=200,
        headers={"Content-Type": "application/octet-stream"},
    )


@app.route("/wopi/files/<file_id>/contents", methods=["POST"])
async def wopi_put_file(file_id: str):
    caller = _resolve_wopi_caller(file_id)
    if not caller["can_write"]:
        # View-share recipients hit this if coolwsd serializes a save
        # despite UserCanWrite=False.  Reject loudly so the editor
        # surfaces "your changes were not saved" rather than silently
        # dropping bytes.
        abort(403, description="this share is read-only")
    # Lock check: honour the lock only when one is actually held AND the
    # caller supplied a mismatching lock.  coolwsd holds its own lock and
    # echoes it here, so this passes; a save with no lock in play (the
    # historical behaviour) still goes through unchanged.
    current_lock = _locks.get(file_id)
    provided_lock = _lock_header()
    if current_lock is not None and provided_lock and provided_lock != current_lock:
        return _lock_conflict(file_id)
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    body = await request.get_data()
    dest = _file_path(file_id)
    # Atomic replace: write to a sibling tempfile, fsync, rename.  Otherwise
    # a kill mid-write would corrupt the document the user is editing.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("wb") as out:
        out.write(body)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(dest)
    update_file_size(file_id, len(body), time.time())
    # WOPI spec: 200 with empty body on PutFile success.  coolwsd accepts
    # JSON or empty; empty is canonical.
    return Response(status=200)


@app.route("/wopi/files/<file_id>", methods=["POST"])
async def wopi_files_op(file_id: str):
    """Dispatch the WOPI operations multiplexed onto POST /wopi/files/<id>.

    The operation is selected by the ``X-WOPI-Override`` header.  This is the
    complete closed set coolwsd can emit against the file endpoint; an
    override we don't recognise returns a logged 501 rather than a bare 404,
    so nothing can fail silently as an "expired session".
    """
    caller = _resolve_wopi_caller(file_id)
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    override = request.headers.get("X-WOPI-Override", "").upper()

    if override == "GET_LOCK":
        # Read-only: any authenticated caller may query the lock state.
        resp = Response(status=200)
        resp.headers["X-WOPI-Lock"] = _locks.get(file_id, "")
        return resp

    # Everything below mutates state or takes a lock — writers only.
    if not caller["can_write"]:
        abort(403, description="this share is read-only")

    if override == "LOCK":
        return _wopi_lock(file_id)
    if override == "UNLOCK":
        return _wopi_unlock(file_id)
    if override == "REFRESH_LOCK":
        return _wopi_refresh_lock(file_id)
    if override == "RENAME_FILE":
        return _wopi_rename(file_id, row)
    if override == "DELETE":
        return _wopi_delete(file_id)
    if override == "PUT_RELATIVE":
        if caller.get("share_mode") is not None:
            # Save-As from a share link would create an owner-owned file from
            # a guest action.  Refused — CheckFileInfo also advertises
            # UserCanNotWriteRelative for share callers, so coolwsd shouldn't
            # even offer it.
            app.logger.info("declining share-caller PUT_RELATIVE on file %s", file_id)
            return Response("Save As is not available for share links", status=501)
        return await _wopi_put_relative(row)

    app.logger.warning(
        "unimplemented WOPI override %r on file %s", override or "(none)", file_id
    )
    return Response(f"unimplemented WOPI override: {override or '(none)'}", status=501)


def _wopi_lock(file_id: str) -> Response:
    """LOCK, and UnlockAndRelock when X-WOPI-OldLock is present."""
    requested = _lock_header()
    old = request.headers.get("X-WOPI-OldLock")
    current = _locks.get(file_id)
    if old is not None:
        # UnlockAndRelock: the caller must currently hold ``old``.
        if current != old:
            return _lock_conflict(file_id)
        _locks[file_id] = requested
        return Response(status=200)
    # Plain lock.  Re-locking with the same string is an idempotent success.
    if current is None or current == requested:
        _locks[file_id] = requested
        return Response(status=200)
    return _lock_conflict(file_id)


def _wopi_unlock(file_id: str) -> Response:
    requested = _lock_header()
    current = _locks.get(file_id)
    if current is None or current != requested:
        return _lock_conflict(file_id)
    _locks.pop(file_id, None)
    return Response(status=200)


def _wopi_refresh_lock(file_id: str) -> Response:
    requested = _lock_header()
    current = _locks.get(file_id)
    if current is None or current != requested:
        return _lock_conflict(file_id)
    # No TTL is tracked, so refresh is a success no-op.
    return Response(status=200)


def _wopi_rename(file_id: str, row: dict[str, Any]) -> Response:
    """RENAME_FILE: change the stored filename, preserving the extension.

    coolwsd sends the new *base* name (no extension) in X-WOPI-RequestedName.
    The spec nominally UTF-7-encodes it, but Collabora sends UTF-8/ASCII in
    practice and our filename allowlist rejects anything exotic, so we treat
    it as text.  We re-append the file's current extension so a rename can
    never change the document type.
    """
    current = _locks.get(file_id)
    provided = _lock_header()
    if current is not None and provided and provided != current:
        return _lock_conflict(file_id)

    requested = request.headers.get("X-WOPI-RequestedName", "").strip()
    if not requested:
        abort(400, description="missing X-WOPI-RequestedName")
    ext = row["ext"]
    # Defensive: if the client included the current extension, drop it before
    # we re-append it.
    if ext and requested.lower().endswith("." + ext):
        requested = requested[: -(len(ext) + 1)]
    new_name = f"{requested}.{ext}" if ext else requested
    new_name = _validated_name(new_name)  # aborts 400 on a bad name

    rename_file_row(file_id, new_name, time.time())
    # Response body per spec: the new base name without extension.
    base = new_name[: -(len(ext) + 1)] if ext else new_name
    return jsonify({"Name": base})


def _wopi_delete(file_id: str) -> Response:
    current = _locks.get(file_id)
    provided = _lock_header()
    if current is not None and provided and provided != current:
        return _lock_conflict(file_id)
    dest = _file_path(file_id)
    try:
        dest.unlink()
    except FileNotFoundError:
        pass
    delete_file_row(file_id)
    _locks.pop(file_id, None)
    return Response(status=200)


def _name_exists(name: str) -> bool:
    with _connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM files WHERE name = ? LIMIT 1", (name,)
            ).fetchone()
            is not None
        )


def _dedupe_name(name: str) -> str:
    """Return ``name`` if unused, else ``stem (1).ext``, ``stem (2).ext`` …"""
    if not _name_exists(name):
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 1
    while True:
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        if not _name_exists(candidate):
            return candidate
        n += 1


async def _wopi_put_relative(row: dict[str, Any]) -> Response:
    """PUT_RELATIVE (Save-As): create a NEW stored file from the posted bytes
    and return a WOPI Url the editor switches its session to.  Owner-only
    (the dispatcher rejects share callers before we get here).

    Two mutually-exclusive modes per the WOPI spec:
      - X-WOPI-SuggestedTarget: a full name, or just an extension (".pdf").
        The host may adjust the name to dodge a collision.
      - X-WOPI-RelativeTarget: an exact name.  A collision is a 409 (unless
        X-WOPI-OverwriteRelativeTarget is true), carrying
        X-WOPI-ValidRelativeTarget with a free name.
    """
    suggested = request.headers.get("X-WOPI-SuggestedTarget", "").strip()
    relative = request.headers.get("X-WOPI-RelativeTarget", "").strip()
    overwrite = (
        request.headers.get("X-WOPI-OverwriteRelativeTarget", "").lower() == "true"
    )
    if bool(suggested) == bool(relative):
        # Exactly one of the two must be present.
        abort(400, description="need exactly one of Suggested/RelativeTarget")

    if suggested:
        if suggested.startswith("."):
            # Extension only → keep the source base name, swap the extension.
            base = row["name"][: -(len(row["ext"]) + 1)] if row["ext"] else row["name"]
            desired = f"{base}{suggested}"
        else:
            desired = suggested
        name = _dedupe_name(_validated_name(desired))
    else:
        name = _validated_name(relative)
        if _name_exists(name) and not overwrite:
            resp = Response(status=409)
            resp.headers["X-WOPI-ValidRelativeTarget"] = _dedupe_name(name)
            return resp

    body = await request.get_data()
    new_id = uuid.uuid4().hex
    dest = _file_path(new_id)
    # Atomic write, same as PutFile.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("wb") as out:
        out.write(body)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(dest)
    insert_file(new_id, name, len(body), _ext_of(name), time.time())

    # The Url must carry an access_token the editor can immediately reuse for
    # the new file; for the owner that's the in-process owner WOPI token.
    new_wopi_src = f"{PUBLIC_BASE}/wopi/files/{new_id}"
    return jsonify(
        {
            "Name": name,
            "Url": f"{new_wopi_src}?access_token={quote(WOPI_ACCESS_TOKEN, safe='')}",
            "HostEditUrl": f"{PUBLIC_BASE}/open/{new_id}",
        }
    )


# ---------------------------------------------------------------------------
# Share-link routes (recipient-facing; reachable without OpenHost login)
# ---------------------------------------------------------------------------
#
# Each share token resolves to one of three operations:
#   /share/v/<token>   — view-only editor
#   /share/e/<token>   — editable editor
#   /share/d/<token>   — direct download
#
# These routes MUST be reachable without the OpenHost zone-owner cookie,
# otherwise the recipient (who doesn't have your zone login) gets bounced
# to /login.  That's wired through ``[routing].public_paths`` in
# openhost.toml — every URL prefixed with ``/share/`` skips OpenHost's
# auth gate.  Inside the app we authenticate by the share token in the
# URL itself, which is mode-scoped and revocable.
#
# We deliberately keep the recipient experience cookie-less.  The editor
# iframe relies on the WOPI access_token query param (which equals the
# share token) for auth; nothing in the share flow plants a session
# cookie.  This means a single recipient can be in multiple share links
# at once and have each link behave according to its own scope.
# ---------------------------------------------------------------------------


def _resolved_share_or_404(token: str, expected_mode: str) -> dict[str, Any]:
    share = get_share(token)
    if share is None:
        # Generic 404 rather than a more specific "expired" / "revoked" so
        # an attacker probing tokens can't distinguish "wrong" from
        # "right-but-revoked".  Token shape is checked in get_share.
        abort(404)
    if share["mode"] != expected_mode:
        # Per-mode prefixes are deliberately distinct so /share/e/<t>
        # cannot be used with a view-only token even if someone hand-edits
        # the URL.  Still 404 to avoid leaking that the token exists.
        abort(404)
    return share


@app.route("/share/d/<token>")
async def share_download(token: str):
    """Download the raw document bytes via a download-mode share link."""
    share = _resolved_share_or_404(token, "download")
    row = get_file_row(share["file_id"])
    if row is None:
        abort(404)
    body = _file_path(row["id"]).read_bytes()
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    safe_name = row["name"].encode("ascii", errors="replace").decode("ascii")
    return Response(
        body,
        status=200,
        headers={
            "Content-Type": mime,
            "Content-Disposition": (
                f'attachment; filename="{safe_name}"; '
                f"filename*=UTF-8''{row['name'].replace(' ', '%20')}"
            ),
        },
    )


@app.route("/share/v/<token>")
async def share_view(token: str):
    """Open the document in the editor in read-only mode."""
    share = _resolved_share_or_404(token, "view")
    return await _render_share_editor(share, can_write=False)


@app.route("/share/e/<token>")
async def share_edit(token: str):
    """Open the document in the editor with write access."""
    share = _resolved_share_or_404(token, "edit")
    return await _render_share_editor(share, can_write=True)


async def _render_share_editor(share: dict[str, Any], *, can_write: bool):
    row = get_file_row(share["file_id"])
    if row is None:
        abort(404)
    if row["ext"] not in EDITABLE_EXTENSIONS:
        abort(400, description="this file type is not editable in Collabora")
    # WOPISrc points at our loopback (same reasoning as the owner-side
    # /open/<id> route).  The access_token IS the share token — the WOPI
    # handler validates it against the shares table and applies the
    # mode-scoped permissions.
    wopi_src = f"http://127.0.0.1:8080/wopi/files/{row['id']}"
    cool_url = (
        f"{PUBLIC_BASE}/browser/dist/cool.html"
        f"?WOPISrc={quote(wopi_src, safe='')}"
        f"&closebutton=false"
        f"&permission={'edit' if can_write else 'readonly'}"
    )
    return await render_template(
        "editor.html",
        file=row,
        cool_url=cool_url,
        access_token=share["token"],
        permission=("edit" if can_write else "readonly"),
    )


# ---------------------------------------------------------------------------
# Owner-facing share-management routes (gated by OpenHost zone-owner login)
# ---------------------------------------------------------------------------


@app.route("/manage/<file_id>")
async def manage_shares_page(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        abort(400, description="invalid id")
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    shares = list_shares_for_file(file_id)
    rendered = []
    now = time.time()
    for s in shares:
        letter = MODE_TO_LETTER[s["mode"]]
        share_url = f"{PUBLIC_BASE}/share/{letter}/{s['token']}"
        if s["expires_at"] is None:
            expires_human = "never"
        else:
            delta = s["expires_at"] - now
            if delta <= 0:
                expires_human = "expired"
            elif delta < 3600:
                expires_human = f"in {int(delta // 60)} min"
            elif delta < 86400:
                expires_human = f"in {int(delta // 3600)} h"
            else:
                expires_human = f"in {int(delta // 86400)} days"
        rendered.append(
            {
                **s,
                "share_url": share_url,
                "expires_human": expires_human,
            }
        )
    return await render_template(
        "manage.html",
        file=row,
        shares=rendered,
    )


@app.route("/manage/<file_id>/create", methods=["POST"])
async def create_share_route(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        abort(400, description="invalid id")
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    form = await request.form
    mode = form.get("mode", "")
    if mode not in MODE_TO_LETTER:
        abort(400, description="invalid mode")
    # View / edit shares only make sense for files Collabora can open.
    # Download shares work for any file.
    if mode in ("view", "edit") and row["ext"] not in EDITABLE_EXTENSIONS:
        abort(400, description="this file type cannot be opened in the editor")
    # TTL: form sends seconds or empty for "never".  Cap at 10 years to
    # keep the schema sane.
    raw_ttl = form.get("ttl_seconds", "")
    if raw_ttl == "":
        ttl: int | None = None
    else:
        try:
            ttl = max(0, min(int(raw_ttl), 10 * 365 * 24 * 3600))
        except ValueError:
            abort(400, description="invalid ttl")
    create_share(file_id, mode, ttl)
    return redirect(url_for("manage_shares_page", file_id=file_id))


@app.route("/manage/<file_id>/revoke", methods=["POST"])
async def revoke_share_route(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        abort(400, description="invalid id")
    form = await request.form
    token = form.get("token", "")
    if not token:
        abort(400, description="missing token")
    # Look up the share row directly (bypassing get_share's "active only"
    # filter) so revoking an already-expired or already-revoked row is a
    # no-op rather than a 404 — the manage UI surfaces those rows and
    # the user clicking Revoke on them shouldn't see an error.
    with _connect() as conn:
        row = conn.execute(
            "SELECT file_id FROM shares WHERE token = ?", (token,)
        ).fetchone()
    if row is None:
        abort(404)
    # Only allow the owner of this same file to revoke.  Cross-file
    # revocation is rejected as defence in depth — owner is already
    # OpenHost-authenticated, so this is paranoia, not a security
    # boundary.
    if row["file_id"] != file_id:
        abort(403, description="token belongs to a different file")
    revoke_share(token)
    return redirect(url_for("manage_shares_page", file_id=file_id))


@app.route("/manage/<file_id>/extend", methods=["POST"])
async def extend_share_route(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        abort(400, description="invalid id")
    form = await request.form
    token = form.get("token", "")
    raw_ttl = form.get("ttl_seconds", "")
    if not token:
        abort(400, description="missing token")
    if raw_ttl == "":
        ttl: int | None = None
    else:
        try:
            ttl = max(0, min(int(raw_ttl), 10 * 365 * 24 * 3600))
        except ValueError:
            abort(400, description="invalid ttl")
    # Cross-file scope check (same reasoning as revoke).
    with _connect() as conn:
        row = conn.execute(
            "SELECT file_id FROM shares WHERE token = ?", (token,)
        ).fetchone()
    if row is None:
        abort(404)
    if row["file_id"] != file_id:
        abort(403, description="token belongs to a different file")
    extend_share(token, ttl)
    return redirect(url_for("manage_shares_page", file_id=file_id))


# ---------------------------------------------------------------------------
# Reverse proxy → coolwsd
# ---------------------------------------------------------------------------
#
# Collabora's editor surface lives under a fixed set of path prefixes.
# We forward those (HTTP and WS) to the loopback coolwsd; everything else
# stays in this Quart app.
#
# Patterns cribbed from openhost/compute_space/web/proxy.py — same author,
# same code style, well-tested under similar load.
# ---------------------------------------------------------------------------

_PROXY_PREFIXES = ("/browser/", "/cool/", "/lool/", "/hosting/")


def _is_proxy_path(path: str) -> bool:
    return any(path.startswith(p) for p in _PROXY_PREFIXES)


async def _proxy_http(path: str) -> Response:
    raw_path = request.scope.get("raw_path")
    if raw_path is not None:
        forwarded_path = raw_path.decode("ascii")
    else:
        forwarded_path = path

    target_url = f"http://{COOLWSD_HOST}:{COOLWSD_PORT}{forwarded_path}"
    if request.query_string:
        target_url += f"?{request.query_string.decode('utf-8')}"

    excluded = {
        "host",
        "connection",
        "transfer-encoding",
        "accept-encoding",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
    }
    headers = {k: v for k, v in request.headers if k.lower() not in excluded}
    headers["X-Forwarded-For"] = request.remote_addr or ""
    # We're talking https on the public side; tell coolwsd so it builds
    # links accordingly.  Matches ssl.termination=true in the entrypoint.
    headers["X-Forwarded-Proto"] = "https"
    headers["X-Forwarded-Host"] = PUBLIC_HOST

    body = await request.get_data()
    timeout = httpx.Timeout(connect=10, read=600, write=600, pool=10)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            backend = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                cookies=dict(request.cookies),
                follow_redirects=False,
            )
    except httpx.ConnectError:
        return Response("Collabora backend not responding", status=502)
    except httpx.TimeoutException:
        return Response("Collabora backend timed out", status=504)
    except httpx.TransportError:
        return Response("Collabora backend disconnected", status=502)

    response_excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    response_headers = Headers()
    for key, value in backend.headers.multi_items():
        if key.lower() not in response_excluded:
            response_headers.add(key, value)

    return Response(backend.content, status=backend.status_code, headers=response_headers)


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_or_404(path: str):
    full = "/" + path
    if _is_proxy_path(full):
        return await _proxy_http(full)
    abort(404)


# Coolwsd has well-known root files we should also forward.
@app.route("/favicon.ico")
async def favicon():
    return await _proxy_http("/favicon.ico")


@app.route("/robots.txt")
async def robots():
    return await _proxy_http("/robots.txt")


# ---- WebSocket proxy -------------------------------------------------------


@app.websocket("/cool/<path:rest>")
async def ws_cool(rest: str):
    await _proxy_ws(f"/cool/{rest}")


@app.websocket("/lool/<path:rest>")
async def ws_lool(rest: str):
    # /lool is the older name; some clients still use it.  Forward verbatim.
    await _proxy_ws(f"/lool/{rest}")


async def _proxy_ws(path: str) -> None:
    raw_path = websocket.scope.get("raw_path")
    if raw_path is not None:
        forwarded_path = raw_path.decode("ascii")
    else:
        forwarded_path = path

    target_url = f"ws://{COOLWSD_HOST}:{COOLWSD_PORT}{forwarded_path}"
    if websocket.query_string:
        target_url += f"?{websocket.query_string.decode('utf-8')}"

    extra_headers: dict[str, str] = {}
    subprotocols: list[str] = []
    for key, value in websocket.headers:
        lower = key.lower()
        if lower == "sec-websocket-protocol":
            subprotocols = [s.strip() for s in value.split(",")]
        elif lower not in {
            "host",
            "connection",
            "upgrade",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
            "x-forwarded-for",
            "x-forwarded-proto",
            "x-forwarded-host",
        }:
            extra_headers[key] = value
    extra_headers["X-Forwarded-For"] = websocket.remote_addr or ""
    extra_headers["X-Forwarded-Proto"] = "https"
    extra_headers["X-Forwarded-Host"] = PUBLIC_HOST

    await websocket.accept()

    ws_kwargs: dict[str, Any] = {
        "additional_headers": extra_headers,
        "compression": None,
        "open_timeout": 10,
        "close_timeout": 5,
    }
    if subprotocols:
        ws_kwargs["subprotocols"] = subprotocols

    try:
        async with websockets.connect(target_url, **ws_kwargs) as backend:

            async def b2c() -> None:
                try:
                    async for msg in backend:
                        await websocket.send(msg)
                except Exception:
                    pass

            async def c2b() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        await backend.send(msg)
                except Exception:
                    pass

            tasks = [asyncio.ensure_future(b2c()), asyncio.ensure_future(c2b())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
    except Exception:
        # Backend unreachable — close the client cleanly.
        return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # Local dev only.  Production uses hypercorn launched by the entrypoint.
    app.run(host="0.0.0.0", port=8080)
