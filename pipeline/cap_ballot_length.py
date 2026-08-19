"""
Apply a ballot-length cap to profiles that have already been generated.

Ballot-length caps aren't applied at generation time -- profile_generator only
knows how to apply Cambridge truncation as it writes. This module does the
cap as a second pass over an existing archive, the same way
pipeline.truncate_profiles applies Cambridge truncation: read each ranked
profile out of profiles.zip, cap its ballots by the same methodology
(pipeline.utils.ballot_length_cap), and write the result to a new archive
with the same entry names.

Only slate_pl and slate_bt are capped by default. cambridge is excluded
because its ballots are already historically shaped rather than a full
ranking to cap; name_cumulative is excluded because score ballots have no
ranking to cap.

Usage:
    python -m pipeline.cap_ballot_length configs/2b-basic.json
    python -m pipeline.cap_ballot_length configs/2b-basic.json --in-place
    python -m pipeline.cap_ballot_length configs/2b-basic.json --archive general_profiles.zip
"""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from votekit import RankProfile

from pipeline.profile_generator import profile_class_for_mode
from pipeline.simulate_elections import _load_profile_from_bytes
from pipeline.truncate_profiles import ARCHIVE_NAMES, _district_config, _parse_arcname
from pipeline.utils.ballot_length_cap import apply_ballot_length_cap
from pipeline.utils.helpers import (
    find_settings_file,
    load_run_config,
    parse_plan_district_rep_from_path,
)

if TYPE_CHECKING:
    from votekit.ballot_generator import BlocSlateConfig

# The voter models a ballot-length cap applies to by default: full rankings
# with no shape of their own to preserve. See the module docstring for why
# cambridge and name_cumulative are excluded.
CAPPED_MODES = ("slate_pl", "slate_bt")


def cap_archive(
    config: dict,
    archive_path: Path,
    output_path: Path,
    cap_cfg: Optional[dict] = None,
    modes: Optional[List[str]] = None,
) -> Dict[str, int]:
    """
    Write a copy of one profile archive with every capped-mode ballot cut to
    its district's ballot-length limit.

    Args:
        config: Parsed run config, for the run name, the settings files, and
            the "ballot_length_cap" block when cap_cfg isn't given.
        archive_path: The existing archive to read.
        output_path: Where to write the capped copy. May equal archive_path
            only via the caller's --in-place handling, which writes beside it
            and swaps; this function never writes to the file it is reading.
        cap_cfg: Overrides config["ballot_length_cap"]. Must carry "ratio",
            "min_length", and "max_length".
        modes: Only cap these voter models. Defaults to CAPPED_MODES.

    Returns:
        Counts of what happened: capped, copied, and skipped entries.

    Raises:
        ValueError: If no cap config is available, or a capped entry's
            settings file cannot be found -- capping without it would mean
            guessing the district's candidate count.
    """
    cap_cfg = cap_cfg or config.get("ballot_length_cap")
    if not cap_cfg:
        raise ValueError(
            f"No ballot-length cap config: {config['run_name']} has no "
            "'ballot_length_cap' block and none was passed. It needs 'ratio', "
            "'min_length', and 'max_length'."
        )
    modes = list(modes) if modes is not None else list(CAPPED_MODES)

    run_name = config["run_name"]
    tally = {"capped": 0, "copied": 0, "skipped": 0}

    # One BlocSlateConfig per district settings file: the cap depends only on
    # that district's candidate count, and rebuilding it per profile is the
    # bulk of the work in a large archive.
    config_cache: Dict[Path, "BlocSlateConfig"] = {}

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
            if mode not in modes or profile_class_for_mode(mode) is not RankProfile:
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
                    f"{settings_dir}. Capping needs it to know the district's "
                    "candidate count; regenerate the settings stage first."
                )

            if settings_path not in config_cache:
                config_cache[settings_path] = _district_config(settings_path)
            district_config = config_cache[settings_path]

            profile = _load_profile_from_bytes(raw, profile_class_for_mode(mode))
            capped = apply_ballot_length_cap(
                profile,
                district_config,
                cap_cfg["ratio"],
                cap_cfg["min_length"],
                cap_cfg["max_length"],
            )
            target.writestr(info, capped.to_csv())
            tally["capped"] += 1

    return tally


def cap_run(
    config: dict,
    archive: str = "profiles.zip",
    in_place: bool = False,
    modes: Optional[List[str]] = None,
) -> Optional[Path]:
    """
    Cap one of a run's archives, writing beside it or replacing it.

    Args:
        config: Parsed run config.
        archive: Which archive to read, one of ARCHIVE_NAMES.
        in_place: Replace the archive with the capped copy. The copy is
            written to a temporary name and moved over the original only once
            it is complete, so an interrupted run leaves the original intact.
        modes: Restrict capping to these voter models.

    Returns:
        The path written, or None if the archive doesn't exist.
    """
    run_dir = Path("outputs") / config["run_name"]
    archive_path = run_dir / archive
    if not archive_path.is_file():
        print(f"[cap_ballot_length] No {archive} for {config['run_name']}; nothing to do.")
        return None

    output_path = archive_path.with_name(
        archive_path.stem + (".capping.tmp.zip" if in_place else "_capped.zip")
    )
    tally = cap_archive(config, archive_path, output_path, modes=modes)

    if in_place:
        shutil.move(str(output_path), str(archive_path))
        output_path = archive_path

    print(
        f"[cap_ballot_length] {config['run_name']} / {archive}: "
        f"{tally['capped']} capped, {tally['copied']} copied, "
        f"{tally['skipped']} passed through -> {output_path}"
    )
    return output_path


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Cap ranked-ballot length in an already-generated profile archive.",
    )
    parser.add_argument("config", help="Path or name of the run config.")
    parser.add_argument(
        "--archive", default="profiles.zip", choices=ARCHIVE_NAMES,
        help="Which archive to cap (default: profiles.zip).",
    )
    parser.add_argument(
        "--all-archives", action="store_true",
        help="Cap every archive the run has, not just one.",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Replace the archive instead of writing <name>_capped.zip beside it.",
    )
    parser.add_argument(
        "--modes", nargs="+",
        help="Only cap these voter models (default: slate_pl slate_bt).",
    )
    args = parser.parse_args(argv)

    config = load_run_config(args.config)
    archives = ARCHIVE_NAMES if args.all_archives else (args.archive,)
    for name in archives:
        cap_run(config, archive=name, in_place=args.in_place, modes=args.modes)


if __name__ == "__main__":
    main()
