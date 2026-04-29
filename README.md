# openhost-collabora

Online office suite (Writer / Calc / Impress) for your OpenHost zone.
Upload or create documents, click to edit them in the browser, save back
to the same list.  No external file server needed — the file manager and
the Collabora editor backend ship together in one container.

Built on [Collabora Online (CODE)](https://www.collaboraonline.com/).

## What's inside

A single container running two processes:

- **coolwsd** — Collabora's editor backend, on `127.0.0.1:9980` (loopback only).
- **Quart** — a small Python web app on `0.0.0.0:8080` that serves:
  - `/`                            — the file list
  - `/upload`, `/new/<kind>`, `/delete/<id>`, `/download/<id>`, `/open/<id>` — file-management routes
  - `/wopi/files/<id>`, `/wopi/files/<id>/contents` — the WOPI host endpoints coolwsd talks to during edits
  - `/browser/`, `/cool/`, `/lool/`, `/hosting/`, `/favicon.ico`, `/robots.txt` — reverse-proxied to coolwsd (HTTP and WebSocket)

Everything reaches the user through the single `8080` port that OpenHost
publishes.  `coolwsd` is loopback-only; the only way to reach the editor
from outside the container is through the Quart proxy.

## What works

- List, upload, download, delete documents.
- "+ New document / spreadsheet / presentation" creates a blank `.odt` /
  `.ods` / `.odp` and drops you straight into the editor.
- Click an editable file → opens the Collabora editor in an iframe.  Save
  in the editor → file in the list updates (size + mtime change).

## What deliberately doesn't work (yet)

- No folders, multi-select, sort, search, rename.
- No multi-user / sharing / per-document permissions.  Whoever has the
  OpenHost zone-owner cookie sees and edits everything.
- No version history.  PutFile overwrites the document atomically; old
  bytes are gone.
- No file type other than the ones Collabora can edit (Writer / Calc /
  Impress family).  You can upload anything via `/upload` and download
  it via `/download/<id>`, but only office-document extensions show an
  "Open" link.

## Persistent state

Lives under `OPENHOST_APP_DATA_DIR`:

```
files/<uuid>     — raw document bytes, one per file
index.db         — sqlite metadata (filename, size, ext, mtime)
```

The blank-document templates baked into the image (under
`/opt/collabora-blank-templates/`) are read-only and not user data; they
get copied into `files/` as new documents are created.

## Security caveats — please read

Two things to know:

1. **Process isolation is weaker than upstream.**  The standard Collabora
   deployment uses a `CAP_SYS_ADMIN` mount jail plus a custom seccomp
   profile to isolate document-rendering child processes.  Rootless
   OpenHost provides neither, so this image disables both:
   `--o:security.capabilities=false`, `--o:security.seccomp=false`.
   Process isolation falls back to the OpenHost user namespace plus
   `no_new_privileges=true`.  This is **weaker** than upstream's default:
   a LibreOffice document-rendering bug that escapes the per-document
   forkit could read other documents the same container has loaded.  Do
   not pair this app with documents from mutually-distrusting authors.
   For a single-user / single-tenant zone (you and your own files), this
   is the same threat model as running LibreOffice locally.

2. **Authentication is the OpenHost zone-owner cookie.**  All UI routes
   are gated by OpenHost's regular login.  The WOPI endpoints
   (`/wopi/files/<id>`, `/wopi/files/<id>/contents`) are gated by an
   in-process random `access_token` instead — they have to be reachable
   without an OpenHost cookie because coolwsd inside the same container
   calls them with no session of its own.  That token is generated fresh
   on every container start.  It's not a security boundary against an
   attacker who can already MITM the loopback interface, but it does
   reject accidental WOPI calls from unrelated apps on the same host.

## Resources

The defaults are 2 GB RAM / 2 CPUs.  LibreOffice's per-document RAM
appetite is real; bump higher under heavy concurrent editing or large
spreadsheets.

## Limitations inherited from OpenHost

- `[resources].gpu = true` is not honoured by the OpenHost router (it
  stores the field but never adds the device flag).
- `--shm-size` cannot be configured via the OpenHost manifest; the
  container uses the rootless-podman default (64 MiB).  Sufficient for
  typical document workloads.
- Logs are not rotated by OpenHost; the container's stdout/stderr append
  unbounded to `docker.log` until the next reload.

## Configuration

| Env var            | Default                         | Purpose                                                   |
|--------------------|---------------------------------|-----------------------------------------------------------|
| `WOPI_HOST_REGEX`  | `https://[^/]+\.{zone-domain}`  | Hosts coolwsd will accept WOPI document-load calls from.  |

`OPENHOST_APP_NAME` and `OPENHOST_ZONE_DOMAIN` are read automatically.

## Layout

```
.
├── Dockerfile                         # collabora/code base + python venv + UI
├── openhost.toml                      # OpenHost manifest (port 8080)
├── openhost-entrypoint.sh             # supervises coolwsd + hypercorn
├── scripts/
│   └── generate-blank-templates.sh    # build-time: creates blank.{odt,ods,odp}
└── app/
    ├── server.py                      # Quart UI + WOPI host + reverse proxy
    └── templates/
        ├── index.html                 # file list
        └── editor.html                # iframe shell that POSTs to coolwsd
```

## Upstream

- Collabora image: <https://hub.docker.com/r/collabora/code>
- Source: <https://github.com/CollaboraOnline/online>
- WOPI protocol:
  <https://learn.microsoft.com/en-us/microsoft-365/cloud-storage-partner-program/online/wopi-rest-apis>
- Collabora SDK:
  <https://sdk.collaboraonline.com/docs/installation/Configuration.html>
