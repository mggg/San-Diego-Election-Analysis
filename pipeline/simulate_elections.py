"""
Run elections on generated voter profiles and record the winners.

Reads voter profiles from the run's compressed profiles.zip archive (written by
the profile-generation step) and runs each election rule configured under the
config's ``voting_configs`` field, then writes aggregated election results to
JSON files.

Which rules run, and with what parameters, is entirely config-driven: each key
in ``voting_configs`` is the name of a VoteKit election class (e.g. "FastSTV",
"Plurality") and its value is the keyword arguments passed straight to that
class. That includes the seat count -- multi-winner rules need their ``n_seats``
argument set in the config.

The profile type each rule needs (RankProfile vs ScoreProfile) is discovered
from the election class's own signature, so score-based rules load the profile
csv into the right class automatically.
"""

from __future__ import annotations
import json
import inspect
import os
import tempfile
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from joblib import Parallel, delayed
from votekit import RankProfile, ScoreProfile, elections
from typing import List, Iterable, Dict, Optional, Tuple, get_args

# Optional progress bar for joblib.
try:
    from joblib_progress import joblib_progress
except Exception:
    joblib_progress = None

from pipeline.utils.helpers import (
    parse_district_configs,
    score_rule_budgets,
    get_voter_models,
    election_results_signature,
    load_json,
    parse_plan_district_rep_from_path,
    find_settings_file,
    primary_profiles_zip_path,
    read_existing_zip_members,
)
from pipeline.profile_generator import (
    generator_accepts_total_points,
    profile_arcname,
    profile_class_for_mode,
    score_budgets_for_run,
)
from pipeline.two_round_election import is_two_round, run_primary_general_election


def _required_profile(cls) -> Tuple[type, ...]:
    """
    Return the profile type(s) an election class accepts as its first argument.

    Reads the annotation of the class's ``profile`` constructor parameter. When
    that annotation is a union (e.g. RankProfile | ScoreProfile) every member is
    returned; when it is a single class, a one-tuple of that class is returned.
    """
    annotation = inspect.signature(cls.__init__).parameters["profile"].annotation
    expected_types = get_args(annotation)
    return expected_types if expected_types else (annotation,)


@dataclass(frozen=True)
class ElectionPlanEntry:
    """
    One resolved voting_configs entry: either an ordinary VoteKit election class,
    or the generic primary/general rule (see pipeline.two_round_election).

    accepted_profiles is every profile type the rule can run on, straight from
    its own signature: ranked rules accept RankProfile, Cumulative/Limited accept
    ScoreProfile, and a few (BlockPlurality) accept either. It decides which
    voter models a rule is run under -- see plan_for_profile_class.
    """
    rule: str
    accepted_profiles: Tuple[type, ...]       # RankProfile and/or ScoreProfile
    election_class: Optional[type] = None     # set for ordinary VoteKit rules
    is_two_round: bool = False                # set for the primary/general rule

    @property
    def is_custom(self) -> bool:
        return self.is_two_round

    def accepts(self, profile_class: type) -> bool:
        return profile_class in self.accepted_profiles


def _build_election_plan(voting_configs: dict) -> List[ElectionPlanEntry]:
    """
    Resolve each configured voting rule to the plan entry that describes how to
    run it, once. This work only depends on voting_configs (not on any profile),
    so doing it a single time up front avoids repeating class lookups and
    signature introspection for every profile file.

    A rule is the generic primary/general rule when its kwargs carry
    "general_class" (or its former name "round2_class") -- checked first, and
    unconditionally, since a config can freely name this rule after the VoteKit
    class it's modeled on ("Alaska", "TopTwo") without that name being taken
    literally. Only when that marker is absent does the rule name get resolved
    as an ordinary VoteKit election class via getattr(elections, rule).
    Anything else is an unrecognized rule name.

    Args:
        voting_configs: Mapping of rule name -> kwargs from the config file.

    Returns:
        List of ElectionPlanEntry in config order.

    Raises:
        ValueError: If a rule name is neither a VoteKit election class nor a
            two-round rule (missing "general_class"/"round2_class").
    """
    plan: List[ElectionPlanEntry] = []
    for rule, kwargs in voting_configs.items():
        if is_two_round(kwargs):
            # The primary (Plurality) always runs on a RankProfile.
            plan.append(ElectionPlanEntry(rule, (RankProfile,), is_two_round=True))
            continue

        election_class = getattr(elections, rule, None)
        if election_class is None:
            raise ValueError(
                f"Unknown voting rule '{rule}' in voting_configs. "
                f"Expected a VoteKit election class name (e.g. 'FastSTV', 'Plurality', 'IRV'), "
                f"or a two-round rule with a 'general_class' kwarg."
            )
        profile_types = tuple(
            t for t in _required_profile(election_class) if t in (RankProfile, ScoreProfile)
        )
        plan.append(ElectionPlanEntry(rule, profile_types, election_class=election_class))
    return plan


def plan_for_profile_class(
    plan: List[ElectionPlanEntry], profile_class: type
) -> List[ElectionPlanEntry]:
    """
    The rules in `plan` that can run on ballots of this type.

    Voter models and voting rules are no longer interchangeable: the ranked
    models (slate_pl, slate_bt) produce RankProfiles that STV and IRV read but
    Cumulative and Limited cannot, and name_cumulative produces ScoreProfiles the
    other way around. Rather than fail when a run configures both families, each
    model runs the subset of rules its ballots support.
    """
    return [entry for entry in plan if entry.accepts(profile_class)]


def plan_needs_settings(plan: List[ElectionPlanEntry]) -> bool:
    """
    Whether any rule in the plan needs the district's settings dict (i.e. at
    least one primary/general rule is configured). Gates the extra
    settings-file lookups in simulate_elections so ordinary runs pay no cost.
    """
    return any(entry.is_custom for entry in plan)


def _candidate_list_from_elected(elected: Iterable[set]) -> List[str]:
    """
    Flatten votekit election output (iterable of per-round elected sets) into a
    list of strings.

    A round's set isn't always a singleton: multi-winner methods like FastSTV
    can elect several candidates in the same round (e.g. multiple candidates
    crossing quota together, or a final round electing everyone once remaining
    candidates == remaining seats), so every candidate in every round's set must
    be kept, not just one.

    Args:
        elected: Iterable of sets (one per round), as returned by votekit election
            methods -- each set may contain one or more candidates.

    Returns:
        List of candidate id strings in election order. Empty sets are skipped silently.
    """
    winners: List[str] = []
    for s in elected:
        winners.extend(str(c) for c in s)
    return winners


def _load_profile_from_bytes(csv_bytes: bytes, profile_class):
    """
    Load a profile csv (already read out of the run's profiles.zip) into the
    given profile class.

    Neither RankProfile nor ScoreProfile can read from an in-memory buffer, so we
    write the bytes to a unique temp file and delete it after loading. The bytes
    are read from the archive once in the main process (see simulate_elections)
    and handed to the worker, so the worker never opens the zip itself -- which
    avoids re-parsing the archive's central directory once per profile (an
    O(profiles^2) cost on large runs).

    Args:
        csv_bytes: The profile csv content, as read from the zip member.
        profile_class: RankProfile or ScoreProfile.

    Returns:
        The loaded profile instance.
    """
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.csv', delete=False) as tmp:
        temp_path = Path(tmp.name)
        tmp.write(csv_bytes)
    try:
        return profile_class.from_csv(temp_path)
    finally:
        os.remove(temp_path)


def _process_profile(
    csv_bytes: bytes,
    election_plan: List[ElectionPlanEntry],
    voting_configs: dict,
    settings: Optional[dict] = None,
    mode: Optional[str] = None,
    primary_csv_bytes: Optional[bytes] = None,
    profile_class: type = RankProfile,
    rule_budgets: Optional[Dict[str, int]] = None,
) -> Tuple[Dict[str, List[str]], Dict[str, str], Dict[str, List[str]]]:
    """
    Load a voter profile (its csv bytes, read from profiles.zip by the caller) and
    run each configured election to determine winners.

    Args:
        csv_bytes: The profile csv content, as read from the zip member.
        election_plan: Precomputed ElectionPlanEntry list from
            _build_election_plan, already filtered to the rules this voter model's
            ballots support (see plan_for_profile_class).
        voting_configs: Mapping of rule name -> kwargs from the config file. The
            kwargs are spread straight into the election class, so any VoteKit
            parameter (including the seat count) is set there.
        settings: The profile's district settings dict (only needed when the
            plan includes a two-round rule; see plan_needs_settings).
        mode: Voter model name this profile was generated with (only needed for
            the same reason as settings).
        primary_csv_bytes: The matching entry from primary_profiles.zip, when the
            run models a separate primary electorate. Two-round rules narrow on
            these ballots instead of csv_bytes; every other rule, and the general
            itself, is unaffected.
        profile_class: The profile type this voter model produces -- RankProfile
            for the ranked models, ScoreProfile for name_cumulative.
        rule_budgets: For score models, {rule -> budget}; csv_bytes is then a
            {budget -> csv} mapping, since Cumulative and Limited reject ballots
            worth more than their own budget and so cannot share one profile.
            None for ranked models, where csv_bytes is a single csv.

    Returns:
        (results, general_profiles, primary_results):
            results maps each configured rule name to its list of winner ids,
            e.g. {"FastSTV": ["A2", "B1"], "Plurality": ["A2", "A3"]}. For a
            two-round rule these are the general's winners.
            general_profiles maps each two-round rule name to the freshly
            sampled general-election profile's CSV text (empty when the plan has
            none), for the caller to persist.
            primary_results maps each two-round rule name to the finalists its
            primary advanced -- the intermediate outcome the general was decided
            from, which is otherwise lost.
    """
    if not election_plan:
        return {}, {}, {}

    # A ranked model has one profile for every rule. A score model has one per
    # budget, parsed on first use and reused across rules sharing that budget.
    parsed: Dict = {}

    def profile_for(entry: ElectionPlanEntry):
        key = rule_budgets.get(entry.rule) if rule_budgets else None
        if key not in parsed:
            raw = csv_bytes[key] if rule_budgets else csv_bytes
            parsed[key] = _load_profile_from_bytes(raw, profile_class)
        return parsed[key]

    primary_profile_cache: List = []

    results: Dict[str, List[str]] = {}
    general_profiles: Dict[str, str] = {}
    primary_results: Dict[str, List[str]] = {}
    for entry in election_plan:
        profile = profile_for(entry)
        if entry.is_custom:
            # Primary-round ballots are a separate electorate, so they are parsed
            # separately -- once, then shared across the two-round rules.
            primary_profile = profile
            if primary_csv_bytes is not None:
                if not primary_profile_cache:
                    primary_profile_cache.append(
                        _load_profile_from_bytes(primary_csv_bytes, profile_class)
                    )
                primary_profile = primary_profile_cache[0]

            winners, general_csv, finalists = run_primary_general_election(
                primary_profile, settings, mode, voting_configs[entry.rule]
            )
            general_profiles[entry.rule] = general_csv
            primary_results[entry.rule] = finalists
        else:
            elected = entry.election_class(profile, **voting_configs[entry.rule]).get_elected()
            winners = _candidate_list_from_elected(elected)

        results[entry.rule] = winners

    return results, general_profiles, primary_results


def _result_file_current(out_path: Path, expected_len: int, signature: str) -> bool:
    """
    Whether an existing election-results file can be reused as-is.

    Reusable only when it loads cleanly, was written under the current signature
    (so the profiles and voting rules that produced it still match this config),
    and holds exactly the expected number of results (so a run whose profile set
    later grew -- e.g. more replicates -- is re-simulated rather than left short).

    Args:
        out_path: Path to the (mode, district) election-results json file.
        expected_len: Number of profiles this (mode, district) should now have.
        signature: The current election-results signature.

    Returns:
        True if the file is present, current, and complete; False otherwise
        (missing, unreadable, stale signature, or wrong length).
    """
    if not out_path.is_file():
        return False
    try:
        data = json.load(open(out_path, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("signature") != signature:
        return False
    results = data.get("election_results", [])
    return len(results) == expected_len and len(data.get("profile_files", [])) == expected_len


def simulate_elections(config) -> None:
    """
    Run the configured elections in parallel over all voter profiles.

    Args:
        config: Parsed config dict. Must include a ``voting_configs`` mapping of
            VoteKit election class name -> kwargs.

    Outputs:
        One json file per (mode, district_count, winners) combination at
        outputs/<run_name>/election_results/<mode>/
        <run_name>_<n>_districts_<w>_winners_for_voter_mode_<mode>.json.
        Each file contains an "election_results" list, index-aligned with
        "profile_files", where each entry maps every configured rule name to its
        list of winner ids, e.g. {"FastSTV": [...], "Plurality": [...]}.

        If any rule is a primary/general rule (see pipeline.two_round_election),
        also writes:

        * outputs/<run_name>/primary_results/<mode>/<same filename>.json -- the
          finalists each rule's primary advanced, in the same shape as the
          election results and index-aligned with the same "profile_files", so
          the primary and general files line up row for row.
        * outputs/<run_name>/general_profiles.zip -- one freshly-sampled,
          finalists-only profile per (rule, profile file) -- and a
          general_profiles_metadata.json sidecar recording the signature it was
          written under.

        Configs with no two-round rule never touch any of them.

    Returns:
        None.
    """
    run_name = str(config["run_name"])
    district_configs = parse_district_configs(config["district_configs"])

    voting_configs = config["voting_configs"]
    # Resolve rules to classes once up front (also surfaces bad rule names before
    # any elections run).
    election_plan = _build_election_plan(voting_configs)
    needs_settings = plan_needs_settings(election_plan)

    # Results already written under this signature (profiles + voting rules) can
    # be reused; a (mode, district) whose file is missing, stale, or short is
    # (re-)simulated -- so adding a voter model / voting rule / district config,
    # or growing the profile set, only fills in what's missing. Round-2 profile
    # content depends on the same signature (it's derived from voting_configs'
    # m_1/general_kwargs), so general_profiles.zip and primary_results/ are kept
    # current against it too.
    signature = election_results_signature(config)

    modes = get_voter_models(config)
    n_jobs = -1  # use all available cores

    out_root = Path("outputs") / f'{run_name}' / "election_results"
    out_root.mkdir(parents=True, exist_ok=True)
    # Finalists from the primary stage of any two-round rule, mirroring
    # election_results/ file for file.
    primary_out_root = Path("outputs") / f'{run_name}' / "primary_results"

    # profiles for the whole run live in one compressed archive; list its members
    # once and select the relevant ones per (mode, district count) below.
    zip_path = Path(f"outputs/{run_name}/profiles.zip")
    with zipfile.ZipFile(zip_path) as archive:
        all_members = archive.namelist()

    # Two-round rules narrow on the primary electorate when the run models one.
    # Entry names match profiles.zip, so the same member key reads both.
    primary_zip_path = primary_profiles_zip_path(run_name) if config.get("primary_turnout") else None
    if primary_zip_path is not None and not primary_zip_path.is_file():
        raise FileNotFoundError(
            f"config sets primary_turnout but {primary_zip_path} is missing; "
            "re-run the profile stage to generate the primary-round profiles."
        )
    if primary_zip_path is not None:
        builtin_two_round = [r for r in ("Alaska", "TopTwo") if r in voting_configs]
        if builtin_two_round:
            print(
                f"[simulate_elections] WARNING: primary_turnout is set, but {builtin_two_round} "
                "build their general from the primary's own ballots and cannot use a separate "
                "primary electorate. They will run on the standard profiles."
            )

    # Settings-file lookups are only needed by two-round rules; cached by
    # resolved path (not (plan, district)) so every replicate sharing one
    # settings file triggers just one JSON load, amortized across the whole run.
    settings_cache: Dict[Path, dict] = {}

    def _settings_for_member(member: str, district_num: int) -> dict:
        plan, district, _rep = parse_plan_district_rep_from_path(member)
        settings_dir = Path(f"outputs/{run_name}/settings/{district_num}")
        settings_path = find_settings_file(settings_dir, run_name, plan=plan, district=district)
        if settings_path is None:
            raise FileNotFoundError(
                f"No settings file for member '{member}' (plan={plan}, district={district})."
            )
        if settings_path not in settings_cache:
            settings_cache[settings_path] = load_json(settings_path)
        return settings_cache[settings_path]

    # Round-2 profiles are keyed by the same arcname as their round-1 profile
    # (nested under the rule name), so a naive append risks a stale duplicate
    # entry sitting alongside a fresh one -- ZipFile.read() would keep resolving
    # to whichever was written first. Guard against that the same way
    # profiles.zip's resumability does: rebuild wholesale on any signature
    # mismatch (every combo is stale and gets recomputed in this same call, so
    # the archive is fully repopulated in one pass); append-with-dedup only when
    # resuming under an unchanged signature.
    general_zip_path = Path(f"outputs/{run_name}/general_profiles.zip")
    general_metadata_path = Path(f"outputs/{run_name}/general_profiles_metadata.json")
    general_archive: Optional[zipfile.ZipFile] = None
    existing_general_members: set = set()

    if needs_settings:
        prior_general_signature = None
        if general_metadata_path.is_file():
            try:
                prior_general_signature = load_json(general_metadata_path).get("signature")
            except (json.JSONDecodeError, OSError):
                prior_general_signature = None

        general_zip_path.parent.mkdir(parents=True, exist_ok=True)
        if prior_general_signature == signature:
            resumed_members = read_existing_zip_members(general_zip_path)
            if resumed_members is not None:
                existing_general_members = resumed_members
                general_archive = zipfile.ZipFile(general_zip_path, "a", compression=zipfile.ZIP_DEFLATED)
        if general_archive is None:
            general_archive = zipfile.ZipFile(general_zip_path, "w", compression=zipfile.ZIP_DEFLATED)
            existing_general_members = set()

    try:
        # run elections for each voter model
        for mode in modes:
            output_dir = out_root / mode
            output_dir.mkdir(parents=True, exist_ok=True)

            # A voter model's ballots decide which rules can run on them: the
            # ranked models feed STV/IRV/Plurality, name_cumulative feeds
            # Cumulative/Limited. Rules the model cannot supply are skipped here
            # rather than raising when the csv fails to load as the wrong type.
            mode_profile_class = profile_class_for_mode(mode)
            mode_plan = plan_for_profile_class(election_plan, mode_profile_class)
            skipped = [e.rule for e in election_plan if e not in mode_plan]
            if skipped:
                print(
                    f"[simulate_elections] {mode} produces {mode_profile_class.__name__} ballots; "
                    f"skipping {skipped} (they need "
                    f"{'ScoreProfile' if mode_profile_class is RankProfile else 'RankProfile'})."
                )
            if not mode_plan:
                print(
                    f"[simulate_elections] WARNING: no configured voting rule can run on "
                    f"{mode}'s ballots. Nothing to simulate for this voter model."
                )
                continue
            mode_needs_settings = plan_needs_settings(mode_plan)
            # Score models store one profile per budget; the canonical member
            # list comes from any one budget's subtree and the others are read at
            # the same path with the budget swapped.
            is_score_mode = generator_accepts_total_points(mode)
            mode_rule_budgets = (
                {e.rule: score_rule_budgets(voting_configs)[e.rule] for e in mode_plan
                 if e.rule in score_rule_budgets(voting_configs)}
                if is_score_mode else None
            )

            for dc in district_configs:
                if is_score_mode:
                    budgets = score_budgets_for_run(config, dc.winners)
                    canonical_budget = budgets[0]
                    prefix = f"{mode}/{canonical_budget}/{dc.num_districts}/"
                else:
                    budgets = [None]
                    canonical_budget = None
                    prefix = f"{mode}/{dc.num_districts}/"
                all_profile_files = [m for m in all_members if m.startswith(prefix) and m.endswith(".csv")]

                out_path = output_dir / (
                    f"{run_name}_{dc.num_districts}_districts_{dc.winners}_winners_for_voter_mode_{mode}.json"
                )
                if _result_file_current(out_path, len(all_profile_files), signature):
                    print(f"[simulate_elections] Up to date, skipping: {out_path}")
                    continue

                desc = f"Running elections for {dc.num_districts} districts, {dc.winners} winner(s), mode={mode}"
                if joblib_progress is not None:
                    ctx = joblib_progress(description=desc, total=len(all_profile_files))
                else:
                    ctx = None

                # Open the archive once for the whole batch and stream each member's
                # bytes to the workers, rather than having every worker reopen the zip
                # (which re-parses the central directory per profile -- an
                # O(profiles^2) cost on large runs). The bytes are yielded lazily, so
                # joblib's pre_dispatch bounds how many decompressed profiles are held
                # in memory at once. results_list stays index-aligned with
                # all_profile_files because the generator yields in that order.
                with ExitStack() as archives:
                    archive = archives.enter_context(zipfile.ZipFile(zip_path))
                    primary_archive = (
                        archives.enter_context(zipfile.ZipFile(primary_zip_path))
                        if primary_zip_path is not None and mode_needs_settings
                        else None
                    )
                    def _payload(member: str):
                        """One profile for a ranked model; one per budget for a score model."""
                        if not is_score_mode:
                            return archive.read(member)
                        tail = member.split("/", 2)[2]  # <district_num>/<file>.csv
                        return {b: archive.read(f"{mode}/{b}/{tail}") for b in budgets}

                    tasks = (
                        delayed(_process_profile)(
                            _payload(member),
                            mode_plan,
                            voting_configs,
                            _settings_for_member(member, dc.num_districts) if mode_needs_settings else None,
                            mode if mode_needs_settings else None,
                            primary_archive.read(member) if primary_archive is not None else None,
                            mode_profile_class,
                            mode_rule_budgets,
                        )
                        for member in all_profile_files
                    )
                    if ctx is not None:
                        with ctx:
                            results_list = Parallel(n_jobs=n_jobs)(tasks)
                    else:
                        print(f"[simulate_elections] {desc} (no joblib_progress installed)")
                        results_list = Parallel(n_jobs=n_jobs)(tasks)

                # results_list holds (results, general_profiles, primary_results)
                # triples; the winners dict goes into the election-results json and
                # the finalists into the primary-results json beside it.
                election_results = [r for r, _, _ in results_list]
                primary_results = [pr for _, _, pr in results_list]

                if mode_needs_settings:
                    for member, (_, general_profiles, _) in zip(all_profile_files, results_list):
                        for rule, csv_text in general_profiles.items():
                            arcname = f"{rule}/{member}"
                            if arcname not in existing_general_members:
                                general_archive.writestr(arcname, csv_text)
                                existing_general_members.add(arcname)

                # write all winners for this district/mode combo to one json file
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "run_name": run_name,
                            "voter_mode": mode,
                            "district_num": dc.num_districts,
                            "winners_per_district": dc.winners,
                            "signature": signature,
                            "profile_files": all_profile_files,
                            "election_results": election_results,
                        },
                        f,
                        indent=2,
                    )

                print(f"[simulate_elections] Wrote: {out_path}")

                # The primary's finalists are an outcome in their own right --
                # who reached the general -- and are otherwise unrecoverable
                # without re-running the primary. Written in the same shape as the
                # general's results, keyed by the same profile_files order, so the
                # two files line up row for row.
                if mode_needs_settings and any(primary_results):
                    primary_dir = primary_out_root / mode
                    primary_dir.mkdir(parents=True, exist_ok=True)
                    primary_path = primary_dir / out_path.name
                    with open(primary_path, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "run_name": run_name,
                                "voter_mode": mode,
                                "district_num": dc.num_districts,
                                "winners_per_district": dc.winners,
                                "signature": signature,
                                "stage": "primary",
                                "advances_per_rule": {
                                    rule: voting_configs[rule].get("m_1")
                                    for rule in primary_results[0]
                                },
                                "profile_files": all_profile_files,
                                "primary_results": primary_results,
                            },
                            f,
                            indent=2,
                        )
                    print(f"[simulate_elections] Wrote: {primary_path}")
    finally:
        if general_archive is not None:
            general_archive.close()

    if needs_settings:
        with open(general_metadata_path, "w", encoding="utf-8") as f:
            json.dump({"signature": signature}, f)

if __name__ == '__main__':
    config = load_json("configs/basic.json")
    simulate_elections(config)