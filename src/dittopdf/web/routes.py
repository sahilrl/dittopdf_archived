"""HTTP routes. All PDF work is delegated to :mod:`dittopdf.services`."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import pikepdf
from flask import (Blueprint, Flask, abort, current_app, flash, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

from dittopdf.services import copier, report
from dittopdf.services.comparison import COPY_STATUS, STATUS, compare
from dittopdf.services.copier import PERMISSIONS, Options
from dittopdf.services.inspector import SECTIONS, InspectError, inspect_pdf
from dittopdf.services.model import ACTIONS, CLASSES
from dittopdf.web.workspace import Workspace, sweep

log = logging.getLogger(__name__)
bp = Blueprint("web", __name__)
ROLES = {"original": "original PDF", "second": "second PDF"}
INFO_KEY_RE = re.compile(r"^/[A-Za-z0-9_.:\-]{1,64}$")
# On the edit screen, properties absent from both PDFs are only offered where adding one is useful.
EDIT_ABSENT_SECTIONS = {"Document Info", "XMP", "Catalog", "Trailer"}


# ----------------------------------------------------------------------------- plumbing


def cfg(key: str) -> Any:
    return current_app.config[key]


def get_ws(create: bool = False) -> Workspace | None:
    token = session.get("ws")
    if token:
        try:
            ws = Workspace(cfg("WORK_DIR"), token)
        except ValueError:
            ws = None
        if ws is not None and ws.exists():
            ws.touch()
            return ws
    if not create:
        return None
    ws = Workspace.create(cfg("WORK_DIR"))
    session["ws"] = ws.token
    return ws


def require_ws() -> Workspace:
    ws = get_ws()
    if ws is None:
        flash("Your session has expired or no PDF was uploaded yet. Start by uploading the original PDF.", "warn")
        abort(redirect(url_for("web.index")))
    return ws


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@bp.before_app_request
def _before() -> None:
    sweep(cfg("WORK_DIR"), cfg("WORKSPACE_TTL_SECONDS"))
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "The form token is missing or out of date. Reload the page and try again.")


@bp.app_context_processor
def _context() -> dict[str, Any]:
    return {"csrf_token": csrf_token, "STATUS": STATUS, "COPY_STATUS": COPY_STATUS, "CLASSES": CLASSES,
            "ACTIONS": ACTIONS, "max_upload_mb": cfg("MAX_UPLOAD_MB"), "step": None}


@bp.app_template_filter("filesize")
def _filesize(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


@bp.app_template_filter("anchor")
def _anchor(text: str) -> str:
    return "s-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def register_handlers(app: Flask) -> None:
    @app.errorhandler(RequestEntityTooLarge)
    def too_large(e: Exception) -> Any:
        return render_template("error.html", title="File too large",
                               message=f"Uploads are limited to {app.config['MAX_UPLOAD_MB']} MB "
                                       "(DITTOPDF_MAX_UPLOAD_MB)."), 413

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException) -> Any:
        return render_template("error.html", title=f"{e.code} {e.name}", message=e.description), e.code

    @app.errorhandler(Exception)
    def internal(e: Exception) -> Any:
        log.exception("unhandled error")
        return render_template("error.html", title="Something went wrong",
                               message="The request could not be completed. The uploaded files were not "
                                       "modified. Details have been logged on the server."), 500


# ----------------------------------------------------------------------------- uploads


def display_name(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[-200:] or "upload.pdf"


def receive(ws: Workspace, role: str) -> str | None:
    """Store an uploaded file for ``role``; return an error message or None."""
    f = request.files.get("pdf")
    if f is None or not f.filename:
        return "Choose a PDF file to upload."
    head = f.stream.read(1024)
    f.stream.seek(0)
    if b"%PDF-" not in head:
        return "This file does not look like a PDF: no %PDF- header in its first kilobyte."
    ws.save_upload(role, f.stream)
    mtime = request.form.get("last_modified", "")
    browser_mtime = None
    if mtime.isdigit():
        try:
            browser_mtime = datetime.fromtimestamp(int(mtime) / 1000, timezone.utc).isoformat()
        except (OverflowError, ValueError, OSError):
            browser_mtime = None
    state = ws.read("state", {})
    state[role] = {
        "filename": display_name(f.filename),
        "password": request.form.get("password", ""),
        "fs": {"content_type": f.mimetype or None, "browser_mtime": browser_mtime,
               "received": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    }
    ws.write("state", state)
    invalidate(ws, role)
    return None


def invalidate(ws: Workspace, role: str) -> None:
    """Uploading a new file discards everything derived from the old one."""
    ws.delete("comparison", "plan", "options", "result", "submitted")
    ws.drop("output")
    ws.delete(f"inspect_{role}")
    if role == "original":
        ws.delete("inspect_second")


def inspect_role(ws: Workspace, role: str) -> str | None:
    """Inspect the stored upload. Returns None, "password" or an error message."""
    st = ws.read("state", {}).get(role)
    if st is None or not ws.path(role).exists():
        return "missing"
    try:
        result = inspect_pdf(ws.path(role), st["password"], filename=st["filename"], fs=st["fs"],
                             max_pages=cfg("MAX_DETAIL_PAGES"))
    except pikepdf.PasswordError:
        return "password"
    except InspectError as e:
        return str(e)
    ws.write(f"inspect_{role}", result)
    return None


def after_upload(ws: Workspace, role: str, success: str) -> Any:
    err = inspect_role(ws, role)
    if err == "password":
        return redirect(url_for("web.password", role=role))
    if err:
        ws.drop(role)
        flash(f"The {ROLES[role]} could not be opened: {err}", "error")
        return redirect(url_for("web.index" if role == "original" else "web.original"))
    return redirect(url_for(success))


# ----------------------------------------------------------------------------- pages


@bp.get("/")
def index() -> Any:
    ws = get_ws()
    has_original = bool(ws and ws.read("inspect_original"))
    return render_template("upload_original.html", step=1, has_original=has_original)


@bp.post("/upload/original")
def upload_original() -> Any:
    ws = get_ws(create=True)
    assert ws is not None
    err = receive(ws, "original")
    if err:
        flash(err, "error")
        return redirect(url_for("web.index"))
    return after_upload(ws, "original", "web.original")


@bp.route("/password/<role>", methods=["GET", "POST"])
def password(role: str) -> Any:
    if role not in ROLES:
        abort(404)
    ws = require_ws()
    state = ws.read("state", {})
    if role not in state or not ws.path(role).exists():
        return redirect(url_for("web.index"))
    wrong = False
    if request.method == "POST":
        state[role]["password"] = request.form.get("password", "")
        ws.write("state", state)
        nxt = "web.original" if role == "original" else "web.compare"
        err = inspect_role(ws, role)
        if err is None:
            return redirect(url_for(nxt))
        if err != "password":
            flash(f"The {ROLES[role]} could not be opened: {err}", "error")
            return redirect(url_for("web.index"))
        wrong = True
    return render_template("password.html", role=role, label=ROLES[role], filename=state[role]["filename"],
                           wrong=wrong, step=1 if role == "original" else 2)


@bp.get("/original")
def original() -> Any:
    ws = require_ws()
    insp = ws.read("inspect_original")
    if insp is None:
        return redirect(url_for("web.index"))
    tree = single_tree(insp["entries"])
    has_second = bool(ws.read("inspect_second"))
    return render_template("upload_second.html", step=2, insp=insp, tree=tree, has_second=has_second)


@bp.post("/upload/second")
def upload_second() -> Any:
    ws = require_ws()
    if ws.read("inspect_original") is None:
        return redirect(url_for("web.index"))
    err = receive(ws, "second")
    if err:
        flash(err, "error")
        return redirect(url_for("web.original"))
    return after_upload(ws, "second", "web.compare")


def load_comparison(ws: Workspace) -> tuple[dict, dict, dict]:
    orig, second = ws.read("inspect_original"), ws.read("inspect_second")
    if orig is None:
        abort(redirect(url_for("web.index")))
    if second is None:
        abort(redirect(url_for("web.original")))
    cmp = ws.read("comparison")
    if cmp is None:
        cmp = compare(orig, second)
        cmp.pop("tree")
        ws.write("comparison", cmp)
    return orig, second, cmp


@bp.get("/compare", endpoint="compare")
def compare_view() -> Any:
    ws = require_ws()
    orig, second, cmp = load_comparison(ws)
    from dittopdf.services.comparison import build_tree

    return render_template("comparison.html", step=3, orig=orig, second=second, cmp=cmp,
                           tree=build_tree(cmp["rows"]))



@bp.route("/edit", methods=["GET", "POST"])
def edit() -> Any:
    ws = require_ws()
    orig, second, cmp = load_comparison(ws)
    from dittopdf.services.comparison import build_tree

    edit_tree = build_tree([r for r in cmp["rows"]
                            if r["status"] != "absent" or r["path"][0] in EDIT_ABSENT_SECTIONS])
    state = ws.read("state", {})
    defaults = copier.default_options(orig, second, state.get("original", {}).get("password", ""))
    if request.method == "GET":
        opts = Options.from_dict(ws.read("options") or defaults.to_dict())
        submitted = ws.read("submitted", {})
        return render_template("edit.html", step=4, orig=orig, second=second, cmp=cmp,
                               tree=edit_tree, opts=opts, defaults=defaults, submitted=submitted,
                               errors={}, opt_errors=[], add_info=submitted.get("__add_info__", []),
                               PERMISSIONS=PERMISSIONS)

    submitted = parse_submitted(request.form)
    add_info, add_errors = parse_add_info(request.form)
    plan, errors = copier.build_plan(cmp["rows"], submitted)
    opts, opt_errors = parse_options(request.form, defaults)
    opt_errors += add_errors
    if errors or opt_errors:
        flash(f"{len(errors) + len(opt_errors)} value(s) need attention before the PDF can be written.", "error")
        return render_template("edit.html", step=4, orig=orig, second=second, cmp=cmp,
                               tree=edit_tree, opts=opts, defaults=defaults, submitted=submitted,
                               errors=errors, opt_errors=opt_errors, add_info=add_info,
                               PERMISSIONS=PERMISSIONS), 400
    if add_info:
        plan["__add_info__"] = {"items": add_info}
    ws.write("submitted", {**submitted, "__add_info__": add_info})
    ws.write("options", opts.to_dict())
    ws.write("plan", plan)
    ws.drop("output")
    try:
        copy_report = copier.copy_properties(
            ws.path("original"), state["original"]["password"], ws.path("second"), state["second"]["password"],
            ws.path("output"), cmp["rows"], plan, opts, max_pages=cfg("MAX_DETAIL_PAGES"))
    except (copier.PlanError, pikepdf.PdfError, ValueError) as e:
        ws.drop("output")
        flash(f"The output PDF could not be written: {e}", "error")
        return redirect(url_for("web.edit"))
    out_pw = copier._output_password(opts, state["second"]["password"])
    out_name = output_name(state["second"]["filename"])
    output = inspect_pdf(ws.path("output"), out_pw, filename=out_name,
                         fs={"received": "generated"}, max_pages=cfg("MAX_DETAIL_PAGES"))
    result = report.build_result(orig, second, output, cmp["rows"], copy_report,
                                 annotations_merged=opts.annotations == "merge")
    result["output_name"] = out_name
    ws.write("result", result)
    return redirect(url_for("web.result"))


def parse_submitted(form: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key in form.keys():
        if not key.startswith("a::"):
            continue
        id = key[3:]
        out[id] = {"action": form.get(key, ""), "value": form.get("v::" + id)}
    return out


def parse_add_info(form: Any) -> tuple[list[list[str]], list[str]]:
    items, errors = [], []
    for k, v in zip(form.getlist("add_info_key"), form.getlist("add_info_value")):
        k, v = k.strip(), v
        if not k and not v:
            continue
        if not k.startswith("/"):
            k = "/" + k
        if not INFO_KEY_RE.match(k):
            errors.append(f"Info key {k!r} must be 1–64 letters, digits or _ . : -")
            continue
        items.append([k, v])
    return items, errors


def parse_options(form: Any, defaults: Options) -> tuple[Options, list[str]]:
    o = Options.from_dict(defaults.to_dict())
    errors: list[str] = []

    def choice(name: str, allowed: set[str], current: str) -> str:
        v = form.get(name, current)
        return v if v in allowed else current

    o.annotations = choice("opt_annotations", {"keep", "replace", "merge"}, o.annotations)
    o.struct_tree = choice("opt_struct_tree", {"keep", "copy"}, o.struct_tree)
    o.id_mode = choice("opt_id_mode", {"exact", "first"}, o.id_mode)
    o.header_mode = choice("opt_header_mode", {"match", "writer"}, o.header_mode)
    o.numbering = choice("opt_numbering", {"preserve", "writer"}, o.numbering)
    o.object_streams = choice("opt_object_streams", {"match", "generate", "disable", "preserve"},
                              o.object_streams)
    o.encryption = choice("opt_encryption", {"none", "original", "second"}, o.encryption)
    o.propagate_xmp = form.get("opt_propagate_xmp") == "on"
    o.linearize = form.get("opt_linearize") == "on"
    o.compress = form.get("opt_compress") == "on"
    if o.linearize and o.numbering == "preserve":
        errors.append("Linearization needs qpdf's writer, which renumbers objects: choose 'let the writer "
                      "renumber' under Object numbers, or turn linearization off.")
    version = form.get("opt_version", o.version).strip()
    if version and not re.fullmatch(r"1\.[0-7]|2\.0", version):
        errors.append(f"PDF version {version!r} is not one of 1.0–1.7 or 2.0.")
    else:
        o.version = version
    if o.encryption == "original":
        o.enc_user = form.get("opt_enc_user", "")
        o.enc_owner = form.get("opt_enc_owner", "")
        try:
            o.enc_R = int(form.get("opt_enc_R", o.enc_R))
        except ValueError:
            errors.append("Invalid encryption revision.")
        if o.enc_R not in (2, 3, 4, 6):
            errors.append("Encryption revision must be 2, 3, 4 or 6.")
        o.enc_aes = form.get("opt_enc_aes") == "on"
        o.enc_metadata = form.get("opt_enc_metadata") == "on"
        o.enc_allow = {p: form.get(f"opt_perm_{p}") == "on" for p in PERMISSIONS}
    if o.encryption == "second" and not ws_second_encrypted():
        errors.append("The second PDF is not encrypted, so its encryption cannot be kept.")
    return o, errors


def ws_second_encrypted() -> bool:
    ws = get_ws()
    second = ws.read("inspect_second") if ws else None
    return bool(second and second["summary"]["encrypted"])


def output_name(second_filename: str) -> str:
    stem = second_filename.rsplit(".", 1)[0] if "." in second_filename else second_filename
    safe = secure_filename(stem)[:100] or "output"
    return f"{safe}-ditto.pdf"


@bp.get("/result")
def result() -> Any:
    ws = require_ws()
    res = ws.read("result")
    if res is None or not ws.path("output").exists():
        return redirect(url_for("web.edit"))
    return render_template("result.html", step=5, res=res)


@bp.get("/download")
def download() -> Any:
    ws = require_ws()
    res = ws.read("result")
    path = ws.path("output")
    if res is None or not path.exists():
        abort(404, "No output PDF has been generated yet.")
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=res["output_name"],
                     max_age=0)


@bp.post("/reset")
def reset() -> Any:
    ws = get_ws()
    if ws is not None:
        ws.destroy()
    session.pop("ws", None)
    flash("All uploaded and generated files for this session were deleted.", "info")
    return redirect(url_for("web.index"))


# ----------------------------------------------------------------------------- view helpers


def single_tree(entries: list[dict]) -> list[dict]:
    """Group one inspection's entries into sections and groups for display."""
    sections: dict[str, dict] = {}
    for e in entries:
        sec = sections.setdefault(e["path"][0], {"name": e["path"][0], "groups": {}, "present": 0})
        title = e["path"][1] if len(e["path"]) > 1 else ""
        grp = sec["groups"].setdefault(title, {"title": title, "rows": [], "present": 0})
        e = dict(e, sub=" › ".join(e["path"][2:]))
        grp["rows"].append(e)
        if e["present"]:
            grp["present"] += 1
            sec["present"] += 1
    order = {name: i for i, name in enumerate(SECTIONS)}
    out = sorted(sections.values(), key=lambda s: order.get(s["name"], 99))
    for s in out:
        s["groups"] = list(s["groups"].values())
    return out
