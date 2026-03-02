from glob import glob
from votekit.ballot_generator import (
    BlocSlateConfig,
    slate_pl_profile_generator,
    slate_bt_profile_generator,
    cambridge_profile_generator,
)
from joblib import Parallel, delayed
from joblib_progress import joblib_progress
import json
from pathlib import Path
import time
from pipeline.utils.helpers import load_json


# maps mode name to votekit profile generator function
generator_name_to_function = {
    "slate_pl": slate_pl_profile_generator,
    "slate_bt": slate_bt_profile_generator,
    "cambridge": cambridge_profile_generator,
}


def process_settings_file(settings_file, profile_folder, mode, duplicate_indx):
    """
    Generate a voter profile csv for a single district using the given voter model.

    args:
        settings_file: path to a votekit settings json file for one district.
        profile_folder: directory where the output csv will be written.
        mode: voter model name; one of "slate_pl", "slate_bt", or "cambridge".
        duplicate_indx: replicate index, appended as _v<n> in the output filename.

    outputs:
        a csv file in profile_folder named after the settings file stem.
    """
    settings = load_json(settings_file)

    config = BlocSlateConfig(
        n_voters = settings['num_voters'],
        slate_to_candidates=settings["slate_to_candidates"],
        bloc_proportions=settings["bloc_proportions"],
        cohesion_mapping=settings["cohesion_parameters"],
    )

    config.set_dirichlet_alphas(settings["alphas"])
    setting_file_stem = Path(settings_file).stem

    output_file = (
        profile_folder
        / f"{setting_file_stem.replace('sample_settings', 'profile')}_v{duplicate_indx}.csv"
    )
    profile = generator_name_to_function[mode](config)
    profile.to_csv(output_file)


def generate_profiles(config_path):
    """
    Generate voter profile csvs for all districts, modes, and replicates in the config.

    args:
        config_path: path to the json config file.

    outputs:
        csv files at outputs/profiles/<run_name>/<mode>/<district_num>/*.csv.
    """
    config = load_json(config_path)

    num_reps = config['num_reps']
    # repeat for each replicate
    for duplicate_indx in range(num_reps):
        rep_start = time.perf_counter()
        print(f"[rep {duplicate_indx + 1}/{num_reps}] Start at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        district_nums =  [d_config['num_districts'] for d_config in config['district_configs']]
        for district_num in district_nums:
            for mode in ["slate_pl", "slate_bt", "cambridge"]:
                settings_folder = Path(f"outputs/settings/{config['run_name']}_settings/{district_num}")
                profile_folder = Path(f"outputs/profiles/{config['run_name']}/{mode}/{district_num}")
                profile_folder.mkdir(exist_ok=True, parents=True)

                all_settings_files = glob(f"{settings_folder}/*.json")
    
                with joblib_progress(
                    description=f"[rep {duplicate_indx + 1:03d}/{num_reps}] Generating VK profiles for {district_num:02d} districts and voter model {mode}",
                    total=len(all_settings_files),
                ):
                    Parallel(n_jobs=-1)(
                        delayed(process_settings_file)(settings_file, profile_folder, mode, duplicate_indx)
                        for settings_file in all_settings_files
                    )
        rep_elapsed = time.perf_counter() - rep_start
        print(f"[rep {duplicate_indx + 1}/{num_reps}] Done in {rep_elapsed:.1f}s")

