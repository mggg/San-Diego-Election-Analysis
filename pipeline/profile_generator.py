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

from contextlib import ExitStack
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
import time
import json
import zipfile
from pipeline.utils.helpers import (
    load_json,
    get_voter_models,
    profiles_signature,
    primary_profiles_signature,
    primary_profiles_metadata_path,
    primary_profiles_zip_path,
    read_existing_zip_members,
)
from pipeline.utils.preference_matrix import preference_matrix_arcname, preference_matrix_json
from pipeline.settings_generator import primary_turnout_map

# maps mode name to votekit profile generator function
generator_name_to_function = {
    "slate_pl": slate_pl_profile_generator,
    "slate_bt": slate_bt_profile_generator,
    "cambridge": cambridge_profile_generator,
}

def _profiles_metadata_path(run_name: str) -> Path:
    """Sidecar recording the signature the profiles.zip was generated under."""
    return Path(f"outputs/{run_name}/profiles_metadata.json")


def _expected_profile_filename(settings_file, duplicate_indx) -> str:
    """
    The profile filename for a given settings file and replicate index.

    Kept in one place so the writer here and the readers (simulate_elections,
    run.has_valid_profiles) agree on the naming convention: the settings file's
    stem with "sample_settings" replaced by "profile" and "_v<n>.csv" appended.
    """
    setting_file_stem = Path(settings_file).stem
    return f"{setting_file_stem.replace('sample_settings', 'profile')}_v{duplicate_indx}.csv"


def process_settings_file(settings_file, mode, duplicate_indx, proportions_key="bloc_proportions"):
    """
    Generate a voter profile for a single district using the given voter model.

    Runs entirely in memory (no filesystem write) so it can be called from a
    parallel worker and have its result written into the run's shared zip
    archive by the caller, avoiding concurrent writes to one zip file.

    Args:
        settings_file: Path to a votekit settings json file for one district.
        mode: Voter model name; one of "slate_pl", "slate_bt", or "cambridge".
        duplicate_indx: Replicate index, appended as _v<n> in the output filename.
        proportions_key: Which electorate to sample from -- "bloc_proportions"
            (the configured turnout) or "primary_bloc_proportions" (the lower
            turnout of a two-round rule's narrowing round).

    Returns:
        (filename, csv_text): filename is the profile's zip entry name within its
        <mode>/<district_num>/ folder (see _expected_profile_filename); csv_text
        is the profile's CSV content (per votekit's PreferenceProfile.to_csv()).
    """
    settings = load_json(settings_file)

    config = BlocSlateConfig(
        n_voters = settings['num_voters'],
        slate_to_candidates=settings["slate_to_candidates"],
        bloc_proportions=settings[proportions_key],
        cohesion_mapping=settings["cohesion_parameters"],
    )

    config.set_dirichlet_alphas(settings["alphas"])

    filename = _expected_profile_filename(settings_file, duplicate_indx)
    profile = generator_name_to_function[mode](config)
    csv_text = profile.to_csv()
    matrix_json = preference_matrix_json(config)

    return filename, csv_text, matrix_json


def _generate_profile_archive(
    config,
    zip_path: Path,
    metadata_path: Path,
    signature: str,
    proportions_key: str,
    matrix_zip_path=None,
    label: str = "generate_profiles",
):
    """
    Generate one archive of voter profiles: every district, mode, and replicate
    sampled from the electorate named by proportions_key.

    Called once for the standard profiles and, when a run models a separate
    primary electorate, a second time for those -- same resumability, parallel
    dispatch, and sequential-write structure both times.

    Args:
        config: Parsed config dict.
        zip_path: Archive to write the profile csvs into.
        metadata_path: Sidecar recording the signature this archive was built under.
        signature: Signature to record and to check a prior archive against.
        proportions_key: Settings key holding the bloc proportions to sample from.
        matrix_zip_path: Optional preference-matrix archive kept in sync with the
            profiles. The primary pass passes None -- the matrices describe the
            slate preference intervals, which a turnout override doesn't change.
        label: Prefix for this pass's console output.
    """
    num_reps = config['num_reps']
    run_name = config['run_name']

    voter_models = get_voter_models(config)

    zip_path.parent.mkdir(exist_ok=True, parents=True)
    track_matrices = matrix_zip_path is not None

    # Resume only when the archives are readable AND were generated under the
    # same signature; otherwise their contents may be stale or inconsistent,
    # so rebuild from scratch.
    prior_signature = None
    if metadata_path.is_file():
        try:
            prior_signature = load_json(metadata_path).get("signature")
        except (json.JSONDecodeError, OSError):
            prior_signature = None

    same_signature = prior_signature == signature
    existing_members = read_existing_zip_members(zip_path) if same_signature else None
    existing_matrix_members = (
        read_existing_zip_members(matrix_zip_path) if same_signature and track_matrices else None
    )

    # Require every tracked archive to be intact; if any is missing or corrupted,
    # rebuild everything so they stay in sync.
    resume = existing_members is not None and (existing_matrix_members is not None or not track_matrices)
    if not resume:
        existing_members = set()
        existing_matrix_members = set()
    elif not track_matrices:
        existing_matrix_members = set()

    archive_mode = "a" if resume else "w"
    if resume:
        matrices_note = f"and {len(existing_matrix_members)} matrix(es) " if track_matrices else ""
        print(
            f"[{label}] Resuming: {len(existing_members)} profile(s) {matrices_note}"
            "already present with a matching signature; generating only what's missing."
        )
    else:
        print(f"[{label}] No compatible prior profiles found; generating from scratch.")

    # Opened once for the whole run: workers only compute (filename, csv_text)
    # pairs in parallel, and every actual write to the shared archive happens
    # here, sequentially, in the main process (a zip can't be written from
    # multiple processes at once).
    #
    # return_as="generator_unordered" yields each worker's result as soon as
    # it's ready instead of collecting the whole batch into memory before any of
    # it is written, so peak memory is bounded by what's in flight, not the full
    # batch of profiles.
    with ExitStack() as stack:
        archive = stack.enter_context(
            zipfile.ZipFile(zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED)
        )
        matrix_archive = (
            stack.enter_context(
                zipfile.ZipFile(matrix_zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED)
            )
            if track_matrices
            else None
        )
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
                        or (
                            track_matrices
                            and f"{mode}/{district_num}/{preference_matrix_arcname(_expected_profile_filename(sf, duplicate_indx))}"
                            not in existing_matrix_members
                        )
                    ]
                    if not pending_settings_files:
                        continue

                    with joblib_progress(
                        description=f"[{label}][rep {duplicate_indx + 1:03d}/{num_reps}] Generating VK profiles for {district_num:02d} districts and voter model {mode}",
                        total=len(pending_settings_files),
                    ):
                        results = Parallel(n_jobs=-1, return_as="generator_unordered")(
                            delayed(process_settings_file)(
                                settings_file, mode, duplicate_indx, proportions_key
                            )
                            for settings_file in pending_settings_files
                        )

                        for filename, csv_text, matrix_json in results:
                            profile_arcname = f"{mode}/{district_num}/{filename}"
                            # Guard against duplicate zip entries when one archive
                            # had an entry the other lacked.
                            if profile_arcname not in existing_members:
                                archive.writestr(profile_arcname, csv_text)
                                existing_members.add(profile_arcname)
                            if track_matrices:
                                matrix_arcname = f"{mode}/{district_num}/{preference_matrix_arcname(filename)}"
                                if matrix_arcname not in existing_matrix_members:
                                    matrix_archive.writestr(matrix_arcname, matrix_json)
                                    existing_matrix_members.add(matrix_arcname)
            rep_elapsed = time.perf_counter() - rep_start
            print(f"[rep {duplicate_indx + 1}/{num_reps}] Done in {rep_elapsed:.1f}s")

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump({"signature": signature}, f)


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

    A config that sets "primary_turnout" gets a second archive on the same terms,
    sampled from the lower-turnout electorate of a two-round rule's narrowing
    round. Two-round rules run round 1 on those ballots; every other rule, and
    round 2 itself, keeps using the standard profiles.

    Args:
        config: Parsed config dict.

    Outputs:
        outputs/<run_name>/profiles.zip, containing one csv entry per
        (mode, district_num, settings file, replicate) at
        "<mode>/<district_num>/<...>_v<duplicate_indx>.csv".
        outputs/<run_name>/profiles_metadata.json, recording the profile
        signature so a later call can tell whether the archive is safe to resume.
        With "primary_turnout" set, also outputs/<run_name>/primary_profiles.zip
        and primary_profiles_metadata.json, keyed by the same entry names.
    """
    run_name = config['run_name']

    _generate_profile_archive(
        config,
        zip_path=Path(f"outputs/{run_name}/profiles.zip"),
        metadata_path=_profiles_metadata_path(run_name),
        signature=profiles_signature(config),
        proportions_key="bloc_proportions",
        matrix_zip_path=Path(f"outputs/{run_name}/preference_matrices.zip"),
        label="generate_profiles",
    )

    if primary_turnout_map(config) is not None:
        _generate_profile_archive(
            config,
            zip_path=primary_profiles_zip_path(run_name),
            metadata_path=primary_profiles_metadata_path(run_name),
            signature=primary_profiles_signature(config),
            proportions_key="primary_bloc_proportions",
            matrix_zip_path=None,
            label="generate_primary_profiles",
        )


if __name__ == '__main__':
    config = load_json("configs/basic.json")
    generate_profiles(config)
