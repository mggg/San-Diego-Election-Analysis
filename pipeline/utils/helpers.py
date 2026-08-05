from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Dict, Optional, Set
import re
import json
import hashlib
import gzip
import zipfile
import zlib

@dataclass(frozen=True)
class DistrictConfig:
    """One district configuration: number of districts and seats won per district."""
    num_districts: int
    winners: int

def read_existing_zip_members(zip_path: Path) -> Optional[Set[str]]:
    """
    Return the set of member names already in zip_path, or None if it doesn't
    exist or can't be safely read (missing, corrupted, or truncated).

    None signals that resuming isn't possible and the archive must be rebuilt
    from scratch. testzip() decompresses every member to verify its CRC, so a
    truncated entry (e.g. a process killed mid-write) is caught here too.

    Shared by profile_generator.py and simulate_elections.py -- both append to a
    resumable zip archive of generated profiles.
    """
    if not zip_path.is_file():
        return None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                return None
            return set(archive.namelist())
    except (zipfile.BadZipFile, OSError, zlib.error, EOFError):
        return None


def load_json(path: Path) -> Dict[str, Any]:
    """
    Load and return the contents of a json file.

    Args:
        path: Path to the json file.

    Returns:
        Parsed json contents as a dict.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# Project config
#
# Settings that describe the project rather than any one simulation: where the
# geodata comes from and how it is built, which columns carry population, and
# the two knobs that fix the district-plan ensemble. They live once in
# project-settings.json at the repo root instead of being copied into every run
# config. Anchored to the repo root rather than the working directory, so it
# resolves the same from a notebook or any other subdirectory.
# --------------------------------------------------------------------------- #
PROJECT_DIR = Path(__file__).resolve().parents[2]
PROJECT_CONFIG_PATH = PROJECT_DIR / "project-settings.json"

PROJECT_KEYS = [
    "geodata_path",
    "geometry_data",
    "gerrychain_output_dir",
    "population_column",
    "population_vap_column",
    "pop_of_interest_column",
    "seed",
    "chain_length",
]


def load_project_config(path: Path = PROJECT_CONFIG_PATH) -> Dict[str, Any]:
    """
    Load the project-wide config, or an empty dict when there isn't one.

    Args:
        path: Path to the project config json.

    Returns:
        Parsed project settings; empty if the file is absent, so a run config
        that still carries its own copies keeps working.
    """
    path = Path(path)
    return load_json(path) if path.is_file() else {}


def merge_configs(project: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    """
    Layer a run config over the project config.

    The run wins on any key it sets, so a single run can override a project
    default without editing it for everyone. Nested dicts merge key by key --
    a run tweaking one "geometry_data" entry keeps the rest of the project's.

    Args:
        project: Project-wide settings.
        run: One run's settings.

    Returns:
        The merged config.
    """
    merged = dict(project)
    for key, value in run.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def load_run_config(config_path, project_path: Path = PROJECT_CONFIG_PATH) -> Dict[str, Any]:
    """
    Load one run config merged over the project config.

    The single entry point for reading a config, so every caller sees the same
    resolved settings.

    Args:
        config_path: Path to the run config json.
        project_path: Path to the project config json.

    Returns:
        The merged config dict.
    """
    return merge_configs(load_project_config(project_path), load_json(Path(config_path)))


# Voter models generated/simulated/summarized when a config does not specify its
# own "voter_models" list. Kept for backward compatibility with older configs.
DEFAULT_VOTER_MODELS = ["slate_pl", "slate_bt", "cambridge"]


def get_voter_models(config) -> List[str]:
    """
    Return the ordered list of voter models for this run.

    Read from config["voter_models"] when present (a list of model-name strings),
    otherwise fall back to DEFAULT_VOTER_MODELS. This is the single source of truth
    for which models the profile, election, and summary stages iterate over.

    Args:
        config: Parsed config dict.

    Returns:
        List of voter-model name strings.

    Raises:
        ValueError: If "voter_models" is present but not a list of strings.
    """
    models = config.get("voter_models", DEFAULT_VOTER_MODELS)
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise ValueError('config["voter_models"] must be a list of strings.')
    return list(models)


# Config keys that determine the *content* of a generated profile. If any of
# these changes, existing profiles are stale and must be regenerated. Keys
# outside this set only add new profiles rather than changing existing ones --
# num_reps (more replicates), voter_models (more models), and district_configs
# (more magnitudes) all namespace their outputs, so existing files stay valid
# and only the missing ones are filled in (see generate_profiles).
PROFILE_SIGNATURE_KEYS = [
    "geodata_path",
    "population_column",
    "population_vap_column",
    "pop_of_interest_column",
    "group_vap_columns",
    "blocs",
    "turnout",
    "slate_to_candidates",
    "cohesion_parameters",
    "alphas",
    "num_voters",
    "seed",
    "chain_length",
    "num_subsamples",
    # slate_to_candidates is no longer passed through as configured -- each
    # district's slate sizes are drawn from these two (see
    # settings_generator._build_slate_to_candidates), so they determine profile
    # content just as directly. Two runs differing only in an availability
    # exponent must not share a signature, or the second silently reuses the
    # first's profiles.
    "availability_exps",
    "candidate_geometric_p",
]


def config_signature(config, keys) -> str:
    """
    A stable short hash of the given config keys.

    Two configs that agree on all of ``keys`` (a missing key is treated as null)
    produce the same signature, so it can be stored alongside generated outputs
    to detect whether those outputs are still compatible with the current config
    (reuse them) or stale (regenerate them).

    Args:
        config: Parsed config dict.
        keys: The config keys whose values define the signature.

    Returns:
        A 16-character hex digest of the selected key/value subset.
    """
    subset = {k: config.get(k) for k in keys}
    blob = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def score_rule_budgets(voting_configs) -> Dict[str, int]:
    """
    {rule -> per-voter point budget} for every configured rule that reads score
    ballots.

    A score ballot is only valid for one budget: Cumulative fixes it at n_seats,
    Limited states it outright as `budget`, and both reject ballots that spend
    more. Two such rules with different budgets therefore need two different sets
    of ballots, which is why the budget is part of where a profile is stored.

    votekit is imported inside the function so this module stays importable
    without it (and to keep the import off the hot path for runs with no score
    rules).
    """
    import inspect
    from typing import get_args
    from votekit import ScoreProfile, elections

    budgets: Dict[str, int] = {}
    for rule, kwargs in (voting_configs or {}).items():
        election_class = getattr(elections, rule, None)
        if election_class is None:
            continue
        annotation = inspect.signature(election_class.__init__).parameters["profile"].annotation
        accepted = get_args(annotation) or (annotation,)
        if ScoreProfile not in accepted:
            continue
        # Limited names its budget; Cumulative inherits n_seats as one.
        budgets[rule] = int(kwargs.get("budget", kwargs.get("n_seats", 1)))
    return budgets


def required_score_budgets(voting_configs) -> List[int]:
    """The distinct score-ballot budgets a run needs, ascending."""
    return sorted(set(score_rule_budgets(voting_configs).values()))


def profiles_signature(config) -> str:
    """
    Signature of the config parameters that determine profile content.

    The score budgets are folded in on top of PROFILE_SIGNATURE_KEYS: they come
    from voting_configs, which is otherwise not profile-determining, but changing
    a budget changes which ballots have to exist.
    """
    subset = {k: config.get(k) for k in PROFILE_SIGNATURE_KEYS}
    subset["_score_budgets"] = required_score_budgets(config.get("voting_configs"))
    # The candidate-pool draw lives inside district_configs, which is deliberately
    # not profile-determining as a whole (adding a magnitude only adds outputs).
    # Its ceiling and mean *are*: they change the ballots for a magnitude that
    # already exists.
    subset["_candidate_pools"] = sorted(
        (
            d.get("num_districts"),
            d.get("winners"),
            d.get("candidate_pool_max"),
            d.get("candidate_pool_mean"),
        )
        for d in (config.get("district_configs") or [])
    )
    blob = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def primary_profiles_zip_path(run_name: str) -> Path:
    """
    Archive of primary-round profiles: the same districts, modes, and replicates
    as profiles.zip, under the same entry names, but sampled from the lower
    primary_turnout electorate. Kept here so the writer (profile_generator), the
    reader (simulate_elections), and the stage check (run.py) can't drift.
    """
    return Path("outputs") / run_name / "primary_profiles.zip"


def primary_profiles_metadata_path(run_name: str) -> Path:
    """Sidecar recording the signature primary_profiles.zip was generated under."""
    return Path("outputs") / run_name / "primary_profiles_metadata.json"


def primary_profiles_signature(config) -> str:
    """
    Signature for the primary-round profile archive: everything that determines
    profile content, plus the primary_turnout override that sets its electorate.

    Deliberately separate from profiles_signature -- primary_turnout changes only
    the primary archive, so folding it into the main signature would invalidate
    every standard profile for no reason.
    """
    return config_signature(config, PROFILE_SIGNATURE_KEYS + ["primary_turnout"])


# Config keys that determine the *content* of the districting Markov chain.
# run_name and every other key (num_reps, voting_configs, blocs, ...) only
# affect downstream stages, so two configs that agree on these keys produce
# byte-identical district plans and can share one chain run.
DISTRICTING_SIGNATURE_KEYS = [
    "geodata_path",
    "population_column",
    "seed",
    "chain_length",
]


def districting_signature(config) -> str:
    """Signature of the config parameters that determine district-plan content."""
    return config_signature(config, DISTRICTING_SIGNATURE_KEYS)


def find_cached_district_output(config, num_districts: int, config_dir="configs") -> Optional[Path]:
    """
    Look for an existing district chain output equivalent to what generate_districts
    would produce for this config and num_districts, so the chain_length-step
    recombination Markov chain isn't rerun for a config that only changes
    downstream parameters (num_reps, voting_configs, blocs, ...).

    Scans every run config under config_dir for one whose districting_signature
    matches (same geodata, population column, seed, and chain length) and that
    has already produced a complete output file for num_districts.

    Args:
        config: Parsed config dict of the run needing this district count.
        num_districts: District count being generated.
        config_dir: Root directory to search for other run configs.

    Returns:
        Path to a reusable district output file, or None if no match is found.
    """
    target_signature = districting_signature(config)
    chain_length = config["chain_length"]
    own_run_name = config["run_name"]

    for path in sorted(Path(config_dir).rglob("*.json")):
        try:
            other = load_run_config(path)
        except (json.JSONDecodeError, OSError):
            continue
        other_run_name = other.get("run_name")
        if not other_run_name or other_run_name == own_run_name:
            continue
        if districting_signature(other) != target_signature:
            continue

        candidate = (
            Path("outputs") / other_run_name / "districts"
            / f"{other_run_name}_{num_districts}_districts.jsonl.gz"
        )
        if not candidate.is_file():
            continue
        try:
            with gzip.open(candidate, "rt", encoding="utf-8") as g:
                if sum(1 for _ in g) != chain_length:
                    continue
        except OSError:
            continue
        return candidate
    return None


def election_results_signature(config) -> str:
    """
    Signature for election results: profile content plus the voting rules.

    Changing a voting rule (or its parameters) makes existing results stale even
    when the underlying profiles are unchanged, so voting_configs is folded in on
    top of the profile signature. primary_turnout joins them: it leaves the
    standard profiles alone but changes the ballots two-round rules narrow on,
    and so changes their winners.
    """
    return config_signature(
        config, PROFILE_SIGNATURE_KEYS + ["voting_configs", "primary_turnout"]
    )


def get_non_focal_group(config):
    """
    Determine the non focal group using the turnout parameter and focal group parameter specified in the configuration file.

    Args:
        config: Parsed config dict.

    Returns:
        Name of the non-focal group as a string.
    """
    non_focal_group = next(iter(config["turnout"].keys() - {config["focal_group"]}))
    return non_focal_group

def parse_district_configs(raw: Any) -> List[DistrictConfig]:
    """
    Parse the district_configs field from the config file into DistrictConfig objects.
    accepts two schemas:
      - newer: [{"num_districts": 5, "winners": 2}, ...]
      - older: [{<num_districts>: <winners>}, ...] e.g. [{80: 1}, {20: 4}]

    Args:
        raw: The raw district_configs value from the config (expected to be a list).

    Returns:
        List of DistrictConfig(num_districts, winners).

    Raises:
        ValueError: If raw is not a list or entries don't match either schema.
    """
    if not isinstance(raw, list):
        raise ValueError("district_configs must be a list")

    parsed: List[DistrictConfig] = []
    for item in raw:
        if isinstance(item, dict) and "num_districts" in item and "winners" in item:
            parsed.append(DistrictConfig(int(item["num_districts"]), int(item["winners"])))
        elif isinstance(item, dict) and len(item) == 1:
            (k, v), = item.items()
            parsed.append(DistrictConfig(int(k), int(v)))
        else:
            raise ValueError(
                "Each district_configs entry must be either "
                '{"num_districts": <int>, "winners": <int>} or {<int>: <int>}.'
            )
    return parsed


def parse_plan_district_rep_from_path(p: str | Path):
    """
    Parse the plan index, district id, and replicate number from a profile file path.

    Args:
        p: Path to a profile csv file, expected to contain substrings like
           "district_plan_000", "district_02", and "v0" (replicate index is 0-based).

    Returns:
        Tuple (plan, district, rep) where each is an int parsed directly from the
        path (not normalized to any index base), or None if the pattern is not found.
    """
    s = str(p)

    # plan: match "district_plan_000" OR "plan_000"
    m_plan = re.search(r"(?:district[_-]?plan[_-]?|plan[_-]?)(\d+)", s, flags=re.IGNORECASE)
    plan = int(m_plan.group(1)) if m_plan else None

    # district: collect all occurrences like "district_00" and take the last one
    districts = re.findall(r"district[_-]?(\d+)", s, flags=re.IGNORECASE)
    district = int(districts[-1]) if districts else None

    # replicate/version: files use v0, v1... so parse "v0"
    m_v = re.search(r"(?:^|[_-])v(\d+)(?:\D|$)", s, flags=re.IGNORECASE)
    rep = int(m_v.group(1)) if m_v else None

    return plan, district, rep


def is_focal_candidate(candidate: str, focal_group: str, slate_to_candidates: Dict[str, List[str]]) -> bool:
    """
    Check whether a candidate belongs to the focal group.
    a candidate matches if they appear in the explicit slate list, or if the focal
    group is a single character and the candidate id starts with that character.

    Args:
        candidate: Candidate id string.
        focal_group: Name of the focal group (e.g., "A").
        slate_to_candidates: Mapping from group name to list of candidate ids.

    Returns:
        True if the candidate is focal, false otherwise.
    """
    focal_list = set(map(str, slate_to_candidates.get(focal_group, [])))
    c = str(candidate)

    if c in focal_list:
        return True
    if len(focal_group) == 1 and c.startswith(focal_group):
        return True
    return False


def count_focal_winners(
    winners: Iterable[str],
    focal_group: str,
    slate_to_candidates: Dict[str, List[str]],
) -> int:
    """
    Count the number of election winners belonging to the focal group.

    Args:
        winners: Iterable of winning candidate id strings.
        focal_group: Name of the focal group.
        slate_to_candidates: Mapping from group name to list of candidate ids.

    Returns:
        Integer count of focal-group winners.
    """
    return sum(1 for w in winners if is_focal_candidate(str(w), focal_group, slate_to_candidates))


def find_settings_file(
    settings_dir: Path,
    run_name: str,
    *,
    plan: Optional[int],
    district: Optional[int],
) -> Optional[Path]:
    """
    Locate the settings json file for a given (plan, district) pair.

    Parses each candidate filename's plan/district numbers with the same regex
    parse_plan_district_rep_from_path uses on profile filenames, then requires
    an exact integer match. A glob-star pattern like "*plan_200*" would also
    match "plan_2000" or "plan_2900" (200 is a numeric prefix of both),
    silently resolving to the wrong district's settings -- exact integer
    comparison after parsing avoids that.

    Args:
        settings_dir: Directory containing settings json files.
        run_name: Unused; reserved for future use in filename matching.
        plan: Plan index (zero-based sample index from the chain).
        district: District id within the plan.

    Returns:
        Path to the matching settings file, or None if not found (or if
        neither plan nor district is given and more than one file exists).
    """
    if not settings_dir.exists():
        return None

    all_files = sorted(settings_dir.glob("*.json"))
    if plan is None and district is None:
        return all_files[0] if len(all_files) == 1 else None

    for f in all_files:
        f_plan, f_district, _ = parse_plan_district_rep_from_path(f)
        if plan is not None and f_plan != plan:
            continue
        if district is not None and f_district != district:
            continue
        return f

    return None
