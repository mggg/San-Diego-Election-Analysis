"""
Generate VoteKit settings files from sampled district plans.

Reads district assignments produced by the district-generation step,
aggregates population counts by district, computes turnout-adjusted
bloc proportions, and writes one settings JSON file per sampled plan
and district.

Voter blocs and candidate slates are independent axes. "blocs" maps each
voter bloc to the demographic groups it aggregates (e.g. {"W-A": ["W", "A"],
"B": ["B"], "H": ["H"]}), each demographic group resolves to a VAP column via
"group_vap_columns" (or DEFAULT_GROUP_VAP_COLUMNS), and each bloc's
proportion is its turnout-weighted summed VAP normalized across blocs. This
allows more than two blocs and coalitions of groups voting together.

* Legacy two-group model (no "blocs" in the config): the focal group's
  turnout-adjusted share is computed from pop_of_interest_column vs
  population_column, and the complement is the single non-focal group.
* Coalition / multi-bloc model (config has a "blocs" entry): voter blocs and
  candidate slates are independent axes. "blocs" maps each voter bloc to the
  demographic groups it aggregates (e.g. {"WHI-AAPI": ["WHI", "AAPI"],
  "HIS": ["HIS"], "BAIO": ["BAIO"]}), each demographic group resolves to one or
  more VAP columns via "group_vap_columns" (or DEFAULT_GROUP_VAP_COLUMNS), and
  each bloc's proportion is its turnout-weighted summed VAP normalized across
  blocs. This allows more than two blocs and coalitions of groups voting
  together, while slate_to_candidates / cohesion_parameters / alphas still
  describe the (independent) candidate slates.

Population is combined at two levels, in this order: a demographic group sums
its VAP columns (BAIO = Black + American Indian + other races), then a bloc sums
its groups. Both are recorded in every settings file.
slate_to_candidates is not passed through statically -- each district gets its
own randomly-sized slate_to_candidates, apportioned across slates in
proportion to that district's slate VAP shares (see _build_slate_to_candidates),
with cohesion_parameters / alphas filtered to match whichever slates survive.
"""

import json
import gzip
import random
import math
import numpy as np
import geopandas as gpd
from pathlib import Path
import jsonlines as jl
from tqdm import tqdm
from pipeline.utils.helpers import load_run_config


# Default mapping from demographic-group label -> VAP column(s) in the geodata
# (matches the schema written by data_generator). A group may map to a single
# column or to a list of columns that are summed, which is how BAIO combines
# Black, American Indian, and other-race VAP into one group. Override per-run
# with a "group_vap_columns" entry if your groups or column names differ.
DEFAULT_GROUP_VAP_COLUMNS = {
    "WHI": "white_vap_20",
    "HIS": "hvap_20",
    "AAPI": "asian_nhpi_vap_20",
    "BAIO": ["bvap_20", "amin_vap_20", "other_vap_20"],
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
    Return the {demographic_group: [vap_column, ...]} mapping for the given groups.

    Columns come from config["group_vap_columns"] when present, otherwise
    DEFAULT_GROUP_VAP_COLUMNS. A group's entry may be a single column name or a
    list of columns; either way it is normalized to a list, and a group's VAP is
    the sum over that list. This is what lets one group span several census
    categories (BAIO = Black + American Indian + other races).

    Args:
        config: Parsed config dict.
        demographic_groups: Iterable of demographic-group labels to resolve.

    Returns:
        Dict mapping each demographic group to its list of VAP column names.

    Raises:
        KeyError: If any group has no VAP column mapping.
        ValueError: If a group maps to an empty column list, or lists the same
            column twice (which would double-count it).
    """
    mapping = config.get("group_vap_columns", DEFAULT_GROUP_VAP_COLUMNS)
    missing = [g for g in demographic_groups if g not in mapping]
    if missing:
        raise KeyError(
            f"No VAP column mapping for demographic group(s) {missing}. Add them "
            "to 'group_vap_columns' in the config or to DEFAULT_GROUP_VAP_COLUMNS."
        )

    resolved = {}
    for group in demographic_groups:
        entry = mapping[group]
        columns = [entry] if isinstance(entry, str) else list(entry)
        if not columns:
            raise ValueError(f"Demographic group '{group}' maps to no VAP column.")
        if len(columns) != len(set(columns)):
            raise ValueError(
                f"Demographic group '{group}' lists a VAP column more than once: {columns}."
            )
        resolved[group] = columns

    # A column feeding two groups would be counted twice in the bloc weights.
    seen = {}
    for group, columns in resolved.items():
        for column in columns:
            if column in seen:
                raise ValueError(
                    f"VAP column '{column}' is claimed by both '{seen[column]}' and "
                    f"'{group}'; groups must partition the columns they use."
                )
            seen[column] = group

    return resolved


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


DEFAULT_AVAILABILITY_EXP = 2


def _exaggerate(proportions, availability_exps=None):
    """
    Exaggerate a share distribution by the "proportional to the exponent" method:
    exponentiate each share, then renormalize so the powers sum to 1.

    Exponents above 1 inflate larger shares and shrink smaller ones relative to
    each other (e.g. [0.5, 0.3, 0.15, 0.05] -> squares [0.25, 0.09, 0.0225,
    0.0025] -> renormalized [0.685, 0.247, 0.062, 0.007]), so the resulting slate
    sizes are more concentrated on the dominant slate(s) than the underlying VAP
    shares. An exponent of 1 leaves shares untouched, and below 1 flattens them
    toward uniform.

    Args:
        proportions: Dict mapping key -> share of the total (sums to ~1).
        availability_exps: Optional dict mapping key -> exponent. Keys absent
            from it (or a missing/empty dict) fall back to
            DEFAULT_AVAILABILITY_EXP, so a config may set an exponent for only
            the slates it wants to treat differently.

    Returns:
        Dict mapping each key to its exponentiated-and-renormalized share (sums to 1).
    """
    exps = availability_exps or {}
    power = {
        k: v ** exps.get(k, DEFAULT_AVAILABILITY_EXP) for k, v in proportions.items()
    }
    total = sum(power.values())
    if total <= 0:
        return {k: 1.0 / len(proportions) for k in proportions}
    return {k: v / total for k, v in power.items()}


def _sample_slate_counts(proportions, candidate_count):
    """
    Fill candidate_count candidate slots by sampling with replacement from
    proportions, treated as a categorical distribution over slates.

    Unlike a deterministic apportionment, this is stochastic: a slate's count
    is a random draw weighted by its share, not its share times candidate_count
    rounded to the nearest integer, so counts vary run to run even for the same
    proportions.

    Args:
        proportions: Dict mapping slate -> probability (sums to ~1).
        candidate_count: Number of candidate slots to fill; must be >= 0.

    Returns:
        Dict mapping each slate to how many slots it was drawn for (some may
        be 0), summing to candidate_count.
    """
    if candidate_count < 0:
        raise ValueError(f"candidate_count ({candidate_count}) must be non-negative.")

    slates = list(proportions)
    counts = {s: 0 for s in slates}
    if candidate_count == 0:
        return counts

    weights = [proportions[s] for s in slates]
    for s in random.choices(slates, weights=weights, k=candidate_count):
        counts[s] += 1
    return counts


def _build_slate_to_candidates(row, slate_columns, candidate_count, config):
    """
    Build a district-specific slate_to_candidates mapping sized proportionally
    to each slate's share of modeled VAP in this district.

    Each slate's linear VAP share is exaggerated by squaring-and-renormalizing
    (see _exaggerate), then candidate_count candidate slots are
    filled by sampling from that exaggerated distribution (see
    _sample_slate_counts) rather than a deterministic apportionment — so the
    resulting slate sizes both skew toward the district's dominant slate(s)
    and vary randomly run to run.

    Slates apportioned zero candidates by the sampling are omitted entirely —
    VoteKit's BlocSlateConfig rejects a slate with an empty candidate list, so a
    slate with negligible population share simply doesn't run in that district.

    Args:
        row: Row from the district population dataframe.
        slate_columns: Dict mapping each slate to its list of VAP column names,
            summed to give that slate's VAP (BAIO spans three columns).
        candidate_count: Total number of candidate slots to fill.
        config: Parsed config dict; read for the optional "availability_exps".

    Returns:
        Dict mapping each slate with a nonzero count to a list of candidate ids,
        e.g. {"WHI": ["WHI1", "WHI2"]}.
    """
    slates = list(slate_columns)
    weighted = {s: sum(float(row[column]) for column in slate_columns[s]) for s in slates}
    denom = sum(weighted.values())
    if denom > 0:
        proportions = {s: weighted[s] / denom for s in slates}
    else:
        proportions = {s: 1.0 / len(slates) for s in slates}

    exaggerated = _exaggerate(proportions, config.get("availability_exps"))
    counts = _sample_slate_counts(exaggerated, candidate_count)
    return {
        s: [f"{s}{i}" for i in range(1, counts[s] + 1)]
        for s in slates
        if counts[s] > 0
    }


def _filter_cohesion_to_slates(cohesion_parameters, active_slates):
    """
    Restrict each bloc's cohesion row to the active slates and renormalize so
    each row still sums to 1.

    Dropping a slate from a district removes the candidate-facing column
    entirely (VoteKit requires cohesion_df columns to match slate_to_candidates
    exactly), so the cohesion mass a bloc had assigned to the dropped slate is
    redistributed proportionally across the slates still running.

    Args:
        cohesion_parameters: Dict mapping bloc -> {slate: cohesion value}.
        active_slates: Iterable of slate labels with a nonzero candidate count.

    Returns:
        Dict with the same bloc keys, each row restricted to active_slates and
        renormalized to sum to 1.

    Raises:
        ValueError: If a bloc's cohesion mass is entirely on dropped slates,
            leaving nothing to renormalize.
    """
    active_slates = list(active_slates)
    result = {}
    for bloc, row in cohesion_parameters.items():
        restricted = {s: row[s] for s in active_slates}
        total = sum(restricted.values())
        if total <= 0:
            raise ValueError(
                f"cohesion_parameters['{bloc}'] has no remaining mass once slates "
                f"outside {active_slates} are dropped; cannot renormalize."
            )
        result[bloc] = {s: v / total for s, v in restricted.items()}
    return result


def _filter_alphas_to_slates(alphas, active_slates):
    """
    Restrict each bloc's Dirichlet alpha row to the active slates.

    Unlike cohesion parameters, alphas aren't required to sum to 1, so dropped
    slates are simply removed with no renormalization needed.

    Args:
        alphas: Dict mapping bloc -> {slate: alpha value}.
        active_slates: Iterable of slate labels with a nonzero candidate count.

    Returns:
        Dict with the same bloc keys, each row restricted to active_slates.
    """
    active_slates = list(active_slates)
    return {bloc: {s: row[s] for s in active_slates} for bloc, row in alphas.items()}


def _build_district_settings(row, config, group_columns, bloc_definitions):
    """
    Compute turnout-adjusted bloc proportions and population values for a district.

    Each voter bloc's weight is its turnout times the summed VAP of the
    demographic groups it aggregates; weights are normalized so the proportions
    sum to 1. Blocs and slates are independent — bloc_definitions says which
    demographic groups make up each bloc, so a "W-A" bloc sums White + Asian VAP.

    The output also carries population_column and pop_of_interest_column so the
    downstream summary stage (summarize_results.py) can read total_vap /
    total_ivap for each district.

    Args:
        row: Row from the district population dataframe.
        config: Parsed config dict.
        group_columns: Dict mapping each demographic group to its list of VAP
            column names, which are summed to give that group's VAP.
        bloc_definitions: Dict mapping each bloc to its demographic groups.

    Returns:
        Dict containing bloc_proportions (one entry per bloc), the combined VAP
        behind each demographic group, and the raw column counts those came from.
    """
    turnout = config['turnout']
    blocs = list(bloc_definitions)

    # A group's VAP is the sum over its columns, so a multi-column group like
    # BAIO (Black + American Indian + other) is combined before anything else
    # uses it.
    group_vap = {
        group: sum(float(row[column]) for column in columns)
        for group, columns in group_columns.items()
    }

    # Turnout-weighted VAP per bloc (sum over the bloc's demographic groups),
    # then normalize across the blocs.
    weighted = {
        bloc: turnout[bloc] * sum(group_vap[g] for g in bloc_definitions[bloc])
        for bloc in blocs
    }
    denom = sum(weighted.values())
    if denom > 0:
        bloc_proportions = {bloc: weighted[bloc] / denom for bloc in blocs}
    else:
        # District with no modeled VAP: fall back to equal shares.
        bloc_proportions = {bloc: 1.0 / len(blocs) for bloc in blocs}

    settings = {"bloc_proportions": bloc_proportions}
    # Record what fed the proportions: the combined VAP per group, and the raw
    # columns behind it so a multi-column group stays auditable.
    settings["group_vap"] = group_vap
    for col in dict.fromkeys(c for columns in group_columns.values() for c in columns):
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
        bloc_proportions in each file are turnout-adjusted proportions, one entry
        per voter bloc. slate_to_candidates is resized per-district: candidate_count
        (drawn from a geometric distribution, capped at a log-VAP ceiling, floored
        at the district's winner count) is apportioned across slates in proportion
        to each slate's share of modeled VAP in that district (see
        _build_slate_to_candidates). A slate sampled zero candidates is dropped from
        slate_to_candidates, and its column is dropped (with cohesion_parameters
        renormalized) from that district's cohesion_parameters and alphas.
    """
    random.seed(config["seed"])

    if "candidate_geometric_p" not in config:
        raise ValueError(
            "'candidate_geometric_p' must be set in the config -- it's the success "
            "probability for the per-district candidate-count draw (e.g. 0.2 for "
            "50-district plans, 0.1 for 10-district plans)."
        )

    bloc_definitions = get_bloc_definitions(config)
    _validate_bloc_config(config, bloc_definitions)
    # The demographic groups we need VAP for are the union across all blocs.
    demographic_groups = list(dict.fromkeys(
        g for groups in bloc_definitions.values() for g in groups
    ))
    group_columns = get_group_vap_columns(config, demographic_groups)
    slate_columns = get_group_vap_columns(config, config["slate_to_candidates"].keys())

    population_data = gpd.read_file(config['geodata_path'])
    # group_columns / slate_columns map each label to a *list* of columns (a group
    # like BAIO spans several), so flatten before de-duplicating.
    needed_columns = list(dict.fromkeys(
        [c for columns in group_columns.values() for c in columns]
        + [c for columns in slate_columns.values() for c in columns]
        + [config['population_vap_column'], config['population_column'], config['pop_of_interest_column']]
    ))
    population_data = population_data[needed_columns]

    # subsample evenly spaced plans from the chain
    chain_length = config['chain_length']
    num_subsamples = config['num_subsamples']
    subsample_interval = chain_length // num_subsamples

    # pull only the relevant keys from config to pass downstream
    # (slate_to_candidates, cohesion_parameters, and alphas are computed
    # per-district below, not passed through as-is)
    district_params = ['num_voters']
    output_settings = {k:config[k] for k in config if k in district_params}
    run_name = config['run_name']

    for district_num, winners in [
        (d_config['num_districts'], d_config['winners']) for d_config in config['district_configs']
    ]:
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
                    district_settings = _build_district_settings(row, config, group_columns, bloc_definitions)

                    # candidate_max is a log-scale ceiling on this district's VAP;
                    # the geometric distribution's success probability is a fixed,
                    # per-config value (candidate_geometric_p) rather than derived
                    # from VAP, so different district-magnitude configs can be
                    # tuned independently (e.g. a lower p, and so a larger expected
                    # candidate count, for 10-district plans than 50-district ones).
                    # Floored at `winners`: an election can never elect more seats
                    # than there are candidates, so a multi-winner district (e.g.
                    # 5-seat STV) must have at least that many candidates on the
                    # ballot regardless of what the geometric draw happened to
                    # sample -- the geometric distribution's support starts at 1
                    # and has real mass below small ceilings like 5, so without
                    # this floor some districts would otherwise be unelectable.
                    vap = district_settings[config["population_vap_column"]]
                    candidate_max = math.ceil(math.log(vap))
                    candidate_count = min(np.random.geometric(config["candidate_geometric_p"]), candidate_max)
                    candidate_count = max(candidate_count, winners)

                    slate_to_candidates = _build_slate_to_candidates(
                        row, slate_columns, candidate_count, config
                    )
                    active_slates = list(slate_to_candidates)
                    cohesion_parameters = _filter_cohesion_to_slates(config["cohesion_parameters"], active_slates)
                    alphas = _filter_alphas_to_slates(config["alphas"], active_slates)
                    settings = output_settings | district_settings | {
                        "slate_to_candidates": slate_to_candidates,
                        "cohesion_parameters": cohesion_parameters,
                        "alphas": alphas,
                    }
                    with open(
                        f"{settings_folder}/{run_name}_{district_num}_sample_settings_district_plan_{sample_idx:03d}_district_{district:02d}.json",
                        "w",
                    ) as out_file:
                        json.dump(settings, out_file, indent=2)

if __name__ == '__main__':
    config = load_run_config("configs/basic.json")
    generate_settings(config)
