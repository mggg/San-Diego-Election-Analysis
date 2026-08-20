"""
Truncate voter profiles to match Cambridge, MA historical ballot-length behavior.

Cambridge's real 2009-2017 RCV ballots recorded, for two behavioral groups --
voters whose first choice was the historical majority slate vs. the historical
minority slate -- how many candidates each ballot went on to rank. VoteKit's own
Cambridge generator (`cambridge_profiles_by_bloc_generator`) only works with
exactly two slates whose names match two blocs, so it can't run directly against
a config whose slates are not literally named majority/minority (e.g. San Diego's WAIO/POC).

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

# Cambridge's historical data splits ballots by whether the voter's first
# choice was the White-majority slate or not. A run states its own mapping via
# "cambridge_truncation.majority_slates"/"minority_slates" in its config; WAIO --
# consistently San Diego's largest single slate -- is only a fallback for a run
# that leaves that mapping unset.
DEFAULT_MAJORITY_SLATE = "WAIO"


def _default_majority_minority_slates(slate_to_candidates: dict) -> tuple[list, list]:
    """
    Split this run's slates into Cambridge's majority/minority groups by the
    DEFAULT_MAJORITY_SLATE convention.

    Fallback for a run whose config doesn't set
    "cambridge_truncation.majority_slates"/"minority_slates" (see
    apply_cambridge_truncation). Every slate lands in exactly one group
    (DEFAULT_MAJORITY_SLATE in "majority", everything else in "minority"), so
    truncate_profile never finds an unmapped slate. A run with no
    DEFAULT_MAJORITY_SLATE candidates at all -- unusual, but not invalid --
    pools everything into "minority".

    Args:
        slate_to_candidates: This run's slate names mapped to candidate lists
            (config["slate_to_candidates"], or a district's own).

    Returns:
        (majority_slates, minority_slates) lists of slate names.
    """
    slates = list(slate_to_candidates)
    majority = [DEFAULT_MAJORITY_SLATE] if DEFAULT_MAJORITY_SLATE in slates else []
    minority = [s for s in slates if s != DEFAULT_MAJORITY_SLATE]
    return majority, minority


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


def _clip_and_renormalize(length_pmf: dict, min_length: int, max_length: int) -> dict:
    """
    Restrict a length distribution to [min_length, max_length].

    Lengths outside the bounds are dropped outright (not folded into the
    nearest bound), and the remaining probabilities are rescaled to sum back
    to 1, preserving their relative proportions.

    Args:
        length_pmf: {length: probability}, e.g. one group's output of
            build_length_distributions.
        min_length: Shortest length kept, inclusive.
        max_length: Longest length kept, inclusive.

    Returns:
        {length: probability} restricted to [min_length, max_length].

    Raises:
        ValueError: If no length in length_pmf falls within the bounds --
            there is nothing left to renormalize.
    """
    clipped = {
        length: prob for length, prob in length_pmf.items() if min_length <= length <= max_length
    }
    total = sum(clipped.values())
    if total <= 0:
        raise ValueError(
            f"No ballot lengths fall within [{min_length}, {max_length}]; "
            f"the historical distribution only covers {sorted(length_pmf)}."
        )
    return {length: prob / total for length, prob in clipped.items()}


def build_bounded_length_distributions(
    config: "BlocSlateConfig",
    majority_slates: Sequence[str],
    minority_slates: Sequence[str],
    min_length: int,
    max_length: int,
) -> dict:
    """
    Cambridge historical ballot-length distributions, restricted to
    [min_length, max_length] and renormalized.

    Starts from the same historical majority/minority length marginals as
    build_length_distributions, then drops every length outside the bounds
    and rescales what's left (see _clip_and_renormalize) so a bounded run's
    ballots are still drawn from a real distribution, just confined to a
    narrower length range than Cambridge's raw history.

    Args:
        config: This district's BlocSlateConfig (already built from settings).
        majority_slates: Slate names pooled into the historical "majority" (W) group.
        minority_slates: Slate names pooled into the historical "minority" (C) group.
        min_length: Shortest ballot length allowed, inclusive.
        max_length: Longest ballot length allowed, inclusive.

    Returns:
        {"majority": {length: probability}, "minority": {length: probability}},
        each restricted to [min_length, max_length].
    """
    length_distributions = build_length_distributions(config, majority_slates, minority_slates)
    return {
        group: _clip_and_renormalize(dist, min_length, max_length)
        for group, dist in length_distributions.items()
    }


def build_length_distributions_for_method(
    config: "BlocSlateConfig",
    majority_slates: Sequence[str],
    minority_slates: Sequence[str],
    method: str = "historical",
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
) -> dict:
    """
    Dispatch to the length-distribution builder for a run's configured
    "cambridge_truncation.method", so both callers of truncate_profile
    (apply_cambridge_truncation and truncate_profiles.truncate_archive) pick
    the same distribution for the same config.

    Args:
        config: This district's BlocSlateConfig.
        majority_slates: Slate names pooled into the "majority" group.
        minority_slates: Slate names pooled into the "minority" group.
        method: "historical" (Cambridge's raw historical length distribution,
            the default) or "bounded" (that same distribution restricted to
            [min_length, max_length] -- see build_bounded_length_distributions).
        min_length: Required when method is "bounded".
        max_length: Required when method is "bounded".

    Returns:
        {"majority": {length: probability}, "minority": {length: probability}}.

    Raises:
        ValueError: If method is "bounded" and either bound is missing, or if
            method is neither "historical" nor "bounded".
    """
    if method == "historical":
        return build_length_distributions(config, majority_slates, minority_slates)
    if method == "bounded":
        if min_length is None or max_length is None:
            raise ValueError(
                "cambridge_truncation method 'bounded' requires both min_length and max_length."
            )
        return build_bounded_length_distributions(
            config, majority_slates, minority_slates, min_length, max_length
        )
    raise ValueError(f"Unknown cambridge_truncation method '{method}'.")


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
    majority_slates: Optional[Sequence[str]] = None,
    minority_slates: Optional[Sequence[str]] = None,
    method: str = "historical",
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    rng: Optional[np.random.Generator] = None,
) -> RankProfile:
    """
    Truncate profile's ballots to Cambridge-derived lengths.

    Single entry point combining build_length_distributions_for_method +
    truncate_profile, for callers that just want a truncated profile.

    Args:
        profile: A RankProfile already produced by slate_pl/slate_bt.
        config: This district's BlocSlateConfig.
        majority_slates: Slate names pooled into the historical "majority" (W)
            group -- normally a run's configured "cambridge_truncation.majority_slates".
            When omitted (along with minority_slates), falls back to the
            DEFAULT_MAJORITY_SLATE convention (see _default_majority_minority_slates).
        minority_slates: Slate names pooled into the historical "minority" (C)
            group -- normally "cambridge_truncation.minority_slates".
        method: The run's configured "cambridge_truncation.method" -- "historical"
            (default) or "bounded" (see build_length_distributions_for_method).
        min_length: Required when method is "bounded" -- the run's configured
            "cambridge_truncation.min_length".
        max_length: Required when method is "bounded" -- the run's configured
            "cambridge_truncation.max_length".
        rng: Optional numpy Generator, for reproducible sampling in tests.

    Returns:
        A new, truncated RankProfile.
    """
    if majority_slates is None or minority_slates is None:
        majority_slates, minority_slates = _default_majority_minority_slates(
            config.slate_to_candidates.to_dict()
        )
    length_distributions = build_length_distributions_for_method(
        config, majority_slates, minority_slates, method, min_length, max_length
    )
    return truncate_profile(
        profile, config, majority_slates, minority_slates, length_distributions, rng=rng
    )
