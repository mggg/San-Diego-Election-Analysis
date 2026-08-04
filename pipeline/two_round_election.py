"""
Generic primary/general election with a freshly-resampled general-election profile.

VoteKit's built-in two-round rules (Alaska, TopTwo) both narrow the field to a
fixed number of finalists via a Plurality primary, then decide winners among just
those finalists -- but VoteKit builds the general-election ballots by mechanically
stripping non-finalists out of the primary ballots and re-condensing, never by
sampling fresh ballots. This module instead samples a brand-new profile
restricted to the finalists (same voter model/mode as the primary, so ballots are
shorter simply because fewer candidates remain), then runs any VoteKit ranking
election on it for the general.

The two rounds are named for what they are throughout this pipeline: the
**primary** narrows, the **general** decides. Its outputs follow suit --
primary_results/ holds each rule's finalists, general_profiles.zip holds the
resampled ballots the general was decided on.

Config-driven, not rule-specific: a voting_configs entry becomes a two-round
rule by naming "general_class" (any name in votekit.elections) instead of being
a real VoteKit election class itself. Alaska-equivalent and TopTwo-equivalent
behavior are both just config, e.g.:

    "AlaskaTwoProfile": {
        "m_1": 4, "tiebreak": "random",
        "general_class": "STV",
        "general_kwargs": {"n_seats": 1, "tiebreak": "random"}
    },
    "TopTwoTwoProfile": {
        "m_1": 2, "tiebreak": "random",
        "general_class": "Plurality",
        "general_kwargs": {"n_seats": 1, "tiebreak": "random"}
    }

"round2_class" / "round2_kwargs" are still accepted as the former names of
"general_class" / "general_kwargs", so existing configs keep running unchanged.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from votekit import RankProfile, elections
from votekit.ballot_generator import BlocSlateConfig
from votekit.elections import Plurality

from pipeline.profile_generator import generator_name_to_function
from pipeline.settings_generator import filter_alphas_to_slates, filter_cohesion_to_slates


# The general-election stage was originally configured as "round2_*". Both names
# resolve to the same behavior; the newer one wins when a config sets both.
GENERAL_CLASS_KEYS = ("general_class", "round2_class")
GENERAL_KWARGS_KEYS = ("general_kwargs", "round2_kwargs")


def general_class_name(kwargs: dict) -> Optional[str]:
    """
    The VoteKit election class the general is run with, or None when this rule is
    not a two-round rule at all. Accepts either spelling of the config key.
    """
    for key in GENERAL_CLASS_KEYS:
        if key in kwargs:
            return kwargs[key]
    return None


def general_kwargs(kwargs: dict) -> dict:
    """Keyword arguments for the general-election class, under either spelling."""
    for key in GENERAL_KWARGS_KEYS:
        if key in kwargs:
            return kwargs[key]
    return {}


def is_two_round(kwargs: dict) -> bool:
    """Whether a voting_configs entry describes a primary/general rule."""
    return general_class_name(kwargs) is not None


def _general_slate_to_candidates(slate_to_candidates: Dict[str, List[str]], finalists: set) -> Dict[str, List[str]]:
    """
    Restrict each slate's candidate list to the finalists the primary advanced.

    Per-candidate filter, not per-slate all-or-nothing: a slate can survive with
    fewer candidates than the primary began with if only some of its candidates
    made the cut. A slate left with none is dropped entirely.
    """
    filtered = {s: [c for c in cs if c in finalists] for s, cs in slate_to_candidates.items()}
    return {s: cs for s, cs in filtered.items() if cs}


def build_general_profile(settings: dict, mode: str, finalists: List[str]) -> Tuple[RankProfile, str]:
    """
    Sample a fresh general-election profile restricted to the finalists, using the
    same voter model (mode) the primary used.

    Args:
        settings: The district's settings dict (as written by
            settings_generator.generate_settings), giving num_voters,
            slate_to_candidates, bloc_proportions, cohesion_parameters, alphas.
        mode: Voter model name ("slate_pl", "slate_bt", or "cambridge") -- the
            same one the primary's profile was generated with.
        finalists: Candidate ids the primary advanced.

    Returns:
        (profile, csv_text): the freshly sampled RankProfile and its CSV form,
        so the caller can both run the general on it and persist it.

    Note:
        The electorate here is always settings["bloc_proportions"] -- the
        configured turnout. A run modelling lower primary participation does that
        by feeding the primary different ballots (see "primary_turnout" in the
        README), which leaves the general at full turnout.
    """
    finalist_set = {str(c) for c in finalists}
    active_slate_to_candidates = _general_slate_to_candidates(settings["slate_to_candidates"], finalist_set)
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


def run_primary_general_election(
    profile: RankProfile, settings: dict, mode: str, kwargs: dict
) -> Tuple[List[str], str, List[str]]:
    """
    Run a primary followed by a general: Plurality primary narrows to
    kwargs["m_1"] finalists, then a freshly-resampled profile (restricted to those
    finalists, same mode as the primary) feeds kwargs["general_class"] run with
    kwargs["general_kwargs"].

    Args:
        profile: The primary's RankProfile (the one already loaded from
            profiles.zip, or from primary_profiles.zip when the run models a
            lower primary turnout).
        settings: The district's settings dict; needed to rebuild a
            BlocSlateConfig for the general's sample.
        mode: Voter model name the primary was generated with.
        kwargs: This rule's voting_configs entry -- "m_1" (required, how many
            candidates the primary advances), "tiebreak" (the primary's,
            optional), "general_class" (required, a name in votekit.elections),
            "general_kwargs" (optional, passed straight to general_class).

    Returns:
        (winners, general_csv_text, finalists): the general's winner ids, the
        general profile's CSV text for the caller to persist, and the finalists
        the primary advanced -- the last so the caller can record the primary's
        own outcome alongside the general's.
    """
    m_1 = kwargs["m_1"]
    tiebreak = kwargs.get("tiebreak")
    class_name = general_class_name(kwargs)
    general_election_class = getattr(elections, class_name, None) if class_name else None
    if general_election_class is None:
        raise ValueError(
            f"Unknown general_class '{class_name}' for a two-round rule."
        )

    primary = Plurality(profile, m_1, tiebreak)
    finalists = [str(c) for s in primary.get_elected() for c in s]

    general_profile, general_csv = build_general_profile(settings, mode, finalists)

    winners = [
        str(c)
        for s in general_election_class(general_profile, **general_kwargs(kwargs)).get_elected()
        for c in s
    ]
    return winners, general_csv, finalists
