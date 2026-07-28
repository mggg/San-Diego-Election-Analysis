"""
Generate voter preference profiles from district-level settings files.

Reads VoteKit settings JSON files, generates synthetic voter profiles for
each district, voter model, and replicate, and writes the resulting profiles
to CSV files for downstream election simulations.
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
import time, json,zlib, zipfile
from pipeline.utils.helpers import load_json, get_voter_models
from typing import Optional, Set
from pipeline.utils.preference_matrix import preference_matrix_arcname, preference_matrix_json

# maps mode name to votekit profile generator function
generator_name_to_function = {
    "slate_pl": slate_pl_profile_generator,
    "slate_bt": slate_bt_profile_generator,
    "cambridge": cambridge_profile_generator,
}

def _expected_profile_filename(settings_file, duplicate_indx: int) -> str:
    """
    The profile filename process_settings_file will produce for a given
    settings file and replicate index, without actually generating it.

    Shared by process_settings_file and generate_profiles' resume-detection so
    the naming convention lives in one place.
    """
    setting_file_stem = Path(settings_file).stem
    return f"{setting_file_stem.replace('sample_settings', 'profile')}_v{duplicate_indx}.csv"


def _profiles_metadata_path(run_name: str) -> Path:
    return Path(f"outputs/{run_name}/profiles_metadata.json")

def _read_existing_zip_members(zip_path: Path) -> Optional[Set[str]]:
    """
    Return the set of member names already in zip_path, or None if it doesn't
    exist or can't be safely read (missing, corrupted, or truncated) -- None
    signals that resuming isn't possible and the archive must be rebuilt fresh.
    """
    if not zip_path.is_file():
        return None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                return None
            return set(archive.namelist())
    except (zipfile.BadZipFile, OSError, zlib.error, EOFError):
        # testzip()/read() decompress every member to verify its CRC, so a
        # truncated entry (e.g. a process killed mid-write) surfaces as a raw
        # zlib.error rather than zipfile.BadZipFile -- either way, the archive
        # can't be trusted, so treat it the same as missing/corrupted.
        return None


def _can_resume_profiles(config) -> bool:
    """
    Whether a prior profiles.zip for this run can be resumed (its existing
    entries reused, generating only what's missing) rather than rebuilt from
    scratch.

    Scoped deliberately to num_voters, per-district voter count: a profile's
    ballots are sampled at that count, so a change there invalidates every
    existing profile, while other config changes (e.g. a larger num_reps) are
    exactly the case resuming is for. If there's no metadata to compare
    against (e.g. the run's profiles predate this check), resuming isn't
    provably safe, so this returns False and generate_profiles rebuilds once,
    writing metadata that future calls can then resume from.

    Args:
        config: Parsed config dict.

    Returns:
        True if outputs/<run_name>/profiles_metadata.json exists and recorded
        the same num_voters as config.
    """
    metadata_path = _profiles_metadata_path(config["run_name"])
    if not metadata_path.is_file():
        return False
    try:
        metadata = load_json(metadata_path)
    except (json.JSONDecodeError, OSError):
        return False
    return metadata.get("num_voters") == config["num_voters"]


# def process_settings_file(settings_file, profile_folder, mode, duplicate_indx):
def process_settings_file(settings_file, mode, duplicate_indx):
    """
    Generate a voter profile csv for a single district using the given voter model.

    Args:
        settings_file: Path to a votekit settings json file for one district.
        profile_folder: Directory where the output csv will be written.
        mode: Voter model name; one of "slate_pl", "slate_bt", or "cambridge".
        duplicate_indx: Replicate index, appended as _v<n> in the output filename.

    Outputs:
        A csv file in profile_folder with "sample_settings" replaced by "profile" in the
        settings file stem, suffixed with _v<duplicate_indx>.
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

    # setting_file_stem = Path(settings_file).stem

    # output_file = (
    #     profile_folder
    #     / f"{setting_file_stem.replace('sample_settings', 'profile')}_v{duplicate_indx}.csv"
    # )
    # profile = generator_name_to_function[mode](config)
    # profile.to_csv(output_file)


def generate_profiles(config):
    """
    Generate voter profile csvs for all districts, modes, and replicates in the config.

    Args:
        config: Parsed config dict.

    Outputs:
        csv files at outputs/profiles/<run_name>/<mode>/<district_num>/*.csv.
    """

    num_reps = config['num_reps']
    run_name = config['run_name']

    models = get_voter_models(config)
    unknown = [m for m in models if m not in generator_name_to_function]

    if unknown:
        raise ValueError(
            f"Unknown voter_models {unknown}. Valid models: "
            f"{sorted(generator_name_to_function)}."
        )
    if "cambridge" in models and len(config["slate_to_candidates"]) != 2:
        raise ValueError(
            "The 'cambridge' model supports exactly 2 slates, but this run has "
            f"{len(config['slate_to_candidates'])}. Remove 'cambridge' from "
            "voter_models or reduce to 2 slates."
        )
    zip_path = Path(f"outputs/{run_name}/profiles.zip")
    zip_path.parent.mkdir(exist_ok=True, parents=True)
    preference_matrix_zip_path = Path(f"outputs/{run_name}/preference_matrices.zip")
    metadata_path = _profiles_metadata_path(run_name)

    # Existing members are read from both archives up front (before either is
    # reopened for writing) so the decision to resume vs. rebuild is made once,
    # from a consistent snapshot -- if either archive is missing/corrupted, or
    # num_voters no longer matches, both are rebuilt together so they can never
    # drift out of the 1:1 correspondence downstream code relies on.
    resume = _can_resume_profiles(config)
    existing_members = _read_existing_zip_members(zip_path) if resume else None
    existing_matrix_members = _read_existing_zip_members(preference_matrix_zip_path) if resume else None
    if existing_members is None or existing_matrix_members is None:
        resume = False
        existing_members = set()

    archive_mode = "a" if resume else "w"
    if resume:
        print(
            f"[generate_profiles] Resuming: {len(existing_members)} profile(s) "
            f"already present for num_voters={config['num_voters']}; generating "
            "only what's missing."
        )
    else:
        print("[generate_profiles] No compatible prior profiles found; generating from scratch.")

    # repeat for each replicate
    with zipfile.ZipFile(zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED) as archive, \
         zipfile.ZipFile(preference_matrix_zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED) as matrix_archive:
            for duplicate_indx in range(num_reps):
                rep_start = time.perf_counter()
                print(f"[rep {duplicate_indx + 1}/{num_reps}] Start at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                district_nums =  [d_config['num_districts'] for d_config in config['district_configs']]
                for district_num in district_nums:
                    for mode in ["slate_pl", "slate_bt", "cambridge"]:
                        settings_folder = Path(f"outputs/{run_name}/settings/{district_num}")
                        all_settings_files = glob(f"{settings_folder}/*.json")
                        # profile_folder = Path(f"outputs/{run_name}/profiles/{mode}/{district_num}")
                        # profile_folder.mkdir(exist_ok=True, parents=True)

                        # all_settings_files = glob(f"{settings_folder}/*.json")
                        pending_settings_files = [
                                sf for sf in all_settings_files
                                if f"{mode}/{district_num}/{_expected_profile_filename(sf, duplicate_indx)}"
                                not in existing_members
                            ]
                        if not pending_settings_files:
                            continue
            
                        with joblib_progress(
                            description=f"[rep {duplicate_indx + 1:03d}/{num_reps}] Generating VK profiles for {district_num:02d} districts and voter model {mode}",
                            total=len(all_settings_files),
                        ):
                            results = Parallel(n_jobs=-1)(
                                delayed(process_settings_file)(settings_file, mode, duplicate_indx)
                                for settings_file in all_settings_files
                            )
                            for filename, csv_text, matrix_json in results:
                                    archive.writestr(f"{mode}/{district_num}/{filename}", csv_text)
                                    matrix_archive.writestr(
                                        f"{mode}/{district_num}/{preference_matrix_arcname(filename)}",
                                        matrix_json,
                                    )

            rep_elapsed = time.perf_counter() - rep_start
            print(f"[rep {duplicate_indx + 1}/{num_reps}] Done in {rep_elapsed:.1f}s")

    with open(metadata_path, "w") as f:
        json.dump({"num_voters": config["num_voters"]}, f)

