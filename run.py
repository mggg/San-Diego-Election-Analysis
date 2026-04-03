from pipeline.district_generator import generate_districts
from pipeline.settings_generator import generate_settings
from pipeline.profile_generator import generate_profiles
from pipeline.simulate_elections import simulate_elections
from pipeline.summarize_results import summarize_results
from setup import setup_config
from pathlib import Path
import sys
import gzip
import json

def has_valid_district_outputs(config) -> bool:
    run = config["run_name"]
    n = config["chain_length"]
    base = Path("outputs") / run / "districts"
    if not base.is_dir():
        print("Distrct files do not exist. Running entire pipeline.")
        return False
    for d in config["district_configs"]:
        f = base / f"{run}_{d['num_districts']}_districts.jsonl.gz"
        if not f.is_file():
            print(f"{d['num_districts']} distrct configuration files do not exist. Running entire pipeline.")
            return False
        try:
            with gzip.open(f, "rt", encoding="utf-8") as g:
                if sum(1 for _ in g) != n:
                    print("Incomplete districting file. Running entire pipeline.")
                    return False
        except Exception:
            return False
    return True

def has_valid_settings(config):
    run = config["run_name"]
    base = Path("outputs") / run / "settings"
    if not base.is_dir():
        print("Settings do not exist. Running pipeline from settings stage.")
        return False
    district_nums = [d["num_districts"] for d in config["district_configs"]]
    for num_districts in district_nums:
        count = sum(1 for f in (base / str(num_districts)).rglob("*.json") if f.stat().st_size > 0)
        expected_per_num_district = config["num_subsamples"] * num_districts
        if count != expected_per_num_district:
            print(f"Missing valid settings for {num_districts} districts. Running pipeline from settings stage.")
            return False
    return True

def has_valid_profiles(config):
    run = config["run_name"]
    base = Path("outputs") / run / "profiles"
    if not base.is_dir():
        print("Profiles do not exist. Running pipeline from profiles stage.")
        return False
    expected_per_mode = (
        config["num_subsamples"]
        * sum(d["num_districts"] for d in config["district_configs"])
        * config["num_reps"]
    )
    for mode in ["slate_pl", "slate_bt", "cambridge"]:
        count = sum(1 for f in (base / mode).rglob("*.csv") if f.stat().st_size > 0)
        if count != expected_per_mode:
            print(f"Missing valid settings for {mode} mode. Running pipeline from profiles stage.")
            return False
    return True

def has_valid_election_results(config):
    run = config["run_name"]
    base = Path("outputs") / run / "election_results"
    if not base.is_dir():
        print("Election results do not exist. Running pipeline from election simulation stage.")
        return False
    for mode in ["slate_pl", "slate_bt", "cambridge"]:
        mode_dir = base / mode
        if not mode_dir.is_dir():
            print(f"Election results for {mode} mode do not exist. Running pipeline from election simulation stage.")
            return False
        for d in config["district_configs"]:
            n = d["num_districts"]
            files = list(mode_dir.glob(f"{run}_{n}_districts_*_voter_mode_{mode}.json"))
            if len(files) != 1:
                print(f"Election results for {mode} mode and {d} number of districts do not exist. Running pipeline from election simulation stage.")
                return False
            try:
                with open(files[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                expected_len = config["num_subsamples"] * n * config["num_reps"]
                if len(data.get("profile_files", [])) != expected_len:
                    print(f"Election results for {mode} mode and {d} number of districts have incorrect length. Running pipeline from election simulation stage.")
                    return False
            except Exception:
                return False
    return True

def has_valid_summaries(config):
    run = config["run_name"]
    base = Path("outputs") / run / "summaries"
    figs = base / "figures"
    csv = base / f"{run}_summary.csv"
    if not base.is_dir() or not figs.is_dir() or not csv.is_file():
        print("Summaries do not exist. Running pipeline from summary stage.")
        return False
    expected_figs = sum(2 if d["winners"] == 1 else 1 for d in config["district_configs"])
    actual_figs = sum(1 for _ in figs.glob("*.png"))
    if actual_figs != expected_figs:
        print("Incorrect number of figures.")
    return actual_figs == expected_figs

def run_pipeline(config):
    run_dir = Path("outputs") / config["run_name"]
    # check if run already exists
    if run_dir.exists():
        print(f"Run '{config['run_name']}' already exists at {run_dir}")
        if has_valid_district_outputs(config):
            if has_valid_settings(config):
                if has_valid_profiles(config):
                    if has_valid_election_results(config):
                        if has_valid_summaries(config):
                            print(f"Run '{config['run_name']}' has valid outputs. Exiting.")
                            sys.exit(0)
                        else:
                            summarize_results(config)
                    else:
                        simulate_elections(config)
                        summarize_results(config)
                else:
                    generate_profiles(config)
                    simulate_elections(config)
                    summarize_results(config)
            else:
                generate_settings(config)
                generate_profiles(config)
                simulate_elections(config)
                summarize_results(config)
        else:
            pipeline(config)
    else:      
        pipeline(config)

def pipeline(config):
    generate_districts(config)
    generate_settings(config)
    generate_profiles(config)
    simulate_elections(config)
    summarize_results(config)


if __name__ == "__main__":
    run_pipeline(setup_config())

    