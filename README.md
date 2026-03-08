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

Follow the instructions for setting up the necessary software [here](https://github.com/hanelee/CA_STV/tree/peter-workflow-updates?tab=readme-ov-file#software-setup).

### 2. Run the Full Pipeline (`run.py`)

Run the pipeline using:

`uv run --env-file py_env run.py`

When the script starts, you will be prompted to choose whether you want to **use an existing configuration file** or **create a new one**.

- If you choose **yes**, the script will prompt you to provide the path to an existing config JSON file (e.g., `configs/your_run_name.json`).
- If you choose **no**, the script will guide you through an interactive setup to create a new configuration file.

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


## Configuration File

All simulation parameters are defined in a JSON configuration file. You will be prompted for the following parameters:

| Prompt                    | Type         | Description                                                                              |
| --------------------------| -------------| ----------------------------------------------------------------------------------------|
| Run name                                          | string       | Identifier used for output directories and logs                                         |
| Path to geodata fil                               | string       | Path to the geographic dataset (.geojson or .gpkg)                                      |
| Population column name                            | string       | Column in the geographic dataset containing total population                            |
| Population of interest column name                | string       | Column containing the population of the focal demographic group                         |
| Total number of seats                             | integer      | Total number of representatives elected                                                 |
| Number of districts                               | integer      | Number of districts in a district configuration. This value must evenly divide the total number of seats so that the number of winners per district is an integer. Users may specify multiple district configurations. |
| Number of simulated elections per district plan   | integer      | Number of simulated elections per district plan                                         |
| Group names                                       | string       | Names of the bloc groups, comma-separated (e.g. A, B). Specify focal group first.       |
| Candidate names                                   | string       | Names of the bloc group's candidates, comma-separated (e.g. A1, A2, A3).                |
| Cohesion parameters                               | float (0-1)  | Probability that voters from a group vote for candidates from their own group. Higher values indicate stronger within-group voting cohesion. |
| Candidate strength parameters                     | positive float | Shape parameters of the Dirichlet distribution that control how voters within a group distribute their preferences across candidate slates. α = 0 models perfect consensus among voters, α = 1 neutral preferences, and α → ∞ indifference. |
| Turnout                                           | float (0-1)  | Turnout rate for each voter bloc                                                        |



