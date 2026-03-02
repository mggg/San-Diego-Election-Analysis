import json
from pipeline.district_generator import generate_districts
from pipeline.settings_generator import generate_settings
from pipeline.profile_generator import generate_profiles
from pipeline.simulate_elections import simulate_elections
from pipeline.summarize_results import summarize_results
from setup import setup_config


def run_pipeline(config):
    generate_districts(config)
    generate_settings(config)
    generate_profiles(config)
    simulate_elections(config)
    summarize_results(config)


if __name__ == "__main__":
    run_pipeline(setup_config())

    