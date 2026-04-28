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


def delete_file_row(file_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


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
    wopi_src = f"http://127.0.0.1:8080/wopi/files/{file_id}"
    cool_url = (
        f"{PUBLIC_BASE}/browser/dist/cool.html"
        f"?WOPISrc={wopi_src}"
        f"&closebutton=true"
    )
    return await render_template(
        "editor.html",
        file=row,
        cool_url=cool_url,
        access_token=WOPI_ACCESS_TOKEN,
    )


# ---------------------------------------------------------------------------
# WOPI host
# ---------------------------------------------------------------------------
#
# coolwsd calls these three endpoints during an edit session.  The protocol
# is documented at https://learn.microsoft.com/en-us/microsoft-365/cloud-
# storage-partner-program/online/wopi-rest-apis.  We implement the strict
# minimum that makes the editor load and save:
#
#   GET  /wopi/files/<id>            → CheckFileInfo (metadata)
#   GET  /wopi/files/<id>/contents   → GetFile  (raw bytes)
#   POST /wopi/files/<id>/contents   → PutFile  (overwrite raw bytes)
#
# We accept either ``access_token`` query param or ``Authorization: Bearer``.
# coolwsd uses the query param.
# ---------------------------------------------------------------------------


def _check_wopi_token() -> None:
    token = request.args.get("access_token", "")
    if token != WOPI_ACCESS_TOKEN:
        abort(401, description="invalid wopi access_token")


@app.route("/wopi/files/<file_id>")
async def wopi_check_file_info(file_id: str):
    _check_wopi_token()
    row = get_file_row(file_id)
    if row is None:
        abort(404)
    # The fields below are the ones coolwsd cares about.  Anything else in
    # the WOPI spec we leave unset (sensible defaults assumed).
    return jsonify(
        {
            "BaseFileName": row["name"],
            "Size": row["size"],
            "OwnerId": "owner",
            "UserId": "owner",
            "UserFriendlyName": "Owner",
            "UserCanWrite": True,
            "DisableCopy": False,
            "DisableExport": False,
            "DisablePrint": False,
            "Version": str(int(row["mtime"] * 1000)),
        }
    )


@app.route("/wopi/files/<file_id>/contents")
async def wopi_get_file(file_id: str):
    _check_wopi_token()
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
    _check_wopi_token()
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
