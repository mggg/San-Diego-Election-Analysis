"""
Generate VoteKit settings files from sampled district plans.

Reads district assignments produced by the district-generation step,
aggregates population counts by district, computes turnout-adjusted
bloc proportions, and writes one settings JSON file per sampled plan
and district.
"""

import json
import gzip
import geopandas as gpd
from pathlib import Path
import jsonlines as jl
from tqdm import tqdm
from pipeline.utils.helpers import get_non_focal_group

def _build_district_settings(row, config):
    """
    Compute turnout-adjusted bloc proportions and population values for a district.

    Args:
        row: Row from the district population dataframe.
        turnout: Dict mapping group -> turnout rate.
        focal_group: Group of interest.
        other_group: Non-focal comparison group.
        config: Parsed config dict.

    Returns:
        Dict containing bloc_proportions and population counts for the district.
    """
    turnout = config['turnout']
    focal_group = config['focal_group']
    other_group = get_non_focal_group(config)

    prop = float(row[config['pop_of_interest_column']] / row[config['population_column']])
    adjusted_prop = (
        prop * turnout[focal_group]
        / (prop * turnout[focal_group] + (1 - prop) * turnout[other_group])
    )
    return {
        "bloc_proportions": {
            focal_group: adjusted_prop,
            other_group: 1 - adjusted_prop,
        },
        config["pop_of_interest_column"]: row[config["pop_of_interest_column"]],
        config["population_column"]: row[config["population_column"]],
    }

def generate_settings(config):
    """
    For each sampled district plan, compute per-district bloc proportions and write
    votekit settings json files.

    Args:
        config: Parsed config dict.

    Outputs:
        One json settings file per (district count, sampled plan, district) triple at
        outputs/settings/<run_name>_settings/<district_count>/<run_name>_<district_count>_sample_settings_district_plan_<plan_idx>_district_<district_id>.json.
        where <plan_idx> is the zero-based chain sample index and <district_id> is the district label.
        bloc_proportions in each file are turnout-adjusted focal group proportions.
    """
    population_data = gpd.read_file(config['geodata_path'])
    population_data = population_data[[config['pop_of_interest_column'],config['population_column']]]

    # subsample evenly spaced plans from the chain
    chain_length = config['chain_length']
    num_subsamples = config['num_subsamples']
    subsample_interval = chain_length // num_subsamples   

    # pull only the relevant keys from config to pass downstream
    district_params = ['num_voters', 'slate_to_candidates', 'cohesion_parameters', 'alphas']
    output_settings = {k:config[k] for k in config if k in district_params}
    run_name = config['run_name']

    for district_num in [d_config['num_districts'] for d_config in config['district_configs']]:
        settings_folder = Path(f'outputs/{run_name}/settings/{district_num}')
        settings_folder.mkdir(exist_ok=True, parents=True)

        path_to_districting = Path(f'outputs/{run_name}/districts/{run_name}_{district_num}_districts.jsonl.gz')
        
        with gzip.open(path_to_districting, mode="rt", encoding="utf-8") as gz_file:
            file = jl.Reader(gz_file)
            for sample_idx, sample in tqdm(
                enumerate(file),
                total=chain_length,
                desc=f"Generating VK settings for {district_num:02d} districts",
            ):
                if sample_idx % subsample_interval != 0:
                    continue

                district_plan = sample["assignment"]
                population_data["district_plan"] = district_plan
                data_by_district = population_data.groupby("district_plan").sum()

                for _, row in data_by_district.iterrows():
                    district = row.name
                    district_settings = _build_district_settings(row, config)
                    settings = output_settings | district_settings
                    with open(
                        f"{settings_folder}/{run_name}_{district_num}_sample_settings_district_plan_{sample_idx:03d}_district_{district:02d}.json",
                        "w",
                    ) as out_file:
                        json.dump(settings, out_file, indent=2)