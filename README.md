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
| **Must be regenerated** | `/Prev`, xref offsets, object count, file hash | produced by the PDF writer |
| **Cannot reliably be reproduced** | structure tree, optional content, thumbnails, `/Perms`, `/DSS`, single-element `/ID` | if the original has none, removed so the output matches it; if both have one, the second PDF's is kept unless you opt in |

What this means in practice:

- **Signatures are never copied.** A signature covers the exact bytes of the signed
  file. If you replace annotations, signature fields are copied *unsigned*. If the
  second PDF is signed, you're warned that writing a new file invalidates its
  signatures.
- **References to pages that don't exist** in the second PDF (for example, a
  bookmark to page 3 of a 2-page output) become null, and the report says so.
- **The XMP packet** is copied byte for byte when unchanged. Edits are applied
  with lxml, so unrelated properties and custom schemas are preserved. pikepdf's
  automatic XMP rewrite during save is disabled (`fix_metadata_version=False`),
  and the written packet is verified.
- **Byte-level fidelity.** qpdf renumbers every object and re-serializes it in
  its own style (spaced, keys sorted) when it writes a file. So by default
  dittopdf writes the output itself (`pdfwriter.py`), reading the original's
  bytes directly (`rawobjects.py`):
  - An object identical to the original object with the same number is copied
    **byte for byte** from the original file, stream data included. Unchanged
    object streams are copied verbatim too.
  - A changed object is re-written only where it changed, in the original's
    style (compact `<</Type/Catalog>>` or spaced, line endings, separators
    around `obj`/`stream`/`endobj`), keeping its key order, number formatting
    (`612` vs `612.0`) and string form for every unchanged part.
  - Objects with no original counterpart (the second PDF's content) are also
    written in the original's style.
  - Objects follow the original's physical order, and the trailer and
    cross-reference data follow its format (xref entry line endings, xref
    stream field widths, predictor and compression level).
- **Compression.** dittopdf never recompresses streams. When one of the second
  PDF's streams decodes to exactly the same data as an original stream (same
  image pixels, same page content, same font file), it takes the original's
  encoded bytes, filters and object number. The stored size then matches too.
- **Re-encoded images.** Another producer often stores the same picture
  differently: a new JPEG of the same logo, or DeviceRGB instead of CalRGB.
  Images of the same size on the same page are decoded and compared,
  including their transparency. When the picture is the same, every reference
  to the second PDF's image points at the original's image instead. That image
  is copied byte for byte under its own number, and the picture is stored once.
  "The same" means a mean difference of at most 4/255 and no 4×4-pixel block
  differing by more than 32/255. Averaging over blocks removes re-encoding
  noise but keeps real changes: a changed digit, barcode bar or chart bar fails
  the test even when the mean barely moves. Colour-keyed images must match
  exactly. Option: *similar* (default), *identical* pixels only, or *none*.
- **Images only the original has**, such as JasperReports' 1×1 transparent
  spacer images, are copied byte for byte under their original numbers. Those
  that can't change the page's appearance (fully transparent soft mask or a
  colour-key mask covering every pixel) are drawn at their original positions
  by a small content stream placed before the second PDF's own content
  streams, which stay unchanged. Visible ones are copied into the page
  resources but not drawn, because that would change the page.
- **Resource names.** Content streams refer to fonts, images, graphics states,
  colour spaces, patterns, shadings and marked-content properties by the name
  they have in the page's resource dictionary (`/F1 12 Tf`, `/Im0 Do`). Those
  names are copied from the original: each of the second PDF's resources is
  matched to an original resource on the same page (the same object, then the
  same font ignoring the subset prefix or an image of the same size, then the
  same kind in order of first use) and takes its name. The dictionary keys and
  the name operands in every content stream using them are renamed together;
  every other byte of the content is unchanged, and the stream is recompressed
  at the zlib level it had. Form XObjects paired this way get the same
  treatment for their own resources. When the renamed content equals the
  original's, the original's stream bytes and number are used (see
  *Compression*). Names are left alone where renaming could change the page:
  resource dictionaries also used by content without a counterpart
  (annotation appearances, Type 3 glyphs, the form's `/DR`, extra pages), a
  content stream shared between different resources, one that can't be
  tokenized reliably (checked against qpdf's parser) or one compressed with
  filters other than Flate. The report lists them.
- **Matching the original's catalog.** If the original has no structure tree,
  the second PDF's tags and their references are removed. The marked-content
  operators inside its page content remain, and the report says so. Catalog
  entries the original lacks (named destinations, viewer preferences, mark
  info, optional content, an interactive form…) are removed by default.
- **Object numbers.** In the same writer, object numbers match the original's:
  - Objects that *are* the original's keep their number and generation: the
    catalog, Info, page tree, pages by number, the XMP stream, and every object
    copied from the original.
  - The second PDF's objects take the number of the original object in the same
    place (e.g. page 1's `/Contents`, font `/F1`), then numbers the original
    used for objects that aren't in the output, then numbers above its highest.
  - Object streams follow the original's layout (same members in the same
    stream numbers), and the cross-reference table or stream (with its number)
    and `/Size` match where possible.
  - Encrypted output is supported (RC4 and AES; each object is encrypted under
    its own number), and `/ID` is then exact too.
  - Every file is reopened and compared object by object before it's kept; if
    anything differs, qpdf writes the file instead and the report explains why.
  - Linearization needs qpdf's writer, so it's off by default when numbers are
    kept. Choose *let the writer renumber* to linearize.
- **Header bytes.** The original's header line, binary-marker comment and any
  blank lines before the first object (e.g. `%PDF-1.7\r\n%âãÏÓ\r\n`) are
  written byte for byte. When qpdf writes the file instead (linearization, or
  *let the writer renumber*), its header is replaced after saving: object
  offsets are shifted and the cross-reference data is corrected, and the
  result is verified. Linearized output can only be rewritten when the
  lengths are equal. Bytes before `%PDF-` are not reproduced.
- **`/ID`** is written exactly by dittopdf's writer. When qpdf writes the file,
  it keeps the first element and regenerates the second; dittopdf then
  reproduces the second element with a verified same-length substitution
  (not possible for encrypted output on that path).
- **Encryption.** When the original is encrypted and you don't change the
  passwords or permissions, the original's encryption dictionary and file key
  are reused. The output opens with the original's user *and* owner passwords,
  and unchanged encrypted objects can be copied byte for byte. If you set new
  passwords, they're used instead; a blank owner password then gets a random
  one, shown in the report.
- **Unavoidable differences** are listed on the result page. They include:
  cross-reference offsets; several revisions collapsed into one; second-PDF
  streams whose content differs from every original stream; marked content
  left in the second PDF's content streams; objects that only the original had
  and the output can't contain. The page also counts objects that are
  byte-identical to the original, re-written, or new.

### Output options

| Option | Choices |
|---|---|
| Annotations & forms | keep the second PDF's (default; comment metadata is copied onto matching annotations, and the form dictionary is removed if the original has none) · replace with the original's · add the original's |
| Structure tree | match the original (default: removed if the original is untagged, otherwise the second PDF's is kept) · keep the second PDF's · copy the original's (only correct if the content is the same) |
| Images only the original has | copy them and draw the invisible ones (default) · don't copy them |
| Re-encoded images | use the original's when the picture is the same (default) · only when the pixels are identical · keep the second PDF's |
| Resource names | use the original's names (default) · keep the second PDF's |
| File identifier | reproduce both elements (default) · keep the first, regenerate the second |
| Metadata consistency | write Info overrides into the matching XMP properties (default on) |
| File structure | object numbers (keep the original's · let the writer renumber), header version (defaults to the original's), header bytes (match the original · writer default), linearization (needs the writer to renumber), object streams (defaults to match the original), compress uncompressed streams |
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
│   ├── rawobjects.py  # objects read straight from the bytes: spans, key order, style
│   ├── headerfix.py   # reproduce header bytes after a qpdf save (offset correction)
│   ├── numbering.py   # output object numbers matching the original's
│   ├── pdfwriter.py   # writer keeping numbers, bytes and style (object streams, xref, encryption)
│   ├── resources.py   # font/image discovery, image placement & DPI
│   ├── imagematch.py  # same picture despite re-encoding (pixel comparison)
│   ├── resnames.py    # resource names: content-stream scanning, matching, renaming
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
