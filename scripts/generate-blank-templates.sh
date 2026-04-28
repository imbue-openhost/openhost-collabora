#!/bin/bash
# Run inside the Collabora image at build time.  Produces three minimal
# OpenDocument seed files that the UI's "+ New" buttons copy into the
# user's app_data when creating a fresh document.
#
# Output: /opt/collabora-blank-templates/blank.{odt,ods,odp}
#
# We initially tried ``soffice --convert-to`` but text→ods/odp filters
# don't exist in headless LibreOffice; ``soffice macro://`` invocations
# of ``loadComponentFromURL`` are flaky across LO builds.  The simplest
# hermetic approach is to write the OpenDocument zips by hand: each is a
# zipfile containing mimetype, manifest.xml, content.xml, styles.xml,
# meta.xml.  An empty Writer/Calc/Impress document fits in ~1 KB; LO
# accepts these as legitimate documents and Collabora opens them in the
# corresponding editor mode.

set -euo pipefail

OUT=/opt/collabora-blank-templates
mkdir -p "$OUT"

python3 - <<'PYEOF'
import os
import zipfile

OUT = "/opt/collabora-blank-templates"
os.makedirs(OUT, exist_ok=True)

MANIFEST = '''<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
  <manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="{mime}"/>
  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
  <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
  <manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
'''

STYLES = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.2"/>
'''

META = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
                       xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
                       office:version="1.2">
  <office:meta>
    <meta:generator>openhost-collabora blank-template</meta:generator>
  </office:meta>
</office:document-meta>
'''

CONTENT_TEMPLATES = {
    "odt": (
        "application/vnd.oasis.opendocument.text",
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
        "<office:body><office:text><text:p/></office:text></office:body>"
        "</office:document-content>",
    ),
    "ods": (
        "application/vnd.oasis.opendocument.spreadsheet",
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" office:version="1.2">'
        "<office:body><office:spreadsheet>"
        '<table:table table:name="Sheet1"><table:table-row><table:table-cell/></table:table-row></table:table>'
        "</office:spreadsheet></office:body>"
        "</office:document-content>",
    ),
    "odp": (
        "application/vnd.oasis.opendocument.presentation",
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" office:version="1.2">'
        "<office:body><office:presentation>"
        '<draw:page draw:name="Slide1"/>'
        "</office:presentation></office:body>"
        "</office:document-content>",
    ),
}

for ext, (mime, content_xml) in CONTENT_TEMPLATES.items():
    path = os.path.join(OUT, f"blank.{ext}")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        # OpenDocument requires the "mimetype" entry to be first AND
        # stored uncompressed, so the magic-byte detector at the file
        # head can identify the document family.
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, mime)
        zf.writestr("META-INF/manifest.xml", MANIFEST.format(mime=mime))
        zf.writestr("content.xml", content_xml)
        zf.writestr("styles.xml", STYLES)
        zf.writestr("meta.xml", META)
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")
PYEOF

ls -la "$OUT"
