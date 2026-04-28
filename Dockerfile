# syntax=docker/dockerfile:1.4
#
# Collabora Online (CODE) backend, packaged for rootless podman / OpenHost.
#
# Upstream image:
#   https://hub.docker.com/r/collabora/code
# Source:
#   https://github.com/CollaboraOnline/online/tree/main/docker/from-packages
#
# Notes on rootless adaptation:
#   - Upstream runs ``coolwsd --o:ssl.enable=true`` by default and the entrypoint
#     auto-generates a self-signed cert on every start.  OpenHost terminates TLS
#     in Caddy and proxies plain HTTP, so we set ``DONT_GEN_SSL_CERT`` to skip
#     cert generation and ``ssl.enable=false`` + ``ssl.termination=true`` so
#     coolwsd's redirect/HSTS logic still treats the connection as secure.
#   - The standard Collabora jail uses ``CAP_SYS_ADMIN`` for ``mount`` /
#     ``pivot_root`` plus a custom seccomp profile.  Rootless podman provides
#     neither.  We disable capabilities-based jailing
#     (``security.capabilities=false``) and rely on user-namespace isolation +
#     OpenHost's ``no_new_privileges=true``.  Process isolation is weaker
#     than the upstream default; do not pair this with untrusted documents.

FROM collabora/code:25.04.9.4.1

# ``cool`` (uid 1001) is the runtime user baked into the upstream image.  Run
# the wrapper as that user so the existing /etc/coolwsd / /opt/cool ownership
# is honoured.
USER 0
COPY openhost-entrypoint.sh /openhost-entrypoint.sh
RUN chmod 0755 /openhost-entrypoint.sh
USER 1001

ENV DONT_GEN_SSL_CERT=1

# ``coolwsd`` listens on 9980; OpenHost's manifest declares the same.
EXPOSE 9980

ENTRYPOINT ["/openhost-entrypoint.sh"]
