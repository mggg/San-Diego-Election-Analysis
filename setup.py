import argparse
import json
import os
from pipeline.utils.helpers import load_json

# default values for numeric pipeline parameters
DEFAULTS = {
    "chain_length": 1000,
    "num_subsamples": 5,
    "num_voters": 10000,
    "num_reps": 2,
}

def prompt(label):
    """
    Prompt the user for a single string value.
    Args: label: the prompt label shown to the user.
    Returns: Stripped string input from the user.
    """
    return input(f"{label}: ").strip()

def prompt_dict_of_floats(label, keys):
    """
    Prompt the user for a float value for each key and return as a dict.

    Args:
        label: header label printed before prompting.
        keys: list of keys to prompt for.

    Returns:
        Dict mapping each key to a float entered by the user.
    """
    result = {}
    print(f"{label}")
    for k in keys:
        result[k] = float(prompt(f"  {k}"))
    return result


def build_config():
    """
    Interactively collect pipeline configuration from the user and return it as a dict.

    Prompts for geodata path, column names, district configuration, group names,
    candidates, cohesion parameters, alphas, and turnout rates.

    Returns:
        Dict containing all fields required by the pipeline config schema.
    """
    # dict inits
    slate_to_candidates = {}
    cohesion_parameters = {}
    alphas = {}

    # load defaults for numeric parameters
    chain_length = DEFAULTS["chain_length"]
    num_subsamples = DEFAULTS["num_subsamples"]
    num_voters = DEFAULTS["num_voters"]
    num_reps = DEFAULTS["num_reps"]

    # collect basic user input
    run_name = prompt("run_name")
    geodata_path = prompt("geodata_path")
    population_column = prompt("population_column")
    pop_of_interest_col = prompt("pop_of_interest_column")

    num_districts = int(prompt("num_districts"))
    winners       = int(prompt("winners"))
    total_seats   = num_districts * winners

    # collect group names
    groups_raw = prompt("Group names (comma-separated, e.g. A,B)")
    groups = [g.strip() for g in groups_raw.split(",")]

    # collect per-group info
    for g in groups:
        cands_raw = prompt(f"  Candidate names for group {g} (comma-separated)")
        slate_to_candidates[g] = [c.strip() for c in cands_raw.split(",")]

    for g in groups:
        cohesion_parameters[g] = prompt_dict_of_floats(f"Cohesion parameters for group {g}:", groups)

    for g in groups:
        alphas[g] = prompt_dict_of_floats(f"Alpha parameters for group {g}:", groups)

    turnout = prompt_dict_of_floats("Turnout per group:", groups)

    focal_group = groups[0]  # first group is focal by default

    # assemble and return the full config dict
    return {
        "run_name":                run_name,
        "geodata_path":            geodata_path,
        "gerrychain_output_dir":   f"outputs/districts/{run_name}",
        "population_column":       population_column,
        "pop_of_interest_column":  pop_of_interest_col,
        "total_seats":             total_seats,
        "district_configs":        [{"num_districts": num_districts, "winners": winners}],
        "chain_length":            chain_length,
        "num_subsamples":          num_subsamples,
        "num_reps":                num_reps,
        "num_voters":              num_voters,
        "slate_to_candidates":     slate_to_candidates,
        "turnout":                 turnout,
        "focal_group":             focal_group,
        "cohesion_parameters":     cohesion_parameters,
        "alphas":                  alphas,
      
    }

def setup_config():
    """
    Prompt the user to either load the sample config or build a new one interactively.

    If the user chooses the sample config, loads and returns it from configs/sample.json.
    Otherwise, calls build_config(), saves the result to configs/<run_name>.json,
    and returns the config dict.

    Returns:
        Parsed config dict ready to pass to the pipeline.
    """
    name = input("Use sample config file? (y/n): ")

    if name == "y":  # skip setup, load sample file
        print("Loading sample file...")
        config = load_json("configs/sample.json")
    else:
        config = build_config()
        out = f"configs/{config['run_name']}.json"

        with open(out, "w") as f:
            json.dump(config, f, indent=2)

        print(f"\nConfig saved to {out}")

    return config