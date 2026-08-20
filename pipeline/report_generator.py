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
from pipeline.settings_generator import minimum_candidates
from pipeline.utils.helpers import PROJECT_DIR, load_json

DOCS_DIR = PROJECT_DIR / "docs"
ARTIFACT_FILES = ("run.json", "focal_seats.json", "slate_seats.json")

# Written only when a run's settings are still on disk to build them from, so a
# run missing this publishes everything else rather than dropping off the page.
OPTIONAL_ARTIFACT_FILES = ("availability.json",)

# Runs kept out of the published report even though their artifacts are
# complete. Excluded by slug rather than deleted from outputs/, so the
# scenario stays reproducible if it's ever brought back.
EXCLUDED_RUN_SLUGS = {"basic-3-x-3-truncation"}

# The hand-edited page outline: which prose sections and which scenarios the
# report renders, and in what order. It is read, never written -- generate_report
# does not reconcile it against what it found, so a scenario leaves the page
# exactly when someone takes it out of this file.
#
# It sits in docs/ beside the prose it orders, which also means it is served with
# the site: the outline that decided what the page shows is fetchable next to the
# page, rather than living in a repo the reader may not have.
SECTIONS_FILE = "report-sections.json"
SECTIONS_PATH = DOCS_DIR / SECTIONS_FILE

# Used when SECTIONS_PATH is absent, which is the pre-outline behavior: these
# four prose sections, and every run whose artifacts are complete. Each is a
# file in docs/prose/; a missing or empty one renders as a placeholder rather
# than breaking the build, so the site is publishable before the writing is done.
DEFAULT_PROSE_SECTIONS = [
    {"id": "abstract", "file": "abstract.md", "title": "Abstract"},
    {"id": "background", "file": "background.md", "title": "Background"},
    {"id": "methodology", "file": "methodology.md", "title": "Methodology"},
    {"id": "conclusion", "file": "conclusion.md", "title": "Conclusion"},
]


def load_page_outline(path: Optional[Path] = None) -> Dict[str, Any]:
    """
    The page outline from report-sections.json: prose sections and the scenario
    allow-list, both in the order they should appear.

    Args:
        path: Outline file to read. Defaults to SECTIONS_PATH.

    Returns:
        {"prose_sections": [...], "scenarios": [...] or None}. `scenarios` is
        None when no outline governs the page -- the file is missing, or it
        omits the key -- and select_page_runs reads that as "every run".
    """
    path = path or SECTIONS_PATH
    if not path.is_file():
        print(f"[report_generator] No {path.name}; publishing every run with complete artifacts.")
        return {"prose_sections": list(DEFAULT_PROSE_SECTIONS), "scenarios": None}

    outline = load_json(path)
    return {
        "prose_sections": outline.get("prose_sections") or list(DEFAULT_PROSE_SECTIONS),
        "scenarios": outline.get("scenarios"),
    }


def _listed_slugs(scenarios: List[Any]) -> List[str]:
    """
    The slugs an outline's `scenarios` list asks for, in order.

    An entry is either a bare slug or an object carrying one, so a scenario can
    be parked with {"slug": ..., "enabled": false} instead of being deleted and
    later retyped from memory.
    """
    slugs = []
    for entry in scenarios:
        if isinstance(entry, str):
            slugs.append(entry)
        elif entry.get("enabled", True):
            slugs.append(entry["slug"])
    return slugs


def select_page_runs(runs: List[Dict[str, Any]], scenarios: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """
    The runs the page renders, in outline order.

    An allow-list, not a filter: a run the outline does not name stays out of
    index.html however complete its artifacts are. It keeps its entry in
    manifest.json, its folder in docs/data/, and its place in the cross-run
    comparison -- hiding a scenario is a decision about the page, and making it
    also drop the data would mean the two charts disagreed about what was run.

    Args:
        runs: Manifest run entries, as built by build_manifest.
        scenarios: The outline's `scenarios` list, or None to render them all.

    Returns:
        The subset of `runs` the outline names, ordered as it names them.
    """
    if scenarios is None:
        return runs

    by_slug = {run["slug"]: run for run in runs}
    selected, unknown = [], []
    for slug in _listed_slugs(scenarios):
        if slug in by_slug:
            selected.append(by_slug[slug])
        else:
            unknown.append(slug)

    if unknown:
        print(f"[report_generator] {SECTIONS_FILE} lists slug(s) with no artifacts, skipped: "
              f"{', '.join(unknown)}")
    hidden = [run["slug"] for run in runs if run["slug"] not in {s["slug"] for s in selected}]
    if hidden:
        print(f"[report_generator] Not in {SECTIONS_FILE}, kept off the page: {', '.join(hidden)}")
    return selected


def _scenario_entries(scenarios: Optional[List[Any]]) -> Dict[str, Dict[str, Any]]:
    """The outline's scenario entries in object form, keyed by slug."""
    entries = {}
    for entry in scenarios or []:
        if isinstance(entry, str):
            entries[entry] = {"slug": entry}
        else:
            entries[entry["slug"]] = entry
    return entries


def _artifact_paths(slug: str, source: Path) -> Dict[str, str]:
    """Where the page fetches one run's bundle from, relative to index.html."""
    paths = {
        "run": f"data/{slug}/run.json",
        "focalSeats": f"data/{slug}/focal_seats.json",
        "slateSeats": f"data/{slug}/slate_seats.json",
    }
    if (source / "availability.json").is_file():
        paths["availability"] = f"data/{slug}/availability.json"
    return paths


def resolve_composition(
    systems: List[Dict[str, Any]],
    runs: List[Dict[str, Any]],
    config_reference: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Resolve a section's configured `systems` into the sources the page selects between.

    A composed section is not a run: it collects one contest at a time out of
    however many runs, so 9 X 1 IRV and 3 X 5 STV can sit in one dropdown though
    they were simulated separately and live in different bundles. Each source
    carries everything the page needs to redraw for it -- where to fetch that
    run's records, which contest inside them is the one, and the parameters that
    produced it -- so switching systems never means reasoning about which run a
    number came from.

    Args:
        systems: The outline's `systems` list. Each entry names a `run` (slug)
            and a `system` (id), optionally `districts`/`seats` to disambiguate a
            run that uses one rule in more than one contest, optionally a
            `label` to override the name shown in the dropdown, and optionally a
            `blocModel` tag the page uses to build its Two Bloc / Four Bloc
            toggle.
        runs: Run metadata from discover_runs.
        config_reference: Rows from _config_reference, for the parameters card.

    Returns:
        One resolved source per entry that matched, in configured order. Entries
        naming a run or system that isn't there are reported and dropped, so a
        typo costs one option rather than the section.
    """
    by_slug = {meta["slug"]: meta for meta in runs}
    records_cache: Dict[str, List[Dict[str, Any]]] = {}
    resolved = []

    for spec in systems:
        run_slug = spec.get("run")
        meta = by_slug.get(run_slug)
        if meta is None:
            print(f"[report_generator] {SECTIONS_FILE}: no run '{run_slug}', skipping that system.")
            continue

        matches = [
            (dc, system)
            for dc in meta["districtConfigs"]
            for system in dc["systems"]
            if system["id"] == spec.get("system")
            and spec.get("districts") in (None, dc["numDistricts"])
            and spec.get("seats") in (None, dc["winners"])
        ]
        if not matches:
            print(f"[report_generator] {SECTIONS_FILE}: run '{run_slug}' has no system "
                  f"'{spec.get('system')}'{_shape_suffix(spec)}, skipping it.")
            continue
        if len(matches) > 1:
            print(f"[report_generator] {SECTIONS_FILE}: '{spec.get('system')}' is ambiguous in "
                  f"'{run_slug}'; add \"districts\"/\"seats\". Using the first.")
        dc, system = matches[0]

        # The combined view of a hybrid run is tagged numDistricts=0 with the
        # citywide total already in winners (see _tag_hybrid_combined); every
        # real contest multiplies out.
        seats = dc["winners"] if dc["numDistricts"] == 0 else dc["numDistricts"] * dc["winners"]
        shape = f"{dc['numDistricts']} × {dc['winners']}"
        # Prefer the row for this exact contest, so a run with two of them gets
        # its own pool and matrices rather than its first. Falling back to the
        # run's other row matters when a config has moved on from the results on
        # disk -- alternative_electoral.json asks for 15 districts while its
        # published results are 9 X 1 -- where the cohesion and alpha matrices
        # are still the ones that produced them, and dropping the row would take
        # the matrices off a card that had them before.
        by_run = [row for row in config_reference if row["run"] == meta["runName"]]
        cfg = next((row for row in by_run if row["shape"] == shape), None) \
            or (by_run[0] if by_run else {})

        resolved.append({
            # Stable across builds so a selection survives a rebuild, and unique
            # even when one run contributes two contests under the same rule.
            "id": f"{meta['slug']}::{system['id']}::{dc['numDistricts']}x{dc['winners']}",
            "label": spec.get("label") or system["label"],
            # Which bloc model this source was simulated under ("two"/"four"), or
            # None for a section that doesn't distinguish. Purely a pass-through
            # of the outline's own tag -- resolve_composition doesn't infer it
            # from the run, since a run's own bloc count isn't otherwise recorded
            # anywhere the page can read it.
            "blocModel": spec.get("blocModel"),
            "run": meta["slug"],
            "runName": meta["runName"],
            "system": system["id"],
            "systemLabel": system["label"],
            "numDistricts": dc["numDistricts"],
            "winners": dc["winners"],
            # What the proportional-representation line is a share of.
            "seats": seats,
            "shape": shape,
            "seatMax": meta["seatMax"],
            "seatTicks": meta.get("seatTicks"),
            # This contest's models, not the run's: a run that scores some of its
            # contests under name_cumulative and ranks the rest would otherwise
            # offer a toggle with nothing behind it on the ones that never used it.
            "voterModels": _models_of_contest(meta, dc, system, records_cache),
            "plans": dc.get("plans"),
            "replicates": meta.get("replicates"),
            "turnout": meta.get("turnout"),
            "primaryTurnout": meta.get("primaryTurnout"),
            "candidatePoolMin": cfg.get("candidatePoolMin"),
            "candidatePoolMax": dc.get("candidatePoolMax"),
            "candidatePoolMean": dc.get("candidatePoolMean"),
            # The bloc VAP shares as this run measured them, so a composed
            # section reports the electorate its selected system was run
            # against rather than the section's first run's.
            "slates": meta["slates"],
            "cohesion": cfg.get("cohesion") or {},
            "alphas": cfg.get("alphas") or {},
            # Carried per source rather than looked up in manifest.runs: a
            # composed section may draw on a run that is itself kept off the page.
            "data": _artifact_paths(meta["slug"], meta["_source"]),
        })

    return resolved


def _models_of_contest(
    meta: Dict[str, Any],
    dc: Dict[str, Any],
    system: Dict[str, Any],
    cache: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    The voter models one contest actually has records for, in the run's order.

    A run's `voterModels` covers everything it simulated across all of its
    contests, which is not the same list per contest: Basic - 3 X 3 scores its
    Cumulative and Limited ballots under name_cumulative and ranks its STV
    ballots under the other two, so offering the run's list against its STV
    contest would put a toggle on screen that can only ever draw nothing.

    Falls back to the run's full list if the records can't be read -- a toggle
    too many is a better failure than a section with no models at all.
    """
    slug = meta["slug"]
    if slug not in cache:
        try:
            cache[slug] = load_json(meta["_source"] / "focal_seats.json")
        except (OSError, ValueError):
            cache[slug] = []
    records = cache[slug]
    if not records:
        return meta["voterModels"]

    present = {
        r["mode"] for r in records
        if r.get("system") == system["id"]
        and r.get("numDistricts", dc["numDistricts"]) == dc["numDistricts"]
        and r.get("winners", dc["winners"]) == dc["winners"]
    }
    models = [m for m in meta["voterModels"] if m["id"] in present]
    return models or meta["voterModels"]


def _shape_suffix(spec: Dict[str, Any]) -> str:
    """' at 3 × 5' for a spec that pinned a contest, '' for one that didn't."""
    if spec.get("districts") is None and spec.get("seats") is None:
        return ""
    return f" at {spec.get('districts', '*')} × {spec.get('seats', '*')}"


def attach_compositions(
    manifest: Dict[str, Any],
    runs: List[Dict[str, Any]],
    scenarios: Optional[List[Any]],
) -> None:
    """
    Give every composed section its resolved sources, in place on the manifest.

    Only sections the outline configures a `systems` list for are touched; every
    other run entry keeps describing exactly the run it came from.
    """
    for slug, entry in _scenario_entries(scenarios).items():
        if not entry.get("systems"):
            continue
        target = next((run for run in manifest["runs"] if run["slug"] == slug), None)
        if target is None:
            print(f"[report_generator] {SECTIONS_FILE}: '{slug}' configures systems but has no "
                  "artifacts; nothing to compose.")
            continue
        composition = resolve_composition(
            entry["systems"], runs, manifest.get("configReference") or [],
        )
        if not composition:
            print(f"[report_generator] {SECTIONS_FILE}: '{slug}' resolved to no systems; "
                  "leaving it as its own run.")
            continue
        target["composition"] = composition
        if entry.get("title"):
            target["name"] = entry["title"]
        print(f"[report_generator] {slug}: composed from {len(composition)} system(s) -> "
              f"{', '.join(s['label'] for s in composition)}")


def _rebase_relative_srcs(html: str, source_dir: Path, docs_dir: Path) -> str:
    """
    Rewrite relative image paths from prose-relative to index.html-relative.

    A prose file links its images the way an editor's preview needs them --
    ../assets/x.png from docs/prose/, ../../assets/x.png from docs/prose/runs/.
    index.html sits at the root of docs/, so those same paths would climb out of
    docs/ entirely and 404. Each one is resolved against the file it was written
    in and re-expressed relative to docs/, which keeps the markdown previewable
    and the built page correct.

    Absolute URLs, root-relative paths, and data: URIs are left alone, as is
    anything that genuinely resolves outside docs/ -- that is a broken link in
    the prose, and rewriting it would only hide it.
    """
    import os

    def rebase(match: re.Match) -> str:
        src = match.group("src")
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|/|#)", src, re.I):
            return match.group(0)
        resolved = (source_dir / src).resolve()
        try:
            relative = resolved.relative_to(docs_dir.resolve())
        except ValueError:
            print(f"[report_generator] warning: {source_dir.name} links outside docs/: {src}")
            return match.group(0)
        return f'{match.group("prefix")}{relative.as_posix().replace(os.sep, "/")}"'

    return re.sub(r'(?P<prefix><img[^>]*?\ssrc=")(?P<src>[^"]*)"', rebase, html)


def _render_markdown(path: Path, docs_dir: Path = DOCS_DIR) -> str:
    """One prose file as an HTML fragment, or "" when it has nothing in it yet."""
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    import mistune

    # "math" only marks the delimiters it finds -- $$..$$ becomes <div class="math">
    # and $..$ becomes <span class="math">, both left as literal TeX for KaTeX to
    # typeset in the browser (see the render call in the template). Without it,
    # markdown chews through the TeX itself: \sim and friends read as escapes.
    html = mistune.create_markdown(plugins=["table", "footnotes", "math"])(text)
    return _rebase_relative_srcs(html, path.parent, docs_dir)


def _run_prose_path(docs_dir: Path, slug: str) -> Path:
    """Where a run's own hand-written description lives, if it has one."""
    return docs_dir / "prose" / "runs" / f"{slug}.md"


def _ensure_run_prose_stubs(docs_dir: Path, runs: List[Dict[str, Any]]) -> None:
    """
    Touch an empty prose file for any run that doesn't have one yet.

    The top-level prose files (abstract.md, background.md, ...) are
    discoverable because they already exist, blank, ready to open and edit --
    this gives each run's description the same discoverability instead of a
    filename convention that's only written down.
    """
    prose_dir = docs_dir / "prose" / "runs"
    prose_dir.mkdir(parents=True, exist_ok=True)
    for meta in runs:
        path = _run_prose_path(docs_dir, meta["slug"])
        if not path.exists():
            path.touch()


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
        if meta.get("slug") in EXCLUDED_RUN_SLUGS:
            continue
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
    # Drop any pooled rows already present: summarize_results emits them now, and
    # re-pooling a pooled row would count the average twice. Filtering first makes
    # this the same answer whether or not the artifact already carries them.
    records = [r for r in records if not r.get("pooled")]

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


def _system_total_seats(meta: Dict[str, Any], system: str) -> int:
    """
    The number of seats one voting system fills in a run.

    A run's districtConfigs entry gives seats per district, so a real contest
    fills numDistricts * winners; the synthetic combined entry of a hybrid run is
    tagged numDistricts=0 with the citywide total already in winners
    (_tag_hybrid_combined). A system appearing in more than one contest -- the
    same rule run at two sizes -- sums them, since its row pools those records.

    Falls back to the run's own total when the system isn't in districtConfigs at
    all, which keeps a share computable rather than dividing by zero.
    """
    seats = sum(
        dc["winners"] if dc["numDistricts"] == 0 else dc["numDistricts"] * dc["winners"]
        for dc in meta["districtConfigs"]
        if any(s["id"] == system for s in dc["systems"])
    )
    return seats or meta["totalSeats"]


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
        meta["_slate_records"] = slate_records

        systems = sorted({r["system"] for r in records})
        for system in systems:
            rows = [r for r in records if r["system"] == system]
            label = rows[0]["systemLabel"] if rows else system
            series.append({
                # How many seats this system actually fills, which is not the
                # run's total when a run splits its seats across two contests:
                # the comparison plots a share of seats, and a 1 X 6 contest
                # measured against its run's citywide 15 would read as less than
                # half the representation it is.
                "totalSeats": _system_total_seats(meta, system),
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
    # Straight from MODE_COLORS, the pooled row included: one definition of what
    # colour a voter model is, shared by the figures and the page.
    palette = {mode: MODE_COLORS.get(mode, DEFAULT_MODE_COLOR) for mode in LEGEND_MAPPING}

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
                    **(
                        {"availability": f"data/{m['slug']}/availability.json"}
                        if (m["_source"] / "availability.json").is_file()
                        else {}
                    ),
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
                # The floor of the pool draw, which is the smallest ballot every
                # rule in the run can actually be run on rather than a number
                # anyone chose -- see settings_generator.minimum_candidates. It
                # completes the min/mean/max the card reports, and it is derived
                # here because the run artifacts have never carried it.
                "candidatePoolMin": minimum_candidates(cfg),
                "candidatePoolMax": dc.get("candidate_pool_max"),
                "candidatePoolMean": dc.get("candidate_pool_mean"),
                # The configured matrices, not the per-district ones in the
                # settings files: those are renormalised over whichever slates
                # drew a candidate there, so they describe a district rather than
                # the run.
                "cohesion": cfg.get("cohesion_parameters") or {},
                "alphas": cfg.get("alphas") or {},
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
        for name in OPTIONAL_ARTIFACT_FILES:
            source = meta["_source"] / name
            if source.is_file():
                shutil.copyfile(source, target / name)
            elif (target / name).is_file():
                (target / name).unlink()

        # The by-slate table is republished with its pooled rows filled in, so a
        # chart can offer the combined reading for any slate. summarize_results
        # emits them itself now; this is a deterministic derivation from the same
        # file, so it stays reproducible for artifacts written before it did.
        n_models = len([m for m in meta["voterModels"] if not m["pooled"]]) or 1
        slate_rows = _slate_records_with_pooled(load_json(target / "slate_seats.json"), n_models)
        with open(target / "slate_seats.json", "w", encoding="utf-8") as f:
            json.dump(slate_rows, f, indent=1)

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

    outline = load_page_outline(docs_dir / SECTIONS_FILE)
    runs = discover_runs()
    if not runs:
        print("[report_generator] No run artifacts found; run summarize_results first.")

    docs_dir.mkdir(parents=True, exist_ok=True)
    copy_artifacts(runs, docs_dir)

    manifest = build_manifest(runs)
    manifest["configReference"] = _config_reference(config_dir)
    # Sections that collect systems from several runs, resolved once here so the
    # page never has to work out which bundle a dropdown entry belongs to.
    attach_compositions(manifest, runs, outline["scenarios"])
    data_dir = docs_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)

    # What the page shows is the outline's call, not the pipeline's: the
    # manifest and docs/data written above stay complete either way.
    prose_sections = outline["prose_sections"]
    page_runs = select_page_runs(manifest["runs"], outline["scenarios"])

    prose = {
        section["id"]: {
            **section,
            "html": _render_markdown(docs_dir / "prose" / section["file"], docs_dir),
        }
        for section in prose_sections
    }

    _ensure_run_prose_stubs(docs_dir, page_runs)
    run_prose = {
        meta["slug"]: _render_markdown(_run_prose_path(docs_dir, meta["slug"]), docs_dir)
        for meta in page_runs
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
        prose_order=[s["id"] for s in prose_sections],
        run_prose=run_prose,
        runs=page_runs,
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
        f"[report_generator] {len(page_runs)} of {len(runs)} run(s) -> {index_path} "
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
    # KaTeX typesets the prose's math in the browser. Unlike the other two CDN
    # entries this stylesheet pulls its own woff2 files from jsDelivr as it
    # renders, so the math fonts are the one asset on the page still fetched from
    # a third party -- vendor_assets could take them local the way it does
    # Google's if that matters more than the smaller build.
    {
        "url": "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css",
        "integrity": "sha384-nB0miv6/jRmo5UMMR1wu3Gz6NLsoTkbqJghGIsx//Rlm+ZU03BU6SQNC66uf4l5+",
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
    {
        "url": "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js",
        "integrity": "sha384-7zkQWkzuo3B5mTepMUcHkMB5jZaolc2xDwL6VFqjFALcbeS9Ggm/Yr2r3Dy4lfFg",
    },
    # Finds the delimiters mistune left behind and hands what is between them to
    # KaTeX; loads after katex.min.js, which it calls.
    {
        "url": "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js",
        "integrity": "sha384-43gviWU0YVjaDtb/GhzOouOXtZMP/7XUzwPTstBeZFe/+rCMvRwr4yROQP43s0Xk",
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
