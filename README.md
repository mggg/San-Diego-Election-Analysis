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


Two parameters are currently being set to a default value:

| Parameter                 | Value        | Description                                                                            |
| --------------------------| -------------| ---------------------------------------------------------------------------------------|
| Number of subsamples | 5 | Number of district plans to retain for election simulation. |
| Number of voters | 10,000 | Number of voters for each simulation. |

Chain length was previously defaulted here; it is now a project-wide setting in
`project-settings.json`.
