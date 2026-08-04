import os
import subprocess
import sys

# Python fixes hash randomization when the interpreter starts, so PYTHONHASHSEED
# cannot be set from inside a running process -- assigning it to os.environ here
# would have no effect on this process at all. Re-exec once with a fixed seed so
# GerryChain's chains are reproducible without every caller having to remember
# `--env-file py_env`. Done before the heavy imports below so the discarded
# process costs almost nothing.
if os.environ.get("PYTHONHASHSEED") != "0" and __name__ == "__main__":
    os.environ["PYTHONHASHSEED"] = "0"
    sys.exit(subprocess.run([sys.executable, *sys.argv], env=os.environ).returncode)

from pipeline.data_generator import generate_data
from pipeline.district_generator import generate_districts
from pipeline.settings_generator import generate_settings
from pipeline.profile_generator import (
    generate_profiles,
    generator_accepts_total_points,
    score_budgets_for_run,
)
from pipeline.simulate_elections import simulate_elections
from pipeline.summarize_results import (
    summarize_results,
    plot_combined_bubbles_all_runs,
    aggregate_to_plan_level,
    expected_figure_count,
)
from pipeline.utils.helpers import (
    election_results_signature,
    find_cached_district_output,
    get_voter_models,
    load_run_config,
    primary_profiles_metadata_path,
    primary_profiles_signature,
    primary_profiles_zip_path,
    profiles_signature,
)
from pipeline.two_round_election import is_two_round
from setup import setup_config
from pathlib import Path
from glob import glob
import argparse
import gzip
import json
import shutil
import zipfile
import zlib
import pandas as pd


def load_config(config_path: str) -> dict:
    """Load a run config, layered over the shared project config."""
    return load_run_config(config_path)


def load_all_configs(config_dir="configs"):
    """
    Load every run config in config_dir, so simulations can run without the CLI.

    Project-wide settings live in project-settings.json at the repo root, so
    everything in config_dir is a run.
    """
    return [load_config(path) for path in sorted(glob(f"{config_dir}/*.json"))]


def resolve_config_path(name: str, config_dir="configs") -> Path:
    """
    Resolve a single config given by path or bare name.

    Accepts a direct path, or a name looked up in config_dir with or without the
    .json extension (so "sample", "sample.json", and "configs/sample.json" all
    resolve to the same file).

    Raises:
        FileNotFoundError: if none of the candidate paths exist.
    """
    candidates = [Path(name), Path(config_dir) / name, Path(config_dir) / f"{name}.json"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Config '{name}' not found. Looked for: {', '.join(str(c) for c in candidates)}."
    )

def has_valid_geodata(config) -> bool:
    path = Path(config["geodata_path"])
    if not path.exists() or path.stat().st_size == 0:
        print("Geodata file not found. Running data generation step.")
        return False

    # If this config generated the geodata, verify the sidecar matches.
    if "geometry_data" in config:
        meta_path = path.parent / (path.stem + "_meta.json")
        if meta_path.exists():
            with open(meta_path) as f:
                saved = json.load(f).get("geometry_data", {})
            current = config["geometry_data"]
            conflicts = {
                k: (saved.get(k), current.get(k))
                for k in ("state_fips", "place_fips", "place_name")
                if saved.get(k) != current.get(k)
            }
            if conflicts:
                lines = "\n".join(
                    f"  {k}: saved={v[0]!r}  config={v[1]!r}"
                    for k, v in conflicts.items()
                )
                raise ValueError(
                    f"Geodata at '{path}' was generated for a different city.\n"
                    f"Conflicts:\n{lines}\n"
                    "Delete the file or change 'geodata_path' in your config."
                )

    return True


def has_valid_district_outputs(config) -> bool:
    run = config["run_name"]
    n = config["chain_length"]
    base = Path("outputs") / run / "districts"
    for d in config["district_configs"]:
        f = base / f"{run}_{d['num_districts']}_districts.jsonl.gz"
        if not f.is_file():
            # Another config may have already run this exact chain (same
            # geodata/population column/seed/chain length) under a different
            # run_name -- reuse it instead of rerunning the chain.
            cached = find_cached_district_output(config, d["num_districts"])
            if cached is not None:
                print(
                    f"Reusing district chain output for {d['num_districts']} districts "
                    f"from {cached} (same geodata/population column/seed/chain length)."
                )
                base.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cached, f)
            else:
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
    wants_primary = bool(config.get("primary_turnout"))
    for num_districts in district_nums:
        files = [f for f in (base / str(num_districts)).rglob("*.json") if f.stat().st_size > 0]
        expected_per_num_district = config["num_subsamples"] * num_districts
        if len(files) != expected_per_num_district:
            print(f"Missing valid settings for {num_districts} districts. Running pipeline from settings stage.")
            return False

        # Settings written before primary_turnout was added carry no
        # primary_bloc_proportions, and the profile stage would fail on the first
        # one it reads. Counting files can't catch that, so sample one.
        if wants_primary:
            try:
                with open(files[0], encoding="utf-8") as f:
                    sample = json.load(f)
            except (json.JSONDecodeError, OSError):
                sample = {}
            if "primary_bloc_proportions" not in sample:
                print(
                    f"Settings for {num_districts} districts predate primary_turnout. "
                    "Running pipeline from settings stage."
                )
                return False
    return True

def has_valid_profiles(config):
    run = config["run_name"]
    zip_path = Path("outputs") / run / "profiles.zip"
    if not zip_path.is_file():
        print("Profiles do not exist. Running pipeline from profiles stage.")
        return False

    # A signature mismatch means a content-determining parameter changed, so the
    # existing profiles are stale; generate_profiles will rebuild them.
    meta_path = Path("outputs") / run / "profiles_metadata.json"
    if not meta_path.is_file():
        print("Profiles metadata missing. Running pipeline from profiles stage.")
        return False
    try:
        with open(meta_path, encoding="utf-8") as f:
            prior_signature = json.load(f).get("signature")
    except (json.JSONDecodeError, OSError):
        prior_signature = None
    if prior_signature != profiles_signature(config):
        print("Profiles signature changed. Running pipeline from profiles stage.")
        return False

    try:
        with zipfile.ZipFile(zip_path) as archive:
            # testzip() decompresses every member to verify its CRC, so a
            # truncated/corrupted entry (e.g. a process killed mid-write) is
            # caught here, not just a structurally broken archive.
            if archive.testzip() is not None:
                print("Profiles archive has a corrupted entry. Running pipeline from profiles stage.")
                return False
            members = archive.namelist()
    except (zipfile.BadZipFile, OSError, zlib.error, EOFError) as e:
        print(f"Profiles archive is unreadable ({e}). Running pipeline from profiles stage.")
        return False

    # Checked per (mode, district count) rather than summed, so a complete
    # district count can't mask an incomplete one when a config has more than
    # one district configuration.
    for mode in get_voter_models(config):
        for d in config["district_configs"]:
            n = d["num_districts"]
            expected = config["num_subsamples"] * n * config["num_reps"]
            # Score models hold one subtree per ballot budget, so each budget is
            # counted on its own -- a complete set at one budget must not mask a
            # missing set at another.
            if generator_accepts_total_points(mode):
                prefixes = [f"{mode}/{b}/{n}/" for b in score_budgets_for_run(config, d["winners"])]
            else:
                prefixes = [f"{mode}/{n}/"]
            for prefix in prefixes:
                count = sum(1 for m in members if m.startswith(prefix) and m.endswith(".csv"))
                if count != expected:
                    print(
                        f"Missing valid profiles for mode={mode}, district_count={n} "
                        f"(prefix {prefix!r}: found {count}, expected {expected}). "
                        "Running pipeline from profiles stage."
                    )
                    return False

    # Count csvs, not raw entries, so the two archives are compared on the same
    # basis (primary_profiles.zip holds profiles only -- no matrices).
    return has_valid_primary_profiles(
        config, expected_members=sum(1 for m in members if m.endswith(".csv"))
    )


def has_valid_primary_profiles(config, expected_members=None):
    """
    Whether the primary-round profile archive is present and current.

    Trivially true for a run that doesn't set "primary_turnout" -- no archive is
    generated in that case. Otherwise the archive must exist, carry a matching
    signature, and hold one entry per standard profile, since two-round rules
    read it member-for-member alongside profiles.zip.
    """
    if not config.get("primary_turnout"):
        return True

    run = config["run_name"]
    zip_path = primary_profiles_zip_path(run)
    if not zip_path.is_file():
        print("Primary-round profiles do not exist. Running pipeline from profiles stage.")
        return False

    meta_path = primary_profiles_metadata_path(run)
    if not meta_path.is_file():
        print("Primary-round profiles metadata missing. Running pipeline from profiles stage.")
        return False
    try:
        with open(meta_path, encoding="utf-8") as f:
            prior_signature = json.load(f).get("signature")
    except (json.JSONDecodeError, OSError):
        prior_signature = None
    if prior_signature != primary_profiles_signature(config):
        print("Primary turnout changed. Running pipeline from profiles stage.")
        return False

    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                print(
                    "Primary-round profiles archive has a corrupted entry. "
                    "Running pipeline from profiles stage."
                )
                return False
            count = sum(1 for m in archive.namelist() if m.endswith(".csv"))
    except (zipfile.BadZipFile, OSError, zlib.error, EOFError) as e:
        print(f"Primary-round profiles archive is unreadable ({e}). Running pipeline from profiles stage.")
        return False

    if expected_members is not None and count != expected_members:
        print(
            f"Primary-round profiles are incomplete (found {count}, expected {expected_members}). "
            "Running pipeline from profiles stage."
        )
        return False
    return True

def has_valid_election_results(config):
    run = config["run_name"]
    base = Path("outputs") / run / "election_results"
    if not base.is_dir():
        print("Election results do not exist. Running pipeline from election simulation stage.")
        return False
    signature = election_results_signature(config)
    for mode in get_voter_models(config):
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
                # A signature mismatch means the profiles or voting rules changed,
                # so these results are stale and must be re-simulated.
                if data.get("signature") != signature:
                    print(f"Election results for {mode} mode and {d} number of districts are stale (signature changed). Running pipeline from election simulation stage.")
                    return False
                expected_len = config["num_subsamples"] * n * config["num_reps"]
                if len(data.get("profile_files", [])) != expected_len:
                    print(f"Election results for {mode} mode and {d} number of districts have incorrect length. Running pipeline from election simulation stage.")
                    return False
            except Exception:
                return False

            # A run with a primary/general rule owes a primary-results file too.
            # Results simulated before that file existed look complete otherwise,
            # so check for it explicitly rather than inferring it from the general.
            if any(is_two_round(kwargs) for kwargs in config["voting_configs"].values()):
                primary_file = base.parent / "primary_results" / mode / files[0].name
                if not primary_file.is_file():
                    print(
                        f"Primary results for {mode} mode and {d} number of districts do not exist. "
                        "Running pipeline from election simulation stage."
                    )
                    return False
                try:
                    with open(primary_file, "r", encoding="utf-8") as f:
                        primary_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    return False
                if primary_data.get("signature") != signature:
                    print(
                        f"Primary results for {mode} mode and {d} number of districts are stale "
                        "(signature changed). Running pipeline from election simulation stage."
                    )
                    return False
    return True

def has_valid_summaries(config):
    run = config["run_name"]
    csv = Path("outputs") / run / "summaries" / f"{run}_summary.csv"
    figs_dir = Path("figures") / run
    if not csv.is_file() or not figs_dir.is_dir():
        print("Summaries do not exist. Running pipeline from summary stage.")
        return False
    try:
        df_plan = aggregate_to_plan_level(pd.read_csv(csv))
    except (pd.errors.EmptyDataError, OSError):
        print("Summary csv unreadable. Running pipeline from summary stage.")
        return False
    expected_figs = expected_figure_count(df_plan, config)
    actual_figs = sum(1 for _ in figs_dir.rglob("*.png"))
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
                            print(f"Run '{config['run_name']}' has valid outputs. Skipping.")
                            return
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
    # Refresh the cross-run bubble figure over every completed run's summary
    # CSV, not just when --run-all is used.
    plot_combined_bubbles_all_runs(config)

def pipeline(config):
    if not has_valid_geodata(config):
        if "geometry_data" not in config:
            raise ValueError(
                f"Geodata file '{config['geodata_path']}' not found and no "
                "'geometry_data' in config to generate it."
            )
        generate_data(config)
    # Guarded rather than unconditional: a brand-new run_name reaches this
    # function directly (run_pipeline only checks has_valid_district_outputs
    # when the run directory already exists), and this check is also what
    # reuses a matching chain from another config instead of rerunning it.
    if not has_valid_district_outputs(config):
        generate_districts(config)
    generate_settings(config)
    generate_profiles(config)
    simulate_elections(config)
    summarize_results(config)


def run_all(config_dir="configs"):
    """Run the pipeline for every config in config_dir."""
    configs = load_all_configs(config_dir)
    for config in configs:
        print("=" * 100, f"\n Running {config['run_name']}\n", "=" * 20)
        run_pipeline(config)


def main(argv=None):
    """
    Entry point. Three ways to start the pipeline:

      * (default) no arguments -> interactive CLI config setup (setup_config).
      * --run-all              -> run every config in configs/.
      * <config>               -> run a single config given by path or name.
    """
    parser = argparse.ArgumentParser(
        description="Run the electoral-redistricting simulation pipeline."
    )
    parser.add_argument(
        "config",
        nargs="?",
        help="Path or name of a single config to run (e.g. 'sample', 'sample.json', "
        "or 'configs/sample.json'). Omit to configure interactively.",
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="Run every config in the configs/ directory instead of a single run.",
    )
    args = parser.parse_args(argv)

    if args.run_all and args.config:
        parser.error("Pass either a single config or --run-all, not both.")

    if args.run_all:
        run_all()
    elif args.config:
        run_pipeline(load_config(str(resolve_config_path(args.config))))
    else:
        run_pipeline(setup_config())


if __name__ == "__main__":
    main()