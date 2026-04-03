import json
import random
import glob
import os
try:
    import readline
except ImportError:
    import pyreadline3 as readline



def _path_completer(text, state):
    expanded = os.path.expanduser(text)
    matches = glob.glob(expanded + '*')
    matches = [m + '/' if os.path.isdir(m) else m for m in matches]
    try:
        return matches[state]
    except IndexError:
        return None


def prompt_path(label):
    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    readline.set_completer(_path_completer)
    readline.set_completer_delims('')
    if 'libedit' in readline.__doc__:
        readline.parse_and_bind('bind ^I rl_complete')
    else:
        readline.parse_and_bind('tab: complete')
    try:
        result = input(f"\n{label}\n\t> ").strip()
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)
    return result

# default values for numeric pipeline parameters
DEFAULTS = {
    "chain_length": 1000,
    "num_subsamples": 5,
    "num_voters": 10000,
}

def prompt(label):
    """
    Prompt the user for a single string value.

    Args:
        label: the prompt label shown to the user.

    Returns:
        Stripped string input from the user.
    """
    return input(f"\n{label}\n\t> ").strip()

def prompt_yes_no(label):
    while True:
        response = prompt(label).lower()
        if response in ("y", "n"):
            return response
        print("Invalid input, please enter 'y' or 'n'.")

def prompt_int(label):
    while True:
        try:
            return int(prompt(label))
        except ValueError:
            print("Invalid input, please enter an integer.")

def prompt_dict_of_floats(label, keys):
    """
    Prompt the user for a float value for each key and return as a dict.

    Args:
        label: Header label printed before prompting.
        keys: List of keys to prompt for.

    Returns:
        Dict mapping each key to a float entered by the user.
    """
    result = {}
    print(f"{label}")
    for k in keys:
        while True:
            try:
                result[k] = float(prompt(f"  {k}"))
                break
            except ValueError:
                print("  Invalid input, please enter a number.")
    return result

def collect_district_configs(total_seats):
    """
    Prompt the user for one or more district configurations.

    For each configuration:
      - ask for number of districts
      - compute winners = total_seats / num_districts
      - accept only if winners is an integer

    Args:
        total_seats: Total number of seats available.

    Returns:
        List of valid district configuration dicts.
    """
    district_configs = []

    while True:
        num_districts = prompt_int("Number of districts")

        if num_districts <= 0:
            print("Invalid input, number of districts must be greater than 0.")
            continue

        if total_seats % num_districts != 0:
            print(
                f"Invalid district configuration: {total_seats} total seats cannot be evenly "
                f"divided into {num_districts} districts. Please provide a valid number of districts."
            )
            add_another = prompt_yes_no("Add another district configuration? (y/n)")
            if add_another == "n":
                break
            continue

        winners = total_seats // num_districts
        config = {"num_districts": num_districts, "winners": winners}
        district_configs.append(config)

        print(f"Confirmed district configuration: {config}")

        add_another = prompt_yes_no("Add another district configuration? (y/n)")
        if add_another == "n":
            break

    return district_configs


def build_config():
    """
    Interactively collect pipeline configuration from the user and return it as a dict.

    Prompts for geodata path, column names, district configuration, group names,
    candidates, cohesion parameters, alphas, and turnout rates. Seed is generated randomly.
    chain_length, num_subsamples, and num_voters are set from DEFAULTS and not prompted.

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

    # collect basic user input
    run_name = prompt("Run name")
    geodata_path = prompt_path("Path to geodata file")
    population_column = prompt("Population column name")
    pop_of_interest_col = prompt("Population of interest column name")

    seed          = random.randint(0, 2**32 - 1)
    total_seats = prompt_int("Total number of seats")
    district_configs = collect_district_configs(total_seats)
    num_reps      = prompt_int('Number of simulated elections per district plan')

    # collect group names
    while True:
        groups_raw = prompt("Group names (comma-separated, e.g. A,B), specify focal group first")
        groups = [g.strip() for g in groups_raw.split(",")]

        if len(groups) < 2:
            print("You must provide at least two groups.")
            continue

        if any(g == "" for g in groups):
            print("Group names cannot be empty.")
            continue

        if len(groups) != len(set(groups)):
            print("Group names must be unique.")
            continue

        break

    # collect per-group info
    all_candidates = set()

    for g in groups:
        while True:
            cands_raw = prompt(f"Candidate names for group '{g}' (comma-separated)")
            candidates = [c.strip() for c in cands_raw.split(",")]

            if any(c == "" for c in candidates):
                print("Candidate names cannot be empty.")
                continue

            if len(candidates) != len(set(candidates)):
                print("Duplicate candidate names are not allowed within a group.")
                continue

            if any(c in all_candidates for c in candidates):
                print("Candidate names must be unique across all groups.")
                continue

            slate_to_candidates[g] = candidates
            all_candidates.update(candidates)
            break

    print()
    print(f"Cohesion parameters for group {groups[0]}:")
    while True:
            try:
                g0_cohesion = float(prompt(f"  {groups[0]}"))
                if not (0 <= g0_cohesion <= 1):
                    print("  Value must be between 0 and 1.")
                    continue
                cohesion_parameters[groups[0]] = {groups[0]: g0_cohesion, groups[1]: 1-g0_cohesion}
                print(f"        Cohesion parameters for group '{groups[0]}': {cohesion_parameters[groups[0]]}")
                break
            except ValueError:
                print("  Invalid input, please enter a number.")
    print()
    print(f"Cohesion parameters for group {groups[1]}:")
    while True:
            try:
                g1_cohesion = float(prompt(f"  {groups[1]}"))
                if not (0 <= g1_cohesion <= 1):
                    print("  Value must be between 0 and 1.")
                    continue
                cohesion_parameters[groups[1]] = {groups[0]: 1-g1_cohesion, groups[1]: g1_cohesion}
                print(f"        Cohesion parameters for group '{groups[1]}': {cohesion_parameters[groups[1]]}")
                break
            except ValueError:
                print("  Invalid input, please enter a number.")
            
            

    for g in groups:
        print()
        alphas[g] = prompt_dict_of_floats(f"Candidate strength parameters for group {g}:", groups)

    turnout = prompt_dict_of_floats("Turnout per group:", groups)

    focal_group = groups[0]  # first group is focal by default

    # assemble and return the full config dict
    return {
        "run_name":                run_name,
        "geodata_path":            geodata_path,
        "gerrychain_output_dir":   f"outputs/{run_name}/districts/",
        "population_column":       population_column,
        "pop_of_interest_column":  pop_of_interest_col,
        "seed":                    seed,
        "total_seats":             total_seats,
        "district_configs":        district_configs,
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

    If the user chooses an existing config, prompts for a path and loads it.
    Otherwise, calls build_config(), saves the result to configs/<run_name>.json,
    and returns the config dict.

    Returns:
        Parsed config dict ready to pass to the pipeline.

    Outputs:
        configs/<run_name>.json written when building a new config.
    """
    while True:
        sample = input("\nUse existing config file? (y/n)\n\t> ").strip().lower()
        if sample in ("y", "n"):
            break
        print("Invalid input, please enter 'y' or 'n'.")

    if sample == "y":  # skip setup, load sample file
        while True:
            config_path = prompt_path("Path to config file")
            if not os.path.exists(config_path):
                print("File not found. Please enter a valid path.")
                continue
            if os.path.getsize(config_path) == 0:
                print("File is empty. Please enter a valid config file.")
                continue
            break
        print("Loading config file...")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = build_config()
        out = f"configs/{config['run_name']}.json"

        with open(out, "w") as f:
            json.dump(config, f, indent=2)

        print(f"\nConfig saved to {out}")

    return config

if __name__ == "__main__":
    setup_config()