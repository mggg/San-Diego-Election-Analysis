"""
Apply Cambridge ballot truncation to profiles that have already been generated.

profile_generator truncates as it writes, which means a run only gets truncated
ballots if it was configured for them before its profiles were built -- and
rebuilding profiles to change that is the most expensive stage in the pipeline
to repeat. This module does the same truncation as a second pass over an
existing archive: read each ranked profile out of profiles.zip, truncate its
ballots by the same methodology (pipeline.utils.cambridge_truncation), and write
the result to a new archive with the same entry names.

The truncation itself is not reimplemented here. Every ballot goes through
apply_cambridge_truncation, so a profile truncated by this pass and one
truncated during generation differ only in their random draws.

Score profiles are copied through untouched: name_cumulative ballots have no
ranking to shorten, which is the same rule profile_generator applies.

Usage:
    python -m pipeline.truncate_profiles configs/2b-basic.json
    python -m pipeline.truncate_profiles configs/2b-basic.json --in-place
    python -m pipeline.truncate_profiles configs/2b-basic.json --archive general_profiles.zip
"""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from votekit import RankProfile
from votekit.ballot_generator import BlocSlateConfig

from pipeline.profile_generator import profile_class_for_mode
from pipeline.simulate_elections import _load_profile_from_bytes
from pipeline.utils.cambridge_truncation import build_length_distributions, truncate_profile
from pipeline.utils.helpers import (
    find_settings_file,
    load_json,
    load_run_config,
    parse_plan_district_rep_from_path,
)

# The archives a run may hold, all sharing one entry-name scheme. profiles.zip is
# the one elections are read from; the other two exist only for runs with a
# two-round rule (see simulate_elections).
ARCHIVE_NAMES = ("profiles.zip", "general_profiles.zip", "primary_profiles.zip")


def _parse_arcname(arcname: str) -> Optional[Tuple[str, Optional[int], int, str]]:
    """
    Split a profile entry name back into its parts.

    The inverse of profile_generator.profile_arcname, which writes
    "<mode>/<district_num>/<file>" for ranked models and
    "<mode>/<budget>/<district_num>/<file>" for score models, whose ballots
    depend on the budget they were drawn for.

    Returns:
        (mode, budget, district_num, filename), or None for an entry that does
        not match either shape -- a directory marker, or anything a future
        writer adds -- which the caller copies through rather than guessing at.
    """
    parts = arcname.split("/")
    try:
        if len(parts) == 3:
            mode, district, filename = parts
            return mode, None, int(district), filename
        if len(parts) == 4:
            mode, budget, district, filename = parts
            return mode, int(budget), int(district), filename
    except ValueError:
        return None
    return None


def _district_config(settings_path: Path) -> BlocSlateConfig:
    """
    Rebuild the BlocSlateConfig a district's profile was generated against.

    Only slate_to_candidates is load-bearing for truncation -- it is what
    classifies a ballot's first choice as majority or minority, and what sizes
    the Cambridge shape distributions -- but BlocSlateConfig requires a complete
    electorate, so the rest is filled in from the same settings file the
    generator used.
    """
    settings = load_json(settings_path)
    config = BlocSlateConfig(
        n_voters=settings["num_voters"],
        slate_to_candidates=settings["slate_to_candidates"],
        bloc_proportions=settings["bloc_proportions"],
        cohesion_mapping=settings["cohesion_parameters"],
    )
    config.set_dirichlet_alphas(settings["alphas"])
    return config


def truncate_archive(
    config: dict,
    archive_path: Path,
    output_path: Path,
    truncation_cfg: Optional[dict] = None,
    modes: Optional[List[str]] = None,
    seed: Optional[int] = None,
) -> Dict[str, int]:
    """
    Write a copy of one profile archive with every ranked ballot truncated.

    Args:
        config: Parsed run config, for the run name, the settings files, and the
            "cambridge_truncation" block when truncation_cfg isn't given.
        archive_path: The existing archive to read.
        output_path: Where to write the truncated copy. May equal archive_path
            only via the caller's --in-place handling, which writes beside it
            and swaps; this function never writes to the file it is reading.
        truncation_cfg: Overrides config["cambridge_truncation"]. Must carry
            "majority_slates" and "minority_slates".
        modes: Only truncate these voter models. Defaults to every ranked model
            in the archive; score models are always copied through.
        seed: Seeds the length draws, so a run can be reproduced.

    Returns:
        Counts of what happened: truncated, copied, and skipped entries.

    Raises:
        ValueError: If no truncation config is available, or a ranked entry's
            settings file cannot be found -- truncating without it would mean
            guessing which slate a candidate belongs to.
    """
    truncation_cfg = truncation_cfg or config.get("cambridge_truncation")
    if not truncation_cfg:
        raise ValueError(
            f"No truncation config: {config['run_name']} has no 'cambridge_truncation' "
            "block and none was passed. It needs 'majority_slates' and 'minority_slates'."
        )

    run_name = config["run_name"]
    rng = np.random.default_rng(seed)
    tally = {"truncated": 0, "copied": 0, "skipped": 0}

    # One config and one set of length distributions per district settings file:
    # the distributions depend only on that district's candidate counts, and
    # rebuilding them per profile is the bulk of the work in a large archive.
    config_cache: Dict[Path, Tuple[BlocSlateConfig, dict]] = {}

    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(
        output_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            raw = source.read(info.filename)
            parsed = _parse_arcname(info.filename)

            if parsed is None:
                target.writestr(info, raw)
                tally["skipped"] += 1
                continue

            mode, _budget, district_num, filename = parsed
            if not _is_ranked(mode) or (modes is not None and mode not in modes):
                target.writestr(info, raw)
                tally["copied"] += 1
                continue

            plan, district, _rep = parse_plan_district_rep_from_path(filename)
            settings_dir = Path("outputs") / run_name / "settings" / str(district_num)
            settings_path = find_settings_file(
                settings_dir, run_name, plan=plan, district=district
            )
            if settings_path is None:
                raise ValueError(
                    f"No settings file for plan {plan}, district {district} under "
                    f"{settings_dir}. Truncation needs it to know which slate each "
                    "candidate belongs to; regenerate the settings stage first."
                )

            if settings_path not in config_cache:
                district_config = _district_config(settings_path)
                config_cache[settings_path] = (
                    district_config,
                    build_length_distributions(
                        district_config,
                        truncation_cfg["majority_slates"],
                        truncation_cfg["minority_slates"],
                    ),
                )
            district_config, distributions = config_cache[settings_path]

            profile = _load_profile_from_bytes(raw, profile_class_for_mode(mode))
            truncated = truncate_profile(
                profile,
                district_config,
                truncation_cfg["majority_slates"],
                truncation_cfg["minority_slates"],
                distributions,
                rng=rng,
            )
            target.writestr(info, truncated.to_csv())
            tally["truncated"] += 1

    return tally


def _is_ranked(mode: str) -> bool:
    """
    Whether this voter model's ballots have a ranking to shorten.

    Score ballots (name_cumulative) do not, which is the same test
    profile_generator applies when it truncates during generation. A model the
    pipeline has never heard of is treated as not ranked and copied through
    rather than guessed at.
    """
    from pipeline.profile_generator import generator_name_to_function

    if mode not in generator_name_to_function:
        return False
    return profile_class_for_mode(mode) is RankProfile


def truncate_run(
    config: dict,
    archive: str = "profiles.zip",
    in_place: bool = False,
    modes: Optional[List[str]] = None,
    seed: Optional[int] = None,
) -> Optional[Path]:
    """
    Truncate one of a run's archives, writing beside it or replacing it.

    Args:
        config: Parsed run config.
        archive: Which archive to read, one of ARCHIVE_NAMES.
        in_place: Replace the archive with the truncated copy. The copy is
            written to a temporary name and moved over the original only once it
            is complete, so an interrupted run leaves the original intact.
        modes: Restrict truncation to these voter models.
        seed: Seeds the length draws.

    Returns:
        The path written, or None if the archive doesn't exist.
    """
    run_dir = Path("outputs") / config["run_name"]
    archive_path = run_dir / archive
    if not archive_path.is_file():
        print(f"[truncate_profiles] No {archive} for {config['run_name']}; nothing to do.")
        return None

    output_path = archive_path.with_name(
        archive_path.stem + (".truncating.tmp.zip" if in_place else "_truncated.zip")
    )
    tally = truncate_archive(
        config, archive_path, output_path, modes=modes, seed=seed,
    )

    if in_place:
        shutil.move(str(output_path), str(archive_path))
        output_path = archive_path

    print(
        f"[truncate_profiles] {config['run_name']} / {archive}: "
        f"{tally['truncated']} truncated, {tally['copied']} copied, "
        f"{tally['skipped']} passed through -> {output_path}"
    )
    return output_path


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Truncate the ballots in an already-generated profile archive.",
    )
    parser.add_argument("config", help="Path or name of the run config.")
    parser.add_argument(
        "--archive", default="profiles.zip", choices=ARCHIVE_NAMES,
        help="Which archive to truncate (default: profiles.zip).",
    )
    parser.add_argument(
        "--all-archives", action="store_true",
        help="Truncate every archive the run has, not just one.",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Replace the archive instead of writing <name>_truncated.zip beside it.",
    )
    parser.add_argument(
        "--modes", nargs="+",
        help="Only truncate these voter models (default: every ranked model).",
    )
    parser.add_argument(
        "--seed", type=int, help="Seed for the ballot-length draws.",
    )
    args = parser.parse_args(argv)

    config = load_run_config(args.config)
    archives = ARCHIVE_NAMES if args.all_archives else (args.archive,)
    for name in archives:
        truncate_run(
            config, archive=name, in_place=args.in_place, modes=args.modes, seed=args.seed,
        )


if __name__ == "__main__":
    main()
