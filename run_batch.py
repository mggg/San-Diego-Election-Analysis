"""
Run a batch of configs end to end, sharing identical district ensembles.

District generation is by far the slowest single-threaded stage (~75 min for a
10,000-step chain on San Diego's blocks). It depends only on the geodata, the
seed, the chain length, and the district count -- none of which are what these
runs vary -- so configs agreeing on all four produce byte-identical ensembles.
Rather than recompute that four times, the first run's ensemble is copied into
the later runs' output directories, but only after checking those four keys
match exactly. Everything downstream still runs per config.

Usage:
    uv run run_batch.py                # all configs listed in ORDER
    uv run run_batch.py basic diverse-preferences
"""

import os
import subprocess
import sys

# Fix the hash seed before importing anything that builds a GerryChain graph.
if os.environ.get("PYTHONHASHSEED") != "0" and __name__ == "__main__":
    os.environ["PYTHONHASHSEED"] = "0"
    sys.exit(subprocess.run([sys.executable, *sys.argv], env=os.environ).returncode)

import shutil
import time
import traceback
from pathlib import Path

from pipeline.utils.helpers import load_run_config
from run import run_pipeline

# Basic first: it produces the 3-district ensemble the other 3x3 runs reuse.
ORDER = [
    "basic",
    "alternative_electoral",
    "diverse-preferences",
    "low-aapi-turnout",
    "low-aapi-availability",
]

# Everything a recom ensemble depends on. Two configs agreeing here get the same
# chain, so one may reuse the other's.
ENSEMBLE_KEYS = ("geodata_path", "seed", "chain_length")


def ensemble_fingerprint(config, num_districts):
    return tuple(config[k] for k in ENSEMBLE_KEYS) + (num_districts,)


def district_file(run_name, num_districts):
    return Path("outputs") / run_name / "districts" / f"{run_name}_{num_districts}_districts.jsonl.gz"


def seed_districts_from_cache(config, cache):
    """
    Copy any already-computed ensemble this config would otherwise recompute.

    Returns the number of ensembles seeded.
    """
    seeded = 0
    for d_config in config["district_configs"]:
        num_districts = d_config["num_districts"]
        target = district_file(config["run_name"], num_districts)
        if target.exists():
            continue

        source = cache.get(ensemble_fingerprint(config, num_districts))
        if source is None or not source.exists():
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"  [batch] reused {source} -> {target}")
        seeded += 1
    return seeded


def main(argv=None):
    names = list(argv) if argv else ORDER
    cache = {}          # ensemble fingerprint -> path of a computed ensemble
    results = {}

    for name in names:
        config = load_run_config(f"configs/{name}.json")
        run_name = config["run_name"]
        print("=" * 78)
        print(f"[batch] {name}  ->  outputs/{run_name}")
        print("=" * 78, flush=True)

        seed_districts_from_cache(config, cache)

        started = time.perf_counter()
        try:
            run_pipeline(config)
            results[name] = f"ok in {(time.perf_counter() - started) / 60:.1f} min"
        except SystemExit:
            # run_pipeline exits when a run is already complete.
            results[name] = f"already complete ({(time.perf_counter() - started) / 60:.1f} min)"
        except Exception:
            results[name] = "FAILED"
            traceback.print_exc()

        # Register whatever ensembles this run now has, for later runs to reuse.
        for d_config in config["district_configs"]:
            num_districts = d_config["num_districts"]
            path = district_file(run_name, num_districts)
            if path.exists():
                cache.setdefault(ensemble_fingerprint(config, num_districts), path)

        print(f"[batch] {name}: {results[name]}", flush=True)

    print("\n" + "=" * 78)
    print("[batch] summary")
    for name in names:
        print(f"  {name:28s} {results.get(name, 'not run')}")
    return 0 if all(v != "FAILED" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
