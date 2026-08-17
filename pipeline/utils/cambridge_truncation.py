"""
Truncate voter profiles to match Cambridge, MA historical ballot-length behavior.

Cambridge's real 2009-2017 RCV ballots recorded, for two behavioral groups --
voters whose first choice was the historical majority slate vs. the historical
minority slate -- how many candidates each ballot went on to rank. VoteKit's own
Cambridge generator (`cambridge_profiles_by_bloc_generator`) only works with
exactly two slates whose names match two blocs, so it can't run directly against
a config with more than two candidate slates (e.g. San Diego's WAIO/HIS/AAPI/BLK).

This module reuses votekit's own shape-reduction logic (`_reduce_ballot_pmfs`)
against a stand-in config that pools an arbitrary number of real slates into two
groups, then truncates already-generated ballots (from slate_pl/slate_bt) to a
length sampled from the matching pooled distribution -- so ballot length/dropoff
behavior matches Cambridge history regardless of how many slates are on the
actual ballot.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional, Sequence

import numpy as np
import votekit
from votekit import RankBallot, RankProfile
from votekit.ballot_generator.bloc_slate_generator.cambridge import _reduce_ballot_pmfs

if TYPE_CHECKING:
    from votekit.ballot_generator import BlocSlateConfig

_DATA_DIR = (
    Path(votekit.__file__).resolve().parent / "ballot_generator" / "bloc_slate_generator" / "data"
)
_MAJORITY_BALLOT_PATH = (
    _DATA_DIR / "Cambridge_09to17_ballot_types_start_with_W_ballots_distribution.json"
)
_MINORITY_BALLOT_PATH = (
    _DATA_DIR / "Cambridge_09to17_ballot_types_start_with_C_ballots_distribution.json"
)


def _pooled_candidates(slate_to_candidates: dict, slates: Sequence[str]) -> list:
    return [c for slate in slates for c in slate_to_candidates.get(slate, [])]


def _length_marginal(shape_pmf: dict) -> dict:
    lengths: dict = {}
    for shape, freq in shape_pmf.items():
        lengths[len(shape)] = lengths.get(len(shape), 0.0) + freq
    return lengths


def build_length_distributions(
    config: "BlocSlateConfig",
    majority_slates: Sequence[str],
    minority_slates: Sequence[str],
) -> dict:
    """
    Cambridge historical ballot-length distributions for this district, reduced
    to its actual candidate counts and pooled across majority_slates / minority_slates.

    Calls votekit's own Cambridge shape-reduction (`_reduce_ballot_pmfs`) against a
    stand-in config whose two "slates" are the pooled majority/minority candidate
    lists, exactly as votekit's Cambridge model would reduce a literal two-slate
    config -- then collapses the reduced shape pmf to a length-only marginal, since
    truncation only needs how many candidates get ranked, not which slate is where.

    Args:
        config: This district's BlocSlateConfig (already built from settings).
        majority_slates: Slate names pooled into the historical "majority" (W) group.
        minority_slates: Slate names pooled into the historical "minority" (C) group.

    Returns:
        {"majority": {length: probability}, "minority": {length: probability}}.
        A pool with zero candidates present in this district collapses to {0: 1.0}.
    """
    slate_to_candidates = config.slate_to_candidates.to_dict()
    pooled_config = SimpleNamespace(
        slate_to_candidates={
            "majority": _pooled_candidates(slate_to_candidates, majority_slates),
            "minority": _pooled_candidates(slate_to_candidates, minority_slates),
        }
    )
    reduced_majority_pmf, reduced_minority_pmf = _reduce_ballot_pmfs(
        _MAJORITY_BALLOT_PATH, _MINORITY_BALLOT_PATH, pooled_config, "majority", "minority"
    )
    return {
        "majority": _length_marginal(reduced_majority_pmf),
        "minority": _length_marginal(reduced_minority_pmf),
    }


def _slate_for_candidate_lookup(slate_to_candidates: dict) -> dict:
    return {c: slate for slate, cands in slate_to_candidates.items() for c in cands}


def _bloc_type_for_slate(
    slate: str, majority_slates: Sequence[str], minority_slates: Sequence[str]
) -> str:
    if slate in majority_slates:
        return "majority"
    if slate in minority_slates:
        return "minority"
    raise ValueError(
        f"Slate '{slate}' is not in majority_slates or minority_slates -- every candidate "
        "slate must be pooled into exactly one group to truncate its ballots."
    )


def truncate_profile(
    profile: RankProfile,
    config: "BlocSlateConfig",
    majority_slates: Sequence[str],
    minority_slates: Sequence[str],
    length_distributions: dict,
    rng: Optional[np.random.Generator] = None,
) -> RankProfile:
    """
    Truncate every ballot in profile to a length drawn from the Cambridge-derived
    distribution matching its first choice's bloc (majority or minority).

    Ballots are classified by first choice, not by voter bloc -- a minority-bloc
    voter whose cohesion draw put a majority candidate first draws from the
    majority length distribution, matching how the historical data itself is
    split (by which slate a ballot starts with, not who cast it).

    A grouped ballot (weight > 1, i.e. many voters cast the identical ranking) is
    expanded so each voter draws their own truncation length independently, rather
    than truncating the shared ranking once for the whole group.

    Args:
        profile: A RankProfile already produced by slate_pl/slate_bt for this
            district (untruncated).
        config: This district's BlocSlateConfig, for its slate_to_candidates.
        majority_slates: Slate names pooled into the "majority" group.
        minority_slates: Slate names pooled into the "minority" group.
        length_distributions: Output of build_length_distributions.
        rng: Optional numpy Generator, for reproducible sampling in tests.

    Returns:
        A new, re-grouped RankProfile with the same candidates and truncated ballots.
    """
    rng = rng if rng is not None else np.random.default_rng()
    slate_for_candidate = _slate_for_candidate_lookup(config.slate_to_candidates.to_dict())

    truncated_ballots = []
    for ballot in profile.ballots:
        first_choice = next(iter(ballot.ranking[0]))
        bloc_type = _bloc_type_for_slate(
            slate_for_candidate[first_choice], majority_slates, minority_slates
        )
        dist = length_distributions[bloc_type]

        n_voters = round(ballot.weight)
        sampled_lengths = rng.choice(list(dist.keys()), size=n_voters, p=list(dist.values()))
        for length in sampled_lengths:
            truncated_ballots.append(
                RankBallot(ranking=ballot.ranking[: min(length, len(ballot.ranking))], weight=1)
            )

    return RankProfile(ballots=tuple(truncated_ballots), candidates=profile.candidates).group_ballots()


def apply_cambridge_truncation(
    profile: RankProfile,
    config: "BlocSlateConfig",
    truncation_cfg: dict,
    rng: Optional[np.random.Generator] = None,
) -> RankProfile:
    """
    Truncate profile's ballots to Cambridge-derived lengths, per truncation_cfg.

    Single entry point combining build_length_distributions + truncate_profile,
    for callers that just want a truncated profile from a run's
    "cambridge_truncation" config block.

    Args:
        profile: A RankProfile already produced by slate_pl/slate_bt.
        config: This district's BlocSlateConfig.
        truncation_cfg: Dict with "majority_slates" and "minority_slates" keys
            (lists of slate names from config.slate_to_candidates).
        rng: Optional numpy Generator, for reproducible sampling in tests.

    Returns:
        A new, truncated RankProfile.
    """
    majority_slates = truncation_cfg["majority_slates"]
    minority_slates = truncation_cfg["minority_slates"]
    length_distributions = build_length_distributions(config, majority_slates, minority_slates)
    return truncate_profile(
        profile, config, majority_slates, minority_slates, length_distributions, rng=rng
    )
