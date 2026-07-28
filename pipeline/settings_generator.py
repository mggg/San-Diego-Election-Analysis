"""
Generate VoteKit settings files from sampled district plans.

Reads district assignments produced by the district-generation step,
aggregates population counts by district, computes turnout-adjusted
bloc proportions, and writes one settings JSON file per sampled plan
and district.

Two bloc models are supported:

* Legacy two-group model (no "blocs" in the config): the focal group's
  turnout-adjusted share is computed from pop_of_interest_column vs
  population_column, and the complement is the single non-focal group.
* Coalition / multi-bloc model (config has a "blocs" entry): voter blocs and
  candidate slates are independent axes. "blocs" maps each voter bloc to the
  demographic groups it aggregates (e.g. {"W-A": ["W", "A"], "B": ["B"],
  "H": ["H"]}), each demographic group resolves to a VAP column via
  "group_vap_columns" (or DEFAULT_GROUP_VAP_COLUMNS), and each bloc's
  proportion is its turnout-weighted summed VAP normalized across blocs.
  This allows more than two blocs and coalitions of groups voting together,
  while slate_to_candidates / cohesion_parameters / alphas still describe the
  (independent) candidate slates.
"""

import json
import gzip
import geopandas as gpd
from pathlib import Path
import jsonlines as jl
from tqdm import tqdm
from pipeline.utils.helpers import get_non_focal_group


# Default mapping from demographic-group label -> VAP column in the geodata
# (matches the schema written by data_generator). Override per-run with a
# "group_vap_columns" entry in the config if your groups or column names differ.
DEFAULT_GROUP_VAP_COLUMNS = {
    "W": "white_vap_20",
    "B": "bvap_20",
    "H": "hvap_20",
    "A": "asian_nhpi_vap_20",
}


def get_bloc_definitions(config):
    """
    Return {bloc: [demographic_group, ...]} defining which demographic groups
    each voter bloc aggregates.

    Voter blocs (who votes) and candidate slates (who runs) are independent axes.
    By default every slate gets a matching single-group bloc of the same name
    (the original blocs == slates behavior). Set a "blocs" entry in the config to
    combine demographic groups into one bloc, e.g.
    {"W-A": ["W", "A"], "B": ["B"], "H": ["H"]} — a 3-bloc electorate that still
    faces the 4 slates in slate_to_candidates.

    Args:
        config: Parsed config dict.

    Returns:
        Dict mapping each bloc label to the list of demographic groups it covers.
    """
    if "blocs" in config:
        return {bloc: list(groups) for bloc, groups in config["blocs"].items()}
    return {slate: [slate] for slate in config["slate_to_candidates"].keys()}


def get_group_vap_columns(config, demographic_groups):
    """
    Return the {demographic_group: vap_column} mapping for the given groups.

    Columns come from config["group_vap_columns"] when present, otherwise
    DEFAULT_GROUP_VAP_COLUMNS.

    Args:
        config: Parsed config dict.
        demographic_groups: Iterable of demographic-group labels to resolve.

    Returns:
        Dict mapping each demographic group to its VAP column name.

    Raises:
        KeyError: If any group has no VAP column mapping.
    """
    mapping = config.get("group_vap_columns", DEFAULT_GROUP_VAP_COLUMNS)
    missing = [g for g in demographic_groups if g not in mapping]
    if missing:
        raise KeyError(
            f"No VAP column mapping for demographic group(s) {missing}. Add them "
            "to 'group_vap_columns' in the config or to DEFAULT_GROUP_VAP_COLUMNS."
        )
    return {g: mapping[g] for g in demographic_groups}


def _validate_bloc_config(config, bloc_definitions):
    """
    Check turnout, cohesion_parameters, and alphas are keyed consistently with the
    blocs (rows) and slates (columns) this run uses.

    Args:
        config: Parsed config dict.
        bloc_definitions: Dict mapping each bloc to its demographic groups.

    Raises:
        KeyError: with a specific message if any bloc or slate entry is missing.
    """
    blocs = set(bloc_definitions)
    slates = set(config["slate_to_candidates"])

    missing_turnout = blocs - set(config["turnout"])
    if missing_turnout:
        raise KeyError(f"turnout is missing entries for bloc(s) {sorted(missing_turnout)}.")

    for name in ("cohesion_parameters", "alphas"):
        matrix = config[name]
        missing_rows = blocs - set(matrix)
        if missing_rows:
            raise KeyError(f"{name} is missing row(s) for bloc(s) {sorted(missing_rows)}.")
        for bloc in blocs:
            missing_cols = slates - set(matrix[bloc])
            if missing_cols:
                raise KeyError(
                    f"{name}['{bloc}'] is missing column(s) for slate(s) {sorted(missing_cols)}."
                )


def _build_district_settings(row, config):
    """
    Legacy two-group bloc proportions for a district (focal vs non-focal).

    Used when the config has no "blocs" entry. The focal group's turnout-adjusted
    share is derived from pop_of_interest_column vs population_column, and the
    single non-focal group takes the complement.

    Args:
        row: Row from the district population dataframe.
        config: Parsed config dict.

    Returns:
        Dict containing bloc_proportions and population counts for the district.
    """
    turnout = config['turnout']
    focal_group = config['focal_group']
    other_group = get_non_focal_group(config)

    prop = float(row[config['pop_of_interest_column']] / row[config['population_column']])
    adjusted_prop = (
        prop * turnout[focal_group]
        / (prop * turnout[focal_group] + (1 - prop) * turnout[other_group])
    )
    return {
        "bloc_proportions": {
            focal_group: adjusted_prop,
            other_group: 1 - adjusted_prop,
        },
        config["pop_of_interest_column"]: row[config["pop_of_interest_column"]],
        config["population_column"]: row[config["population_column"]],
    }


def _build_multibloc_district_settings(row, config, group_columns, bloc_definitions):
    """
    Compute turnout-adjusted bloc proportions and population values for a district
    under the coalition / multi-bloc model.

    Each voter bloc's weight is its turnout times the summed VAP of the
    demographic groups it aggregates; weights are normalized so the proportions
    sum to 1. Blocs and slates are independent — bloc_definitions says which
    demographic groups make up each bloc, so a "W-A" bloc sums White + Asian VAP.

    The output also carries population_column and pop_of_interest_column so the
    downstream summary stage keeps reading the same population fields it does for
    the legacy path.

    Args:
        row: Row from the district population dataframe.
        config: Parsed config dict.
        group_columns: Dict mapping each demographic group to its VAP column name.
        bloc_definitions: Dict mapping each bloc to its demographic groups.

    Returns:
        Dict containing bloc_proportions (one entry per bloc) and per-group plus
        total VAP counts for the district.
    """
    turnout = config['turnout']
    blocs = list(bloc_definitions)

    # Turnout-weighted VAP per bloc (sum over the bloc's demographic groups),
    # then normalize across the blocs.
    weighted = {
        bloc: turnout[bloc] * sum(float(row[group_columns[g]]) for g in bloc_definitions[bloc])
        for bloc in blocs
    }
    denom = sum(weighted.values())
    if denom > 0:
        bloc_proportions = {bloc: weighted[bloc] / denom for bloc in blocs}
    else:
        # District with no modeled VAP: fall back to equal shares.
        bloc_proportions = {bloc: 1.0 / len(blocs) for bloc in blocs}

    settings = {"bloc_proportions": bloc_proportions}
    # Record the raw per-demographic-group VAP counts that fed the proportions.
    for col in dict.fromkeys(group_columns.values()):
        settings[col] = float(row[col])
    settings[config["population_vap_column"]] = float(row[config["population_vap_column"]])
    # Keep the fields the summary stage reads for both bloc models.
    settings[config["population_column"]] = float(row[config["population_column"]])
    settings[config["pop_of_interest_column"]] = float(row[config["pop_of_interest_column"]])
    return settings


def generate_settings(config):
    """
    For each sampled district plan, compute per-district bloc proportions and write
    votekit settings json files.

    Args:
        config: Parsed config dict.

    Outputs:
        One json settings file per (district count, sampled plan, district) triple at
        outputs/settings/<run_name>_settings/<district_count>/<run_name>_<district_count>_sample_settings_district_plan_<plan_idx>_district_<district_id>.json.
        where <plan_idx> is the zero-based chain sample index and <district_id> is the district label.
        bloc_proportions in each file are turnout-adjusted proportions (focal vs
        non-focal for the legacy model, or one entry per bloc for the multi-bloc
        model). slate_to_candidates / cohesion_parameters / alphas pass through
        from the config unchanged.
    """
    use_blocs = "blocs" in config
    if use_blocs:
        bloc_definitions = get_bloc_definitions(config)
        _validate_bloc_config(config, bloc_definitions)
        # The demographic groups we need VAP for are the union across all blocs.
        demographic_groups = list(dict.fromkeys(
            g for groups in bloc_definitions.values() for g in groups
        ))
        group_columns = get_group_vap_columns(config, demographic_groups)
        needed_columns = list(dict.fromkeys(
            list(group_columns.values())
            + [config['population_vap_column'], config['population_column'], config['pop_of_interest_column']]
        ))
    else:
        bloc_definitions = None
        group_columns = None
        needed_columns = [config['pop_of_interest_column'], config['population_column']]

    population_data = gpd.read_file(config['geodata_path'])
    population_data = population_data[needed_columns]

    # subsample evenly spaced plans from the chain
    chain_length = config['chain_length']
    num_subsamples = config['num_subsamples']
    subsample_interval = chain_length // num_subsamples

    # pull only the relevant keys from config to pass downstream
    district_params = ['num_voters', 'slate_to_candidates', 'cohesion_parameters', 'alphas']
    output_settings = {k:config[k] for k in config if k in district_params}
    run_name = config['run_name']

    for district_num in [d_config['num_districts'] for d_config in config['district_configs']]:
        settings_folder = Path(f'outputs/{run_name}/settings/{district_num}')
        settings_folder.mkdir(exist_ok=True, parents=True)

        path_to_districting = Path(f'outputs/{run_name}/districts/{run_name}_{district_num}_districts.jsonl.gz')

        with gzip.open(path_to_districting, mode="rt", encoding="utf-8") as gz_file:
            file = jl.Reader(gz_file)
            for sample_idx, sample in tqdm(
                enumerate(file),
                total=chain_length,
                desc=f"Generating VK settings for {district_num:02d} districts",
            ):
                if sample_idx % subsample_interval != 0:
                    continue

                district_plan = sample["assignment"]
                population_data["district_plan"] = district_plan
                data_by_district = population_data.groupby("district_plan").sum()

                for _, row in data_by_district.iterrows():
                    district = row.name
                    if use_blocs:
                        district_settings = _build_multibloc_district_settings(
                            row, config, group_columns, bloc_definitions
                        )
                    else:
                        district_settings = _build_district_settings(row, config)
                    settings = output_settings | district_settings
                    with open(
                        f"{settings_folder}/{run_name}_{district_num}_sample_settings_district_plan_{sample_idx:03d}_district_{district:02d}.json",
                        "w",
                    ) as out_file:
                        json.dump(settings, out_file, indent=2)
