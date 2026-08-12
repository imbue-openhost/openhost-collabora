# bottled-collabora

Online office suite (Writer / Calc / Impress) for your Cloud in a Bottle zone.
Upload or create documents, click to edit them in the browser, save back
to the same list.  No external file server needed — the file manager and
the Collabora editor backend ship together in one container.

Built on [Collabora Online (CODE)](https://www.collaboraonline.com/).

## What's inside

A single container running two processes:

- **coolwsd** — Collabora's editor backend, on `127.0.0.1:9980` (loopback only).
- **Quart** — a small Python web app on `0.0.0.0:8080` that serves:
  - `/`                            — the file list
  - `/upload`, `/new/<kind>`, `/delete/<id>`, `/download/<id>`, `/open/<id>` — file-management routes (owner only, gated by Cloud in a Bottle login)
  - `/manage/<id>`, `/manage/<id>/{create,revoke,extend}` — share-link administration (owner only)
  - `/share/v/<token>`, `/share/e/<token>`, `/share/d/<token>` — recipient-facing share routes (no Cloud in a Bottle login required; auth is the token)
  - `/wopi/files/<id>`, `/wopi/files/<id>/contents` — the WOPI host endpoints coolwsd talks to during edits
  - `/browser/`, `/cool/`, `/lool/`, `/hosting/`, `/favicon.ico`, `/robots.txt` — reverse-proxied to coolwsd (HTTP and WebSocket)

Everything reaches the user through the single `8080` port that Cloud in a Bottle
publishes.  `coolwsd` is loopback-only; the only way to reach the editor
from outside the container is through the Quart proxy.

## What works

- List, upload, download, delete documents.
- "+ New document / spreadsheet / presentation" creates a blank `.odt` /
  `.ods` / `.odp` and drops you straight into the editor.
- Click an editable file → opens the Collabora editor in an iframe.  Save
  in the editor → file in the list updates (size + mtime change).
- **Per-document share links.**  From the file list click *Share* → pick
  one of: *view-only*, *view & edit*, *download*.  Each click mints an
  unguessable, mode-scoped, revocable URL with a default 30-day expiry.
  Recipients open the link without a Cloud in a Bottle login.  Edit-share
  recipients editing the same file at the same time see each other's
  cursors live (Collabora's built-in real-time co-editing); each share
  link gets a stable distinct UserId so cursors are coloured per link.

## What deliberately doesn't work (yet)

- No folders, multi-select, sort, search, rename.
- No per-recipient identity.  Share links are fungible: if you mail one
  link to three people, the editor shows them all as "Guest" (with one
  shared UserId per link → one shared cursor colour across those three
  recipients).  Distinguishing recipients would require a real account
  system; Cloud in a Bottle is single-owner so that has to live inside this app,
  and it's out of scope for "barebones".
- No notification / email out.  The owner copies the share URL out of
  the manage page and sends it manually (Signal, email, etc.).
- No version history.  PutFile overwrites the document atomically; old
  bytes are gone.
- No file type other than the ones Collabora can edit (Writer / Calc /
  Impress family).  You can upload anything via `/upload` and download
  it via `/download/<id>`, but only office-document extensions show an
  "Open" link, and only office-document files can be view-/edit-shared
  (download shares work for any file type).

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
   Cloud in a Bottle provides neither, so this image disables both:
   `--o:security.capabilities=false`, `--o:security.seccomp=false`.
   Process isolation falls back to the Cloud in a Bottle user namespace plus
   `no_new_privileges=true`.  This is **weaker** than upstream's default:
   a LibreOffice document-rendering bug that escapes the per-document
   forkit could read other documents the same container has loaded.  Do
   not pair this app with documents from mutually-distrusting authors.
   For a single-user / single-tenant zone (you and your own files), this
   is the same threat model as running LibreOffice locally.

2. **Authentication has two layers: Cloud in a Bottle cookie + share tokens.**
   - **Owner routes** (`/`, `/upload`, `/new`, `/open`, `/manage`, ...)
     are gated by Cloud in a Bottle's regular zone-owner login.
   - **Share routes** (`/share/v/<token>`, `/share/e/<token>`,
     `/share/d/<token>`) are reachable without a Cloud in a Bottle cookie; the
     token in the URL is itself the credential.  Tokens are 24-byte
     URL-safe random strings, scoped to a single file and a single mode
     (view / edit / download), with a default 30-day expiry, revocable
     from the manage page.
   - **WOPI endpoints** accept either the in-process owner WOPI token
     (regenerated on every container start) or a share token; coolwsd
     supplies whichever was baked into the editor URL it was launched
     with.  Share tokens get `UserCanWrite=False` for view shares,
     `UserCanWrite=True` for edit shares.

   What that means in practice:
   - A view-share link is a "see this document" capability; the holder
     can read the contents and copy text out of the editor (no DRM —
     this isn't trying to be).
   - An edit-share link is a "co-edit this document" capability; the
     holder can change the bytes.  All edits go through the same
     atomic-replace WOPI handler, so concurrent saves either succeed
     cleanly or fail loudly.
   - A download-share link is a "fetch the bytes once, in the original
     format" capability.  It's the simplest kind to revoke because the
     recipient has to use it before you revoke it; once the bytes are
     downloaded they're out of your hands.
   - Anyone who learns a share URL gets the access it grants until you
     revoke it.  Treat URLs accordingly: paste into Signal, not Twitter.

## Resources

The defaults are 2 GB RAM / 2 CPUs.  LibreOffice's per-document RAM
appetite is real; bump higher under heavy concurrent editing or large
spreadsheets.

## Limitations inherited from Cloud in a Bottle

- `[resources].gpu = true` is not honoured by the Cloud in a Bottle router (it
  stores the field but never adds the device flag).
- `--shm-size` cannot be configured via the Cloud in a Bottle manifest; the
  container uses the rootless-podman default (64 MiB).  Sufficient for
  typical document workloads.
- Logs are not rotated by Cloud in a Bottle; the container's stdout/stderr append
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
    ├── server.py                      # Quart UI + WOPI host + share tokens + reverse proxy
    └── templates/
        ├── index.html                 # file list
        ├── editor.html                # iframe shell that POSTs to coolwsd
        └── manage.html                # per-document share-link admin page
```

## Upstream

- Collabora image: <https://hub.docker.com/r/collabora/code>
- Source: <https://github.com/CollaboraOnline/online>
- WOPI protocol:
  <https://learn.microsoft.com/en-us/microsoft-365/cloud-storage-partner-program/online/wopi-rest-apis>
- Collabora SDK:
  <https://sdk.collaboraonline.com/docs/installation/Configuration.html>
