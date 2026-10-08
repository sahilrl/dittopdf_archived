"""Post-copy verification: re-inspect the output and compare it with both inputs."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from dittopdf.services.comparison import compare
from dittopdf.services.copier import OUTCOMES
from dittopdf.services.model import READONLY, REGENERATE, UNREPRODUCIBLE

VERIFY = {
    "match": "Matches the original",
    "expected": "Necessarily or deliberately differs",
    "unreproduced": "Could not be reproduced",
    "unexpected": "Still different (unexpected)",
}


def build_result(orig: dict, second: dict, output: dict, rows: list[dict], copy: dict,
                 *, annotations_merged: bool = False) -> dict[str, Any]:
    vs_orig = {r["id"]: r for r in compare(orig, output)["rows"]}
    vs_second = {r["id"]: r for r in compare(second, output)["rows"]}
    outcome_of = {i["id"]: i for i in copy["items"]}

    # Group copy outcomes for the report lists.
    by_outcome: dict[str, list[dict]] = defaultdict(list)
    for item in copy["items"]:
        by_outcome[item["outcome"]].append(item)

    verify: dict[str, list[dict]] = defaultdict(list)
    for id, r in vs_orig.items():
        if not (r["o"]["present"] or r["s"]["present"]):
            continue
        item = outcome_of.get(id)
        outcome = item["outcome"] if item else ""
        same = r["status"] in ("same", "absent")
        s_row = vs_second.get(id)
        entry = {
            "id": id, "section": r["path"][0], "group": r["path"][1] if len(r["path"]) > 1 else "",
            "label": r["label"], "original": r["o"]["display"] if r["o"]["present"] else "—",
            "output": r["s"]["display"] if r["s"]["present"] else "—",
            "second": s_row["o"]["display"] if s_row and s_row["o"]["present"] else "—",
            "outcome": OUTCOMES.get(outcome, ""), "reason": (item or {}).get("message") or r["note"],
        }
        if same:
            verify["match"].append(entry)
        elif item and item.get("partial"):
            verify["expected"].append(entry)
        elif item is None and not r["o"]["present"]:
            entry["reason"] = "Present only in the output: it comes from the second PDF's content."
            verify["expected"].append(entry)
        elif annotations_merged and id.startswith("annot:"):
            entry["reason"] = "Annotations were merged, so positions differ from the original's."
            verify["expected"].append(entry)
        elif outcome == "failed":
            verify["unreproduced"].append(entry)
        elif outcome in ("kept", "overridden", "regenerated", "readonly") or \
                (outcome == "removed" and not r["o"]["present"]) or \
                r["cls"] in (REGENERATE, READONLY, UNREPRODUCIBLE):
            if not entry["reason"]:
                entry["reason"] = {READONLY: "Describes the second PDF's own content.",
                                   REGENERATE: "Produced by the PDF writer.",
                                   UNREPRODUCIBLE: "Cannot be reproduced reliably."}.get(r["cls"], "")
            verify["expected"].append(entry)
        else:
            verify["unexpected"].append(entry)

    def sections(items: list[dict]) -> list[tuple[str, list[dict]]]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            grouped[it["section"]].append(it)
        return list(grouped.items())

    o_sum, s_sum, x_sum = orig["summary"], second["summary"], output["summary"]
    identical = x_sum["sha256"] == o_sum["sha256"]
    return {
        "outcomes": [(k, OUTCOMES[k], sections(by_outcome.get(k, []))) for k in OUTCOMES],
        "outcome_counts": {k: len(v) for k, v in by_outcome.items()},
        "verify": [(k, VERIFY[k], sections(verify.get(k, []))) for k in VERIFY],
        "verify_counts": {k: len(v) for k, v in verify.items()},
        "verify_by_section": _by_section(verify),
        "notes": copy["notes"],
        "unavoidable": copy.get("unavoidable", []),
        "objects": copy.get("objects"),
        "save": copy["save"],
        "files": [("Original", o_sum), ("Second PDF", s_sum), ("Output", x_sum)],
        "byte_identical": identical,
        "pages_kept": x_sum["pages"] == s_sum["pages"],
    }


def _by_section(verify: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    out: dict[str, Counter] = defaultdict(Counter)
    for k, items in verify.items():
        for it in items:
            out[it["section"]][k] += 1
    return [(s, dict(c)) for s, c in out.items()]
