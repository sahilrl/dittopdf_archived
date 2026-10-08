"""Flask web application: inspect, compare, edit and copy PDF metadata.

Run with ``dittopdf-web`` (development server) or any WSGI server pointing at
``dittopdf.web:create_app()``.
"""

from __future__ import annotations

import argparse
from typing import Any

from flask import Flask


def create_app(overrides: dict[str, Any] | None = None) -> Flask:
    from dittopdf.web import routes
    from dittopdf.web.config import Config

    app = Flask(__name__)
    app.config.from_object(Config)
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    if overrides:
        app.config.update(overrides)
    app.config["WORK_DIR"].mkdir(mode=0o700, parents=True, exist_ok=True)
    app.register_blueprint(routes.bp)
    routes.register_handlers(app)
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="dittopdf-web", description="Run the dittopdf web application.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)
    create_app().run(host=args.host, port=args.port, debug=args.debug)
