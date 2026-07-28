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
from pathlib import Path
from joblib import Parallel, delayed
from votekit import RankProfile, ScoreProfile, elections
from typing import List, Iterable, Dict, Tuple, get_args

# Optional progress bar for joblib.
try:
    from joblib_progress import joblib_progress
except Exception:
    joblib_progress = None

from pipeline.utils.helpers import parse_district_configs, get_voter_models, election_results_signature


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


def _import_voting_rules_from_votekit(rules: Iterable[str]) -> Dict[str, type]:
    """
    Resolve configured rule names to VoteKit election classes.

    Args:
        rules: Election class names (the keys of the config's voting_configs).

    Returns:
        Dict mapping each rule name to its votekit.elections class.

    Raises:
        ValueError: If a name does not correspond to a VoteKit election class.
    """
    classes: Dict[str, type] = {}
    for rule in rules:
        cls = getattr(elections, rule, None)
        if cls is None:
            raise ValueError(
                f"Unknown voting rule '{rule}' in voting_configs. "
                f"Expected a VoteKit election class name (e.g. 'FastSTV', 'Plurality', 'IRV')."
            )
        classes[rule] = cls
    return classes


def _build_election_plan(voting_configs: dict) -> List[tuple]:
    """
    Resolve each configured voting rule to its VoteKit election class and the
    profile class it requires, once.

    This work only depends on voting_configs (not on any profile), so doing it a
    single time up front avoids repeating class lookups and signature
    introspection for every profile file.

    Args:
        voting_configs: Mapping of rule name -> kwargs from the config file.

    Returns:
        List of (rule, election_class, profile_class) tuples in config order.
        profile_class is RankProfile when the rule accepts one, otherwise
        ScoreProfile.
    """
    plan: List[tuple] = []
    for rule, election_class in _import_voting_rules_from_votekit(voting_configs.keys()).items():
        profile_types = _required_profile(election_class)
        profile_class = RankProfile if RankProfile in profile_types else ScoreProfile
        plan.append((rule, election_class, profile_class))
    return plan


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
    election_plan: List[tuple],
    voting_configs: dict,
) -> Dict[str, List[str]]:
    """
    Load a voter profile (its csv bytes, read from profiles.zip by the caller) and
    run each configured election to determine winners.

    Args:
        csv_bytes: The profile csv content, as read from the zip member.
        election_plan: Precomputed (rule, election_class, profile_class) tuples
            from _build_election_plan; avoids per-file class lookup/introspection.
        voting_configs: Mapping of rule name -> kwargs from the config file. The
            kwargs are spread straight into the election class, so any VoteKit
            parameter (including the seat count) is set there.

    Returns:
        Dict mapping each configured rule name to its list of winner ids,
        e.g. {"FastSTV": ["A2", "B1"], "Plurality": ["A2", "A3"]}.
    """
    # Parse each distinct profile type from the csv at most once and reuse it
    # across rules that need it (e.g. two rank-based rules), instead of
    # re-reading the same file per rule.
    profile_cache: dict = {}

    results: Dict[str, List[str]] = {}
    for rule, election_class, profile_class in election_plan:
        profile = profile_cache.get(profile_class)
        if profile is None:
            profile = _load_profile_from_bytes(csv_bytes, profile_class)
            profile_cache[profile_class] = profile

        elected = election_class(profile, **voting_configs[rule]).get_elected()
        results[rule] = _candidate_list_from_elected(elected)

    return results


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

    Returns:
        None.
    """
    run_name = str(config["run_name"])
    district_configs = parse_district_configs(config["district_configs"])

    voting_configs = config["voting_configs"]
    # Resolve rules to classes once up front (also surfaces bad rule names before
    # any elections run).
    election_plan = _build_election_plan(voting_configs)

    # Results already written under this signature (profiles + voting rules) can
    # be reused; a (mode, district) whose file is missing, stale, or short is
    # (re-)simulated -- so adding a voter model / voting rule / district config,
    # or growing the profile set, only fills in what's missing.
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
            with zipfile.ZipFile(zip_path) as archive:
                tasks = (
                    delayed(_process_profile)(archive.read(member), election_plan, voting_configs)
                    for member in all_profile_files
                )
                if ctx is not None:
                    with ctx:
                        results_list = Parallel(n_jobs=n_jobs)(tasks)
                else:
                    print(f"[simulate_elections] {desc} (no joblib_progress installed)")
                    results_list = Parallel(n_jobs=n_jobs)(tasks)

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
                        "election_results": results_list,
                    },
                    f,
                    indent=2,
                )

            print(f"[simulate_elections] Wrote: {out_path}")
