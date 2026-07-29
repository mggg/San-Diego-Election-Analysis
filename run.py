from pipeline.data_generator import generate_data
from pipeline.district_generator import generate_districts
from pipeline.settings_generator import generate_settings
from pipeline.profile_generator import generate_profiles
from pipeline.simulate_elections import simulate_elections
from pipeline.summarize_results import summarize_results, plot_combined_bubbles_all_runs
from pipeline.utils.helpers import get_voter_models, profiles_signature, election_results_signature
from setup import setup_config
from pathlib import Path
from glob import glob
import argparse
import gzip
import json
import zipfile
import zlib


def load_config(config_path: str) -> dict:
    """Load config from JSON file."""
    with open(config_path) as f:
        return json.load(f)


def load_all_configs(config_dir="configs"):
    """Load every config JSON in config_dir, so simulations can run without the CLI."""
    return [load_config(path) for path in glob(f"{config_dir}/*.json")]


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
            prefix = f"{mode}/{n}/"
            count = sum(1 for m in members if m.startswith(prefix) and m.endswith(".csv"))
            if count != expected:
                print(
                    f"Missing valid profiles for mode={mode}, district_count={n} "
                    f"(found {count}, expected {expected}). Running pipeline from profiles stage."
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

def pipeline(config):
    if not has_valid_geodata(config):
        if "geometry_data" not in config:
            raise ValueError(
                f"Geodata file '{config['geodata_path']}' not found and no "
                "'geometry_data' in config to generate it."
            )
        generate_data(config)
    generate_districts(config)
    generate_settings(config)
    generate_profiles(config)
    simulate_elections(config)
    summarize_results(config)


def run_all(config_dir="configs"):
    """Run the pipeline for every config in config_dir, then draw cross-run summaries."""
    configs = load_all_configs(config_dir)
    for config in configs:
        print("=" * 100, f"\n Running {config['run_name']}\n", "=" * 20)
        run_pipeline(config)
    # One cross-run bubble figure over every run's summary CSV. Any config
    # supplies the shared seat-axis range and population reference line.
    if configs:
        plot_combined_bubbles_all_runs(configs[-1])


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