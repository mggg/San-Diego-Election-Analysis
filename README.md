# Electoral Redistricting

## 1. Setup Software

Follow the instructions for setting up the necessary software [here](https://github.com/hanelee/CA_STV/tree/peter-workflow-updates?tab=readme-ov-file#software-setup).

## 2. Run the Full Pipeline (`run.py`)

Run the pipeline using:

`uv run --env-file py_env run.py`

When the script starts, you will be prompted to choose whether you want to **use an existing configuration file** or **create a new one**.

- If you choose **yes**, the script will prompt you to provide the path to an existing config JSON file (e.g., `configs/your_run_name.json`).
- If you choose **no**, the script will guide you through an interactive setup to create a new configuration file.

Once the configuration is loaded or created, the pipeline will execute the entire simulation workflow sequentially.

The pipeline will execute the following stages in order:

| Stage | Script | Summary |
|-------|--------|---------|
| 1 | `Districts_generator.py` | Generates district plans using GerryChain by converting geographical data into a graph |
| 2 | `Settings_generator.py` | Creates VoteKit settings JSONs by aggregating population data and computing turnout-adjusted bloc proportions for subsampled district plans |
| 3 | `Profile_generator.py` | Generates voter preference profiles (simulated ballots) for each settings file under three voting behavior models (impulsive, deliberate, and Cambridge) |
| 4 | `Simulate_elections.py` | Runs the election simulation (FastSTV) on the generated voter profiles to determine and record the winners |
| 5 | `Summarize_results.py` | Post-processes the election results into a dataframe and generates histograms of seat counts for comparative analysis |
