#!/bin/bash
# Run inside the Collabora image at build time.  Uses the bundled
# Collabora Office (libreoffice headless) to produce three minimal seed
# documents that the UI's "+ New" buttons copy into the user's app_data.
#
# We generate at build time rather than runtime because:
#   - The seeds are deterministic (no per-instance customization needed).
#   - Doing it at runtime would require LO to be running when the user
#     hits "New", adding 2-3 seconds of latency per click.
#   - Shipping a couple of ~3 KB fixtures in the image is cheap.
#
# Output: /opt/collabora-blank-templates/blank.{odt,ods,odp}
#
# We feed LO an empty input from /dev/null and ask it to convert to the
# three target formats.  Each command exits non-zero loud so Dockerfile
# build fails fast if the bundled LO binary path moved.

set -euo pipefail

OUT=/opt/collabora-blank-templates
mkdir -p "$OUT"

# Seed an empty plaintext file; LO converts it cleanly to odt/ods/odp.
SEED=/tmp/blank-seed.txt
: > "$SEED"

# coolwsd's LibreOffice install lives under /opt/collaboraoffice;
# /usr/bin/libreoffice is a thin wrapper that may not exist in the
# Collabora CODE image.  Resolve by globbing.
SOFFICE=$(find /opt/collaboraoffice* -name soffice -type f -executable 2>/dev/null | head -1 || true)
if [[ -z "${SOFFICE}" ]]; then
  echo "no soffice binary found under /opt/collaboraoffice*" >&2
  exit 1
fi

cd "$OUT"
export HOME=/tmp
"$SOFFICE" --headless --convert-to odt "$SEED" --outdir "$OUT"
"$SOFFICE" --headless --convert-to ods "$SEED" --outdir "$OUT"
"$SOFFICE" --headless --convert-to odp "$SEED" --outdir "$OUT"

# soffice names outputs after the input (blank-seed.odt, etc).  Rename to
# the names the Quart app expects so we don't have to special-case in code.
mv "$OUT/blank-seed.odt" "$OUT/blank.odt"
mv "$OUT/blank-seed.ods" "$OUT/blank.ods"
mv "$OUT/blank-seed.odp" "$OUT/blank.odp"

ls -la "$OUT"
