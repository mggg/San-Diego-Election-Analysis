"""
Instruments the REAL pipeline.utils.cambridge_truncation functions (no
reimplementation) to print exactly what they do, ballot by ballot, so the
behavior can be checked against the source rather than taken on faith.
"""
import numpy as np
from votekit.ballot_generator import BlocSlateConfig, slate_pl_profile_generator

from pipeline.utils.cambridge_truncation import (
    build_length_distributions,
    truncate_profile,
    _slate_for_candidate_lookup,
    _bloc_type_for_slate,
)

# Small config: WAIO majority (3 cands), HIS+AAPI+BLK pooled minority (3 cands total)
slate_to_candidates = {
    "WAIO": ["W1", "W2", "W3"],
    "HIS": ["H1"],
    "AAPI": ["A1"],
    "BLK": ["B1"],
}
config = BlocSlateConfig(
    n_voters=40,
    slate_to_candidates=slate_to_candidates,
    bloc_proportions={"WAIO": 0.55, "HIS": 0.2, "AAPI": 0.15, "BLK": 0.1},
    cohesion_mapping={
        "WAIO": {"WAIO": 0.8, "HIS": 0.1, "AAPI": 0.05, "BLK": 0.05},
        "HIS": {"WAIO": 0.3, "HIS": 0.6, "AAPI": 0.05, "BLK": 0.05},
        "AAPI": {"WAIO": 0.3, "HIS": 0.05, "AAPI": 0.6, "BLK": 0.05},
        "BLK": {"WAIO": 0.3, "HIS": 0.05, "AAPI": 0.05, "BLK": 0.6},
    },
)
config.set_dirichlet_alphas({s: {t: 1 for t in slate_to_candidates} for s in slate_to_candidates})

majority_slates = ["WAIO"]
minority_slates = ["HIS", "AAPI", "BLK"]

# --- Step 1: build_length_distributions (real function) ---
dists = build_length_distributions(config, majority_slates, minority_slates)
print("=== Length distributions returned by build_length_distributions ===")
for group, d in dists.items():
    print(f"  {group}: {d}")

# --- Instrument the RNG used inside truncate_profile so every .choice() call is logged ---
class LoggingGenerator:
    def __init__(self, seed):
        self._rng = np.random.default_rng(seed)
        self.calls = []

    def choice(self, a, size=None, p=None):
        result = self._rng.choice(a, size=size, p=p)
        self.calls.append({"options": list(a), "probs": list(p) if p is not None else None, "size": size, "result": result})
        return result

    def __getattr__(self, name):
        return getattr(self._rng, name)

logging_rng = LoggingGenerator(seed=42)

# --- Step 2: generate an untruncated profile with the REAL slate_pl generator ---
profile = slate_pl_profile_generator(config, rng_seed=1)
slate_for_candidate = _slate_for_candidate_lookup(config.slate_to_candidates.to_dict())

print("\n=== Original (untruncated) ballots ===")
for i, ballot in enumerate(profile.ballots):
    first_choice = next(iter(ballot.ranking[0]))
    slate = slate_for_candidate[first_choice]
    bloc_type = _bloc_type_for_slate(slate, majority_slates, minority_slates)
    print(f"  ballot {i}: weight={ballot.weight:g}  first_choice={first_choice} (slate={slate} -> {bloc_type})  full_length={len(ballot.ranking)}")

# --- Step 3: call the REAL truncate_profile with the logging rng ---
truncated = truncate_profile(profile, config, majority_slates, minority_slates, dists, rng=logging_rng)

print(f"\n=== rng.choice() call log ({len(logging_rng.calls)} calls -- ONE PER BALLOT GROUP, not per voter) ===")
for i, call in enumerate(logging_rng.calls[:8]):
    print(f"  call {i}: size={call['size']}  options={call['options']}  probs={[round(p,3) for p in call['probs']]}  -> sampled lengths={list(call['result'])}")
if len(logging_rng.calls) > 8:
    print(f"  ... ({len(logging_rng.calls) - 8} more calls)")
print(f"  Number of calls ({len(logging_rng.calls)}) == number of ballots ({len(profile.ballots)}): "
      f"{len(logging_rng.calls) == len(profile.ballots)}")
print("  -> confirms truncate_profile calls rng.choice(..., size=n_voters) ONCE per ballot,")
print("     getting one independent sample per voter back in a single vectorized call --")
print("     not literally one .choice() call per voter, but every voter still gets its own draw.")

# --- Step 4: verify each call's option set matches the distribution for that ballot's bloc_type ---
print("\n=== Cross-check: does each ballot's rng.choice() draw from ITS bloc_type's distribution? ===")
all_match = True
for call, ballot in zip(logging_rng.calls, profile.ballots):
    first_choice = next(iter(ballot.ranking[0]))
    bloc_type = _bloc_type_for_slate(slate_for_candidate[first_choice], majority_slates, minority_slates)
    expected_options = sorted(dists[bloc_type].keys())
    got_options = sorted(call["options"])
    all_match &= (got_options == expected_options)
    assert call["size"] == round(ballot.weight), "size must equal this ballot's voter count"
print(f"  every ballot's rng.choice() used exactly its own bloc_type's distribution: {all_match}")

# --- Step 5: prove per-voter independence -- find a weight>1 group and show varied outcomes ---
print("\n=== Per-voter independence: within one ballot's group, do voters get different lengths? ===")
shown = 0
for call, ballot in zip(logging_rng.calls, profile.ballots):
    if round(ballot.weight) > 1:
        print(f"  {round(ballot.weight)} voters shared this exact ranking -> sampled lengths: {[int(x) for x in call['result']]}")
        shown += 1
    if shown >= 4:
        break

print(f"\nTotal voters truncated: {sum(round(b.weight) for b in profile.ballots)}, "
      f"total voters in output profile: {sum(round(b.weight) for b in truncated.ballots)}")
