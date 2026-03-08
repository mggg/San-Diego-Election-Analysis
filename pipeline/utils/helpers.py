from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Dict, Optional
import re
import json

@dataclass(frozen=True)
class DistrictConfig:
    """One district configuration: number of districts and seats won per district."""
    num_districts: int
    winners: int

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
    tries an exact filename match first, then falls back to glob patterns,
    then returns the only file in the directory if exactly one exists.

    Args:
        settings_dir: Directory containing settings json files.
        run_name: Unused; reserved for future use in filename matching.
        plan: Plan index (zero-based sample index from the chain).
        district: District id within the plan.

    Returns:
        Path to the matching settings file, or None if not found.
    """
    if not settings_dir.exists():
        return None

    # 1) Exact match for the known generator format
    if plan is not None and district is not None:
        exact = settings_dir / f"sample_vk_sample_settings_district_plan_{plan:03d}_district_{district:02d}.json"
        if exact.exists():
            return exact

    # 2) Best-effort matching (tolerant of minor naming variations)
    patterns: List[str] = []
    if plan is not None and district is not None:
        patterns.extend([
            f"*district_plan_{plan:03d}*district_{district:02d}.json",
            f"*plan_{plan:03d}*district_{district:02d}.json",
            f"*plan*{plan}*district*{district:02d}*.json",
            f"*plan*{plan}*district*{district}*.json",
        ])
    elif plan is not None:
        patterns.extend([
            f"*district_plan_{plan:03d}*.json",
            f"*plan_{plan:03d}*.json",
            f"*plan*{plan}*.json",
        ])
    elif district is not None:
        patterns.extend([
            f"*district_{district:02d}.json",
            f"*district*{district:02d}*.json",
        ])

    for pat in patterns:
        hits = sorted(settings_dir.glob(pat))
        if hits:
            return hits[0]

    # 3) If there is exactly one file, return it (useful for quick debugging)
    all_files = sorted(settings_dir.glob("*.json"))
    if len(all_files) == 1:
        return all_files[0]
    return None
