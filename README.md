# openhost-collabora

[Collabora Online (CODE)](https://www.collaboraonline.com/) packaged for
OpenHost.  Serves the in-browser office editor (Writer / Calc / Impress) over
the WOPI protocol.

## What this is — and what it isn't

Collabora Online is a **backend** for an editor iframe.  It does not manage
files itself; documents live on a separate **WOPI host** (Nextcloud, ownCloud,
EtherCalc, a custom WOPI server, ...).  The WOPI host calls Collabora's
`/hosting/discovery` endpoint to learn which file extensions are editable, then
embeds the editor in a `<iframe src=".../cool/<doc-url>?WOPISrc=...">`.

So this app on its own is not a usable office suite.  Open
`https://collabora.{your-zone}/` directly and you get either a "OK" page or
the admin console.  To actually edit a document you need to point a WOPI host
at this URL.

The minimal happy path:

1. Deploy `collabora` (this app) to your OpenHost zone.
2. Deploy a WOPI-capable file server (e.g. Nextcloud + the
   "Nextcloud Office" / "Collabora Online" app) in the same zone.
3. In the WOPI host's settings, set the Collabora server URL to
   `https://collabora.{your-zone}` and enable SSL termination.
4. Open a `.docx` from the WOPI host's UI; it should load the Collabora
   editor in an iframe.

## Smoke test

Without a WOPI host you can still confirm the backend is up:

```sh
curl -s https://collabora.{your-zone}/hosting/discovery | head -20
curl -s https://collabora.{your-zone}/hosting/capabilities
```

`/hosting/discovery` returns an XML doc listing supported MIME types.
`/hosting/capabilities` returns a small JSON capability descriptor.  Both
require nothing more than the backend being healthy.

## WOPI host allowlist

Collabora rejects document-load requests from unknown WOPI hosts.  By default
this image accepts any `https://*.{zone-domain}` host so a sibling app in the
same OpenHost zone works automatically.  Override with the `WOPI_HOST_REGEX`
env var (set in the OpenHost dashboard) for remote WOPI hosts:

```
WOPI_HOST_REGEX=https://(my-nextcloud\\.example\\.com|other-host\\.example\\.org)
```

## Security caveats — read this

The standard Collabora deployment uses a `CAP_SYS_ADMIN` mount jail plus a
custom seccomp profile to isolate document-rendering child processes.
**Rootless OpenHost provides neither**, so this image disables those features:

- `--o:security.capabilities=false`
- `--o:security.seccomp=false`

Process isolation falls back to the OpenHost user namespace plus
`no_new_privileges=true`.  This is **weaker** than upstream's default: a
LibreOffice document-rendering bug that escapes the per-document forkit could
read other documents the same container has loaded.  Do not pair this app
with a WOPI host that serves untrusted documents to mutually-distrusting users.

For the single-user / single-tenant zone case (you and your own files), this
is the same threat model as running LibreOffice locally.

## Configuration

| Env var            | Default                                                | Purpose                                                              |
|--------------------|--------------------------------------------------------|----------------------------------------------------------------------|
| `WOPI_HOST_REGEX`  | `https://[^/]+\.{zone-domain}`                         | Regex matched against WOPI host URLs presented in `WOPISrc=` query.  |

`OPENHOST_APP_NAME` and `OPENHOST_ZONE_DOMAIN` are read automatically to
build the `server_name` Collabora publishes in its discovery payload.

## Resources

The defaults (1 GB RAM, 1 CPU) are enough for a few concurrent documents.
Bump `[resources].memory_mb` if you expect heavy concurrent editing or
large `.xlsx` workbooks; LibreOffice's per-document RAM appetite is real.

## Limitations

- `[resources].gpu = true` is not honoured by OpenHost (router code stores it
  but never adds the device flag), so any GPU acceleration the upstream image
  expects is unavailable.
- `--shm-size` cannot be configured via the OpenHost manifest; the container
  runs with the rootless-podman default (64 MiB).  This is enough for typical
  document workloads but may be tight under heavy spreadsheet recalc.
- The WOPI proof key is regenerated on every container restart (upstream
  default).  WOPI hosts that cache the proof key will need to re-fetch it
  after every redeploy — most do this transparently from
  `/hosting/discovery`.

## Upstream

- Image: <https://hub.docker.com/r/collabora/code>
- Source: <https://github.com/CollaboraOnline/online>
- Configuration reference:
  <https://sdk.collaboraonline.com/docs/installation/Configuration.html>
