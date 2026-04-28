#!/bin/sh
# Wrapper around upstream /start-collabora-online.sh that:
#   1. Builds the public FQDN OpenHost would inject if it had a hostname env
#      var (it doesn't yet — see openhost/docs/manifest_spec.md).  We derive it
#      from OPENHOST_APP_NAME + OPENHOST_ZONE_DOMAIN, both of which the router
#      DOES inject.  ``server_name`` is what coolwsd publishes in its
#      ``/hosting/discovery`` payload, so it has to be the URL the WOPI host
#      will reach us at, not whatever the container thinks its own hostname is.
#
#   2. Forces SSL termination mode.  OpenHost terminates TLS in Caddy and
#      proxies plain HTTP to the container; coolwsd needs to know the upstream
#      scheme is https so its self-built links and HSTS logic stay correct.
#
#   3. Disables the capabilities-based jail.  Rootless podman cannot grant
#      ``CAP_SYS_ADMIN`` and OpenHost's seccomp profile is not customisable,
#      so the upstream ``mount_namespaces`` / ``capabilities`` jail can't
#      initialise.  We fall back to user-namespace isolation only.  See the
#      Dockerfile comment for the security trade-off.
#
#   4. Permits the OpenHost router as a WOPI host out of the box for smoke
#      testing.  Operators MUST add their real WOPI host (Nextcloud, etc.) to
#      ``WOPI_HOST_REGEX`` for production use.
#
# This script intentionally does no state-persisting work — coolwsd has no
# meaningful per-instance state besides its WOPI proof key, and we let the
# upstream image's default (regenerate on each container start) stand.

set -eu

ZONE_DOMAIN="${OPENHOST_ZONE_DOMAIN:-localhost}"
APP_NAME="${OPENHOST_APP_NAME:-collabora}"
SERVER_NAME="${APP_NAME}.${ZONE_DOMAIN}"

# WOPI hosts the editor will accept document load requests from.  Default is
# every host in the same zone domain so a co-located Nextcloud install works
# without manual config.  Override with WOPI_HOST_REGEX to allow remote WOPI
# hosts.  We escape the zone domain via sed (POSIX-portable; bash pattern
# substitution would fail on dash, which is debian:stable-slim's /bin/sh).
ZONE_DOMAIN_REGEX_ESCAPED="$(printf '%s' "${ZONE_DOMAIN}" | sed 's/\./\\./g')"
WOPI_HOST_REGEX="${WOPI_HOST_REGEX:-https://[^/]+\\.${ZONE_DOMAIN_REGEX_ESCAPED}}"

# coolwsd reads ``extra_params`` and appends it after its own argv.  The
# upstream script comments document this contract.
extra_params="
--o:server_name=${SERVER_NAME}
--o:ssl.enable=false
--o:ssl.termination=true
--o:net.proto=IPv4
--o:net.listen=any
--o:storage.wopi.alias_groups[@mode]=groups
--o:storage.wopi.alias_groups.group[1].host[@allow]=true
--o:storage.wopi.alias_groups.group[1].host=${WOPI_HOST_REGEX}
--o:security.capabilities=false
--o:security.seccomp=false
--o:logging.level=warning
"

export extra_params

exec /start-collabora-online.sh
