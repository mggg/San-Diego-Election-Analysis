"""
Run elections on generated voter profiles and record the winners.

Reads voter profile CSV files and runs the appropriate election 
rule (STV for multi-seat districts, plurality and IRV for single-seat districts), 
and writes aggregated election results to JSON files.
"""

from __future__ import annotations
import json
from glob import glob
from pathlib import Path
from joblib import Parallel, delayed
from votekit import RankProfile
from votekit.elections import FastSTV as STV, Plurality
from typing import List, Iterable

# Optional progress bar for joblib.
try:
    from joblib_progress import joblib_progress 
except Exception: 
    joblib_progress = None 

from pipeline.utils.helpers import parse_district_configs

def _candidate_list_from_elected(elected: Iterable[set]) -> List[str]:
    """
    Flatten votekit election output (iterable of singleton sets) into a list of strings.

    Args:
        elected: Iterable of singleton sets, as returned by votekit election methods.

    Returns:
        List of candidate id strings in election order. Empty sets are skipped silently.
    """
    winners: List[str] = []
    for s in elected:
        if s:
            winners.append(str(next(iter(s))))
    return winners

def _process_profile(profile_file: str | Path, n_seats: int) -> List[str]:
    """
    Load a voter profile csv and run an election to determine winners.
    uses stv for multi-seat races and plurality for single-seat races.

    Args:
        profile_file: Path to the voter profile csv.
        n_seats: Number of seats to fill in this election.

    Returns:
        For n_seats > 1: {"stv": [winner ids]}
        For n_seats == 1: {"plurality": [winner ids], "irv": [winner ids]}
    """
    profile_path = Path(profile_file)
    profile: RankProfile = RankProfile.from_csv(profile_path)

    if n_seats > 1:
        elected_stv = STV(profile, m=n_seats, simultaneous=False, tiebreak='random').get_elected()
        return {"stv": _candidate_list_from_elected(elected_stv)}
    else:
        elected_plurality = Plurality(profile, m=1, tiebreak='random').get_elected()
        elected_irv = STV(profile, m=n_seats, simultaneous=False, tiebreak='random').get_elected()
        return {"stv": _candidate_list_from_elected(elected_plurality), "irv": _candidate_list_from_elected(elected_irv)}

def simulate_elections(config) -> None:
    """
    run stv/plurality elections in parallel over all voter profiles.

    Args:
        config: Parsed config dict.

    Outputs:
        One json file per (mode, district_count, winners) combination at
        outputs/election_results/<run_name>_election_results/<mode>/
        <run_name>_<n>_districts_<w>_winners_for_voter_mode_<mode>.json.
        Each file contains a "election_results" list where each entry corresponds
        to one profile file:
          - multi-seat: {"stv": [...]}
          - single-seat: {"plurality": [...], "irv": [...]}

    Returns:
        None.
    """
    run_name = str(config["run_name"])
    district_configs = parse_district_configs(config["district_configs"])

    modes = ["slate_pl", "slate_bt", "cambridge"]
    n_jobs = -1  # use all available cores

    out_root = Path("outputs") / f'{run_name}' / "election_results" 
    out_root.mkdir(parents=True, exist_ok=True)

    # run elections for each voter model
    for mode in modes:
        # profile path
        profile_folder = Path(f"./outputs/{run_name}/profiles/{mode}/")

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
                    results_list = Parallel(n_jobs=n_jobs)(
                        delayed(_process_profile)(pf, dc.winners) for pf in all_profile_files
                    )
            else:
                print(f"[simulate_elections] {desc} (no joblib_progress installed)")
                results_list = Parallel(n_jobs=n_jobs)(
                    delayed(_process_profile)(pf, dc.winners) for pf in all_profile_files
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
                        "election_results": results_list,
                    },
                    f,
                    indent=2,
                )

            print(f"[simulate_elections] Wrote: {out_path}")