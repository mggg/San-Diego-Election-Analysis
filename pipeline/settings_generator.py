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
  demographic groups it aggregates (e.g. {"WAIO-POC": ["WAIO", "POC"]}), each
  demographic group resolves to one or
  more VAP columns via "group_vap_columns" (or DEFAULT_GROUP_VAP_COLUMNS), and
  each bloc's proportion is its turnout-weighted summed VAP normalized across
  blocs. This allows more than two blocs and coalitions of groups voting
  together, while slate_to_candidates / cohesion_parameters / alphas still
  describe the (independent) candidate slates.

Population is combined at two levels, in this order: a demographic group sums
its VAP columns (WAIO = White + American Indian + other races), then a bloc sums
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
from pipeline.utils.helpers import (
    load_run_config,
    CANDIDATE_COUNT_KWARGS,
    parse_district_configs,
    match_hybrid_contests,
    rule_seat_count,
    slate_names,
)


# Default mapping from demographic-group label -> VAP column(s) in the geodata
# (matches the schema written by data_generator). A group may map to a single
# column or to a list of columns that are summed, which is how both groups here
# combine several census categories. Override per-run with a
# "group_vap_columns" entry if your groups or column names differ.
#
# This default describes the two-bloc model: White voters against everyone else.
# American Indian and other-race VAP sit with POC here, so POC is every voter of
# colour rather than the three largest minority groups only. The four-group
# split modelled each minority group as voting on its own, which meant a
# cohesion matrix asserting how each behaved toward the other three; a single
# POC bloc asks the question these runs are actually about -- whether a system
# lets a coalition of minority voters elect candidates of its choice -- without
# those assertions.
#
# The two models name their White bloc differently, because it is a different
# population in each: WHI here is White alone, while the four-bloc runs use WAIO
# for White with American Indian and other-race VAP folded in -- there those two
# categories cannot join BLK/HIS/AAPI without picking one of them arbitrarily.
# Distinct names mean a config cannot quietly inherit the wrong grouping: a
# four-bloc config asking for WAIO gets a KeyError from this default rather than
# a White-only bloc that still partitions the electorate and so would go
# unnoticed. Every four-bloc config states its own "group_vap_columns" anyway.
#
# Every VAP column is claimed by exactly one group, so the two partition the
# electorate; get_group_vap_columns enforces that, and a column left out of both
# would quietly shrink every share computed from them.
DEFAULT_GROUP_VAP_COLUMNS = {
    "WHI": ["white_vap_20"],
    "POC": ["bvap_20", "hvap_20", "asian_nhpi_vap_20", "amin_vap_20", "other_vap_20"],
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
    return {slate: [slate] for slate in slate_names(config)}


def get_group_vap_columns(config, demographic_groups):
    """
    Return the {demographic_group: [vap_column, ...]} mapping for the given groups.

    Columns come from config["group_vap_columns"] when present, otherwise
    DEFAULT_GROUP_VAP_COLUMNS. A group's entry may be a single column name or a
    list of columns; either way it is normalized to a list, and a group's VAP is
    the sum over that list. This is what lets one group span several census
    categories (WAIO = White + American Indian + other races).

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
    slates = set(slate_names(config))

    missing_turnout = blocs - set(config["turnout"])
    if missing_turnout:
        raise KeyError(f"turnout is missing entries for bloc(s) {sorted(missing_turnout)}.")

    # primary_turnout is a partial override of turnout for the narrowing round of
    # two-round rules, so it may name any subset of the blocs -- but naming a bloc
    # that doesn't exist is a typo that would otherwise pass silently.
    primary_turnout = config.get("primary_turnout") or {}
    unknown_primary = set(primary_turnout) - blocs
    if unknown_primary:
        raise KeyError(
            f"primary_turnout has entries for unknown bloc(s) {sorted(unknown_primary)}; "
            f"expected a subset of {sorted(blocs)}."
        )
    bad_rates = {b: r for b, r in primary_turnout.items() if not 0 <= float(r) <= 1}
    if bad_rates:
        raise ValueError(f"primary_turnout rates must be between 0 and 1; got {bad_rates}.")

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


# Voting rules that need more candidates on the ballot than their kwargs state.
# Most rules advertise their requirement through a keyword argument (n_seats for
# multi-winner rules, m_1 for the number Alaska advances to its second round);
# these are the ones whose minimum is implicit in the rule itself.
IMPLICIT_CANDIDATE_MINIMUMS = {
    "TopTwo": 2,
}

def minimum_candidates(config, rule: str | None = None) -> int:
    """
    Smallest ballot every configured election can actually be run on.

    A district's candidate count is drawn at random (see generate_settings), but
    a rule that cannot seat its winners raises rather than returning a result --
    votekit's "Not enough candidates received votes to be elected". So the draw
    is floored here by the most demanding requirement:

    * each district config's `winners` (an election cannot fill more seats than
      it has candidates), and
    * each voting rule's own requirement -- `n_seats` for multi-winner rules,
      `m_1` for the number Alaska advances to round two, and any entry in
      IMPLICIT_CANDIDATE_MINIMUMS for rules that state it nowhere.

    Args:
        config: Parsed config dict.
        rule: When set (hybrid_election runs), floor using only this rule's own
            requirement instead of pooling every rule configured in the run --
            a hybrid entry's ballot never faces any rule but the one paired to
            it, so it shouldn't be inflated by an unrelated contest's seat
            count.

    Returns:
        The minimum number of candidates every district must put on the ballot.
    """
    voting_configs = config.get("voting_configs") or {}
    if rule is not None:
        kwargs = voting_configs[rule]
        required = [rule_seat_count(kwargs), IMPLICIT_CANDIDATE_MINIMUMS.get(rule, 1)]
    else:
        required = [int(d["winners"]) for d in config["district_configs"]]
        for r, kwargs in voting_configs.items():
            required.append(IMPLICIT_CANDIDATE_MINIMUMS.get(r, 1))
            for kwarg in CANDIDATE_COUNT_KWARGS:
                if kwarg in kwargs:
                    required.append(int(kwargs[kwarg]))

    # We set our minimum pool size for all elections to be one more than the
    # largest number of required candidates of the voting systems we're simulating

    return max(required) + 1


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


def _ensure_every_slate_runs(counts, candidate_count):
    """
    Move one candidate to any slate the sampling left empty, taking it from the
    largest slate.

    Only for a two-slate run. Cambridge's generator needs exactly two slates
    with candidates (votekit rejects anything else), so a district where one
    slate drew zero has no Cambridge ballots at all and drops out of that
    model's results -- which is how a nine-district contest ended up with only
    six or seven districts elected under Cambridge, and a hybrid run reported a
    fifteen-seat council filled with ten seats.

    The draw itself is unchanged: slate sizes are still sampled from the
    exaggerated shares, so the dominant slate still takes most of the pool. This
    only rescues the degenerate case, and it swaps rather than adds so the pool
    stays exactly candidate_count.

    Runs with three or more slates are left alone. There a slate drawing zero
    still leaves enough slates for every generator, and dropping it is the
    availability model working as intended rather than a failure.

    Args:
        counts: Dict of slate -> sampled candidate count.
        candidate_count: Total slots; the counts must still sum to this.

    Returns:
        The counts, with each slate holding at least one candidate where that
        is possible without emptying another.
    """
    if len(counts) != 2 or candidate_count < len(counts):
        return counts

    counts = dict(counts)
    for slate, count in counts.items():
        if count > 0:
            continue
        donor = max(counts, key=lambda s: counts[s])
        if counts[donor] < 2:
            break
        counts[donor] -= 1
        counts[slate] += 1
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

    A two-slate run is the exception: an empty slate there leaves one slate
    standing, which no two-slate generator can work with, so one candidate is
    swapped over from the other slate (see _ensure_every_slate_runs).

    Args:
        row: Row from the district population dataframe.
        slate_columns: Dict mapping each slate to its list of VAP column names,
            summed to give that slate's VAP (each group spans three columns).
        candidate_count: Total number of candidate slots to fill.
        config: Parsed config dict; read for the optional "availability_exps".

    Returns:
        Dict mapping each slate with a nonzero count to a list of candidate ids,
        e.g. {"WAIO": ["WAIO1", "WAIO2"]}.
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
    counts = _ensure_every_slate_runs(counts, candidate_count)
    return {
        s: [f"{s}{i}" for i in range(1, counts[s] + 1)]
        for s in slates
        if counts[s] > 0
    }


def filter_cohesion_to_slates(cohesion_parameters, active_slates):
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


def filter_alphas_to_slates(alphas, active_slates):
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


def _turnout_weighted_proportions(group_vap, turnout, bloc_definitions):
    """
    Normalized bloc shares of the modeled electorate: each bloc's turnout times
    the summed VAP of the demographic groups it aggregates, divided by the total.

    Split out from _build_district_settings so the same district VAP can be run
    against two turnout maps -- the configured one, and the lower primary_turnout
    used for the narrowing round of two-round rules.

    Args:
        group_vap: Dict mapping each demographic group to its combined VAP.
        turnout: Dict mapping each bloc to its turnout rate.
        bloc_definitions: Dict mapping each bloc to its demographic groups.

    Returns:
        Dict of bloc -> proportion, summing to 1. A district with no modeled VAP
        falls back to equal shares.
    """
    blocs = list(bloc_definitions)
    weighted = {
        bloc: turnout[bloc] * sum(group_vap[g] for g in bloc_definitions[bloc])
        for bloc in blocs
    }
    denom = sum(weighted.values())
    if denom <= 0:
        return {bloc: 1.0 / len(blocs) for bloc in blocs}
    return {bloc: weighted[bloc] / denom for bloc in blocs}


def primary_turnout_map(config):
    """
    The turnout map for the narrowing round: the configured turnout with
    primary_turnout's entries laid over it, so a config need only name the blocs
    whose participation drops. None when the run doesn't model a separate
    primary electorate.
    """
    primary = config.get("primary_turnout")
    if not primary:
        return None
    return {**config["turnout"], **primary}


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
        When the config sets primary_turnout, also primary_bloc_proportions --
        the same districts under the narrowing round's lower turnout.
    """
    # A group's VAP is the sum over its columns, so a multi-column group like
    # WAIO (White + American Indian + other) is combined before anything else
    # uses it.
    group_vap = {
        group: sum(float(row[column]) for column in columns)
        for group, columns in group_columns.items()
    }

    settings = {
        "bloc_proportions": _turnout_weighted_proportions(
            group_vap, config["turnout"], bloc_definitions
        )
    }

    # Second electorate for the narrowing round of two-round rules. Written only
    # when the run models one; everything downstream keys off its presence.
    primary_turnout = primary_turnout_map(config)
    if primary_turnout is not None:
        settings["primary_bloc_proportions"] = _turnout_weighted_proportions(
            group_vap, primary_turnout, bloc_definitions
        )
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
        per voter bloc. Each district config sets "candidate_pool_max" and
        "candidate_pool_mean", the range and mean of its candidate-pool draw
        (floor + Binomial), where the floor is minimum_candidates(config).
        slate_to_candidates is resized per-district: candidate_count
        (drawn from a geometric distribution, capped at a log-VAP ceiling, floored
        at the district's winner count) is apportioned across slates in proportion
        to each slate's share of modeled VAP in that district (see
        _build_slate_to_candidates). A slate sampled zero candidates is dropped from
        slate_to_candidates, and its column is dropped (with cohesion_parameters
        renormalized) from that district's cohesion_parameters and alphas.
    """
    random.seed(config["seed"])

    bloc_definitions = get_bloc_definitions(config)
    _validate_bloc_config(config, bloc_definitions)
    # The demographic groups we need VAP for are the union across all blocs.
    demographic_groups = list(dict.fromkeys(
        g for groups in bloc_definitions.values() for g in groups
    ))
    group_columns = get_group_vap_columns(config, demographic_groups)
    slate_columns = get_group_vap_columns(config, slate_names(config))

    population_data = gpd.read_file(config['geodata_path'])
    # group_columns / slate_columns map each label to a *list* of columns (a group
    # like WAIO spans several), so flatten before de-duplicating.
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

    # In a hybrid_election run, every district_configs entry is paired to the
    # one voting rule that fills its seat count (e.g. 9x1 <-> IRV, 1x6 <->
    # FastSTV); candidate floors and modeled electorate size are then scoped
    # to that pairing below instead of pooled across the whole run.
    hybrid = bool(config.get("hybrid_election"))
    parsed_district_configs = parse_district_configs(config["district_configs"])
    hybrid_pairing = (
        match_hybrid_contests(parsed_district_configs, config.get("voting_configs") or {})
        if hybrid else {}
    )

    run_name = config['run_name']

    for district_config in config['district_configs']:
        district_num, winners = district_config['num_districts'], district_config['winners']

        # Every district's ballot must be large enough for the rule(s) it will
        # actually face: in a hybrid run that's just this entry's matched rule,
        # otherwise every rule configured in the run (since any of them may run
        # against these districts -- see simulate_elections).
        matched_rule = hybrid_pairing.get(winners) if hybrid else None
        candidate_floor = minimum_candidates(config, rule=matched_rule)
        rules_desc = matched_rule or ', '.join(config.get('voting_configs') or ['none'])
        print(
            f"[generate_settings] {district_num}x{winners}: candidate floor {candidate_floor} "
            f"(voting rule(s): {rules_desc})"
        )

        output_settings = {"num_voters": config["num_voters"]}

        # Candidate pool size is drawn as floor + Binomial(n, p), with the range
        # and the mean coming from this district config: n spans floor..ceiling,
        # and p places the mean where the config asks. Neither is checked -- a
        # mean outside [floor, ceiling] gives numpy a p outside [0, 1] and it
        # will say so, and a ceiling equal to the floor divides by zero.
        pool_n = district_config['candidate_pool_max'] - candidate_floor
        pool_p = (district_config['candidate_pool_mean'] - candidate_floor) / pool_n
        print(
            f"[generate_settings] {district_num}x{winners}: candidate pool in "
            f"[{candidate_floor}, {district_config['candidate_pool_max']}], "
            f"mean {district_config['candidate_pool_mean']}"
        )
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

                    candidate_count = candidate_floor + np.random.binomial(pool_n, pool_p)

                    slate_to_candidates = _build_slate_to_candidates(
                        row, slate_columns, candidate_count, config
                    )
                    active_slates = list(slate_to_candidates)
                    cohesion_parameters = filter_cohesion_to_slates(config["cohesion_parameters"], active_slates)
                    alphas = filter_alphas_to_slates(config["alphas"], active_slates)
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
