# dittopdf

Make one PDF carry another PDF's metadata and document properties, using
[pikepdf](https://github.com/pikepdf/pikepdf).

dittopdf has two front ends:

- a **web application** that inspects the original PDF in full, compares it with
  the second PDF, lets you review and edit every value before copying, writes a
  new output PDF, and then re-inspects that output to show what matches;
- a small **CLI / Python API** that copies only the Info dictionary and XMP packet.

## Install

```bash
uv sync            # or: pip install .
```

## Web application

```bash
dittopdf-web                      # http://127.0.0.1:5000
dittopdf-web --host 0.0.0.0 --port 8080
```

For production, serve `dittopdf.web:create_app()` with any WSGI server and set
`DITTOPDF_SECRET_KEY` (needed when running more than one worker).

### Workflow

1. **Upload the original PDF.** It's inspected and every property is listed, with
   absent properties shown as absent. The uploaded filename is shown separately
   from the metadata inside the PDF.
2. **Upload the second PDF.** It's inspected the same way.
3. **Compare.** Each property is shown side by side as *Same*, *Different*,
   *Only in original*, *Only in second PDF*, *Absent in both* or *Unable to
   inspect*, with its copy classification and the reason when it can't be copied.
4. **Review & edit.** Every copyable property starts as the original's value. You
   can change any field on its own; edited fields are marked *Manual override*.
   You can also keep the second PDF's value or remove the property. The controls
   match the value type: date fields with a picker, name fields with drop-downs,
   a typed JSON editor with a tree view for arrays and dictionaries, an XML editor
   that checks the XMP is well-formed, one-item-per-line lists for XMP bags and
   sequences, and hex for binary strings. **Copy all original metadata** resets
   every field to its default. Document-wide choices are made under *Output
   options* (see below).
5. **Result.** A new PDF is written, and neither upload is ever modified. The
   output is inspected again and compared with both inputs. Properties are
   grouped as *matches the original*, *necessarily or deliberately differs*,
   *could not be reproduced* and *still different (unexpected)*. A full copy
   report lists what was copied, reconstructed, overridden, kept, removed,
   regenerated, failed or read-only. The page says the output is **not**
   byte-for-byte identical to the original, unless it really is.

### What is inspected

| Area | Contents |
|---|---|
| Document Info | standard keys, any private keys (`DocChecksum`, `SourceModified`, `Company`, `GTS_PDFX*`, `PTEX.Fullbanner`, `AAPL:Keywords`, …), PDF dates |
| XMP | the complete packet, plus every property in every namespace present: Dublin Core, XMP Basic, Rights, Media Management, Adobe PDF, Paged-Text, Job Ticket, Dynamic Media, PDF/A (and extension schemas), PDF/X, PDF/UA, PDF/VT, EXIF, TIFF, Photoshop, Camera Raw, IPTC, PLUS and unknown ones. Structures (`stRef`, `stEvt`, `stVer`, `stJob`, `stDim`, `stFnt`) are shown nested |
| Trailer | `/Size /Root /Info /ID /Encrypt /Prev /XRefStm /DocChecksum`, extra keys, cross-reference stream fields |
| Catalog | every key in the checklist, with `/ViewerPreferences`, `/MarkInfo` and `/Names` subtrees broken out |
| Structure | header vs catalog version, page count and sizes, tagging, encryption details, linearization, object streams, compression, object count, revisions and the `/Prev` chain (read from the bytes), qpdf repair warnings |
| Pages | every page key, with inherited boxes and rotation resolved |
| Annotations | common and markup keys, with popup and reply relationships resolved |
| Signatures | signature dictionary, byte-range coverage, certificates (subject, issuer, validity, serial, fingerprints, key and signature algorithms) |
| Fonts | font dictionary, descriptor, embedded program (fontTools: name table, version, revision, tables, glyph count; CFF and Type 1 headers), subset prefix, usage per page |
| Images | image dictionary, encoded size and hash, placement and **effective DPI** (from the content-stream transforms), per-image XMP, EXIF/GPS/IPTC/ICC in JPEG data, inline images |
| Embedded files | file specs, stream params, MD5 checksum verification, portfolio collection schema |
| PieceInfo | document, page and form-XObject application data, including unknown applications |
| Output intents | all fields plus the embedded ICC profile (class, colour space, description) |
| Measurement & geospatial | viewports, `/Measure` (RL and GEO: scale, units, bounds, coordinate system, datum) |
| Document parts | the `/DPartRoot` hierarchy with page ranges and `/DPM` metadata |
| Legacy | Info-only metadata, single-element `/ID`, legacy keys, `/SpiderInfo` |
| Filesystem & transport | upload filename, browser-reported modification time, content type. These are clearly marked as outside the PDF. Creation and access times, xattrs, Finder tags, `Zone.Identifier` and mail headers are not sent by browsers and are reported as unavailable |

### Copy classification

Every property is classified, and the classification is shown in the UI:

| Class | Examples | How it's handled |
|---|---|---|
| **Directly copyable** | Info values, viewer preferences, page layout/mode, language, page boxes, rotation, comment author/text/dates | value copied as-is (unchanged values keep their exact bytes) |
| **Copyable with reconstruction** | XMP properties, outlines, named destinations, page labels, open action, embedded files, output intents, PieceInfo, document parts, `/ID` | deep-copied into the output, with **page references remapped to the same page number** and shared/cyclic objects kept consistent |
| **Read-only / diagnostic** | fonts, images, signatures, filesystem data | shown, never modified |
| **Must be regenerated** | `/Size`, `/Prev`, xref offsets, object count, file hash | produced by the PDF writer |
| **Cannot reliably be reproduced** | structure tree, optional content, thumbnails, `/Perms`, `/DSS`, single-element `/ID` | kept from the second PDF unless you opt in |

What this means in practice:

- **Signatures are never copied.** A signature covers the exact bytes of the signed
  file. If you replace annotations, signature fields are copied *unsigned*. If the
  second PDF is signed, you're warned that writing a new file invalidates its
  signatures.
- **References to pages that don't exist** in the second PDF (for example, a
  bookmark to page 3 of a 2-page output) become null, and the report says so.
- **The XMP packet** is copied byte for byte when unchanged. Edits are applied
  with lxml, so unrelated properties and custom schemas are preserved. pikepdf's
  automatic XMP rewrite during save is disabled, and the written packet is
  verified.
- **`/ID`.** qpdf keeps the first element and regenerates the second. By default,
  dittopdf then reproduces the second element exactly using a same-length
  substitution, and verifies the result. This isn't possible for encrypted
  output.
- **Encryption.** Passwords can't be read out of a PDF. When the original is
  encrypted, the output defaults to the original's revision and permissions,
  with the user password you opened it with and a random owner password (shown
  in the report) unless you set one.

### Output options

| Option | Choices |
|---|---|
| Annotations & forms | keep the second PDF's (default; comment metadata is copied onto matching annotations) · replace with the original's · add the original's |
| Structure tree | keep the second PDF's (default) · copy the original's (only correct if the content is the same) |
| File identifier | reproduce both elements (default) · keep the first, regenerate the second |
| Metadata consistency | write Info overrides into the matching XMP properties (default on) |
| File structure | header version (defaults to the original's), linearization and object streams (both default to match the original), compress uncompressed streams |
| Encryption | none · the original's method and permissions · keep the second PDF's |

### Security

- Each session gets an isolated temporary directory (mode `0700`) named by a
  random token. Files are stored under fixed names (`original.pdf`,
  `second.pdf`, `output.pdf`). Uploaded filenames and PDF metadata are never
  used as paths, and the download name is sanitized.
- Uploads are checked for a `%PDF-` header before qpdf opens them.
  Malformed files produce a clear error, and damage that qpdf repairs is
  reported.
- Upload size is limited (`MAX_CONTENT_LENGTH`). All POSTs are CSRF-protected,
  and XML is parsed without entity resolution or network access.
- Workspaces expire automatically and can be deleted with **Start over**.
  Passwords for encrypted uploads are kept only in the session's workspace
  until it's deleted.

### Configuration

| Variable | Default | |
|---|---|---|
| `DITTOPDF_SECRET_KEY` | random per process | session signing key |
| `DITTOPDF_MAX_UPLOAD_MB` | `100` | per-request upload limit |
| `DITTOPDF_WORK_DIR` | `$TMPDIR/dittopdf-work` | root of the per-session workspaces |
| `DITTOPDF_WORKSPACE_TTL` | `3600` | seconds before an idle workspace is deleted |
| `DITTOPDF_MAX_DETAIL_PAGES` | `200` | pages (and their annotations) inspected individually |

### Architecture

```text
src/dittopdf/
├── services/          # no Flask imports
│   ├── inspector.py   # full inspection → ordered entries with stable ids
│   ├── xmp.py         # XMP parsing / editing (lxml)
│   ├── pdfobj.py      # pikepdf ↔ typed JSON, display, canonical digests
│   ├── rawfile.py     # byte-level structure: header, revisions, /Prev chain
│   ├── resources.py   # font/image discovery, image placement & DPI
│   ├── fonts.py       # embedded font programs (fontTools)
│   ├── signatures.py  # signature fields and certificates (cryptography)
│   ├── model.py       # entry model and copy classifications
│   ├── comparison.py  # match entries, statuses, copy feasibility
│   ├── transplant.py  # deep copy with page/annotation remapping
│   ├── copier.py      # plan validation, applying the plan, saving
│   └── report.py      # re-inspection and verification of the output
└── web/               # Flask app: routes, workspace, templates, static
```

## CLI (Info dictionary + XMP only)

```bash
dittopdf SOURCE.pdf TARGET.pdf                 # writes TARGET.ditto.pdf
dittopdf SOURCE.pdf TARGET.pdf -o out.pdf      # choose output path
dittopdf SOURCE.pdf TARGET.pdf --merge         # keep target keys the source lacks
dittopdf SOURCE.pdf TARGET.pdf --no-xmp        # only the Info dictionary
dittopdf --show SOURCE.pdf [TARGET.pdf]        # print metadata as JSON
```

```python
from dittopdf import copy_metadata, read_metadata

copy_metadata("source.pdf", "target.pdf", "out.pdf")
```

## Tests

```bash
uv run pytest
```
