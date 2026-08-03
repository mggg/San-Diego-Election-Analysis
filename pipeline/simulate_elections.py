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
    get_voter_models,
    election_results_signature,
    load_json,
    parse_plan_district_rep_from_path,
    find_settings_file,
    primary_profiles_zip_path,
    read_existing_zip_members,
)
from pipeline.two_round_election import run_two_round_fresh_profile


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
    or the generic two-round-fresh-profile rule (see pipeline.two_round_election).
    """
    rule: str
    profile_class: type                       # RankProfile or ScoreProfile
    election_class: Optional[type] = None     # set for ordinary VoteKit rules
    is_two_round: bool = False                # set for the two-round-fresh-profile rule

    @property
    def is_custom(self) -> bool:
        return self.is_two_round


def _build_election_plan(voting_configs: dict) -> List[ElectionPlanEntry]:
    """
    Resolve each configured voting rule to the plan entry that describes how to
    run it, once. This work only depends on voting_configs (not on any profile),
    so doing it a single time up front avoids repeating class lookups and
    signature introspection for every profile file.

    A rule is an ordinary VoteKit election class when its name resolves via
    getattr(elections, rule). Otherwise, if its kwargs carry "round2_class", it's
    the generic two-round-fresh-profile rule (Plurality round 1 -> freshly
    resampled profile -> round2_class for round 2; see
    pipeline.two_round_election.run_two_round_fresh_profile) -- this covers
    Alaska- and TopTwo-shaped rules, and any future two-round rule, via config
    alone. Anything else is an unrecognized rule name.

    Args:
        voting_configs: Mapping of rule name -> kwargs from the config file.

    Returns:
        List of ElectionPlanEntry in config order.

    Raises:
        ValueError: If a rule name is neither a VoteKit election class nor a
            two-round rule (missing "round2_class").
    """
    plan: List[ElectionPlanEntry] = []
    for rule, kwargs in voting_configs.items():
        election_class = getattr(elections, rule, None)
        if election_class is not None:
            profile_types = _required_profile(election_class)
            profile_class = RankProfile if RankProfile in profile_types else ScoreProfile
            plan.append(ElectionPlanEntry(rule, profile_class, election_class=election_class))
        elif "round2_class" in kwargs:
            # Round 1 (Plurality) always runs on a RankProfile.
            plan.append(ElectionPlanEntry(rule, RankProfile, is_two_round=True))
        else:
            raise ValueError(
                f"Unknown voting rule '{rule}' in voting_configs. "
                f"Expected a VoteKit election class name (e.g. 'FastSTV', 'Plurality', 'IRV'), "
                f"or a two-round rule with a 'round2_class' kwarg."
            )
    return plan


def plan_needs_settings(plan: List[ElectionPlanEntry]) -> bool:
    """
    Whether any rule in the plan needs the district's settings dict (i.e. at
    least one two-round-fresh-profile rule is configured). Gates the extra
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
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """
    Load a voter profile (its csv bytes, read from profiles.zip by the caller) and
    run each configured election to determine winners.

    Args:
        csv_bytes: The profile csv content, as read from the zip member.
        election_plan: Precomputed ElectionPlanEntry list from
            _build_election_plan; avoids per-file class lookup/introspection.
        voting_configs: Mapping of rule name -> kwargs from the config file. The
            kwargs are spread straight into the election class, so any VoteKit
            parameter (including the seat count) is set there.
        settings: The profile's district settings dict (only needed when the
            plan includes a two-round rule; see plan_needs_settings).
        mode: Voter model name this profile was generated with (only needed for
            the same reason as settings).
        primary_csv_bytes: The matching entry from primary_profiles.zip, when the
            run models a separate primary electorate. Two-round rules narrow on
            these ballots instead of csv_bytes; every other rule, and round 2
            itself, is unaffected.

    Returns:
        (results, round2_profiles):
            results maps each configured rule name to its list of winner ids,
            e.g. {"FastSTV": ["A2", "B1"], "Plurality": ["A2", "A3"]}.
            round2_profiles maps each two-round rule name to its freshly
            sampled round-2 profile's CSV text (empty when the plan has none),
            for the caller to persist.
    """
    # Parse each distinct profile type from the csv at most once and reuse it
    # across rules that need it (e.g. two rank-based rules), instead of
    # re-reading the same file per rule. Primary-round ballots are a separate
    # electorate, so they get their own cache rather than sharing this one.
    profile_cache: dict = {}
    primary_cache: dict = {}

    results: Dict[str, List[str]] = {}
    round2_profiles: Dict[str, str] = {}
    for entry in election_plan:
        profile = profile_cache.get(entry.profile_class)
        if profile is None:
            profile = _load_profile_from_bytes(csv_bytes, entry.profile_class)
            profile_cache[entry.profile_class] = profile

        if entry.is_custom:
            round1_profile = profile
            if primary_csv_bytes is not None:
                round1_profile = primary_cache.get(entry.profile_class)
                if round1_profile is None:
                    round1_profile = _load_profile_from_bytes(primary_csv_bytes, entry.profile_class)
                    primary_cache[entry.profile_class] = round1_profile

            winners, round2_csv = run_two_round_fresh_profile(
                round1_profile, settings, mode, voting_configs[entry.rule]
            )
            round2_profiles[entry.rule] = round2_csv
        else:
            elected = entry.election_class(profile, **voting_configs[entry.rule]).get_elected()
            winners = _candidate_list_from_elected(elected)

        results[entry.rule] = winners

    return results, round2_profiles


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

        If any rule is a two-round-fresh-profile rule (see
        pipeline.two_round_election), also writes
        outputs/<run_name>/round2_profiles.zip -- one freshly-sampled,
        finalists-only profile per (rule, profile file) -- and a
        round2_profiles_metadata.json sidecar recording the signature it was
        written under. Configs with no two-round rule never touch either file.

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
    # m_1/round2_kwargs), so round2_profiles.zip is kept current against it too.
    signature = election_results_signature(config)

    modes = get_voter_models(config)
    n_jobs = -1  # use all available cores

    out_root = Path("outputs") / f'{run_name}' / "election_results"
    out_root.mkdir(parents=True, exist_ok=True)

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
                "build round 2 by stripping the round-1 ballots and cannot use a separate "
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
    round2_zip_path = Path(f"outputs/{run_name}/round2_profiles.zip")
    round2_metadata_path = Path(f"outputs/{run_name}/round2_profiles_metadata.json")
    round2_archive: Optional[zipfile.ZipFile] = None
    existing_round2_members: set = set()

    if needs_settings:
        prior_round2_signature = None
        if round2_metadata_path.is_file():
            try:
                prior_round2_signature = load_json(round2_metadata_path).get("signature")
            except (json.JSONDecodeError, OSError):
                prior_round2_signature = None

        round2_zip_path.parent.mkdir(parents=True, exist_ok=True)
        if prior_round2_signature == signature:
            resumed_members = read_existing_zip_members(round2_zip_path)
            if resumed_members is not None:
                existing_round2_members = resumed_members
                round2_archive = zipfile.ZipFile(round2_zip_path, "a", compression=zipfile.ZIP_DEFLATED)
        if round2_archive is None:
            round2_archive = zipfile.ZipFile(round2_zip_path, "w", compression=zipfile.ZIP_DEFLATED)
            existing_round2_members = set()

    try:
        # run elections for each voter model
        for mode in modes:
            output_dir = out_root / mode
            output_dir.mkdir(parents=True, exist_ok=True)

            for dc in district_configs:
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
                        if primary_zip_path is not None and needs_settings
                        else None
                    )
                    tasks = (
                        delayed(_process_profile)(
                            archive.read(member),
                            election_plan,
                            voting_configs,
                            _settings_for_member(member, dc.num_districts) if needs_settings else None,
                            mode if needs_settings else None,
                            primary_archive.read(member) if primary_archive is not None else None,
                        )
                        for member in all_profile_files
                    )
                    if ctx is not None:
                        with ctx:
                            results_list = Parallel(n_jobs=n_jobs)(tasks)
                    else:
                        print(f"[simulate_elections] {desc} (no joblib_progress installed)")
                        results_list = Parallel(n_jobs=n_jobs)(tasks)

                # results_list is a list of (results, round2_profiles) pairs; only
                # the winners dict goes into the election-results json.
                election_results = [r for r, _ in results_list]

                if needs_settings:
                    for member, (_, round2_profiles) in zip(all_profile_files, results_list):
                        for rule, csv_text in round2_profiles.items():
                            arcname = f"{rule}/{member}"
                            if arcname not in existing_round2_members:
                                round2_archive.writestr(arcname, csv_text)
                                existing_round2_members.add(arcname)

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
    finally:
        if round2_archive is not None:
            round2_archive.close()

    if needs_settings:
        with open(round2_metadata_path, "w", encoding="utf-8") as f:
            json.dump({"signature": signature}, f)

if __name__ == '__main__':
    config = load_json("configs/basic.json")
    simulate_elections(config)