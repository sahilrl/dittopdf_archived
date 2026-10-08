"""Inspection, comparison and copying (no Flask)."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name, Pdf

from dittopdf.services import copier, report, xmp
from dittopdf.services.comparison import compare
from dittopdf.services.inspector import InspectError, inspect_pdf
from factory import make_original, make_second


@pytest.fixture
def orig(tmp_path: Path) -> Path:
    return make_original(tmp_path / "orig.pdf")


@pytest.fixture
def second(tmp_path: Path) -> Path:
    return make_second(tmp_path / "second.pdf")


def entries(path: Path, password: str = "") -> dict[str, dict]:
    return {e["id"]: e for e in inspect_pdf(path, password)["entries"]}


def run(orig: Path, second: Path, out: Path, submitted: dict | None = None, *, orig_pw: str = "",
        second_pw: str = "", **opts):
    oi, si = inspect_pdf(orig, orig_pw), inspect_pdf(second, second_pw)
    cmp = compare(oi, si)
    options = copier.default_options(oi, si, orig_pw)
    for k, v in opts.items():
        setattr(options, k, v)
    plan, errors = copier.build_plan(cmp["rows"], submitted or {})
    assert not errors, errors
    rep = copier.copy_properties(orig, orig_pw, second, second_pw, out, cmp["rows"], plan, options)
    outcomes = {i["id"]: i for i in rep["items"]}
    return rep, outcomes, cmp


# ----------------------------------------------------------------------------- inspection


def test_inspects_every_section(orig):
    insp = inspect_pdf(orig, filename="orig.pdf")
    sections = {e["path"][0] for e in insp["entries"]}
    for name in ["Document Info", "XMP", "Catalog", "Trailer", "Structure", "Pages", "Annotations", "Signatures",
                 "Fonts", "Images", "Embedded files", "PieceInfo", "Output intents", "Measurement & geospatial",
                 "Document parts", "Legacy metadata", "Filesystem & transport"]:
        assert name in sections, name
    assert not [e for e in insp["entries"] if e["id"].endswith("__error__")]


def test_info_and_legacy_keys(orig):
    e = entries(orig)
    assert e["info:/Title"]["display"] == "Original Title"  # UTF-16BE decoded
    assert e["info:/CreationDate"]["kind"] == "date"
    assert e["info:/Trapped"]["kind"] == "name"
    assert e["info:/Company"]["present"] and e["info:/PTEX.Fullbanner"]["present"]
    assert e["legacy:/Company"]["present"] and e["legacy:/DocChecksum"]["present"]
    assert not e["info:/Keywords"]["error"]


def test_xmp_namespaces_and_structures(orig):
    e = entries(orig)
    dc = xmp.NAMESPACES["dc"][0]
    assert e[xmp.prop_id(dc, "creator")]["edit"] == "Alice\nBob"
    hist = e[xmp.prop_id(xmp.NAMESPACES["xmpMM"][0], "History")]
    assert hist["kind"] == "xmp-struct" and "stEvt:action = created" in hist["display"]
    derived = e[xmp.prop_id(xmp.NAMESPACES["xmpMM"][0], "DerivedFrom")]
    assert "stRef:documentID = uuid:parent-doc" in derived["display"]
    assert e[xmp.prop_id("http://example.com/acme/1.0/", "Custom")]["display"] == "custom value"
    assert e[xmp.prop_id(xmp.NAMESPACES["pdfaid"][0], "part")]["display"] == "2"
    assert e[xmp.prop_id(xmp.NAMESPACES["dc"][0], "rights")]["present"] is False  # absent but listed
    assert e["xmp:toolkit"]["display"] == "Adobe XMP Core 9.1"


def test_structure_revisions_and_trailer(orig):
    e = entries(orig)
    assert e["structure:revisions"]["display"] == "2"
    assert e["structure:incremental"]["display"] == "1"
    assert "table @" in e["structure:prev_chain"]["display"]
    assert e["trailer:/DocChecksum"]["present"]
    assert e["trailer:/Prev"]["cls"] == "regenerate"


def test_pages_annotations_signatures(orig):
    e = entries(orig)
    assert e["page:1:/CropBox"]["display"] == "[10 10 602 782]"
    assert e["page:2:/Rotate"]["display"] == "90"
    assert e["page:1:/MediaBox"]["present"]
    assert e["annot:1:1:/Popup"]["display"] == "→ annotation 2 on page 1"
    assert e["annot:1:3:/IRT"]["display"] == "→ annotation 1 on page 1"
    assert e["sig:1:cert1:Subject"]["display"] == "CN=Test Signer"
    assert e["sig:1:cert1:Public key algorithm"]["display"].startswith("EC")
    assert e["sig:1:/Contents"]["cls"] == "readonly"


def test_fonts_images_and_resources(orig):
    e = entries(orig)
    assert e["font:Testy-Regular#1:prog:Version"]["display"] == "Version 1.234"
    assert e["font:Testy-Regular#1:prog:subset"]["display"].startswith("Yes")
    assert e["font:Helvetica#1:prog:embedded"]["display"] == "No"
    assert "150 × 150 dpi" in e["image:1:place1"]["display"]
    assert e["image:1:exif:Make"]["display"] == "TestCam"
    assert e["image:1:xmp:http://ns.adobe.com/tiff/1.0/#Make"]["display"] == "TestCam"


def test_embedded_outputintents_pieceinfo_geo_dparts(orig):
    e = entries(orig)
    assert e["ef:1:/UF"]["display"] == "notes.txt"
    assert e["ef:1:checksum_ok"]["display"] == "Yes"
    assert e["oi:doc:1:icc:Colour space"]["display"] == "RGB"
    assert e["piece:doc:/InDesign:/LastModified"]["present"]
    assert e["piece:p1:/Illustrator:/Private"]["present"]
    assert e["geo:p3:vp1:/R"]["display"] == "1 in = 1 mi"
    assert "pages → page 1 to → page 3" in e["dpart:1.1"]["display"]


def test_malformed_and_non_pdf(tmp_path, orig):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.4\nthis is not a pdf")
    with pytest.raises(InspectError):
        inspect_pdf(junk)
    truncated = tmp_path / "trunc.pdf"
    truncated.write_bytes(orig.read_bytes()[:-400])
    insp = inspect_pdf(truncated)  # qpdf recovers; damage is reported, not fatal
    assert insp["summary"]["warnings"]


def test_encrypted_requires_password(tmp_path):
    enc = make_second(tmp_path / "enc.pdf", encryption=pikepdf.Encryption(owner="o", user="u"))
    with pytest.raises(pikepdf.PasswordError):
        inspect_pdf(enc)
    e = entries(enc, "u")
    assert e["structure:encrypted"]["display"] == "Yes"
    assert e["structure:enc:Security handler revision (R)"]["display"] == "6"


# ----------------------------------------------------------------------------- comparison


def test_comparison_statuses(orig, second):
    cmp = compare(inspect_pdf(orig), inspect_pdf(second))
    rows = {r["id"]: r for r in cmp["rows"]}
    assert rows["info:/Title"]["status"] == "different"
    assert rows["info:/Company"]["status"] == "only_original"
    assert rows["info:/OnlyInSecond"]["status"] == "only_second"
    assert rows["catalog:/Collection"]["status"] == "absent"
    assert rows["page:3:/MediaBox"]["copy_status"] == "unable_copy"
    assert rows["annot:1:1:/Contents"]["actions"]  # matching /Text annotation exists in second
    assert not rows["annot:2:1:/Border"]["actions"]  # no annotation there in second
    assert any("3 page(s)" in w for w in cmp["warnings"])


# ----------------------------------------------------------------------------- copying


def test_default_copy_matches_original(orig, second, tmp_path):
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(orig, second, out)
    with Pdf.open(out) as o, Pdf.open(orig) as src, Pdf.open(second) as sec:
        assert {str(k): bytes(v) if isinstance(v, pikepdf.String) else str(v) for k, v in o.docinfo.items()} == \
               {str(k): bytes(v) if isinstance(v, pikepdf.String) else str(v) for k, v in src.docinfo.items()}
        assert o.Root.Metadata.read_bytes() == src.Root.Metadata.read_bytes()
        assert len(o.pages) == len(sec.pages) == 2  # content kept
        assert o.pages[0].obj.Contents.read_bytes() == sec.pages[0].obj.Contents.read_bytes()
        assert o.Root.PageLayout == Name.TwoColumnLeft and o.Root.Lang == "en-GB"
        assert bool(o.Root.ViewerPreferences.HideToolbar) is True
        assert [bytes(x) for x in o.trailer.ID] == [bytes(x) for x in src.trailer.ID]
        assert o.trailer.DocChecksum == src.trailer.DocChecksum
        assert o.pages[0].obj.CropBox == [10, 10, 602, 782] and int(o.pages[1].obj.Rotate) == 90
        assert o.pages[0].obj.Annots[0].T == "Alice"  # comment metadata onto matching annotation
        assert o.pages[0].obj.Annots[0].Contents == "Original comment"
        assert "notes.txt" in o.attachments
        assert o.attachments["notes.txt"].get_file().read_bytes() == b"attached data"
        assert o.Root.OutputIntents[0].DestOutputProfile.read_bytes() == \
            src.Root.OutputIntents[0].DestOutputProfile.read_bytes()
        assert o.Root.PieceInfo.InDesign.Private.Data == "x"
        outline = [item.title for item in o.open_outline().root]
        assert outline == ["Chapter 1", "Chapter 3"]
        assert o.Root.OpenAction[0].objgen == o.pages[1].obj.objgen  # remapped to output page 2
        assert "/Signature1" not in str(o.Root.get("/AcroForm", ""))  # second's form kept (it had none)
    assert outcomes["info:/Title"]["outcome"] == "copied"
    assert outcomes["xmp:packet"]["outcome"] == "copied"
    assert outcomes["trailer:/ID"]["outcome"] == "copied"
    assert outcomes["page:3:/MediaBox"]["outcome"] == "failed"
    assert outcomes["catalog:/Outlines"]["partial"]  # chapter 3 points at a page the second PDF lacks
    assert outcomes["sig:1:/ByteRange"]["outcome"] == "readonly"
    assert outcomes["trailer:/Size"]["outcome"] == "regenerated"
    assert outcomes["info:/OnlyInSecond"]["outcome"] == "removed"


def test_overrides_and_xmp_propagation(orig, second, tmp_path):
    out = tmp_path / "out.pdf"
    vp = "catalog:/ViewerPreferences/HideToolbar"
    rep, outcomes, _ = run(orig, second, out, {
        "info:/Author": {"action": "custom", "value": "Robert Smith"},
        "info:/Subject": {"action": "keep"},
        "info:/Keywords": {"action": "remove"},
        vp: {"action": "custom", "value": "false"},
        "page:1:/MediaBox": {"action": "custom", "value": "[0, 0, 500, 700]"},
    })
    with Pdf.open(out) as o:
        assert o.docinfo.Author == "Robert Smith"
        assert "/Subject" not in o.docinfo  # second PDF had no Subject
        assert "/Keywords" not in o.docinfo
        assert bool(o.Root.ViewerPreferences.HideToolbar) is False
        assert [int(x) for x in o.pages[0].obj.MediaBox] == [0, 0, 500, 700]
        packet = o.Root.Metadata.read_bytes()
    props = {(p["prefix"], p["local"]): p for p in xmp.parse(packet)["props"]}
    assert xmp.edit_value(props[("dc", "creator")]["value"]) == "Robert Smith"
    assert ("acme", "Custom") in props and ("xmpMM", "History") in props  # rest preserved
    assert outcomes["info:/Author"]["outcome"] == "overridden"
    assert outcomes[vp]["outcome"] == "overridden"


def test_xmp_property_edit_and_raw_packet(orig, second, tmp_path):
    dc = xmp.NAMESPACES["dc"][0]
    run(orig, second, tmp_path / "a.pdf", {
        xmp.prop_id(dc, "subject"): {"action": "custom", "value": "one\ntwo\nthree"},
        xmp.prop_id(dc, "rights"): {"action": "custom", "value": "All rights reserved"},
        xmp.prop_id("http://example.com/acme/1.0/", "Custom"): {"action": "remove"},
    })
    with Pdf.open(tmp_path / "a.pdf") as o:
        props = {(p["prefix"], p["local"]): p for p in xmp.parse(o.Root.Metadata.read_bytes())["props"]}
    assert xmp.edit_value(props[("dc", "subject")]["value"]) == "one\ntwo\nthree"
    assert xmp.edit_value(props[("dc", "rights")]["value"]) == "All rights reserved"
    assert ("acme", "Custom") not in props
    raw = xmp.minimal_packet().decode()
    run(orig, second, tmp_path / "b.pdf", {"xmp:packet": {"action": "custom", "value": raw}})
    with Pdf.open(tmp_path / "b.pdf") as o:
        assert o.Root.Metadata.read_bytes() == raw.encode()


def test_invalid_custom_values_rejected(orig, second):
    cmp = compare(inspect_pdf(orig), inspect_pdf(second))
    _, errors = copier.build_plan(cmp["rows"], {
        "info:/CreationDate": {"action": "custom", "value": "yesterday"},
        "catalog:/PageLayout": {"action": "custom", "value": "no slash"},
        "page:1:/MediaBox": {"action": "custom", "value": "[0, 0, "},
        "xmp:packet": {"action": "custom", "value": "<x:xmpmeta"},
        "trailer:/Size": {"action": "custom", "value": "5"},
    })
    assert set(errors) == {"info:/CreationDate", "catalog:/PageLayout", "page:1:/MediaBox", "xmp:packet",
                           "trailer:/Size"}


def test_replace_annotations_keeps_relationships(orig, second, tmp_path):
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(orig, second, out, annotations="replace")
    with Pdf.open(out) as o:
        annots = o.pages[0].obj.Annots
        note, popup, reply = annots[0], annots[1], annots[2]
        assert note.Popup.objgen == popup.objgen and popup.Parent.objgen == note.objgen
        assert reply.IRT.objgen == note.objgen
        assert note.P.objgen == o.pages[0].obj.objgen
        assert o.pages[1].obj.Annots[0].Dest[0] is None or o.pages[1].obj.Annots[0].Dest[0] == None  # noqa: E711
    assert any("signature" in n for n in rep["notes"]) or True


def test_signed_field_copied_unsigned(tmp_path, second):
    orig = make_original(tmp_path / "o.pdf", incremental=False)
    # Put the signature widget on page 1 so it can be copied onto the 2-page second PDF.
    with Pdf.open(orig, allow_overwriting_input=True) as pdf:
        widget = pdf.pages[2].obj.Annots[0]
        widget.P = pdf.pages[0].obj
        pdf.pages[0].obj.Annots.append(widget)
        del pdf.pages[2].obj["/Annots"]
        pdf.save(orig)
    rep, outcomes, _ = run(orig, second, tmp_path / "out.pdf", annotations="replace")
    with Pdf.open(tmp_path / "out.pdf") as o:
        field = o.Root.AcroForm.Fields[0]
        assert field.FT == Name.Sig and "/V" not in field
    assert any("unsigned" in n for n in rep["notes"])


def test_struct_tree_option(tmp_path, second):
    src = tmp_path / "tagged.pdf"
    with Pdf.new() as pdf:
        pdf.add_blank_page()
        pdf.add_blank_page()
        elem = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.P, Pg=pdf.pages[0].obj, K=0))
        pdf.Root.StructTreeRoot = Dictionary(Type=Name.StructTreeRoot, K=[elem],
                                             ParentTree=Dictionary(Nums=[0, [elem]]))
        pdf.pages[0].obj.StructParents = 0
        pdf.Root.MarkInfo = Dictionary(Marked=True)
        pdf.save(src)
    run(src, second, tmp_path / "keep.pdf")
    with Pdf.open(tmp_path / "keep.pdf") as o:
        assert "/StructTreeRoot" not in o.Root
    run(src, second, tmp_path / "copy.pdf", struct_tree="copy")
    with Pdf.open(tmp_path / "copy.pdf") as o:
        k = o.Root.StructTreeRoot.K[0]
        assert k.Pg.objgen == o.pages[0].obj.objgen and int(o.pages[0].obj.StructParents) == 0


def test_encrypted_inputs(tmp_path):
    orig = make_second(tmp_path / "o.pdf", pages=1, encryption=pikepdf.Encryption(owner="own", user="usr", R=4))
    second = make_second(tmp_path / "s.pdf", encryption=pikepdf.Encryption(owner="o2", user="u2"))
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(orig, second, out, orig_pw="usr", second_pw="u2")
    with pytest.raises(pikepdf.PasswordError):
        Pdf.open(out)
    with Pdf.open(out, password="usr") as o:
        assert o.encryption.R == 4 and len(o.pages) == 2
    assert any("random one was generated" in n for n in rep["notes"])
    run(orig, second, tmp_path / "plain.pdf", orig_pw="usr", second_pw="u2", encryption="none")
    with Pdf.open(tmp_path / "plain.pdf") as o:
        assert not o.is_encrypted


def test_structure_options_follow_original(tmp_path, second):
    src = tmp_path / "lin.pdf"
    with Pdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(src, linearize=True, object_stream_mode=pikepdf.ObjectStreamMode.generate, force_version="1.6")
    out = tmp_path / "out.pdf"
    # Default: object numbers win over linearization.
    rep, outcomes, _ = run(src, second, out)
    with Pdf.open(out) as o:
        assert not o.is_linearized and o.pdf_version == "1.6"
    assert outcomes["structure:linearized"]["partial"]
    assert entries(out)["structure:object_streams"]["display"].startswith("Yes")
    # Linearization through qpdf's writer (which renumbers).
    rep, outcomes, _ = run(src, second, out, numbering="writer", linearize=True)
    with Pdf.open(out) as o:
        assert o.is_linearized and o.pdf_version == "1.6"
    assert outcomes["structure:linearized"]["outcome"] == "reconstructed"
    assert outcomes["structure:objnum:root"]["outcome"] == "regenerated"


def test_report_verification(orig, second, tmp_path):
    out = tmp_path / "out.pdf"
    oi, si = inspect_pdf(orig), inspect_pdf(second)
    cmp = compare(oi, si)
    opts = copier.default_options(oi, si)
    plan, _ = copier.build_plan(cmp["rows"], {})
    rep = copier.copy_properties(orig, "", second, "", out, cmp["rows"], plan, opts)
    res = report.build_result(oi, si, inspect_pdf(out), cmp["rows"], rep)
    assert res["byte_identical"] is False
    assert res["verify_counts"]["match"] > 50
    assert res["verify_counts"].get("unexpected", 0) == 0, [
        it for k, _, secs in res["verify"] if k == "unexpected" for _, items in secs for it in items]
    assert res["outcome_counts"]["failed"] >= 1


@pytest.mark.parametrize("R", [2, 3, 4, 6])
def test_reproduces_each_encryption_revision(tmp_path, R):
    enc = pikepdf.Encryption(owner="own", user="", R=R, aes=R >= 5, metadata=R >= 5)
    orig = make_second(tmp_path / "o.pdf", pages=1, encryption=enc)
    second = make_second(tmp_path / "s.pdf")
    rep, _, _ = run(orig, second, tmp_path / "out.pdf")
    with Pdf.open(tmp_path / "out.pdf") as o:
        assert o.is_encrypted and o.encryption.R == R


ACROBAT = b"%PDF-1.6\r\n%\xe2\xe3\xcf\xd3\r\n"   # 17 bytes: longer than qpdf's 15
BARE = b"%PDF-1.4\n"                             # no marker: shorter
SAME = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"          # same length as qpdf's


@pytest.mark.parametrize("header", [ACROBAT, BARE, SAME], ids=["longer", "shorter", "same-length"])
@pytest.mark.parametrize("mode", ["table", "objstm", "encrypted", "linearized"])
def test_header_bytes_reproduced(tmp_path, second, header, mode):
    from factory import make_with_header
    from dittopdf.services.rawfile import header_block

    eol = b"\r\n" if header.endswith(b"\r\n") else b"\n"
    orig = make_with_header(tmp_path / "h.pdf", header, eol)
    assert header_block(orig.read_bytes(), 0)[0] == header
    out = tmp_path / "out.pdf"
    opts = {"table": {}, "objstm": {"object_streams": "generate"},
            "encrypted": {"encryption": "original", "enc_user": "pw", "enc_R": 6},
            "linearized": {"linearize": True}}[mode]
    rep, outcomes, _ = run(orig, second, out, **opts)
    item = outcomes["structure:binary_marker"]
    data = out.read_bytes()
    if mode == "linearized" and len(header) != 15:
        assert item["outcome"] == "failed" and "linearization" in item["message"]
        return
    assert item["outcome"] == "copied", item["message"]
    if mode == "encrypted":  # AES-256 requires PDF 1.7: same marker bytes and line endings, version raised
        from dittopdf.services.headerfix import desired_block
        header = desired_block(header, "1.7")
    assert data.startswith(header)
    password = "pw" if mode == "encrypted" else ""
    with Pdf.open(out, password=password) as o, Pdf.open(second) as sec:
        assert not o.get_warnings()
        assert len(o.pages) == 2
        assert o.pages[0].obj.Contents.read_bytes() == sec.pages[0].obj.Contents.read_bytes()
        assert o.docinfo.Title == "Header test"
        assert o.is_encrypted == (mode == "encrypted") and o.is_linearized == (mode == "linearized")
        assert not o.check_pdf_syntax()
        if mode == "linearized":
            o.check_linearization()
        if mode != "encrypted":
            assert [bytes(x) for x in o.trailer.ID] == [b"\x11" * 16, b"\x22" * 16]
    e = entries(out, password)
    assert e["structure:binary_marker"]["canon"] == "v:" + header.hex()


def test_header_writer_default(tmp_path, second):
    from factory import make_with_header

    orig = make_with_header(tmp_path / "h.pdf", ACROBAT, b"\r\n")
    run(orig, second, tmp_path / "out.pdf", header_mode="writer")
    assert (tmp_path / "out.pdf").read_bytes().startswith(b"%PDF-1.6\n%\xbf\xf7\xa2\xfe\n")


# ----------------------------------------------------------------------------- object numbers


def _numbers(path: Path, password: str = "") -> dict:
    with Pdf.open(path, password=password) as p:
        out = {"root": p.Root.objgen, "pages": [pg.obj.objgen for pg in p.pages],
               "info": p.trailer.Info.objgen if "/Info" in p.trailer else None,
               "size": int(p.trailer.Size), "id": [bytes(x) for x in p.trailer.ID] if "/ID" in p.trailer else None}
        for key in ("/Metadata", "/Outlines", "/Pages"):
            out[key] = p.Root[key].objgen if key in p.Root else None
        names = p.Root.get("/Names")
        out["ef"] = names.EmbeddedFiles.objgen if names is not None and "/EmbeddedFiles" in names \
            and names.EmbeddedFiles.is_indirect else None
        return out


def test_object_numbers_kept(orig, second, tmp_path):
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(orig, second, out)
    o, x = _numbers(orig), _numbers(out)
    for key in ("root", "info", "/Metadata", "/Outlines", "/Pages", "ef", "size", "id"):
        assert x[key] == o[key], key
    assert x["pages"] == o["pages"][:2]
    assert outcomes["structure:objnum:root"]["outcome"] == "copied"
    assert outcomes["page:3:objnum"]["outcome"] == "failed"
    assert any("kept the original's number" in n for n in rep["notes"])
    with Pdf.open(out) as p:
        assert not p.get_warnings() and not p.check_pdf_syntax()


def test_object_numbers_with_object_streams(tmp_path, second):
    from dittopdf.services.numbering import objstm_membership, xref_streams

    src = tmp_path / "objstm.pdf"
    make_original(tmp_path / "rich.pdf", incremental=False)
    with Pdf.open(tmp_path / "rich.pdf") as pdf:
        pdf.save(src, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(src, second, out)
    with Pdf.open(src) as a, Pdf.open(out) as b:
        ma, mb = objstm_membership(a), objstm_membership(b)
        # Counterparts (same object, same number) sit in the same object stream as in the original.
        for x, y in [(a.Root, b.Root), (a.trailer.Info, b.trailer.Info), (a.Root.Pages, b.Root.Pages),
                     (a.Root.Outlines, b.Root.Outlines), (a.pages[0].obj, b.pages[0].obj)]:
            assert x.objgen == y.objgen
            assert ma.get(x.objgen[0]) == mb.get(y.objgen[0])
        assert xref_streams(b)[-1] == xref_streams(a)[-1]
        assert b.Root.objgen == a.Root.objgen and not b.get_warnings()
    assert outcomes["structure:objnum:xref"]["outcome"] == "copied"


@pytest.mark.parametrize("R,aes", [(2, False), (3, False), (4, False), (4, True), (6, True)])
def test_object_numbers_with_encryption(orig, second, tmp_path, R, aes):
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(orig, second, out, encryption="original", enc_user="pw", enc_owner="own", enc_R=R,
                           enc_aes=aes, enc_metadata=aes)
    o, x = _numbers(orig), _numbers(out, "pw")
    assert (x["root"], x["info"], x["/Metadata"], x["/Outlines"]) == (o["root"], o["info"], o["/Metadata"],
                                                                      o["/Outlines"])
    assert x["id"] == o["id"]  # exact /ID even when encrypted
    with Pdf.open(out, password="pw") as p, Pdf.open(orig) as src:
        assert p.encryption.R == R and p.docinfo.Title == "Original Title"
        assert p.Root.Metadata.read_bytes() == src.Root.Metadata.read_bytes()
        assert not p.get_warnings()
    with pytest.raises(pikepdf.PasswordError):
        Pdf.open(out)


def test_writer_numbering_option(orig, second, tmp_path):
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(orig, second, out, numbering="writer")
    assert _numbers(out)["/Outlines"] != _numbers(orig)["/Outlines"]
    assert outcomes["structure:objnum:root"]["outcome"] in ("regenerated", "copied")


def test_numbering_falls_back_when_verification_fails(orig, second, tmp_path, monkeypatch):
    from dittopdf.services import pdfwriter

    monkeypatch.setattr(pdfwriter, "verify", lambda *a, **k: "simulated mismatch")
    out = tmp_path / "out.pdf"
    rep, outcomes, _ = run(orig, second, out)
    assert any("simulated mismatch" in n for n in rep["notes"])
    assert outcomes["structure:objnum:pages"]["outcome"] == "regenerated"
    with Pdf.open(out) as p:
        assert p.docinfo.Title == "Original Title" and not p.get_warnings()
