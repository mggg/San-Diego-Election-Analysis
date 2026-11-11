"""
Ballot-length cap sweep: one ballot generation, one election run per cap.

The question this answers: if voters were only allowed to rank N candidates in
one contest of an election system, how do the results change? The default case
-- the base run -- lets every voter rank every candidate, and that is what the
slate_pl / slate_bt voter models produce. This script takes those full ballots
and, for each N in a list, writes a derived run in which every ballot of one
tier is cut to its first N choices. Elections and summaries then run on each
derived run exactly as they do on any other run.

This is a ballot-design rule (a hard cap), not a voter behavior. It is
deliberately different from pipeline.utils.cambridge_truncation, which draws a
random length per voter from Cambridge's historical distribution. Here the cap
is deterministic and applied to the *same* ballots for every N, so the runs are
nested and paired: a difference between cap 6 and cap 7 comes from the cap alone,
never from a different random draw. A ballot already shorter than N is left
alone, and ballots are never lengthened.

Only the chosen tier is capped. A tier is identified by its district count, the
same key cambridge_truncation.tier_overrides uses and the same number that
prefixes profiles.zip entries (<mode>/<district_num>/<file>). In the City Charter
proposal the 1 X 6 STV contest is tier 1 and the nine single-winner IRV districts
are tier 9, so `--tier 1` caps the STV contest and leaves the IRV profiles
byte-for-byte identical to the base run.

The base run must hold full ballots: set up its config without a
"cambridge_truncation" block (or with "enabled": false), run it through the
pipeline once, then run this script against it.

Usage:
    python -m pipeline.cap_ballot_length configs/charter-amendment-proposal-full-ballots.json \\
        --tier 1 --caps 4 5 6 7 8 9 10
    python -m pipeline.cap_ballot_length <config> --tier 1 --caps 4 5 6 --run

Each cap N produces:
    outputs/<prefix> - Ballot Cap N/   settings, capped profiles.zip, cap_stats.json
    configs/ballot_caps/<slug>.json    a config for that derived run

The derived configs sit in a subdirectory so `run.py --run-all` (which only reads
configs/*.json) does not pick them up. Run one with `python run.py
configs/ballot_caps/<slug>.json`, or pass --run to do all of them here.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from joblib import Parallel, delayed
from votekit import RankProfile

from pipeline.profile_generator import profile_class_for_mode
from pipeline.simulate_elections import _load_profile_from_bytes
from pipeline.truncate_profiles import _is_ranked, _parse_arcname
from pipeline.utils.ballot_length_cap import truncate_profile as cap_profile
from pipeline.utils.helpers import (
    election_results_signature,
    load_json,
    load_run_config,
    profiles_signature,
)

# Suffix the base run's name carries; stripped to build the derived run names, so
# "City Charter Amendment Proposal - Full Ballots" gives
# "City Charter Amendment Proposal - Ballot Cap 6".
FULL_BALLOTS_SUFFIX = " - Full Ballots"

CONFIG_OUT_DIR = Path("configs") / "ballot_caps"


def _resolve_config_path(name: str) -> Path:
    for candidate in (Path(name), Path("configs") / name, Path("configs") / f"{name}.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Config '{name}' not found.")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _new_stats() -> Dict[str, float]:
    return {
        "profiles": 0,
        "voters": 0.0,
        "voters_shortened": 0.0,
        "ranked_positions_before": 0.0,
        "ranked_positions_after": 0.0,
    }


def _accumulate(stats: Dict[str, float], before: RankProfile, after: RankProfile, cap: int) -> None:
    stats["profiles"] += 1
    stats["voters"] += sum(b.weight for b in before.ballots)
    stats["voters_shortened"] += sum(b.weight for b in before.ballots if len(b.ranking) > cap)
    stats["ranked_positions_before"] += sum(len(b.ranking) * b.weight for b in before.ballots)
    stats["ranked_positions_after"] += sum(len(b.ranking) * b.weight for b in after.ballots)


def _cap_one(arcname: str, data: bytes, caps: List[int]):
    """
    Worker: parse one full-ballot profile and cap it at every requested length.

    Runs in a joblib worker process. Parsing the csv and regrouping ballots is the
    expensive part of the whole build (about a second per cap per profile), so it is
    done here, once per profile, and the per-cap csv text and stats deltas are sent
    back for the parent to write.

    Returns:
        (arcname, {cap: capped csv text}, {cap: stats delta}).
    """
    profile = _load_profile_from_bytes(data, profile_class_for_mode(_parse_arcname(arcname)[0]))
    texts: Dict[int, str] = {}
    deltas: Dict[int, Dict[str, float]] = {}
    for cap in caps:
        capped_profile = cap_profile(profile, cap)
        delta = _new_stats()
        _accumulate(delta, profile, capped_profile, cap)
        texts[cap] = capped_profile.to_csv()
        deltas[cap] = delta
    return arcname, texts, deltas


def _reuse_untouched_results(base_dir: Path, base_name: str, derived: Dict[int, dict], tier: int) -> int:
    """
    Seed each derived run with the base run's election results for the tiers the cap
    did not change, so simulate_elections skips them as up to date.

    A cap on one tier leaves the other tiers' profiles byte-identical, so re-running
    their elections would only redo the same work (for the charter, the 9 X 1 IRV tier
    is about 90% of the profiles). The files are renamed and relabelled for the derived
    run, and are only copied when the base's election-results signature equals the
    derived config's, which is exactly the condition under which simulate_elections
    would itself have reused them.

    Returns:
        Number of result files copied per derived run.
    """
    copied = 0
    first_cap = next(iter(derived))
    for cap, d in derived.items():
        expected = election_results_signature(load_run_config(d["config_out"]))
        for src in sorted((base_dir / "election_results").glob("*/*.json")):
            with open(src, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("district_num") == tier or data.get("signature") != expected:
                continue
            data["run_name"] = d["name"]
            dest = d["dir"] / "election_results" / src.parent.name / src.name.replace(base_name, d["name"], 1)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            if cap == first_cap:
                copied += 1
    return copied


def _finish_stats(stats: Dict[str, float], tier: int, cap: int) -> dict:
    voters = stats["voters"]
    return {
        "tier": tier,
        "max_length": cap,
        "profiles_capped": stats["profiles"],
        "share_of_voters_shortened": stats["voters_shortened"] / voters if voters else 0.0,
        "mean_ballot_length_before": stats["ranked_positions_before"] / voters if voters else 0.0,
        "mean_ballot_length_after": stats["ranked_positions_after"] / voters if voters else 0.0,
    }


def build_cap_runs(
    config_path: Path,
    tier: int,
    caps: List[int],
    prefix: Optional[str] = None,
    force: bool = False,
    allow_truncated: bool = False,
) -> List[Path]:
    """
    Write one derived run per cap from a base run's full-ballot profiles.

    Args:
        config_path: The base run's config (full ballots, already generated).
        tier: District count of the tier to cap, e.g. 1 for the 1 X 6 STV contest.
        caps: Maximum ballot lengths, one derived run each.
        prefix: Derived run names are "<prefix> - Ballot Cap N". Defaults to the
            base run name without a trailing " - Full Ballots".
        force: Replace derived runs that already exist. Without it an existing
            derived run is an error, because its old election results would
            otherwise be reused as if they belonged to the new profiles.
        allow_truncated: Permit a base run whose profiles were already
            Cambridge-truncated at generation. Capping those ballots is not the
            "everyone may rank everything" baseline this sweep is defined against.

    Returns:
        Paths of the derived run configs written, in cap order.

    Raises:
        ValueError: On an unknown tier, bad caps, or a base run that is not
            full-ballot.
        FileNotFoundError: If the base run's profiles or settings are missing.
        FileExistsError: If a derived run exists and force is not set.
    """
    raw_config = load_json(config_path)
    config = load_run_config(config_path)
    run_name = config["run_name"]

    tiers = [d["num_districts"] for d in config["district_configs"]]
    if tier not in tiers:
        raise ValueError(f"Tier {tier} is not one of this run's district counts {tiers}.")
    caps = sorted(set(caps))
    if not caps or any(c < 1 for c in caps):
        raise ValueError("Caps must be positive integers.")

    cambridge = config.get("cambridge_truncation")
    if isinstance(cambridge, dict) and cambridge.get("enabled") is True and not allow_truncated:
        raise ValueError(
            f"'{run_name}' has cambridge_truncation enabled, so its profiles are already "
            "shortened. The cap sweep needs full ballots as its baseline: remove that block "
            "(or set enabled to false), regenerate the run, and try again. "
            "Pass --allow-truncated to cap them anyway."
        )

    base_dir = Path("outputs") / run_name
    base_zip = base_dir / "profiles.zip"
    base_settings = base_dir / "settings"
    if not base_zip.is_file():
        raise FileNotFoundError(f"No profiles for '{run_name}' at {base_zip}; run the base config first.")
    if not base_settings.is_dir():
        raise FileNotFoundError(f"No settings for '{run_name}' at {base_settings}.")

    meta_path = base_dir / "profiles_metadata.json"
    if meta_path.is_file() and load_json(meta_path).get("signature") != profiles_signature(config):
        print(
            f"[cap_ballot_length] WARNING: '{run_name}' profiles were generated under a "
            "different config signature than the config now says."
        )

    prefix = prefix or (run_name[: -len(FULL_BALLOTS_SUFFIX)] if run_name.endswith(FULL_BALLOTS_SUFFIX) else run_name)

    # Derived run bookkeeping, built up front so nothing is half-written when a
    # name collision is discovered.
    derived: Dict[int, dict] = {}
    for cap in caps:
        name = f"{prefix} - Ballot Cap {cap}"
        run_dir = Path("outputs") / name
        if run_dir.exists():
            if not force:
                raise FileExistsError(f"{run_dir} already exists; pass --force to replace it.")
            shutil.rmtree(run_dir)
        derived_raw = copy.deepcopy(raw_config)
        derived_raw["run_name"] = name
        derived_raw["ballot_cap"] = {"source_run": run_name, "tier": tier, "max_length": cap}
        derived[cap] = {"name": name, "dir": run_dir, "raw": derived_raw, "stats": _new_stats()}

    for cap, d in derived.items():
        d["dir"].mkdir(parents=True)
        shutil.copytree(base_settings, d["dir"] / "settings")
        if (base_dir / "districts").is_dir():
            shutil.copytree(base_dir / "districts", d["dir"] / "districts")
        d["zip"] = zipfile.ZipFile(d["dir"] / "profiles.zip", "w", compression=zipfile.ZIP_DEFLATED)

    untouched = capped = 0
    try:
        with zipfile.ZipFile(base_zip) as source:
            targets = []
            for info in source.infolist():
                parsed = _parse_arcname(info.filename)
                if parsed is not None and parsed[2] == tier and _is_ranked(parsed[0]):
                    targets.append(info.filename)
                    continue
                data = source.read(info.filename)
                for d in derived.values():
                    d["zip"].writestr(info.filename, data)
                untouched += 1
            print(f"[cap_ballot_length] copied {untouched} untouched entries; "
                  f"capping {len(targets)} profiles on all cores...", flush=True)

            # Parse once and cap every N inside one worker, across all cores. The input
            # is a lazy generator and the output is consumed in order, so only a few
            # profiles' worth of csv text is ever in memory at once.
            jobs = (delayed(_cap_one)(name, source.read(name), caps) for name in targets)
            results = Parallel(n_jobs=-1, return_as="generator", batch_size=1)(jobs)
            for arcname, texts, deltas in results:
                for cap, d in derived.items():
                    d["zip"].writestr(arcname, texts[cap])
                    for key, value in deltas[cap].items():
                        d["stats"][key] += value
                capped += 1
                if capped % 100 == 0:
                    print(f"[cap_ballot_length] {capped}/{len(targets)} profiles capped", flush=True)
    finally:
        for d in derived.values():
            d["zip"].close()

    CONFIG_OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for cap, d in derived.items():
        config_out = CONFIG_OUT_DIR / f"{_slug(d['name'])}.json"
        d["config_out"] = config_out
        with open(config_out, "w", encoding="utf-8") as f:
            json.dump(d["raw"], f, indent=2)
        # The derived run's profile signature is the base's (ballot_cap is not a
        # profile-signature key), so run.py sees these profiles as current.
        with open(d["dir"] / "profiles_metadata.json", "w", encoding="utf-8") as f:
            json.dump({"signature": profiles_signature(load_run_config(config_out))}, f)
        d["summary"] = _finish_stats(d["stats"], tier, cap)
        d["summary"]["source_run"] = run_name
        with open(d["dir"] / "cap_stats.json", "w", encoding="utf-8") as f:
            json.dump(d["summary"], f, indent=2)
        written.append(config_out)

    reused = _reuse_untouched_results(base_dir, run_name, derived, tier)
    if reused:
        print(f"[cap_ballot_length] reused {reused} election-result files per run from the base "
              f"(tiers other than {tier} are unchanged).")

    print(f"[cap_ballot_length] {run_name}: tier {tier} capped in {capped} profiles; "
          f"{untouched} other entries copied unchanged.")
    print(f"{'cap':>4}  {'voters shortened':>17}  {'mean length before':>19}  {'after':>6}")
    for cap, d in derived.items():
        s = d["summary"]
        print(f"{cap:>4}  {s['share_of_voters_shortened']:>16.1%}  "
              f"{s['mean_ballot_length_before']:>19.2f}  {s['mean_ballot_length_after']:>6.2f}")
    return written


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Cap ballot length in one tier of a full-ballot run, one derived run per cap.",
    )
    parser.add_argument("config", help="Path or name of the base (full-ballot) run config.")
    parser.add_argument("--tier", type=int, required=True,
                        help="District count of the tier to cap (1 = the 1 X 6 STV contest).")
    parser.add_argument("--caps", type=int, nargs="+", required=True,
                        help="Maximum ballot lengths, e.g. 4 5 6 7 8 9 10.")
    parser.add_argument("--prefix", help="Derived runs are named '<prefix> - Ballot Cap N'.")
    parser.add_argument("--force", action="store_true", help="Replace derived runs that already exist.")
    parser.add_argument("--allow-truncated", action="store_true",
                        help="Cap a base run that was already Cambridge-truncated.")
    parser.add_argument("--run", action="store_true",
                        help="Also run elections and summaries on each derived run.")
    args = parser.parse_args(argv)

    written = build_cap_runs(
        _resolve_config_path(args.config), args.tier, args.caps,
        prefix=args.prefix, force=args.force, allow_truncated=args.allow_truncated,
    )

    if args.run:
        from run import load_config, run_pipeline  # deferred: run.py pulls in the whole pipeline

        for path in written:
            config = load_config(str(path))
            print("=" * 100, f"\n Running {config['run_name']}\n", "=" * 20)
            run_pipeline(config)


if __name__ == "__main__":
    main()
