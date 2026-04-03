"""
District plan generation using GerryChain.

Constructs or loads a graph from the configured geodata, runs a recombination
Markov chain for each requested district count, and writes sampled district
assignments to gzip files used by later stages of the pipeline.
"""

import os
import gzip
from pathlib import Path
from functools import partial
import networkx as nx
import jsonlines as jl
import random
from tqdm import tqdm
from gerrychain import Graph, Partition, MarkovChain
from gerrychain.proposals import recom
from gerrychain.accept import always_accept
from gerrychain.updaters import Tally
from pipeline.utils.helpers import load_json

# required for gerrychain reproducibility
os.environ.setdefault("PYTHONHASHSEED", "0")


def generate_districts(config):
    """
    Run a recom markov chain for each district count and write sampled plans to jsonl.

    Args:
        config: Parsed config dict.

    Outputs:
        One jsonl file per district count at
        outputs/districts/<run_name>_chain_out/<run_name>_<n>_districts.jsonl,
        where each line is {"assignment": [<district_label>, ...], "sample": n},
        with assignment being a list of integer district labels sorted by precinct index (0-based).
    """
    random.seed(config['seed'])

    run_name = config["run_name"]
    geodata_path = Path(config["geodata_path"])
    population_column = config["population_column"]
    chain_length = config["chain_length"]
    district_configs = config["district_configs"]

    # load cached graph if it exists, otherwise build from geodata and save
    graph_path = geodata_path.parent / (geodata_path.stem + "_graph.json")
    if graph_path.exists():
        graph = Graph.from_json(str(graph_path))
    else:
        graph = Graph.from_file(str(geodata_path))
        graph.to_json(str(graph_path))

    # relabel nodes as 0-indexed integers so list-based assignment serialization works correctly
    graph = Graph.from_networkx(nx.convert_node_labels_to_integers(graph, first_label=0))

    # one output file per district count
    output_dir = Path(f"outputs/{run_name}/districts")
    output_dir.mkdir(parents=True, exist_ok=True)

    updaters = {"population": Tally(population_column, alias="population")}

    # run a separate markov chain for each district count in the config
    for d_config in district_configs:
        num_districts = d_config["num_districts"]

        partition = Partition.from_random_assignment(
            graph=graph,
            n_parts=num_districts,
            epsilon=0.05,
            pop_col=population_column,
            updaters=updaters,
        )

        # derive target population from the partition itself
        ideal_pop = sum(partition["population"].values()) / num_districts

        # recom proposal: merges two adjacent districts and repartitions
        proposal = partial(
            recom, pop_col=population_column, pop_target=ideal_pop, epsilon=0.05
        )

        chain = MarkovChain(
            proposal=proposal,
            constraints=[],
            accept=always_accept,
            initial_state=partition,
            total_steps=chain_length,
        )

        # write each step as a jsonl record: {"assignment": [...], "sample": n}
        output_path = output_dir / f"{run_name}_{num_districts}_districts.jsonl.gz"
        with gzip.open(output_path, mode="wt", encoding="utf-8") as gz_file:
            writer = jl.Writer(gz_file)
            for sample_num, step in enumerate(tqdm(chain, total=chain_length, desc=f"Generating {num_districts}-district plans"), start=1):
                assignment = list(step.assignment.to_series().sort_index())
                writer.write({"assignment": assignment, "sample": sample_num})
            writer.close()
