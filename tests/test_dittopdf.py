from pathlib import Path

import pikepdf
import pytest
from pikepdf import Name, Pdf

from dittopdf import copy_metadata, read_metadata
from dittopdf.cli import main

XMP = b"""<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Source Title</rdf:li></rdf:Alt></dc:title>
</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"""


def make_pdf(path: Path, pages: int, info: dict | None = None, xmp: bytes | None = None, **save_kw) -> Path:
    pdf = Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page()
    if info:
        for k, v in info.items():
            pdf.docinfo[k] = v
    if xmp:
        pdf.Root.Metadata = pdf.make_stream(xmp, Type=Name.Metadata, Subtype=Name.XML)
    pdf.save(path, **save_kw)
    return path


@pytest.fixture
def source(tmp_path):
    return make_pdf(
        tmp_path / "source.pdf",
        1,
        {"/Title": "Source Title", "/Author": "Alice", "/Producer": "SrcProducer",
         "/CreationDate": "D:20200101120000Z", "/Custom": "yes"},
        XMP,
    )


@pytest.fixture
def target(tmp_path):
    return make_pdf(tmp_path / "target.pdf", 3, {"/Title": "Old", "/Keywords": "target-only"})


def test_replaces_info_and_xmp(source, target, tmp_path):
    out = copy_metadata(source, target, tmp_path / "out.pdf")
    with Pdf.open(out) as pdf, Pdf.open(source) as src:
        assert {str(k): str(v) for k, v in pdf.docinfo.items()} == {str(k): str(v) for k, v in src.docinfo.items()}
        assert pdf.Root.Metadata.read_bytes() == src.Root.Metadata.read_bytes()
        assert len(pdf.pages) == 3  # target content kept


def test_merge_keeps_target_only_keys(source, target, tmp_path):
    out = copy_metadata(source, target, tmp_path / "out.pdf", merge=True)
    info = read_metadata(out)["info"]
    assert info["/Keywords"] == "target-only"
    assert info["/Title"] == "Source Title"


def test_source_without_metadata_clears_target(tmp_path, target):
    bare = make_pdf(tmp_path / "bare.pdf", 1)
    with Pdf.open(target) as t:
        t.Root.Metadata = t.make_stream(XMP, Type=Name.Metadata, Subtype=Name.XML)
        t.save(tmp_path / "t2.pdf")
    out = copy_metadata(bare, tmp_path / "t2.pdf", tmp_path / "out.pdf")
    meta = read_metadata(out)
    assert meta["xmp"] is None
    assert "/Keywords" not in meta["info"]


def test_skip_xmp(source, target, tmp_path):
    out = copy_metadata(source, target, tmp_path / "out.pdf", xmp=False)
    meta = read_metadata(out)
    assert meta["xmp"] is None
    assert meta["info"]["/Author"] == "Alice"


def test_in_place(source, target):
    copy_metadata(source, target)
    assert read_metadata(target)["info"]["/Author"] == "Alice"


def test_encrypted_target_stays_encrypted(source, tmp_path):
    enc = make_pdf(tmp_path / "enc.pdf", 2, encryption=pikepdf.Encryption(owner="o", user="u"))
    out = copy_metadata(source, enc, tmp_path / "out.pdf", target_password="u")
    with pytest.raises(pikepdf.PasswordError):
        Pdf.open(out)
    assert read_metadata(out, password="u")["info"]["/Author"] == "Alice"


def test_cli_default_output_name(source, target, capsys):
    assert main([str(source), str(target)]) == 0
    out = target.with_name("target.ditto.pdf")
    assert out.exists()
    assert read_metadata(out)["info"]["/Producer"] == "SrcProducer"
    assert read_metadata(target)["info"]["/Title"] == "Old"  # untouched


def test_cli_missing_file(tmp_path, source, capsys):
    assert main([str(source), str(tmp_path / "nope.pdf"), "-o", str(tmp_path / "x.pdf")]) == 1
