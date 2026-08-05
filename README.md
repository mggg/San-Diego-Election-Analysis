# Electoral Redistricting

## Overview

This repository contains a **modular simulation pipeline for studying electoral systems**, particularly the **single transferable vote (STV)** and plurality voting.

The pipeline integrates:
* district plan generation (GerryChain)
* synthetic ballot generation
* election simulation (VoteKit)
* result analysis and visualization

It enables researchers and practitioners to compare how **district magnitude, voter behavior, and demographic structure affect representation outcomes**. The goal is to make ranked-choice voting simulations easier to run without requiring extensive custom code. 

## Quick Start

### 1. Setup Software
This repository was developed using the UV build system. This build system is generally available through chocolatey on Windows (`choco install uv`), homebrew on MacOS `brew install uv`, and through direct installation on Linux (e.g. `apt install uv`). You can also install directly from source using the instructions at UV's homepage or you can install the system into a conda environment (`conda install conda-forge::uv`).

After installing UV, you can build the necessary virtual environment for this repository by invoking the command

```
uv sync
```

from the terminal while in the base directory.

### 2. Run the Full Pipeline (`run.py`)

There are four ways in, all run from the repository root.

`run.py` sets `PYTHONHASHSEED=0` for itself, so GerryChain's chains are
reproducible without passing anything extra on the command line. (Python fixes
hash randomization at interpreter startup, so this cannot be done from inside a
running process — `run.py` re-launches itself once with the seed set. The
`py_env` file is no longer needed, but `uv run --env-file py_env …` still works
and skips that relaunch.)

#### Run one config

Pass the config by name. The name resolves with or without the directory and
extension, so these are equivalent:

```
uv run run.py basic
uv run run.py basic.json
uv run run.py configs/basic.json
```

Each of those runs the `basic` config end to end: it generates the geodata if
`data/san_diego.geojson` is missing, then district plans, VoteKit settings,
ballots, elections, and summaries.

Outputs are keyed by the config's `run_name`, not its filename — `basic.json`
has `"run_name": "Basic - 3 X 3 STV"`, so its results land in
`outputs/Basic - 3 X 3 STV/`.

Re-running is cheap. Every stage checks whether its outputs already exist and
are still valid for the current config, so a second `run.py basic` picks up
where the first left off instead of redoing finished work.

#### Run every config

```
uv run run.py --run-all
```

Runs each config in `configs/` in turn, then draws one cross-run comparison
figure over all of their summaries. Project-wide settings are not a run, so
`project-settings.json` at the repo root is not picked up here.

#### Build a new config interactively

```
uv run run.py
```

With no arguments you are prompted to **use an existing configuration file** or
**create a new one**.

- Choose **yes** to give the path to an existing config (e.g. `configs/basic.json`).
- Choose **no** to be walked through building one. It is saved to
  `configs/<run_name>.json` and then run. Only run-specific parameters are
  asked for — see [Configuration](#configuration) for the split.

**Note**: The first time you run this command, it may take a moment before the prompt appears. This is because some imports take time to load. Subsequent runs will start much faster.

#### Build the geodata on its own

The data-generation stage is also its own entry point, which is useful after
changing anything under `geometry_data` and before committing to a full run:

```
uv run python -m pipeline.data_generator --config configs/basic.json
```

This writes `geodata_path` plus its adjacency graph and a metadata sidecar, and
nothing else. It reads the same layered configuration as `run.py`, so the
geometry can live in `project-settings.json`.

#### Stages

However it is started, the pipeline executes the whole workflow sequentially,
each stage reading the previous stage's outputs:

| Stage | Script | Summary |
|-------|--------|---------|
| 0 | `data_generator.py` | Builds the block-level geodata for the city — geometry, VAP by race, elections, and district labels — and is skipped when `geodata_path` already holds valid data |
| 1 | `districts_generator.py` | Generates district plans using GerryChain by converting geographical data into a graph |
| 2 | `settings_generator.py` | Creates VoteKit settings JSONs by aggregating population data and computing turnout-adjusted bloc proportions for subsampled district plans |
| 3 | `profile_generator.py` | Generates voter preference profiles (simulated ballots) for each settings file under three voting behavior models (impulsive, deliberate, and Cambridge) |
| 4 | `simulate_elections.py` | Runs the election simulation (FastSTV) on the generated voter profiles to determine and record the winners |
| 5 | `summarize_results.py` | Post-processes the election results into a dataframe and generates histograms of seat counts for comparative analysis |


## Configuration

Configuration is split in two. **`project-settings.json`** holds the settings that
describe the project rather than any one simulation, so they are written once
instead of being copied into every run:

| Key | Description |
| --- | --- |
| `geodata_path` | Path to the geographic dataset every run reads (`.geojson` or `.gpkg`) |
| `geometry_data` | How that dataset is built: state/county, CRSs, block source, Districtr plan, council district layer |
| `gerrychain_output_dir` | Chain output location |
| `population_column`, `population_vap_column`, `pop_of_interest_column` | Columns carrying total population, VAP, and the focal group |
| `seed` | Random seed |
| `chain_length` | Total steps in the Markov chain |

It lives at the repo root, alongside `run.py`. Every file in `configs/` is one
**run**. A run config is layered over the project settings at load time, and any
key it sets wins — nested objects such as `geometry_data` merge key by key, so a
run can override a single geometry setting without restating the rest.

The interactive setup prompts only for run-specific parameters:

| Prompt                    | Type         | Description                                                                              |
| --------------------------| -------------| ----------------------------------------------------------------------------------------|
| Run name                                          | string       | Identifier used for output directories and logs                                         |
| Total number of seats                             | integer      | Total number of representatives elected                                                 |
| Number of districts                               | integer      | Number of districts in a district configuration. This value must evenly divide the total number of seats so that the number of winners per district is an integer. Users may specify multiple district configurations. |
| Number of simulated elections per district plan   | integer      | Number of simulated elections per district plan                                         |
| Group names                                       | string       | Names of the bloc groups, comma-separated (e.g. A, B). Specify focal group first.       |
| Candidate names                                   | string       | Names of the bloc group's candidates, comma-separated (e.g. A1, A2, A3).                |
| Cohesion parameters                               | float (0-1)  | Probability that voters from a group vote for candidates from their own group. Higher values indicate stronger within-group voting cohesion. |
| Candidate strength parameters                     | positive float | Shape parameters of the Dirichlet distribution that control how voters within a group distribute their preferences across candidate slates. α = 0 models perfect consensus among voters, α = 1 neutral preferences, and α → ∞ indifference. |
| Turnout                                           | float (0-1)  | Turnout rate for each voter bloc                                                        |

### Candidate pool size

Each entry in `district_configs` controls how many candidates its districts put
on the ballot. The pool is drawn per district as a binomial over the range
`[floor, candidate_pool_max]`, with the mean placed at `candidate_pool_mean`:

```json
"district_configs": [
  { "num_districts": 9, "winners": 1,
    "candidate_pool_max": 9, "candidate_pool_mean": 5.67 }
]
```

| Key | Meaning |
| --- | --- |
| `candidate_pool_max` | Ceiling on the pool. Required. |
| `candidate_pool_mean` | Where the average pool size sits. Required. |

The **floor** is not configurable: it is the smallest ballot every configured
voting rule can actually run on (`minimum_candidates`), one more than the most
demanding requirement in the run.

Neither key is validated — keep the mean inside `[floor, candidate_pool_max]`.
A mean outside that range gives numpy a probability outside `[0, 1]` and it
raises `ValueError: p < 0, p > 1 or p is NaN`; a ceiling equal to the floor
divides by zero. Because the pool size feeds the per-slate apportionment, it is
the main lever on how often a small slate fields anyone at all.

Both keys are part of the profile signature, so changing either regenerates the
profiles for that magnitude.

### Voter models and ballot types

A voter model determines the *kind* of ballot it produces, and a voting rule can
only read one kind:

| Voter model | Ballots | Rules it can run |
| --- | --- | --- |
| `slate_pl`, `slate_bt` | `RankProfile` | STV, IRV, Plurality, Borda, the two-round rules |
| `name_cumulative` | `ScoreProfile` | Cumulative, Limited |

Configure both families in one run and each rule is simulated under the models
whose ballots it supports, with the rest skipped and reported:

```
[simulate_elections] slate_pl produces RankProfile ballots; skipping ['Cumulative', 'Limited'] (they need ScoreProfile).
[simulate_elections] name_cumulative produces ScoreProfile ballots; skipping ['FastSTV'] (they need RankProfile).
```

Each model writes its own results file, so the summary carries one row per
(model, rule) pair that actually ran. A rule accepting either type (e.g.
`BlockPlurality`) runs under both.

**Score budgets.** A score ballot is only valid for one budget: `Cumulative`
rejects any ballot spending more than `n_seats` points and `Limited` more than
its `budget`. Rules with different budgets therefore need different ballots, so
the profile stage reads the budgets off the configured rules and generates one
set per distinct budget, stored under its own subdirectory:

```
profiles.zip
├── slate_pl/3/…csv                 ranked ballots, no budget
├── name_cumulative/2/3/…csv        every ballot spends exactly 2 points
└── name_cumulative/3/3/…csv        every ballot spends exactly 3 points
```

So `Cumulative` (budget = `n_seats` = 3) and `Limited` (`budget: 2`) run in the
same simulation, each reading the ballots it can accept. Changing any budget
changes the profile signature and regenerates the score profiles, leaving the
ranked ones alone.

### Two-round elections: the primary and the general

A `voting_configs` entry that names a `general_class` is a **two-round rule**. The
two stages are named for what they do: the **primary** narrows the field by
Plurality to `m_1` finalists, then a freshly-sampled profile restricted to those
finalists decides the **general** (see `pipeline/two_round_election.py`).

```json
"AlaskaTwoProfile": {
  "m_1": 4,
  "tiebreak": "random",
  "general_class": "STV",
  "general_kwargs": { "n_seats": 1, "tiebreak": "random" }
}
```

`round2_class` / `round2_kwargs` are still accepted as the former names of
`general_class` / `general_kwargs`, so existing configs keep running. Note that
renaming them in a config changes its election-results signature, which forces
that run to re-simulate.

Both stages are recorded. Alongside the usual
`outputs/<run>/election_results/<mode>/<file>.json`, which holds the general's
winners, a run with a two-round rule also writes
`outputs/<run>/primary_results/<mode>/<same file>.json` giving the finalists each
primary advanced. The two files carry the same `signature` and the same
`profile_files` list in the same order, so they line up row for row:

```jsonc
// primary_results/slate_pl/<run>_9_districts_1_winners_for_voter_mode_slate_pl.json
{
  "stage": "primary",
  "advances_per_rule": { "AlaskaTwoProfile": 4, "TopTwoTwoProfile": 2 },
  "profile_files": ["slate_pl/9/..._district_02_v0.csv", "..."],
  "primary_results": [ { "AlaskaTwoProfile": ["HIS4", "HIS2", "HIS1", "HIS3"] }, ... ]
}
```

The general's ballots are kept too, in `outputs/<run>/general_profiles.zip` — one
resampled, finalists-only profile per (rule, profile file).

By default both rounds draw on the same electorate — the one `turnout` describes.
Add an optional top-level `primary_turnout` block to model a narrowing round with
lower participation, as a primary typically has:

```json
"turnout":         { "AAPI": 0.75, "HIS": 0.75, "WHI": 1, "BAIO": 1 },
"primary_turnout": { "AAPI": 0.4,  "HIS": 0.4 }
```

It is a **partial override**: blocs left out keep their `turnout` rate, so only
the ones whose participation drops need naming. It applies to round 1 of every
two-round rule; round 2 and every single-round rule keep using `turnout`
unchanged. VoteKit's built-in `Alaska` and `TopTwo` build round 2 by stripping
their round-1 ballots rather than resampling, so they cannot use a separate
primary electorate — use `AlaskaTwoProfile` / `TopTwoTwoProfile` instead, and the
simulation stage warns if the built-ins are configured alongside
`primary_turnout`.

Setting it makes the profile stage generate a second archive,
`outputs/<run_name>/primary_profiles.zip`, holding one primary-round profile per
entry in `profiles.zip`. That roughly doubles profile-generation time for the
run: a different electorate means different ballots, and ballots cannot be
reweighted after sampling.


Two parameters are currently being set to a default value:

| Parameter                 | Value        | Description                                                                            |
| --------------------------| -------------| ---------------------------------------------------------------------------------------|
| Number of subsamples | 100 | Number of district plans to retain for election simulation. |
| Number of voters | 10,000 | Number of voters for each simulation. |

Chain length was previously defaulted here; it is now a project-wide setting in
`project-settings.json`.
