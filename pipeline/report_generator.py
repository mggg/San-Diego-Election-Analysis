"""
Assemble the published report from the artifacts every run already writes.

summarize_results emits one JSON bundle per run (see
summarize_results.write_report_artifacts). This stage collects those bundles into
docs/, renders the prose, and writes docs/index.html -- so publishing new results
is a pipeline run rather than an editing session.

The page is data-driven: index.html carries the structure (sections, nav, chart
mount points) and the numbers arrive at runtime from docs/data/. Adding a run
changes manifest.json, not the HTML.

docs/ is what GitHub Pages serves. Bootstrap and D3 come from jsDelivr, pinned by
version and by SRI hash; the fonts are served from docs/ so no visitor's address
reaches a third party for them. `--vendor` fetches the fonts once; after that the
build needs no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline.summarize_results import (
    BAR_EDGE_COLOR,
    COMBINED_MODE,
    DEFAULT_MODE_COLOR,
    LEGEND_MAPPING,
    MODE_COLORS,
    PROP_LINE_COLOR,
    _prop_line_label,
    _rule_display_name,
)
from pipeline.utils.helpers import PROJECT_DIR, load_json

DOCS_DIR = PROJECT_DIR / "docs"
ARTIFACT_FILES = ("run.json", "focal_seats.json", "slate_seats.json")

# Prose sections, in the order they appear on the page. Each is a file in
# docs/prose/; a missing or empty one renders as a placeholder rather than
# breaking the build, so the site is publishable before the writing is done.
PROSE_SECTIONS = [
    {"id": "abstract", "file": "abstract.md", "title": "Abstract"},
    {"id": "background", "file": "background.md", "title": "Background"},
    {"id": "methodology", "file": "methodology.md", "title": "Methodology"},
    {"id": "conclusion", "file": "conclusion.md", "title": "Conclusion"},
]


def _render_markdown(path: Path) -> str:
    """One prose file as an HTML fragment, or "" when it has nothing in it yet."""
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    import mistune

    return mistune.create_markdown(plugins=["table", "footnotes"])(text)


def discover_runs(outputs_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Every run that has report artifacts, in a stable order between builds.

    A run appears here only once summarize_results has written its bundle, so a
    half-finished run never lands on the page.
    """
    outputs_dir = outputs_dir or (PROJECT_DIR / "outputs")
    runs = []
    for run_dir in sorted(outputs_dir.glob("*/summaries/report")):
        run_json = run_dir / "run.json"
        if not run_json.is_file():
            continue
        meta = load_json(run_json)
        if any(not (run_dir / name).is_file() for name in ARTIFACT_FILES):
            print(f"[report_generator] Skipping {meta.get('runName')}: incomplete artifacts.")
            continue
        meta["_source"] = run_dir
        runs.append(meta)

    # "basic"-prefixed runs first, then alphabetical -- the order the cross-run
    # figure already uses, so the page and the figure agree.
    runs.sort(key=lambda m: (not str(m["runName"]).lower().startswith("basic"), m["runName"]))
    return runs


def _slate_records_with_pooled(records: List[Dict[str, Any]], n_models: int) -> List[Dict[str, Any]]:
    """
    The per-slate table plus a pooled row per (system, slate, seat count).

    The pooled row is the same average _occurrence_counts computes for the focal
    group, applied per slate: sum the counts and divide by the number of voter
    models, so a (mode, seats) cell that only some models reached counts as zero
    for the rest rather than being left out of the average. Pooling here rather
    than in the browser keeps one definition of "combined" in the project.

    This is everything the comparison figure needs, for any slate and any voter
    model. The focal group is one of the slates and its rows reproduce the focal
    table exactly, per model and pooled, so the figure has a single code path.
    """
    totals: Dict[tuple, Dict[str, Any]] = {}
    for r in records:
        key = (r["system"], r["slate"], r["seats"])
        row = totals.get(key)
        if row is None:
            row = {**r, "mode": COMBINED_MODE, "modeLabel": LEGEND_MAPPING[COMBINED_MODE],
                   "pooled": True, "plans": 0.0}
            row.pop("share", None)
            totals[key] = row
        row["plans"] += r["plans"]

    for row in totals.values():
        row["plans"] = row["plans"] / n_models

    per_model = [{**r, "pooled": False} for r in records]
    return sorted(per_model + list(totals.values()),
                  key=lambda r: (r["system"], r["slate"], r["mode"], r["seats"]))


def _cross_run_series(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The comparison series: every run's focal-seat distribution, split per voting
    system so a multi-system run contributes one row per system rather than a
    pooled one that hides the differences between them.

    Every record is carried, not just the pooled ones the comparison figure
    draws, so the selection there can change without a rebuild.
    """
    series = []
    for meta in runs:
        records = load_json(meta["_source"] / "focal_seats.json")  # for the labels only

        # Per-slate rows for the same figure, so the comparison can be read for
        # any slate and any voter model, not only the focal group pooled.
        n_models = len([m for m in meta["voterModels"] if not m["pooled"]]) or 1
        slate_records = _slate_records_with_pooled(
            load_json(meta["_source"] / "slate_seats.json"), n_models,
        )

        systems = sorted({r["system"] for r in records})
        for system in systems:
            rows = [r for r in records if r["system"] == system]
            label = rows[0]["systemLabel"] if rows else system
            series.append({
                # Stable across builds: the page keys its rows on this, so a
                # selection survives a rebuild that adds or reorders runs.
                "id": f"{meta['slug']}::{system}",
                "run": meta["runName"],
                "runSlug": meta["slug"],
                "system": system,
                "systemLabel": label,
                # Just the run name when it already names its only system.
                "panelLabel": (
                    meta["runName"]
                    if len(systems) == 1 and label.lower() in meta["runName"].lower()
                    else f"{meta['runName']} - {label}"
                ),
                "records": [r for r in slate_records if r["system"] == system],
            })
    return series


def build_manifest(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    The one file the page fetches first: palette, labels, the run list with
    artifact paths, and the cross-run series.
    """
    palette = {mode: MODE_COLORS.get(mode, DEFAULT_MODE_COLOR) for mode in LEGEND_MAPPING}
    palette[COMBINED_MODE] = "#898781"  # the pooled row reads as ink, not a model

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "palette": palette,
        "modeLabels": dict(LEGEND_MAPPING),
        "colors": {"referenceLine": PROP_LINE_COLOR, "barEdge": BAR_EDGE_COLOR},
        "runs": [
            {
                "slug": m["slug"],
                "name": m["runName"],
                "focalGroup": m["focalGroup"],
                "focalGroupLabel": m["focalGroupLabel"],
                "totalSeats": m["totalSeats"],
                "seatMax": m["seatMax"],
                "focalVapShare": m["focalVapShare"],
                "proportionalSeats": m["proportionalSeats"],
                "proportionalLabel": m["proportionalLabel"],
                "districtConfigs": m["districtConfigs"],
                # Each slate's own reference line, worded by the helper every
                # figure shares so one dotted line is never described two ways.
                "slates": [
                    {
                        **slate,
                        "proportionalSeats": slate["vapShare"] * m["totalSeats"],
                        "proportionalLabel": _prop_line_label(
                            slate["label"], slate["vapShare"], m["totalSeats"],
                        ),
                    }
                    for slate in m["slates"]
                ],
                "data": {
                    "run": f"data/{m['slug']}/run.json",
                    "focalSeats": f"data/{m['slug']}/focal_seats.json",
                    "slateSeats": f"data/{m['slug']}/slate_seats.json",
                },
            }
            for m in runs
        ],
        "crossRun": _cross_run_series(runs),
    }


def _config_reference(config_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    The configuration table, read from configs/ rather than hand-maintained --
    a row can no longer disagree with the run it describes.

    Rules and voter models go through the same label helpers the figures use, so
    the table says "STV" and "Impulsive" rather than the FASTSTV and slate_pl the
    configs are written in.
    """
    config_dir = config_dir or (PROJECT_DIR / "configs")
    rows = []
    for path in sorted(config_dir.glob("*.json")):
        cfg = load_json(path)
        for dc in cfg.get("district_configs", []):
            rows.append({
                "run": cfg.get("run_name", path.stem),
                "shape": f"{dc['num_districts']} × {dc['winners']}",
                "totalSeats": cfg.get("total_seats"),
                "rules": [_rule_display_name(r) for r in (cfg.get("voting_configs") or {})],
                "voterModels": [
                    LEGEND_MAPPING.get(m, str(m).replace("_", " ").title())
                    for m in (cfg.get("voter_models") or [])
                ],
                "turnout": cfg.get("turnout") or {},
                "primaryTurnout": cfg.get("primary_turnout"),
                "candidatePoolMax": dc.get("candidate_pool_max"),
                "candidatePoolMean": dc.get("candidate_pool_mean"),
            })
    return rows


def _asset_version(docs_dir: Path) -> str:
    """A short hash over every front-end source file, for cache busting."""
    digest = hashlib.sha256()
    for path in sorted(docs_dir.glob("js/**/*.js")) + sorted(docs_dir.glob("css/*.css")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _import_map(docs_dir: Path, version: str) -> str:
    """
    An import map pinning every chart module to the current build.

    ES modules import each other by static path, so a version query on the entry
    point never reaches them: a browser holding one stale module alongside fresh
    ones fails with an import error rather than merely showing old numbers. The
    map rewrites every specifier at once, so the graph can only ever load as a
    set. Keys stay relative so this works both at a domain root and under the
    project subpath GitHub Pages serves from.
    """
    modules = sorted(
        p.relative_to(docs_dir).as_posix()
        for p in docs_dir.glob("js/**/*.js")
        if "vendor" not in p.parts  # vendor scripts load as classic <script>
    )
    return json.dumps(
        {"imports": {f"./{m}": f"./{m}?v={version}" for m in modules}},
        indent=1,
    )


def copy_artifacts(runs: List[Dict[str, Any]], docs_dir: Path) -> None:
    """
    Mirror each run's bundle into docs/data/<slug>/.

    A mirror, not an accumulation: a run whose outputs are gone -- deleted,
    renamed, cleared for a re-run -- has its directory removed too. Without that
    its files stay in docs/ and get published, referenced by no manifest and
    regenerated by no pipeline run, which is exactly the state that makes a
    published page impossible to reproduce from the repository.
    """
    data_dir = docs_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    for meta in runs:
        target = data_dir / meta["slug"]
        target.mkdir(parents=True, exist_ok=True)
        for name in ARTIFACT_FILES:
            shutil.copyfile(meta["_source"] / name, target / name)

    live = {meta["slug"] for meta in runs}
    for stale in sorted(p for p in data_dir.iterdir() if p.is_dir() and p.name not in live):
        print(f"[report_generator] Removing {stale.relative_to(docs_dir)}: no run produces it.")
        shutil.rmtree(stale)


def generate_report(docs_dir: Path = DOCS_DIR, config_dir: Optional[Path] = None) -> Path:
    """
    Build docs/ from the runs' artifacts and the prose.

    Returns:
        Path to the written index.html.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    runs = discover_runs()
    if not runs:
        print("[report_generator] No run artifacts found; run summarize_results first.")

    docs_dir.mkdir(parents=True, exist_ok=True)
    copy_artifacts(runs, docs_dir)

    manifest = build_manifest(runs)
    manifest["configReference"] = _config_reference(config_dir)
    data_dir = docs_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)

    prose = {
        section["id"]: {**section, "html": _render_markdown(docs_dir / "prose" / section["file"])}
        for section in PROSE_SECTIONS
    }

    env = Environment(
        loader=FileSystemLoader(str(docs_dir / "templates")),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    version = _asset_version(docs_dir)
    html = env.get_template("index.html.j2").render(
        prose=prose,
        prose_order=[s["id"] for s in PROSE_SECTIONS],
        runs=manifest["runs"],
        generated=manifest["generated"],
        repo_url="https://github.com/mggg/San-Diego-Election-Analysis",
        asset_version=version,
        import_map=_import_map(docs_dir, version),
        stylesheets=CDN_STYLESHEETS,
        scripts=CDN_SCRIPTS,
    )
    index_path = docs_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    print(
        f"[report_generator] {len(runs)} run(s) -> {index_path} "
        f"({len(manifest['crossRun'])} cross-run series)"
    )
    return index_path


# --- Third-party assets -------------------------------------------------------
#
# Bootstrap and D3 are loaded from jsDelivr, pinned to an exact version and to an
# SRI hash of the bytes this report was built and checked against. The hash is
# the load-bearing part: without it a CDN could serve anything under these URLs.
# A mismatch blocks the file outright, so tampering surfaces as a page that does
# not render rather than one that quietly does something else.
#
# Both are used as classic scripts, so they are outside the import map -- see
# _import_map, which versions this project's own modules.
#
# The fonts stay local. They are the largest slice of what used to be vendored,
# and the only one where fetching from a third party would send every reader's IP
# address somewhere they did not choose.

CDN_STYLESHEETS = [
    {
        "url": "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
        "integrity": "sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH",
    },
]

CDN_SCRIPTS = [
    {
        "url": "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
        "integrity": "sha384-YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz",
    },
    {
        "url": "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js",
        "integrity": "sha384-CjloA8y00+1SDAUkjs099PVfnY2KmDC2BZnws9kh8D/lX1s46w6EPhpXdqMfjK6i",
    },
]

FONT_CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Playfair+Display:wght@400;700"
    "&family=Source+Sans+Pro:ital,wght@0,400;0,600;1,400"
    "&family=Source+Serif+Pro:wght@300;400;600;700&display=swap"
)


def vendor_assets(docs_dir: Path = DOCS_DIR) -> None:
    """
    Download the fonts into docs/, once.

    Google serves a stylesheet of @font-face rules pointing at its own CDN, so
    the woff2 files behind it are pulled down and the URLs rewritten to local
    paths -- which is the point: after this the published page asks Google for
    nothing, and no reader's address reaches it.
    """
    import requests

    # A modern UA gets woff2; without one Google serves much older formats.
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    css = requests.get(FONT_CSS_URL, headers=headers, timeout=30).text
    fonts_dir = docs_dir / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)

    for font_url in sorted(set(re.findall(r"url\((https://[^)]+\.woff2)\)", css))):
        name = "-".join(font_url.rsplit("/", 2)[-2:])
        (fonts_dir / name).write_bytes(requests.get(font_url, timeout=30).content)
        css = css.replace(font_url, name)
    (fonts_dir / "fonts.css").write_text(css, encoding="utf-8")
    print(f"[report_generator] vendored {len(list(fonts_dir.glob('*.woff2')))} font files")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the docs/ report site.")
    parser.add_argument(
        "--vendor", action="store_true",
        help="Download the fonts into docs/ (needs network; run once, then commit).",
    )
    args = parser.parse_args()
    if args.vendor:
        vendor_assets()
    generate_report()


if __name__ == "__main__":
    main()
