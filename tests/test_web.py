"""The Flask application: flow, validation and upload safety."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pikepdf
import pytest

from dittopdf.web import create_app
from dittopdf.web.workspace import Workspace, sweep
from factory import make_original, make_second


@pytest.fixture
def app(tmp_path):
    return create_app({"WORK_DIR": tmp_path / "work", "TESTING": True, "MAX_CONTENT_LENGTH": 2 * 1024 * 1024})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def pdfs(tmp_path):
    return make_original(tmp_path / "orig.pdf"), make_second(tmp_path / "second.pdf")


def token(client) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def upload(client, url: str, path: Path | None, name: str = "file.pdf", data: bytes | None = None, **extra):
    body = data if data is not None else path.read_bytes()
    return client.post(url, data={"csrf": token(client), "pdf": (io.BytesIO(body), name), **extra},
                        content_type="multipart/form-data")


def workspace_dir(app, client) -> Path:
    with client.session_transaction() as s:
        return app.config["WORK_DIR"] / s["ws"]


def test_full_flow(app, client, pdfs):
    orig, second = pdfs
    r = upload(client, "/upload/original", orig, "../../etc/passwd.pdf", last_modified="1700000000000")
    assert r.status_code == 302 and r.headers["Location"].endswith("/original")
    page = client.get("/original")
    assert "passwd.pdf" in page.text and "Original Title" in page.text
    assert "2023-11-14T22:13:20+00:00" in page.text  # browser-reported mtime
    assert upload(client, "/upload/second", second, "second.pdf").headers["Location"].endswith("/compare")
    assert "Only in original" in client.get("/compare").text
    assert "Copy all original metadata" in client.get("/edit").text

    tok = token(client)
    r = client.post("/edit", data={"csrf": tok, "opt_encryption": "none", "opt_version": "1.7",
                                   "opt_annotations": "keep", "opt_object_streams": "match",
                                   "a::info:/Author": "custom", "v::info:/Author": "Robert Smith",
                                   "add_info_key": "/Department", "add_info_value": "QA"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/result")
    res = client.get("/result").text
    assert "not</strong> byte-for-byte identical" in res
    dl = client.get("/download")
    assert dl.status_code == 200 and "second-ditto.pdf" in dl.headers["Content-Disposition"]
    with pikepdf.open(io.BytesIO(dl.data)) as pdf:
        assert pdf.docinfo.Author == "Robert Smith" and pdf.docinfo.Department == "QA"
        assert pdf.docinfo.Title == "Original Title"

    # Files are stored under fixed names only; the uploaded name never reaches the filesystem.
    ws = workspace_dir(app, client)
    assert sorted(p.name for p in ws.glob("*.pdf")) == ["original.pdf", "output.pdf", "second.pdf"]
    assert not list(app.config["WORK_DIR"].parent.glob("**/passwd*"))
    assert orig.read_bytes() == (ws / "original.pdf").read_bytes()  # uploads never modified

    client.post("/reset", data={"csrf": tok})
    assert not ws.exists()


def test_validation_errors_rerender(client, pdfs):
    orig, second = pdfs
    upload(client, "/upload/original", orig)
    upload(client, "/upload/second", second)
    r = client.post("/edit", data={"csrf": token(client), "a::info:/ModDate": "custom",
                                   "v::info:/ModDate": "last tuesday", "opt_version": "9.9"})
    assert r.status_code == 400
    assert "Not a PDF date" in r.text and "not one of 1.0" in r.text
    assert "last tuesday" in r.text  # the user's input is preserved


def test_rejects_non_pdf_and_missing_file(client):
    r = upload(client, "/upload/original", None, data=b"GIF89a not a pdf")
    assert r.status_code == 302
    assert "does not look like a PDF" in client.get("/").text
    r = client.post("/upload/original", data={"csrf": token(client)}, content_type="multipart/form-data")
    assert "Choose a PDF file" in client.get(r.headers["Location"]).text


def test_rejects_malformed_pdf(client):
    upload(client, "/upload/original", None, data=b"%PDF-1.7\n garbage garbage")
    assert "could not be opened" in client.get("/").text


def test_upload_size_limit(client):
    r = upload(client, "/upload/original", None, data=b"%PDF-1.4\n" + b"0" * (3 * 1024 * 1024))
    assert r.status_code == 413 and "limited to" in r.text


def test_csrf_required(client, pdfs):
    r = client.post("/upload/original", data={"pdf": (io.BytesIO(pdfs[0].read_bytes()), "a.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_password_flow(client, tmp_path, pdfs):
    enc = make_second(tmp_path / "enc.pdf", encryption=pikepdf.Encryption(owner="own", user="secret"))
    r = upload(client, "/upload/original", enc)
    assert r.headers["Location"].endswith("/password/original")
    r = client.post("/password/original", data={"csrf": token(client), "password": "wrong"})
    assert "did not open it" in r.text
    r = client.post("/password/original", data={"csrf": token(client), "password": "secret"})
    assert r.headers["Location"].endswith("/original")
    assert client.get("/password/../../etc").status_code == 404


def test_steps_require_session(client):
    assert client.get("/compare").status_code == 302
    assert client.get("/download").status_code == 302


def test_workspace_token_validation(tmp_path):
    for bad in ["../x", "a" * 31, "a/b" + "c" * 29, ""]:
        with pytest.raises(ValueError):
            Workspace(tmp_path, bad)
    ws = Workspace.create(tmp_path)
    assert ws.dir.parent == tmp_path and oct(ws.dir.stat().st_mode & 0o777) == "0o700"
    with pytest.raises(ValueError):
        ws.write("../evil", {})


def test_sweep_removes_expired(tmp_path):
    import os
    import time

    ws = Workspace.create(tmp_path)
    old = time.time() - 10_000
    os.utime(ws.dir, (old, old))
    assert sweep(tmp_path, ttl=3600, every=0) == 1
    assert not ws.dir.exists()
