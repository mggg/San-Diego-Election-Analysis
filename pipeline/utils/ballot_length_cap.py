"""
Cap ballot length to a fraction of a district's candidate pool.

Where pipeline.utils.cambridge_truncation models a voter behavior (ballots
get shorter because voters stop ranking, at a length drawn from Cambridge's
historical distribution), this module models a ballot-design rule: a hard
cap on how many candidates a voter may rank at all, applied identically to
every ballot in a district regardless of who a voter prefers.

The cap scales with the district's own candidate count -- a 15-candidate
race and a 6-candidate race under the same "rank about a third of the
field" rule should not get the same absolute cap -- but is clamped to a
configured [min_length, max_length] so a very small or very large field
doesn't produce a nonsensically tight or loose cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from votekit import RankBallot, RankProfile

if TYPE_CHECKING:
    from votekit.ballot_generator import BlocSlateConfig


def target_length(
    config: "BlocSlateConfig", ratio: float, min_length: int, max_length: int
) -> int:
    """
    The ballot-length cap for one district: floor(ratio * candidate count),
    clamped to [min_length, max_length].

    Args:
        config: This district's BlocSlateConfig, for its candidate count.
        ratio: Fraction of the candidate pool a voter may rank, e.g. 1/3.
            Floored rather than rounded, so the cap never exceeds the
            intended fraction of the field.
        min_length: Floor on the cap, so a small field isn't cut down to
            almost nothing.
        max_length: Ceiling on the cap, so a large field isn't left
            effectively unrestricted. A fixed cap regardless of field size
            is the degenerate case min_length == max_length.

    Returns:
        The number of ranked positions a ballot in this district may keep.
    """
    n_candidates = len(config.candidates)
    raw = int(ratio * n_candidates)  # int() truncates toward zero == floor, both operands >= 0
    return min(max(raw, min_length), max_length)


def truncate_profile(profile: RankProfile, length: int) -> RankProfile:
    """
    Cut every ballot in profile to at most `length` ranked candidates.

    Unlike Cambridge truncation, this is a deterministic structural rule --
    the same cap for every ballot in the district regardless of first choice
    -- so it needs no random draw and no per-voter expansion: each grouped
    ballot is truncated once, keeping its existing weight, and group_ballots
    merges any that become identical once cut to the same length.

    Args:
        profile: A RankProfile already produced by slate_pl/slate_bt for this
            district.
        length: The cap to apply, from target_length. A ballot shorter than
            this is left unchanged -- there is no ranking data to extend it
            with.

    Returns:
        A new, re-grouped RankProfile with the same candidates and capped
        ballots.
    """
    capped_ballots = tuple(
        RankBallot(ranking=ballot.ranking[:length], weight=ballot.weight)
        for ballot in profile.ballots
    )
    return RankProfile(ballots=capped_ballots, candidates=profile.candidates).group_ballots()


def apply_ballot_length_cap(
    profile: RankProfile,
    config: "BlocSlateConfig",
    ratio: float,
    min_length: int,
    max_length: int,
) -> RankProfile:
    """
    Cap profile's ballots per a run's "ballot_length_cap" config, in one call.

    Single entry point combining target_length + truncate_profile, mirroring
    cambridge_truncation.apply_cambridge_truncation.

    Args:
        profile: A RankProfile already produced by slate_pl/slate_bt.
        config: This district's BlocSlateConfig.
        ratio: Fraction of the candidate pool a voter may rank.
        min_length: Floor on the cap.
        max_length: Ceiling on the cap.

    Returns:
        A new, capped RankProfile.
    """
    length = target_length(config, ratio, min_length, max_length)
    return truncate_profile(profile, length)
