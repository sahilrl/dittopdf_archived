"""Builders for richly-featured test PDFs."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, String

XMP_RICH = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 9.1">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
    xmlns:stRef="http://ns.adobe.com/xap/1.0/sType/ResourceRef#"
    xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#"
    xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
    xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:acme="http://example.com/acme/1.0/"
    xmp:CreatorTool="Original Writer 7"
    pdfaid:part="2" pdfaid:conformance="B">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Original Title</rdf:li></rdf:Alt></dc:title>
   <dc:creator><rdf:Seq><rdf:li>Alice</rdf:li><rdf:li>Bob</rdf:li></rdf:Seq></dc:creator>
   <dc:subject><rdf:Bag><rdf:li>alpha</rdf:li><rdf:li>beta</rdf:li></rdf:Bag></dc:subject>
   <xmp:CreateDate>2020-01-02T03:04:05+01:00</xmp:CreateDate>
   <xmpMM:DocumentID>uuid:doc-1</xmpMM:DocumentID>
   <xmpMM:InstanceID>uuid:inst-1</xmpMM:InstanceID>
   <xmpMM:DerivedFrom stRef:instanceID="uuid:parent-inst" stRef:documentID="uuid:parent-doc"/>
   <xmpMM:History><rdf:Seq>
     <rdf:li rdf:parseType="Resource"><stEvt:action>created</stEvt:action><stEvt:when>2020-01-02T03:04:05+01:00</stEvt:when><stEvt:softwareAgent>Writer</stEvt:softwareAgent></rdf:li>
     <rdf:li rdf:parseType="Resource"><stEvt:action>saved</stEvt:action><stEvt:changed>/</stEvt:changed></rdf:li>
   </rdf:Seq></xmpMM:History>
   <pdf:Producer>Original Producer</pdf:Producer>
   <photoshop:Headline>Headline text</photoshop:Headline>
   <acme:Custom>custom value</acme:Custom>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""".encode("utf-8")

IMAGE_XMP = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Make="TestCam"/></rdf:RDF></x:xmpmeta>"""


def _jpeg() -> bytes:
    from PIL import Image

    im = Image.new("RGB", (300, 150), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"  # Make
    exif[0x0110] = "Model X"  # Model
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=80)
    return buf.getvalue()


def _icc() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _ttf() -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "A"])
    fb.setupCharacterMap({65: "A"})
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0)); pen.lineTo((500, 700)); pen.lineTo((1000, 0)); pen.closePath()
    glyph = pen.glyph()
    fb.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": glyph})
    fb.setupHorizontalMetrics({".notdef": (500, 0), "A": (1000, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Testy", "styleName": "Regular", "version": "Version 1.234",
                       "psName": "Testy-Regular"})
    fb.setupOS2()
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def _signature(data: bytes = b"signed bytes") -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer")])
    now = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(0x1234).not_valid_before(now).not_valid_after(now + dt.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    return (pkcs7.PKCS7SignatureBuilder().set_data(data).add_signer(cert, key, hashes.SHA256())
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature]))


def make_original(path: Path, *, incremental: bool = True) -> Path:
    pdf = Pdf.new()
    for _ in range(3):
        pdf.add_blank_page(page_size=(612, 792))
    p1, p2, p3 = (pg.obj for pg in pdf.pages)

    # Fonts and an image drawn on page 1.
    ttf = _ttf()
    ff = pdf.make_stream(ttf, Length1=len(ttf))
    fd = pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/ABCDEF+Testy-Regular"),
                                      Flags=32, FontBBox=[0, -200, 1000, 800], ItalicAngle=0, Ascent=800,
                                      Descent=-200, CapHeight=700, StemV=80, FontFile2=ff))
    tt = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.TrueType, BaseFont=Name("/ABCDEF+Testy-Regular"),
                                      FirstChar=65, LastChar=65, Widths=[1000], FontDescriptor=fd,
                                      Encoding=Name.WinAnsiEncoding))
    helv = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    img = pdf.make_stream(_jpeg(), Type=Name.XObject, Subtype=Name.Image, Width=300, Height=150,
                          ColorSpace=Name.DeviceRGB, BitsPerComponent=8, Filter=Name.DCTDecode)
    img.Metadata = pdf.make_stream(IMAGE_XMP, Type=Name.Metadata, Subtype=Name.XML)
    p1.Resources = Dictionary(Font=Dictionary(F1=tt, F2=helv), XObject=Dictionary(Im1=img))
    p1.Contents = pdf.make_stream(b"BT /F1 24 Tf 72 700 Td (A) Tj /F2 12 Tf (hi) Tj ET "
                                  b"q 144 0 0 72 72 500 cm /Im1 Do Q")
    p1.CropBox = [10, 10, 602, 782]
    p1.TrimBox = [20, 20, 592, 772]
    p1.LastModified = String("D:20200101000000Z")
    p1.Tabs = Name.S
    p1.PieceInfo = Dictionary(Illustrator=Dictionary(LastModified=String("D:20200101000000Z"),
                                                     Private=Dictionary(AIMetaData=String("private"))))
    p2.Rotate = 90
    p3.VP = [Dictionary(Type=Name.Viewport, BBox=[0, 0, 300, 300], Name=String("Map"),
                        Measure=Dictionary(Type=Name.Measure, Subtype=Name.RL, R=String("1 in = 1 mi"),
                                           X=[Dictionary(Type=Name.NumberFormat, U=String("mi"), C=1)],
                                           D=[Dictionary(Type=Name.NumberFormat, U=String("mi"), C=1)],
                                           A=[Dictionary(Type=Name.NumberFormat, U=String("sq mi"), C=1)]))]

    # Annotations: a comment with popup and a reply, plus a link.
    note = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[100, 100, 120, 120],
                                        Contents=String("Original comment"), T=String("Alice"),
                                        NM=String("note-1"), M=String("D:20210101000000Z"),
                                        CreationDate=String("D:20210101000000Z"), Subj=String("Comment"),
                                        C=[1, 1, 0], F=4, P=p1))
    popup = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Popup, Rect=[120, 120, 300, 200],
                                         Parent=note, P=p1, Open=False))
    note.Popup = popup
    reply = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[100, 100, 120, 120],
                                         Contents=String("A reply"), T=String("Bob"), IRT=note, P=p1,
                                         RT=Name.R))
    link = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 50, 50],
                                        Dest=[p3, Name.Fit], P=p2, Border=[0, 0, 0]))
    p1.Annots = [note, popup, reply]
    p2.Annots = [link]

    # A signed signature field.
    sig = pdf.make_indirect(Dictionary(Type=Name.Sig, Filter=Name("/Adobe.PPKLite"),
                                       SubFilter=Name("/adbe.pkcs7.detached"), ByteRange=[0, 10, 20, 30],
                                       Contents=String(_signature() + b"\0" * 64), M=String("D:20240101000000Z"),
                                       Name=String("Test Signer"), Reason=String("Approval"),
                                       Location=String("Here"), ContactInfo=String("signer@example.com")))
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Sig,
                                          T=String("Signature1"), V=sig, Rect=[0, 0, 0, 0], F=132, P=p3))
    p3.Annots = [widget]
    pdf.Root.AcroForm = Dictionary(Fields=[widget], SigFlags=3)

    # Catalog-level structures.
    pdf.Root.PageLayout = Name.TwoColumnLeft
    pdf.Root.PageMode = Name.UseOutlines
    pdf.Root.Lang = String("en-GB")
    pdf.Root.ViewerPreferences = Dictionary(DisplayDocTitle=True, HideToolbar=True, Direction=Name.L2R)
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    pdf.Root.PageLabels = Dictionary(Nums=[0, Dictionary(S=Name.r), 2, Dictionary(S=Name.D, St=1)])
    pdf.Root.OpenAction = [p2, Name.Fit]
    pdf.Root.PieceInfo = Dictionary(InDesign=Dictionary(LastModified=String("D:20200101000000Z"),
                                                        Private=Dictionary(Data=String("x"))))
    pdf.Root.SpiderInfo = Dictionary(V=1.0)
    icc = pdf.make_stream(_icc(), N=3)
    pdf.Root.OutputIntents = [Dictionary(Type=Name.OutputIntent, S=Name.GTS_PDFA1,
                                         OutputConditionIdentifier=String("sRGB IEC61966-2.1"),
                                         RegistryName=String("http://www.color.org"), DestOutputProfile=icc)]
    with pdf.open_outline() as outline:
        outline.root.append(pikepdf.OutlineItem("Chapter 1", 0))
        outline.root.append(pikepdf.OutlineItem("Chapter 3", 2))
    embedded = pikepdf.AttachedFileSpec(pdf, b"attached data", description="An attachment",
                                        filename="notes.txt", mime_type="text/plain",
                                        creation_date="D:20200101000000Z", mod_date="D:20200102000000Z")
    pdf.attachments["notes.txt"] = embedded
    names = pdf.Root.Names
    names.Dests = Dictionary(Names=[String("chap3"), Array([p3, Name.Fit])])
    dpart_node = pdf.make_indirect(Dictionary(Type=Name.DPart, Start=p1, End=p3,
                                              DPM=Dictionary(RecordID=String("R-1"))))
    pdf.Root.DPartRoot = Dictionary(Type=Name.DPartRoot, DPartRootNode=pdf.make_indirect(
        Dictionary(Type=Name.DPart, DParts=[[dpart_node]])))
    p1.DPart = dpart_node

    # Metadata.
    pdf.Root.Metadata = pdf.make_stream(XMP_RICH, Type=Name.Metadata, Subtype=Name.XML)
    info = pdf.docinfo
    info.Title = String(b"\xfe\xff" + "Original Title".encode("utf-16-be"))
    info.Author = "Alice"
    info.Subject = "Testing"
    info.Keywords = "alpha, beta"
    info.Creator = "Original Writer 7"
    info.Producer = "Original Producer"
    info.CreationDate = "D:20200102030405+01'00'"
    info.ModDate = "D:20200103030405+01'00'"
    info.Trapped = Name("/False")
    info.Company = "ACME"
    info.SourceModified = "D:20191231000000Z"
    info["/PTEX.Fullbanner"] = "This is pdfTeX"
    pdf.trailer.DocChecksum = Name("/0123456789ABCDEF")
    pdf.trailer.ID = Array([String(b"A" * 16), String(b"B" * 16)])
    pdf.save(path, object_stream_mode=pikepdf.ObjectStreamMode.disable, static_id=False,
             force_version="1.7")
    if incremental:
        _append_incremental_update(path)
    return path


def _append_incremental_update(path: Path) -> None:
    """Append a classic incremental update that adds one object."""
    data = path.read_bytes()
    with Pdf.open(path) as pdf:
        size = int(pdf.trailer.Size)
        root = pdf.Root.objgen
        info = pdf.trailer.Info.objgen
        ID = pdf.trailer.ID
        ids = "".join(f"<{bytes(x).hex()}>" for x in ID)
    prev = int(data.rsplit(b"startxref", 1)[1].split()[0])
    obj_off = len(data)
    obj = f"{size} 0 obj\n<< /Note (incremental) >>\nendobj\n".encode()
    xref_off = obj_off + len(obj)
    xref = (f"xref\n{size} 1\n{obj_off:010d} 00000 n \ntrailer\n<< /Size {size + 1} /Root {root[0]} 0 R "
            f"/Info {info[0]} 0 R /Prev {prev} /ID [{ids}] /DocChecksum /0123456789ABCDEF >>\n"
            f"startxref\n{xref_off}\n%%EOF\n").encode()
    path.write_bytes(data + obj + xref)


def make_second(path: Path, *, pages: int = 2, encryption: pikepdf.Encryption | None = None) -> Path:
    pdf = Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
    p1 = pdf.pages[0].obj
    p1.Contents = pdf.make_stream(b"BT /F1 12 Tf 72 700 Td (Second content) Tj ET")
    p1.Resources = Dictionary(Font=Dictionary(F1=Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                            BaseFont=Name("/Times-Roman"))))
    note = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Text, Rect=[50, 50, 70, 70],
                                        Contents=String("Second's comment"), T=String("Zed"), P=p1))
    p1.Annots = [note]
    pdf.docinfo.Title = "Second Title"
    pdf.docinfo.Author = "Zed"
    pdf.docinfo.Producer = "Second Producer"
    pdf.docinfo["/OnlyInSecond"] = "x"
    pdf.Root.PageLayout = Name.SinglePage
    pdf.save(path, encryption=encryption or False)
    return path


def make_with_header(path: Path, header: bytes, eol: bytes = b"\n") -> Path:
    """A minimal hand-written PDF that starts with exactly ``header``."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] >>",
        b"<< /Title (Header test) /Producer (Acrobat Distiller 23.0) >>",
    ]
    out = bytearray(header)
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % n + eol + body + eol + b"endobj" + eol
    xref = len(out)
    out += b"xref" + eol + b"0 %d" % (len(objects) + 1) + eol + b"0000000000 65535 f\r\n"
    for off in offsets:
        out += b"%010d 00000 n\r\n" % off
    out += (b"trailer" + eol + b"<< /Size %d /Root 1 0 R /Info 4 0 R /ID [<%s><%s>] >>" %
            (len(objects) + 1, b"11" * 16, b"22" * 16) + eol)
    out += b"startxref" + eol + b"%d" % xref + eol + b"%%EOF" + eol
    path.write_bytes(bytes(out))
    return path


def _assemble(objects: dict[int, bytes], trailer: bytes, header: bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n",
              entry_eol: bytes = b" \n") -> bytes:
    """Lay out ``objects`` (num -> full 'N 0 obj ... endobj\\n' bytes) with a classic xref table."""
    out = bytearray(header)
    offsets = {}
    for n in sorted(objects):
        offsets[n] = len(out)
        out += objects[n]
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f" % size + entry_eol
    for n in range(1, size):
        out += (b"%010d 00000 n" % offsets[n] if n in offsets else b"0000000000 00000 f") + entry_eol
    out += b"trailer\n" + trailer.replace(b"SIZE", b"%d" % size) + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _obj(n: int, body: bytes) -> bytes:
    return b"%d 0 obj\n" % n + body + b"\nendobj\n"


def _stream_obj(n: int, dict_body: bytes, data: bytes) -> bytes:
    # iText style: "<<...>>stream\n<data>\nendstream"
    return b"%d 0 obj\n<<" % n + dict_body + b"/Length %d>>stream\n" % len(data) + data + b"\nendstream\nendobj\n"


PHOTO = bytes((x * 7 + y * 3) % 256 for y in range(40) for x in range(64 * 3))


def make_itext_like(path: Path) -> Path:
    """A compact iText/JasperReports-style original with transparent 1×1 spacer images."""
    import zlib

    content = (b"q 64 0 0 40 100 700 cm /img0 Do Q\n"
               b"q 1 0 0 1 10 10 cm /img1 Do Q\nq 5 0 0 5 300 300 cm /img1 Do Q\n"
               b"q 1 0 0 1 50 50 cm /img3 Do Q\nq 20 0 0 20 400 400 cm /img2 Do Q\n")
    objects = {
        1: _obj(1, b"<</Type/Catalog/Pages 2 0 R>>"),
        2: _obj(2, b"<</Type/Pages/Count 1/Kids[3 0 R]>>"),
        3: _obj(3, b"<</Type/Page/MediaBox[0 0 595 842]/Parent 2 0 R/Resources<</ProcSet[/PDF/ImageC]"
                   b"/XObject<</img0 4 0 R/img1 5 0 R/img2 7 0 R/img3 9 0 R>>>>/Contents 8 0 R>>"),
        4: _stream_obj(4, b"/Type/XObject/Subtype/Image/Width 64/Height 40/ColorSpace/DeviceRGB"
                          b"/BitsPerComponent 8/Filter/FlateDecode", zlib.compress(PHOTO, 1)),
        # Transparent spacer: white pixel with an all-zero soft mask (JasperReports' px image).
        5: _stream_obj(5, b"/Type/XObject/Subtype/Image/Width 1/Height 1/ColorSpace/DeviceRGB"
                          b"/BitsPerComponent 8/SMask 6 0 R/Filter/FlateDecode", zlib.compress(b"\xff\xff\xff")),
        6: _stream_obj(6, b"/Type/XObject/Subtype/Image/Width 1/Height 1/ColorSpace/DeviceGray"
                          b"/BitsPerComponent 8/Filter/FlateDecode", zlib.compress(b"\x00")),
        # A visible original-only image.
        7: _stream_obj(7, b"/Type/XObject/Subtype/Image/Width 2/Height 2/ColorSpace/DeviceRGB"
                          b"/BitsPerComponent 8/Filter/FlateDecode", zlib.compress(b"\xff\x00\x00" * 4)),
        8: _stream_obj(8, b"/Filter/FlateDecode", zlib.compress(content)),
        # Second spacer kind: colour-key masked white pixel.
        9: _stream_obj(9, b"/Type/XObject/Subtype/Image/Width 1/Height 1/ColorSpace/DeviceRGB"
                          b"/BitsPerComponent 8/Mask[255 255 255 255 255 255]/Filter/FlateDecode",
                       zlib.compress(b"\xff\xff\xff")),
        10: _obj(10, b"<</Producer(iText 2.1.7 by 1T3XT)/CreationDate(D:20240101120000+01'00')"
                     b"/Creator(JasperReports Library version 6.20.0)>>"),
    }
    ident = b"<" + b"ab" * 16 + b"><" + b"ab" * 16 + b">"
    path.write_bytes(_assemble(objects, b"<</Root 1 0 R/ID [" + ident + b"]/Info 10 0 R/Size SIZE>>"))
    return path


def make_qpdf_like_second(path: Path, *, tags: int = 40) -> Path:
    """The same page re-rendered by another pipeline: same photo (other compression), no spacers,
    a tag tree, named destinations, print scaling, spaced/sorted qpdf formatting."""
    import zlib

    pdf = Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    page = pdf.pages[0].obj
    img = pdf.make_stream(b"", Type=Name.XObject, Subtype=Name.Image, Width=64, Height=40,
                          ColorSpace=Name.DeviceRGB, BitsPerComponent=8)
    img.write(zlib.compress(PHOTO, 9), filter=Name.FlateDecode)
    page.Resources = Dictionary(XObject=Dictionary(img0=img))
    page.Contents = pdf.make_stream(b"/P <</MCID 0>> BDC q 64 0 0 40 100 700 cm /img0 Do Q EMC\n")
    page.StructParents = 0
    kids = [pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.P, Pg=page, K=i)) for i in range(tags)]
    pdf.Root.StructTreeRoot = Dictionary(Type=Name.StructTreeRoot, K=kids,
                                         ParentTree=Dictionary(Nums=[0, Array(kids)]))
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    pdf.Root.ViewerPreferences = Dictionary(PrintScaling=Name("/None"))
    pdf.Root.Names = pdf.make_indirect(Dictionary(Dests=Dictionary(Names=[String("top"), Array([page, Name.Fit])])))
    pdf.docinfo.Producer = "Some other renderer"
    pdf.save(path)
    return path


def make_quirky_layout(path: Path) -> Path:
    """Layout details writers vary in: a blank line after the header, "endobj \\n", and an indirect
    /Length object written right after its stream."""
    import zlib

    content = zlib.compress(b"BT /F1 12 Tf 72 700 Td (quirky) Tj ET")
    parts = {
        1: b"1 0 obj \n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj \n",
        2: b"2 0 obj \n<<\n/Type /Pages\n/Kids [3 0 R]\n/Count 1\n>>\nendobj \n",
        3: b"3 0 obj \n<<\n/Type /Page\n/Parent 2 0 R\n/MediaBox [0 0 300 300]\n/Resources <<\n>>\n/Contents 4 0 R\n>>\nendobj \n",
        4: b"4 0 obj \n<<\n/Length 5 0 R\n/Filter /FlateDecode\n>>\nstream\n" + content + b"\nendstream\nendobj \n",
        5: b"5 0 obj \n%d\nendobj \n" % len(content),
        6: b"6 0 obj \n<<\n/Producer (Quirky Writer)\n>>\nendobj \n",
    }
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n\n")
    offsets = {}
    for n in sorted(parts):
        offsets[n] = len(out)
        out += parts[n]
    xref = len(out)
    out += b"xref\n0 7\n0000000000 65535 f\r\n" + b"".join(b"%010d 00000 n\r\n" % offsets[n] for n in range(1, 7))
    out += (b"trailer\n<<\n/Size 7\n/Root 1 0 R\n/Info 6 0 R\n/ID [<" + b"cd" * 16 + b"> <" + b"cd" * 16
            + b">]\n>>\nstartxref\n%d\n%%%%EOF\n" % xref)
    path.write_bytes(bytes(out))
    return path
