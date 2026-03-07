from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Dict, Optional
from votekit import RankProfile
from votekit.elections import FastSTV as STV, Plurality
import re
import json

@dataclass(frozen=True)
class DistrictConfig:
    """One district configuration: number of districts and winners per district."""
    num_districts: int
    winners: int

def load_json(path: Path) -> Dict[str, Any]:
    """
    Load and return the contents of a json file.
    args: path: path to the json file.
    returns: parsed json contents as a dict.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_district_configs(raw: Any) -> List[DistrictConfig]:
    """
    Parse the district_configs field from the config file into DistrictConfig objects.
    accepts two schemas:
      - newer: [{"num_districts": 5, "winners": 2}, ...]
      - older: [{80: 1}, {20: 4}, ...]

    args:
        raw: the raw district_configs value from the config (expected to be a list).

    returns:
        list of DistrictConfig(num_districts, winners).

    raises:
        ValueError: if raw is not a list or entries don't match either schema.
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


def candidate_list_from_elected(elected: Iterable[set]) -> List[str]:
    """
    Flatten votekit election output (iterable of singleton sets) into a list of strings.

    args:
        elected: iterable of singleton sets, as returned by votekit election methods.

    returns:
        list of candidate id strings in election order.
    """
    winners: List[str] = []
    for s in elected:
        if s:
            winners.append(str(next(iter(s))))
    return winners


def process_profile(profile_file: str | Path, n_seats: int) -> List[str]:
    """
    Load a voter profile csv and run an election to determine winners.
    uses stv for multi-seat races and plurality for single-seat races.

    args:
        profile_file: path to the voter profile csv.
        n_seats: number of seats to fill in this election.

    returns:
        list of winning candidate id strings.
    """
    profile_path = Path(profile_file)
    profile: RankProfile = RankProfile.from_csv(profile_path)

    if n_seats > 1:
        elected = STV(profile, m=n_seats, simultaneous=False, tiebreak='random').get_elected()
    else:
        elected = Plurality(profile, m=1).get_elected()

    return candidate_list_from_elected(elected)

def parse_plan_district_rep_from_path(p: str | Path):
    """
    Parse the plan index, district id, and replicate number from a profile file path.

    args:
        p: path to a profile csv file, expected to contain substrings like
           "district_plan_000", "district_02", and "v1".

    returns:
        tuple (plan, district, rep) where each is an int or None if not found.
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

    args:
        candidate: candidate id string.
        focal_group: name of the focal group (e.g., "A").
        slate_to_candidates: mapping from group name to list of candidate ids.

    returns:
        true if the candidate is focal, false otherwise.
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

    args:
        winners: iterable of winning candidate id strings.
        focal_group: name of the focal group.
        slate_to_candidates: mapping from group name to list of candidate ids.

    returns:
        integer count of focal-group winners.
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

    args:
        settings_dir: directory containing settings json files.
        run_name: prefix used at the start of the settings filename.
        plan: plan index (zero-based sample index from the chain).
        district: district id within the plan.

    returns:
        path to the matching settings file, or none if not found.
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
