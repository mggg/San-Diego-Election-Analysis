"""
Generate voter preference profiles from district-level settings files.

Reads VoteKit settings JSON files, generates synthetic voter profiles for
each district, voter model, and replicate, and bundles the resulting profiles
into a single compressed zip archive per run for downstream election
simulations.

Storing the profiles as entries in one outputs/<run_name>/profiles.zip (rather
than thousands of loose CSV files) keeps the output tree small and avoids the
per-file filesystem overhead of a large ensemble.
"""

from glob import glob
from votekit.ballot_generator import (
    BlocSlateConfig,
    slate_pl_profile_generator,
    slate_bt_profile_generator,
    cambridge_profile_generator,
)
from joblib import Parallel, delayed
from joblib_progress import joblib_progress
from pathlib import Path
from typing import Optional, Set
import time
import json
import zipfile
import zlib
from pipeline.utils.helpers import load_json, get_voter_models, profiles_signature
from pipeline.utils.preference_matrix import preference_matrix_arcname, preference_matrix_json

# maps mode name to votekit profile generator function
generator_name_to_function = {
    "slate_pl": slate_pl_profile_generator,
    "slate_bt": slate_bt_profile_generator,
    "cambridge": cambridge_profile_generator,
}

def _profiles_metadata_path(run_name: str) -> Path:
    """Sidecar recording the signature the profiles.zip was generated under."""
    return Path(f"outputs/{run_name}/profiles_metadata.json")


def _read_existing_zip_members(zip_path: Path) -> Optional[Set[str]]:
    """
    Return the set of member names already in zip_path, or None if it doesn't
    exist or can't be safely read (missing, corrupted, or truncated).

    None signals that resuming isn't possible and the archive must be rebuilt
    from scratch. testzip() decompresses every member to verify its CRC, so a
    truncated entry (e.g. a process killed mid-write) is caught here too.
    """
    if not zip_path.is_file():
        return None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                return None
            return set(archive.namelist())
    except (zipfile.BadZipFile, OSError, zlib.error, EOFError):
        return None


def _expected_profile_filename(settings_file, duplicate_indx) -> str:
    """
    The profile filename for a given settings file and replicate index.

    Kept in one place so the writer here and the readers (simulate_elections,
    run.has_valid_profiles) agree on the naming convention: the settings file's
    stem with "sample_settings" replaced by "profile" and "_v<n>.csv" appended.
    """
    setting_file_stem = Path(settings_file).stem
    return f"{setting_file_stem.replace('sample_settings', 'profile')}_v{duplicate_indx}.csv"


def process_settings_file(settings_file, mode, duplicate_indx):
    """
    Generate a voter profile for a single district using the given voter model.

    Runs entirely in memory (no filesystem write) so it can be called from a
    parallel worker and have its result written into the run's shared zip
    archive by the caller, avoiding concurrent writes to one zip file.

    Args:
        settings_file: Path to a votekit settings json file for one district.
        mode: Voter model name; one of "slate_pl", "slate_bt", or "cambridge".
        duplicate_indx: Replicate index, appended as _v<n> in the output filename.

    Returns:
        (filename, csv_text): filename is the profile's zip entry name within its
        <mode>/<district_num>/ folder (see _expected_profile_filename); csv_text
        is the profile's CSV content (per votekit's PreferenceProfile.to_csv()).
    """
    settings = load_json(settings_file)

    config = BlocSlateConfig(
        n_voters = settings['num_voters'],
        slate_to_candidates=settings["slate_to_candidates"],
        bloc_proportions=settings["bloc_proportions"],
        cohesion_mapping=settings["cohesion_parameters"],
    )

    config.set_dirichlet_alphas(settings["alphas"])

    filename = _expected_profile_filename(settings_file, duplicate_indx)
    profile = generator_name_to_function[mode](config)
    csv_text = profile.to_csv()
    matrix_json = preference_matrix_json(config)

    return filename, csv_text, matrix_json


def generate_profiles(config):
    """
    Generate voter profile csvs for all districts, modes, and replicates in the
    config, bundling them into a single compressed zip archive per run.

    Resumable: if a prior profiles.zip exists, is readable, and was generated
    under the same profile signature (see helpers.profiles_signature -- the
    config parameters that determine profile *content*), its entries are reused
    and only the missing (mode, district, settings-file, replicate) combinations
    are generated and appended. So raising num_reps, adding a voter model, or
    adding a district magnitude fills in only the new profiles. If the signature
    changed (a content-determining parameter differs) or no readable prior
    archive exists, the archive is rebuilt from scratch.

    Args:
        config: Parsed config dict.

    Outputs:
        outputs/<run_name>/profiles.zip, containing one csv entry per
        (mode, district_num, settings file, replicate) at
        "<mode>/<district_num>/<...>_v<duplicate_indx>.csv".
        outputs/<run_name>/profiles_metadata.json, recording the profile
        signature so a later call can tell whether the archive is safe to resume.
    """

    num_reps = config['num_reps']
    run_name = config['run_name']

    voter_models = get_voter_models(config)

    zip_path = Path(f"outputs/{run_name}/profiles.zip")
    zip_path.parent.mkdir(exist_ok=True, parents=True)

    preference_matrix_zip_path = Path(f"outputs/{run_name}/preference_matrices.zip")

    signature = profiles_signature(config)
    metadata_path = _profiles_metadata_path(run_name)

    # Resume only when both archives are readable AND were generated under the
    # same signature; otherwise their contents may be stale or inconsistent,
    # so rebuild both from scratch.
    prior_signature = None
    if metadata_path.is_file():
        try:
            prior_signature = load_json(metadata_path).get("signature")
        except (json.JSONDecodeError, OSError):
            prior_signature = None

    same_signature = prior_signature == signature
    existing_members = _read_existing_zip_members(zip_path) if same_signature else None
    existing_matrix_members = _read_existing_zip_members(preference_matrix_zip_path) if same_signature else None

    # Require both archives to be intact; if either is missing or corrupted,
    # rebuild everything so the two archives stay in sync.
    resume = existing_members is not None and existing_matrix_members is not None
    if not resume:
        existing_members = set()
        existing_matrix_members = set()

    archive_mode = "a" if resume else "w"
    if resume:
        print(
            f"[generate_profiles] Resuming: {len(existing_members)} profile(s) and "
            f"{len(existing_matrix_members)} matrix(es) already present with a matching "
            "signature; generating only what's missing."
        )
    else:
        print("[generate_profiles] No compatible prior profiles found; generating from scratch.")

    # Opened once for the whole run: workers only compute (filename, csv_text)
    # pairs in parallel, and every actual write to the shared archive happens
    # here, sequentially, in the main process (a zip can't be written from
    # multiple processes at once).
    #
    # return_as="generator_unordered" yields each worker's result as soon as
    # it's ready instead of collecting the whole batch into memory before any of
    # it is written, so peak memory is bounded by what's in flight, not the full
    # batch of profiles.
    with zipfile.ZipFile(zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED) as archive,\
        zipfile.ZipFile(preference_matrix_zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED) as matrix_archive:
        # repeat for each replicate
        for duplicate_indx in range(num_reps):
            rep_start = time.perf_counter()
            print(f"[rep {duplicate_indx + 1}/{num_reps}] Start at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            district_nums =  [d_config['num_districts'] for d_config in config['district_configs']]
            for district_num in district_nums:
                for mode in voter_models:
                    settings_folder = Path(f"outputs/{run_name}/settings/{district_num}")
                    all_settings_files = glob(f"{settings_folder}/*.json")

                    # Skip settings files whose profile AND matrix are already in
                    # their respective archives. If either entry is missing,
                    # regenerate both so the two archives stay in sync.
                    pending_settings_files = [
                        sf for sf in all_settings_files
                        if f"{mode}/{district_num}/{_expected_profile_filename(sf, duplicate_indx)}"
                        not in existing_members
                        or f"{mode}/{district_num}/{preference_matrix_arcname(_expected_profile_filename(sf, duplicate_indx))}"
                        not in existing_matrix_members
                    ]
                    if not pending_settings_files:
                        continue

                    with joblib_progress(
                        description=f"[rep {duplicate_indx + 1:03d}/{num_reps}] Generating VK profiles for {district_num:02d} districts and voter model {mode}",
                        total=len(pending_settings_files),
                    ):
                        results = Parallel(n_jobs=-1, return_as="generator_unordered")(
                            delayed(process_settings_file)(settings_file, mode, duplicate_indx)
                            for settings_file in pending_settings_files
                        )

                        for filename, csv_text, matrix_json in results:
                            profile_arcname = f"{mode}/{district_num}/{filename}"
                            matrix_arcname = f"{mode}/{district_num}/{preference_matrix_arcname(filename)}"
                            # Guard against duplicate zip entries when one archive
                            # had an entry the other lacked.
                            if profile_arcname not in existing_members:
                                archive.writestr(profile_arcname, csv_text)
                                existing_members.add(profile_arcname)
                            if matrix_arcname not in existing_matrix_members:
                                matrix_archive.writestr(matrix_arcname, matrix_json)
                                existing_matrix_members.add(matrix_arcname)
            rep_elapsed = time.perf_counter() - rep_start
            print(f"[rep {duplicate_indx + 1}/{num_reps}] Done in {rep_elapsed:.1f}s")

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump({"signature": signature}, f)
