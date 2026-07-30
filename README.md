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

Run the pipeline using:

`uv run --env-file py_env run.py`

When the script starts, you will be prompted to choose whether you want to **use an existing configuration file** or **create a new one**.

- If you choose **yes**, the script will prompt you to provide the path to an existing config JSON file (e.g., `configs/your_run_name.json`).
- If you choose **no**, the script will guide you through an interactive setup to create a new configuration file. A description of the configuration file parameters can be found [below](https://github.com/sarsong/ElectoralRedistricting/blob/main/README.md#configuration-file).

**Note**: The first time you run this command, it may take a moment before the prompt appears. This is because some imports take time to load. Subsequent runs will start much faster.

Once the configuration is loaded or created, the pipeline will execute the entire simulation workflow sequentially.

The pipeline will execute the following stages in order:

| Stage | Script | Summary |
|-------|--------|---------|
| 1 | `districts_generator.py` | Generates district plans using GerryChain by converting geographical data into a graph |
| 2 | `settings_generator.py` | Creates VoteKit settings JSONs by aggregating population data and computing turnout-adjusted bloc proportions for subsampled district plans |
| 3 | `profile_generator.py` | Generates voter preference profiles (simulated ballots) for each settings file under three voting behavior models (impulsive, deliberate, and Cambridge) |
| 4 | `simulate_elections.py` | Runs the election simulation (FastSTV) on the generated voter profiles to determine and record the winners |
| 5 | `summarize_results.py` | Post-processes the election results into a dataframe and generates histograms of seat counts for comparative analysis |

Each stage reads outputs from the previous stage.


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
