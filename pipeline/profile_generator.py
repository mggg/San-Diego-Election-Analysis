"""
Generate voter preference profiles from district-level settings files.

Reads VoteKit settings JSON files, generates synthetic voter profiles for
each district, voter model, and replicate, and bundles the resulting profiles
into a single compressed zip archive per run for downstream election
simulations.

Storing the profiles as entries in one outputs/<run_name>/profiles.zip (rather
than thousands of loose CSV files) keeps the output tree small and avoids the
per-file filesystem overhead of a large ensemble.
"""

import inspect
import math
import sys
from contextlib import ExitStack
from glob import glob
from votekit import RankProfile
from votekit.ballot_generator import (
    BlocSlateConfig,
    slate_pl_profile_generator,
    slate_bt_profile_generator,
    slate_bt_profile_generator_using_mcmc,
    name_cumulative_profile_generator,
    cambridge_profile_generator,
)
from votekit.ballot_generator.utils import system_memory
from joblib import Parallel, delayed
from joblib_progress import joblib_progress
from pathlib import Path
import time
import json
import zipfile
from typing import List

from pipeline.utils.helpers import (
    load_json,
    get_voter_models,
    required_score_budgets,
    profiles_signature,
    primary_profiles_signature,
    primary_profiles_metadata_path,
    primary_profiles_zip_path,
    read_existing_zip_members,
)
from pipeline.utils.preference_matrix import preference_matrix_arcname, preference_matrix_json
from pipeline.utils.cambridge_truncation import apply_cambridge_truncation
from pipeline.settings_generator import primary_turnout_map

# maps mode name to votekit profile generator function. slate_bt is resolved
# per-district instead (see _resolve_slate_bt_generator); this entry is still
# used by profile_class_for_mode/generator_accepts_total_points, which only
# need its signature, not the callable actually run -- both slate_bt variants
# share the same one (RankProfile, no total_points).
generator_name_to_function = {
    "slate_pl": slate_pl_profile_generator,
    "slate_bt": slate_bt_profile_generator,
    # Ranked ballots drawn from Cambridge, MA's historical RCV elections rather
    # than from a parametric model: a voter's first choice comes from the
    # cohesion parameters as usual, and the rest of the ballot is a real ballot
    # shape observed after that first choice. It works only with two blocs and
    # two slates, which is what the runs now model.
    "cambridge": cambridge_profile_generator,
    "name_cumulative": name_cumulative_profile_generator,
}


def _is_cambridge_eligible(settings: dict) -> bool:
    """
    Whether this district has enough active slates for Cambridge's model.

    VoteKit's cambridge_profile_generator requires exactly two slates with
    real candidates (its own _validate_slates_and_blocs rejects anything
    else, and BlocSlateConfig separately rejects an empty candidate list, so
    there's no config shape that works for one). A district whose candidate
    pool drew from only one slate -- common with a small candidate_pool_max
    -- simply has no Cambridge ballots to generate; see process_settings_file.
    """
    return len(settings["slate_to_candidates"]) >= 2


def count_cambridge_eligible_districts(run_name: str, district_num: int) -> int:
    """
    How many of this district count's settings files are eligible for
    Cambridge (see _is_cambridge_eligible) -- the real expected profile count
    for cambridge mode, since process_settings_file skips the rest. Every
    other voter model's expected count is the plain settings-file count
    (num_subsamples * district_num); this is cambridge's equivalent, read
    from disk instead of assumed, because which districts qualify depends on
    each one's own randomly-drawn candidate pool.
    """
    settings_dir = Path("outputs") / run_name / "settings" / str(district_num)
    return sum(
        1 for f in settings_dir.glob("*.json")
        if _is_cambridge_eligible(load_json(f))
    )


def _cambridge_bloc_kwargs(cambridge_cfg):
    """
    Which bloc Cambridge's historical majority and minority labels attach to.

    The Cambridge data labels candidates W and C for its historical majority and
    minority slates, so the generator has to be told which of this run's blocs
    plays each part. Left unset, votekit infers it from the bloc proportions --
    per district, which is the wrong basis here: a district where POC voters are
    the local majority would be generated against the historical *majority*
    distribution, so the ballot shapes a bloc is given would change from one
    district to the next according to how the lines happened to fall.

    Naming them in the config fixes each bloc to one side of that mapping for
    the whole run. An unconfigured run falls back to votekit's inference.
    """
    cfg = cambridge_cfg or {}
    majority, minority = cfg.get("majority"), cfg.get("minority")
    if majority and minority:
        return {"majority_bloc": majority, "minority_bloc": minority}
    return {}

# VoteKit's exact Bradley-Terry generator enumerates every distinct ordering of
# slate-letters across the ballot before sampling from it, and refuses to run
# at all past 12! (~479 million) such arrangements (see votekit's own
# _check_slate_bt_memory, which raises ValueError beyond this point). The MCMC
# variant samples ballot types directly instead of enumerating them, so it has
# no such ceiling, at the cost of drawing from a Markov chain's stationary
# distribution rather than the exact one. Plackett-Luce has no equivalent
# limit or MCMC variant: its exact method samples every ballot directly (no
# enumeration step), so it always uses the one generator regardless of
# candidate count.
BT_MAX_ARRANGEMENTS = math.factorial(12)

# Fudge factor matching votekit's own _check_slate_bt_memory -- an empirical
# upper-bound multiplier on the raw byte estimate, leaving headroom for
# everything else running on the machine rather than assuming it has none.
BT_MEMORY_FUDGE_FACTOR = 1.5


def _bt_arrangement_count(config: BlocSlateConfig) -> int:
    """
    Number of distinct slate-letter orderings the exact Bradley-Terry generator
    would need to enumerate for this district's candidate pool -- the same
    multinomial coefficient (n_cands! / prod(per-slate!)) votekit's own
    _check_slate_bt_memory computes.
    """
    n_cands = len(config.candidates)
    denom = 1
    for candidates in config.slate_to_candidates.values():
        denom *= math.factorial(len(candidates))
    return math.factorial(n_cands) // denom


def _bt_estimated_bytes(config: BlocSlateConfig, arrangements: int) -> float:
    """
    Estimated peak memory (bytes) the exact Bradley-Terry generator would need
    for this district: a full probability table (one entry per arrangement)
    plus the sampled profile itself -- replicating votekit's own
    _check_slate_bt_memory formula so this can be checked before running it,
    not just after it fails.

    Staying under BT_MAX_ARRANGEMENTS (12!) doesn't bound this on its own: a
    288-million-arrangement district (well under the 479-million ceiling)
    was observed needing an estimated 426 GiB, because the table's size scales
    with the arrangement count directly, and 12! of anything is already huge.
    """
    n_cands = len(config.candidates)
    longest = max(config.candidates, key=len)
    est_bytes_pmf = arrangements * sys.getsizeof(longest) * n_cands
    est_bytes_profile = config.n_voters * n_cands * sys.getsizeof(frozenset({longest}))
    return (est_bytes_pmf + est_bytes_profile) * BT_MEMORY_FUDGE_FACTOR


def _resolve_slate_bt_generator(config: BlocSlateConfig):
    """
    The exact Bradley-Terry generator when this district's candidate pool is
    both within VoteKit's arrangement ceiling and small enough to fit in this
    machine's available memory; the MCMC variant otherwise. Both are checked
    because clearing the first doesn't imply the second -- see
    _bt_estimated_bytes.
    """
    arrangements = _bt_arrangement_count(config)
    if arrangements > BT_MAX_ARRANGEMENTS:
        return slate_bt_profile_generator_using_mcmc
    available_bytes = system_memory()["available_gib"] * 2**30
    if _bt_estimated_bytes(config, arrangements) > available_bytes:
        return slate_bt_profile_generator_using_mcmc
    return slate_bt_profile_generator


def profile_class_for_mode(mode: str):
    """
    The profile type a voter model produces -- RankProfile for the ranked models,
    ScoreProfile for the cumulative one.

    Read off each generator's own return annotation rather than a hand-kept
    table, so adding a generator above is enough for the rest of the pipeline to
    know what it yields. simulate_elections uses this to pair each voting rule
    with the models whose ballots it can actually run on: STV cannot read
    cumulative ballots, and Cumulative/Limited cannot read ranked ones.

    Raises:
        KeyError: If mode is not a configured voter model.
        TypeError: If its generator does not annotate a return type.
    """
    generator = generator_name_to_function[mode]
    annotation = inspect.signature(generator).return_annotation
    if annotation is inspect.Signature.empty:
        raise TypeError(
            f"Generator for voter model '{mode}' has no return annotation, so the "
            "profile type it produces cannot be determined."
        )
    return annotation

def _profiles_metadata_path(run_name: str) -> Path:
    """Sidecar recording the signature the profiles.zip was generated under."""
    return Path(f"outputs/{run_name}/profiles_metadata.json")


def _expected_profile_filename(settings_file, duplicate_indx) -> str:
    """
    The profile filename for a given settings file and replicate index.

    Kept in one place so the writer here and the readers (simulate_elections,
    run.has_valid_profiles) agree on the naming convention: the settings file's
    stem with "sample_settings" replaced by "profile" and "_v<n>.csv" appended.
    """
    setting_file_stem = Path(settings_file).stem
    return f"{setting_file_stem.replace('sample_settings', 'profile')}_v{duplicate_indx}.csv"


def generator_accepts_total_points(mode: str) -> bool:
    """Whether this voter model's generator takes a `total_points` budget."""
    return "total_points" in inspect.signature(generator_name_to_function[mode]).parameters


def score_budgets_for_run(config, district_winners: int) -> List[int]:
    """
    Every score-ballot budget this run needs, ascending.

    Read from the configured rules: Cumulative fixes a voter's budget at n_seats,
    Limited states its own, and each rejects ballots that spend more than that.
    Two rules with different budgets need two different sets of ballots, so each
    budget gets generated separately (and stored under its own subdirectory).

    Falls back to the district's seat count when a score model is configured with
    no score rule to read it -- the generator's own default (one point per
    candidate) satisfies no rule, so it is never used.
    """
    budgets = required_score_budgets(config.get("voting_configs"))
    return budgets or [int(district_winners)]


def profile_arcname(mode: str, district_num: int, filename: str, budget=None) -> str:
    """
    Where a profile lives in the archive.

    Ranked models keep the original "<mode>/<district_num>/<file>" layout. Score
    models gain a budget level -- "<mode>/<budget>/<district_num>/<file>" --
    because the same district needs one set of ballots per budget its rules ask
    for, and they are not interchangeable.
    """
    if budget is None:
        return f"{mode}/{district_num}/{filename}"
    return f"{mode}/{budget}/{district_num}/{filename}"


def process_settings_file(
    settings_file, mode, duplicate_indx, proportions_key="bloc_proportions", total_points=None,
    truncate_ballots=False, cambridge_cfg=None, truncation_majority_slates=None,
    truncation_minority_slates=None,
):
    """
    Generate a voter profile for a single district using the given voter model.

    Runs entirely in memory (no filesystem write) so it can be called from a
    parallel worker and have its result written into the run's shared zip
    archive by the caller, avoiding concurrent writes to one zip file.

    Args:
        settings_file: Path to a votekit settings json file for one district.
        mode: Voter model name; one of "slate_pl", "slate_bt", "cambridge", or
            "name_cumulative".
        duplicate_indx: Replicate index, appended as _v<n> in the output filename.
        proportions_key: Which electorate to sample from -- "bloc_proportions"
            (the configured turnout) or "primary_bloc_proportions" (the lower
            turnout of a two-round rule's narrowing round).
        total_points: Score-ballot budget, for models that produce score ballots.
            Ignored by the ranked generators, which take no such argument.
        truncate_ballots: Whether the run config's "cambridge_truncation" key
            is present and its "enabled" field is True. When True, a
            slate_pl or slate_bt profile's ballots are truncated to a length
            sampled from the Cambridge historical distribution matching each
            ballot's first choice (see pipeline.utils.cambridge_truncation).
            Ignored for cambridge mode, whose ballots already come from that
            same historical data, and for score profiles, which have no
            ranking to truncate.
        truncation_majority_slates: The run config's
            "cambridge_truncation.majority_slates" -- slate names pooled into
            Cambridge's historical majority group. Only read when
            truncate_ballots is True; falls back to the DEFAULT_MAJORITY_SLATE
            convention when left unset (see apply_cambridge_truncation).
        truncation_minority_slates: The run config's
            "cambridge_truncation.minority_slates", the historical minority
            group's pooled slates. Same fallback as truncation_majority_slates.

    Returns:
        (filename, csv_text, matrix_json): filename is the profile's zip entry
        name within its <mode>/<district_num>/ folder (see
        _expected_profile_filename); csv_text is the profile's CSV content
        (per votekit's PreferenceProfile.to_csv()). All three are None when
        this (settings_file, mode) combination has nothing to generate -- see
        the cambridge single-slate case below.
    """
    settings = load_json(settings_file)
    filename = _expected_profile_filename(settings_file, duplicate_indx)

    if mode == "cambridge" and not _is_cambridge_eligible(settings):
        return None, None, None

    config = BlocSlateConfig(
        n_voters = settings['num_voters'],
        slate_to_candidates=settings["slate_to_candidates"],
        bloc_proportions=settings[proportions_key],
        cohesion_mapping=settings["cohesion_parameters"],
    )
    config.set_dirichlet_alphas(settings["alphas"])

    # slate_bt's generator depends on this district's own candidate pool (see
    # _resolve_slate_bt_generator); every other mode's is fixed.
    generator = (
        _resolve_slate_bt_generator(config) if mode == "slate_bt"
        else generator_name_to_function[mode]
    )

    # Only the Cambridge generator takes extra keywords; every other model is
    # fully described by the district's own config.
    generator_kwargs = _cambridge_bloc_kwargs(cambridge_cfg) if mode == "cambridge" else {}

    if total_points is not None and generator_accepts_total_points(mode):
        profile = generator(config, total_points=total_points, **generator_kwargs)
    else:
        profile = generator(config, **generator_kwargs)

    if truncate_ballots and mode in ("slate_pl", "slate_bt") and isinstance(profile, RankProfile):
        profile = apply_cambridge_truncation(
            profile, config, truncation_majority_slates, truncation_minority_slates
        )

    csv_text = profile.to_csv()
    matrix_json = preference_matrix_json(config)

    return filename, csv_text, matrix_json


def _generate_profile_archive(
    config,
    zip_path: Path,
    metadata_path: Path,
    signature: str,
    proportions_key: str,
    matrix_zip_path=None,
    label: str = "generate_profiles",
):
    """
    Generate one archive of voter profiles: every district, mode, and replicate
    sampled from the electorate named by proportions_key.

    Called once for the standard profiles and, when a run models a separate
    primary electorate, a second time for those -- same resumability, parallel
    dispatch, and sequential-write structure both times.

    Args:
        config: Parsed config dict.
        zip_path: Archive to write the profile csvs into.
        metadata_path: Sidecar recording the signature this archive was built under.
        signature: Signature to record and to check a prior archive against.
        proportions_key: Settings key holding the bloc proportions to sample from.
        matrix_zip_path: Optional preference-matrix archive kept in sync with the
            profiles. The primary pass passes None -- the matrices describe the
            slate preference intervals, which a turnout override doesn't change.
        label: Prefix for this pass's console output.
    """
    num_reps = config['num_reps']
    run_name = config['run_name']

    voter_models = get_voter_models(config)
    cambridge_truncation_cfg = config.get("cambridge_truncation")
    truncate_ballots = (
        isinstance(cambridge_truncation_cfg, dict)
        and cambridge_truncation_cfg.get("enabled") is True
    )
    truncation_majority_slates = (
        cambridge_truncation_cfg.get("majority_slates") if truncate_ballots else None
    )
    truncation_minority_slates = (
        cambridge_truncation_cfg.get("minority_slates") if truncate_ballots else None
    )
    cambridge_cfg = config.get("cambridge_blocs")

    if truncate_ballots:
        print(f"[{label}] Truncating ballots sampling from Historical Cambridge Data")

    zip_path.parent.mkdir(exist_ok=True, parents=True)
    track_matrices = matrix_zip_path is not None

    # Resume only when the archives are readable AND were generated under the
    # same signature; otherwise their contents may be stale or inconsistent,
    # so rebuild from scratch.
    prior_signature = None
    if metadata_path.is_file():
        try:
            prior_signature = load_json(metadata_path).get("signature")
        except (json.JSONDecodeError, OSError):
            prior_signature = None

    same_signature = prior_signature == signature
    existing_members = read_existing_zip_members(zip_path) if same_signature else None
    existing_matrix_members = (
        read_existing_zip_members(matrix_zip_path) if same_signature and track_matrices else None
    )

    # Require every tracked archive to be intact; if any is missing or corrupted,
    # rebuild everything so they stay in sync.
    resume = existing_members is not None and (existing_matrix_members is not None or not track_matrices)
    if not resume:
        existing_members = set()
        existing_matrix_members = set()
    elif not track_matrices:
        existing_matrix_members = set()

    archive_mode = "a" if resume else "w"
    if resume:
        matrices_note = f"and {len(existing_matrix_members)} matrix(es) " if track_matrices else ""
        print(
            f"[{label}] Resuming: {len(existing_members)} profile(s) {matrices_note}"
            "already present with a matching signature; generating only what's missing."
        )
    else:
        print(f"[{label}] No compatible prior profiles found; generating from scratch.")

    # Opened once for the whole run: workers only compute (filename, csv_text)
    # pairs in parallel, and every actual write to the shared archive happens
    # here, sequentially, in the main process (a zip can't be written from
    # multiple processes at once).
    #
    # return_as="generator_unordered" yields each worker's result as soon as
    # it's ready instead of collecting the whole batch into memory before any of
    # it is written, so peak memory is bounded by what's in flight, not the full
    # batch of profiles.
    with ExitStack() as stack:
        archive = stack.enter_context(
            zipfile.ZipFile(zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED)
        )
        matrix_archive = (
            stack.enter_context(
                zipfile.ZipFile(matrix_zip_path, archive_mode, compression=zipfile.ZIP_DEFLATED)
            )
            if track_matrices
            else None
        )
        # repeat for each replicate
        for duplicate_indx in range(num_reps):
            rep_start = time.perf_counter()
            print(f"[rep {duplicate_indx + 1}/{num_reps}] Start at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            district_nums =  [d_config['num_districts'] for d_config in config['district_configs']]
            for district_num in district_nums:
                winners = next(
                    (
                        d["winners"] for d in config["district_configs"]
                        if d["num_districts"] == district_num
                    ),
                    1,
                )
                for mode in voter_models:
                    settings_folder = Path(f"outputs/{run_name}/settings/{district_num}")
                    all_settings_files = glob(f"{settings_folder}/*.json")

                    # A score model needs one set of ballots per budget its rules
                    # ask for; a ranked model has no budget at all, which the
                    # single None pass stands for.
                    if generator_accepts_total_points(mode):
                        budgets = score_budgets_for_run(config, winners)
                    else:
                        budgets = [None]

                    for budget in budgets:
                        # Skip settings files whose profile AND matrix are already
                        # in their respective archives. If either entry is
                        # missing, regenerate both so the two stay in sync.
                        pending_settings_files = [
                            sf for sf in all_settings_files
                            if profile_arcname(
                                mode, district_num, _expected_profile_filename(sf, duplicate_indx), budget
                            ) not in existing_members
                            or (
                                track_matrices
                                and profile_arcname(
                                    mode, district_num,
                                    preference_matrix_arcname(_expected_profile_filename(sf, duplicate_indx)),
                                    budget,
                                ) not in existing_matrix_members
                            )
                        ]
                        if not pending_settings_files:
                            continue

                        budget_note = f", budget {budget}" if budget is not None else ""
                        with joblib_progress(
                            description=f"[{label}][rep {duplicate_indx + 1:03d}/{num_reps}] Generating VK profiles for {district_num:02d} districts and voter model {mode}{budget_note}",
                            total=len(pending_settings_files),
                        ):
                            results = Parallel(n_jobs=-1, return_as="generator_unordered")(
                                delayed(process_settings_file)(
                                    settings_file, mode, duplicate_indx, proportions_key, budget,
                                    truncate_ballots, cambridge_cfg,
                                    truncation_majority_slates, truncation_minority_slates,
                                )
                                for settings_file in pending_settings_files
                            )

                            for filename, csv_text, matrix_json in results:
                                if filename is None:
                                    # Nothing to write -- e.g. cambridge on a
                                    # single-slate district (see
                                    # process_settings_file).
                                    continue
                                arcname = profile_arcname(mode, district_num, filename, budget)
                                # Guard against duplicate zip entries when one
                                # archive had an entry the other lacked.
                                if arcname not in existing_members:
                                    archive.writestr(arcname, csv_text)
                                    existing_members.add(arcname)
                                if track_matrices:
                                    matrix_arc = profile_arcname(
                                        mode, district_num, preference_matrix_arcname(filename), budget
                                    )
                                    if matrix_arc not in existing_matrix_members:
                                        matrix_archive.writestr(matrix_arc, matrix_json)
                                        existing_matrix_members.add(matrix_arc)
            rep_elapsed = time.perf_counter() - rep_start
            print(f"[rep {duplicate_indx + 1}/{num_reps}] Done in {rep_elapsed:.1f}s")

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump({"signature": signature}, f)


def generate_profiles(config):
    """
    Generate voter profile csvs for all districts, modes, and replicates in the
    config, bundling them into a single compressed zip archive per run.

    Resumable: if a prior profiles.zip exists, is readable, and was generated
    under the same profile signature (see helpers.profiles_signature -- the
    config parameters that determine profile *content*), its entries are reused
    and only the missing (mode, district, settings-file, replicate) combinations
    are generated and appended. So raising num_reps, adding a voter model, or
    adding a district magnitude fills in only the new profiles. If the signature
    changed (a content-determining parameter differs) or no readable prior
    archive exists, the archive is rebuilt from scratch.

    A config that sets "primary_turnout" gets a second archive on the same terms,
    sampled from the lower-turnout electorate of a two-round rule's narrowing
    round. Two-round rules run round 1 on those ballots; every other rule, and
    round 2 itself, keeps using the standard profiles.

    Args:
        config: Parsed config dict.

    Outputs:
        outputs/<run_name>/profiles.zip, containing one csv entry per
        (mode, district_num, settings file, replicate) at
        "<mode>/<district_num>/<...>_v<duplicate_indx>.csv".
        outputs/<run_name>/profiles_metadata.json, recording the profile
        signature so a later call can tell whether the archive is safe to resume.
        With "primary_turnout" set, also outputs/<run_name>/primary_profiles.zip
        and primary_profiles_metadata.json, keyed by the same entry names.
    """
    run_name = config['run_name']

    _generate_profile_archive(
        config,
        zip_path=Path(f"outputs/{run_name}/profiles.zip"),
        metadata_path=_profiles_metadata_path(run_name),
        signature=profiles_signature(config),
        proportions_key="bloc_proportions",
        matrix_zip_path=Path(f"outputs/{run_name}/preference_matrices.zip"),
        label="generate_profiles",
    )

    if primary_turnout_map(config) is not None:
        _generate_profile_archive(
            config,
            zip_path=primary_profiles_zip_path(run_name),
            metadata_path=primary_profiles_metadata_path(run_name),
            signature=primary_profiles_signature(config),
            proportions_key="primary_bloc_proportions",
            matrix_zip_path=None,
            label="generate_primary_profiles",
        )


if __name__ == '__main__':
    config = load_json("configs/basic.json")
    generate_profiles(config)
