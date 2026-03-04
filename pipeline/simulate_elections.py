from __future__ import annotations

import json
from glob import glob
from pathlib import Path

from joblib import Parallel, delayed

# Optional progress bar for joblib.
try:
    from joblib_progress import joblib_progress 
except Exception: 
    joblib_progress = None 

from pipeline.utils.helpers import parse_district_configs, process_profile


def simulate_elections(config) -> None:
    """
    run stv/plurality elections in parallel over all voter profiles.

    args:
        config: parsed config dict.

    outputs:
        one json file per (mode, district_count, winners) combination at
        outputs/election_results/<run_name>_election_results/<mode>/
        <run_name>_<n>_districts_<w>_winners_for_voter_mode_<mode>.json.
        each file contains a "winners" list with one entry per profile file.

    returns:
        none.
    """
    run_name = str(config["run_name"])
    district_configs = parse_district_configs(config["district_configs"])

    modes = ["slate_pl", "slate_bt", "cambridge"]
    n_jobs = -1  # use all available cores

    out_root = Path("outputs") / "election_results" / f"{run_name}_election_results"
    out_root.mkdir(parents=True, exist_ok=True)

    # run elections for each voter model
    for mode in modes:
        # profile path
        profile_folder = Path(f"./outputs/profiles/{config['run_name']}/{mode}/")

        output_dir = out_root / mode
        output_dir.mkdir(parents=True, exist_ok=True)

        for dc in district_configs:
            all_profile_files = glob(f"{profile_folder}/{dc.num_districts}/*.csv")

            desc = f"Running elections for {dc.num_districts} districts, {dc.winners} winner(s), mode={mode}"
            if joblib_progress is not None:
                ctx = joblib_progress(description=desc, total=len(all_profile_files))
            else:
                ctx = None

            if ctx is not None:
                with ctx:
                    winners_list = Parallel(n_jobs=n_jobs)(
                        delayed(process_profile)(pf, dc.winners) for pf in all_profile_files
                    )
            else:
                print(f"[simulate_elections] {desc} (no joblib_progress installed)")
                winners_list = Parallel(n_jobs=n_jobs)(
                    delayed(process_profile)(pf, dc.winners) for pf in all_profile_files
                )

            # write all winners for this district/mode combo to one json file
            out_path = output_dir / (
                f"{run_name}_{dc.num_districts}_districts_{dc.winners}_winners_for_voter_mode_{mode}.json"
            )
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "run_name": run_name,
                        "voter_mode": mode,
                        "district_num": dc.num_districts,
                        "winners_per_district": dc.winners,
                        "profile_files": all_profile_files,
                        "winners": winners_list,
                    },
                    f,
                    indent=2,
                )

            print(f"[simulate_elections] Wrote: {out_path}")