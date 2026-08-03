"""
Generic two-round election with a freshly-resampled round-2 profile.

VoteKit's built-in two-round rules (Alaska, TopTwo) both narrow the field to a
fixed number of finalists via a Plurality round, then decide winners among just
those finalists -- but VoteKit builds the round-2 ballots by mechanically
stripping non-finalists out of the round-1 ballots and re-condensing, never by
sampling fresh ballots. This module instead samples a brand-new profile
restricted to the finalists (same voter model/mode as round 1, so ballots are
shorter simply because fewer candidates remain), then runs any VoteKit ranking
election on it for round 2.

Config-driven, not rule-specific: a voting_configs entry becomes a two-round
rule by naming "round2_class" (any name in votekit.elections) instead of being
a real VoteKit election class itself. Alaska-equivalent and TopTwo-equivalent
behavior are both just config, e.g.:

    "AlaskaTwoProfile": {
        "m_1": 4, "tiebreak": "random",
        "round2_class": "STV",
        "round2_kwargs": {"n_seats": 1, "tiebreak": "random"}
    },
    "TopTwoTwoProfile": {
        "m_1": 2, "tiebreak": "random",
        "round2_class": "Plurality",
        "round2_kwargs": {"n_seats": 1, "tiebreak": "random"}
    }
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from votekit import RankProfile, elections
from votekit.ballot_generator import BlocSlateConfig
from votekit.elections import Plurality

from pipeline.profile_generator import generator_name_to_function
from pipeline.settings_generator import filter_alphas_to_slates, filter_cohesion_to_slates


def _round2_slate_to_candidates(slate_to_candidates: Dict[str, List[str]], finalists: set) -> Dict[str, List[str]]:
    """
    Restrict each slate's candidate list to the surviving finalists.

    Per-candidate filter, not per-slate all-or-nothing: a slate can survive with
    fewer candidates than round 1 if only some of its candidates made the
    finalist cut. A slate left with none is dropped entirely.
    """
    filtered = {s: [c for c in cs if c in finalists] for s, cs in slate_to_candidates.items()}
    return {s: cs for s, cs in filtered.items() if cs}


def build_round2_profile(settings: dict, mode: str, finalists: List[str]) -> Tuple[RankProfile, str]:
    """
    Sample a fresh profile restricted to the finalists, using the same voter
    model (mode) round 1 used.

    Args:
        settings: The district's settings dict (as written by
            settings_generator.generate_settings), giving num_voters,
            slate_to_candidates, bloc_proportions, cohesion_parameters, alphas.
        mode: Voter model name ("slate_pl", "slate_bt", or "cambridge") -- the
            same one round 1's profile was generated with.
        finalists: Candidate ids that survived round 1.

    Returns:
        (profile, csv_text): the freshly sampled RankProfile and its CSV form,
        so the caller can both run round 2 on it and persist it.
    """
    finalist_set = {str(c) for c in finalists}
    active_slate_to_candidates = _round2_slate_to_candidates(settings["slate_to_candidates"], finalist_set)
    active_slates = list(active_slate_to_candidates)

    cohesion_parameters = filter_cohesion_to_slates(settings["cohesion_parameters"], active_slates)
    alphas = filter_alphas_to_slates(settings["alphas"], active_slates)

    config = BlocSlateConfig(
        n_voters=settings["num_voters"],
        slate_to_candidates=active_slate_to_candidates,
        bloc_proportions=settings["bloc_proportions"],
        cohesion_mapping=cohesion_parameters,
    )
    config.set_dirichlet_alphas(alphas)

    profile = generator_name_to_function[mode](config)
    return profile, profile.to_csv()


def run_two_round_fresh_profile(
    profile: RankProfile, settings: dict, mode: str, kwargs: dict
) -> Tuple[List[str], str]:
    """
    Run a generic two-round election: Plurality round 1 narrows to kwargs["m_1"]
    finalists, then a freshly-resampled profile (restricted to those finalists,
    same mode as round 1) feeds kwargs["round2_class"] run with
    kwargs["round2_kwargs"].

    Args:
        profile: Round-1 RankProfile (the one already loaded from profiles.zip).
        settings: The district's settings dict; needed to rebuild a
            BlocSlateConfig for the round-2 sample.
        mode: Voter model name round 1 was generated with.
        kwargs: This rule's voting_configs entry -- "m_1" (required),
            "tiebreak" (round 1's, optional), "round2_class" (required, a name
            in votekit.elections), "round2_kwargs" (optional, passed straight
            to round2_class).

    Returns:
        (winners, round2_csv_text): the round-2 election's winner ids, and the
        round-2 profile's CSV text for the caller to persist.
    """
    m_1 = kwargs["m_1"]
    tiebreak = kwargs.get("tiebreak")
    round2_class_name = kwargs["round2_class"]
    round2_class = getattr(elections, round2_class_name, None)
    if round2_class is None:
        raise ValueError(f"Unknown round2_class '{round2_class_name}' for a two-round rule.")
    round2_kwargs = kwargs.get("round2_kwargs", {})

    plurality = Plurality(profile, m_1, tiebreak)
    finalists = [c for s in plurality.get_elected() for c in s]

    round2_profile, round2_csv = build_round2_profile(settings, mode, finalists)

    winners = [c for s in round2_class(round2_profile, **round2_kwargs).get_elected() for c in s]
    return winners, round2_csv
