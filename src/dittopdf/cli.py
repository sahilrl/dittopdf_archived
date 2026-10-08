"""Command-line interface: ``dittopdf SOURCE TARGET [-o OUTPUT]``."""

from __future__ import annotations

import argparse
import json
import sys

import pikepdf

from dittopdf.core import copy_metadata, read_metadata


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dittopdf",
        description="Copy metadata (Info dictionary and XMP) from SOURCE pdf to TARGET pdf.",
    )
    p.add_argument("source", help="PDF to copy metadata from")
    p.add_argument("target", nargs="?", help="PDF to copy metadata into")
    dest = p.add_mutually_exclusive_group()
    dest.add_argument("-o", "--output", help="write result here (default: <target>.ditto.pdf)")
    dest.add_argument("-i", "--in-place", action="store_true", help="overwrite TARGET")
    p.add_argument("--no-info", dest="info", action="store_false", help="skip the Info dictionary")
    p.add_argument("--no-xmp", dest="xmp", action="store_false", help="skip the XMP metadata stream")
    p.add_argument(
        "--merge",
        action="store_true",
        help="keep target metadata keys not present in source (default: replace entirely)",
    )
    p.add_argument("--source-password", default="", help="password for an encrypted SOURCE")
    p.add_argument("--target-password", default="", help="password for an encrypted TARGET")
    p.add_argument(
        "--show",
        action="store_true",
        help="just print SOURCE's metadata as JSON (and TARGET's, if given) and exit",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.show:
            shown = {args.source: read_metadata(args.source, args.source_password)}
            if args.target:
                shown[args.target] = read_metadata(args.target, args.target_password)
            print(json.dumps(shown, indent=2, ensure_ascii=False))
            return 0
        if not args.target:
            print("dittopdf: error: TARGET is required unless --show is used", file=sys.stderr)
            return 2
        if args.in_place:
            output = args.target
        elif args.output:
            output = args.output
        else:
            stem = args.target[:-4] if args.target.lower().endswith(".pdf") else args.target
            output = f"{stem}.ditto.pdf"
        written = copy_metadata(
            args.source,
            args.target,
            output,
            info=args.info,
            xmp=args.xmp,
            merge=args.merge,
            source_password=args.source_password,
            target_password=args.target_password,
        )
    except (pikepdf.PdfError, pikepdf.PasswordError, OSError) as e:
        print(f"dittopdf: error: {e}", file=sys.stderr)
        return 1
    print(f"Wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
