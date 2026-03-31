"""
Summarize election simulation outputs and generate visualization figures.

Aggregates district-level election results produced by the
pipeline into a single summary dataset and generates histogram
visualizations of representation outcomes. Joins election results
with district-level population data from the corresponding settings
files, computes focal-group representation statistics, and writes a
summary CSV along with figures showing the distribution of seats won
across voter models and election methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import geopandas as gpd

import pandas as pd
import matplotlib.pyplot as plt

from pipeline.utils.helpers import parse_district_configs, parse_plan_district_rep_from_path, count_focal_winners, load_json, find_settings_file, get_non_focal_group


def summarize_results(config) -> Path:
    """
    Aggregate election results into a summary csv and produce histogram figures.

    Args:
        config: Parsed config dict.

    Outputs:
        - outputs/summaries/<run_name>_summary/<run_name>_summary.csv: one row per
          (replicate, plan, district) triple, with columns for plan, mode, district_id,
          rep, focal_seats, dynamic population columns from config, and combined_support.
        - outputs/summaries/<run_name>_summary/figures/*.png: one histogram per
          (district_count, seats_per_district, election_method) showing the
          distribution of focal-group seats across modes.

    Returns:
        Path to the summary directory.
    """

    run_name = str(config["run_name"])
    district_configs = parse_district_configs(config["district_configs"])
    focal_group = str(config["focal_group"])
    slate_to_candidates = config.get("slate_to_candidates", {}) or {}

    geodata_path = Path(config["geodata_path"])
    gdf = gpd.read_file(geodata_path)
    # compute statewide focal group proportion from geodata
    vap = sum(gdf[config["population_column"]])
    ivap = sum(gdf[config["pop_of_interest_column"]])
    iprop = ivap/vap

    turnout = config["turnout"]
    cohesion_parameters = config["cohesion_parameters"]
    if len(turnout) != 2:
        raise ValueError("Turnout does not have exactly two keys")
    non_focal_group = get_non_focal_group(config)
    # adjust proportion by differential turnout
    iprop_turnout = iprop*turnout[focal_group] / (iprop*turnout[focal_group] + (1-iprop)*turnout[non_focal_group])

    # compute combined support: share of votes going to focal candidates
    focal_group_cohesion = cohesion_parameters[focal_group]
    non_focal_group_cohesion = cohesion_parameters[non_focal_group]
    i_cs_turnout = iprop_turnout*focal_group_cohesion[focal_group] + (1-iprop_turnout)*non_focal_group_cohesion[focal_group]

    modes = ["slate_pl", "slate_bt", "cambridge"]

    # Input roots
    results_dir = Path("outputs") /f'{run_name}' / "election_results"
    if not results_dir.exists():
        raise FileNotFoundError(f"Could not find election results directory: {results_dir}")

    # Output roots
    summary_dir = Path("outputs") / f'{run_name}' / "summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    figs_dir = summary_dir / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)

    # collect one row per simulation per district
    rows: List[Dict[str, Any]] = []

    method_name_map = {
        "stv": "STV",
        "plurality": "Plurality",
        "irv": "IRV",
    }   

    for dc in district_configs:
        # Settings directory is grouped by district_num per design doc
        settings_dir = Path("outputs") / f'{run_name}' /"settings" / str(dc.num_districts) 

        for mode in modes:
            # Find results files for this mode & district config.
            mode_dir = results_dir / mode
            if not mode_dir.exists():
                continue

            for rf in sorted(mode_dir.glob("*.json")):
                data = load_json(rf)
               
                district_num = int(data.get("district_num", dc.num_districts))
                winners_per_district = int(data.get("winners_per_district", dc.winners))
                voter_mode = str(data.get("voter_mode", mode))
                if district_num != dc.num_districts or winners_per_district != dc.winners or voter_mode != mode:
                    continue

                election_results: List[Dict[str, List[str]]] = data.get("election_results", [])
                profile_files: Optional[List[str]] = data.get("profile_files")

                if profile_files is None:
                    raise ValueError(f"Missing profile_files in results file: {rf}")

                if len(election_results) != len(profile_files):
                    raise ValueError(
                        f"Length mismatch in {rf}: "
                        f"{len(election_results)=} vs {len(profile_files)=}"
                    ) 

                # Build per-simulation rows
                for idx, result in enumerate(election_results):
                    plan = district = rep = None
                    plan, district, rep = parse_plan_district_rep_from_path(profile_files[idx])

                    settings_path = find_settings_file(settings_dir, config['run_name'], plan=plan, district=district)
                    settings_data = load_json(settings_path) if settings_path else {}
                    total_vap = settings_data.get(config["population_column"], None)
                    total_ivap = settings_data.get(config["pop_of_interest_column"], None)
                    # partisan has p_prop_census -- add?

                    for method_key, winners in result.items():
                        focal_seats = count_focal_winners(
                            winners,
                            focal_group,
                            slate_to_candidates,
                        )             
                        rows.append({
                            "run_name": run_name,
                            "plan": plan,
                            "num_districts": district_num,
                            "seats_per_district": winners_per_district,
                            "election_method": method_name_map.get(method_key, method_key.upper()),
                            "mode": mode,
                            "district_id": district,
                            "rep": rep,
                            "simulation_index": idx,
                            "focal_group": focal_group,
                            "focal_seats": focal_seats,
                            config["population_column"]: total_vap,
                            config["pop_of_interest_column"]: total_ivap,
                            "combined_support": i_cs_turnout,
                        })

    df = pd.DataFrame(rows)
    df = df.sort_values(['mode','rep','num_districts','plan','district_id'])

    # Save dataframe
    csv_path = summary_dir / f"{run_name}_summary.csv"
    df.to_csv(csv_path, index=False)

    # aggregate focal seats to the plan level (sum across districts)
    df_plan = (
        df.groupby(
            ["plan", "num_districts", "seats_per_district", "mode", "election_method", "rep"],
            as_index=False
        )
        .agg({"focal_seats": "sum"})
    )

    mode_colors = {
        "cambridge": "#E32636",
        "slate_bt": "#FFBF00",
        "slate_pl": "#8DB600",
    }

    legend_mapping = {
        "slate_bt": "Deliberative",
        "slate_pl": "Impulsive",
        "cambridge": "Cambridge",
    }
    desired_order = ["slate_pl", "slate_bt", "cambridge"]

    # one histogram per (district count, seats, election method) combo
    for (num_dist, seats_per_district, elm), group_distn in df_plan.groupby(["num_districts", "seats_per_district", "election_method"]):
        fig, ax = plt.subplots(figsize=(6, 4))

        # Plot histogram for each mode and track tallest bin
        max_bin_height = 0

        for mode, group_mode in group_distn.groupby("mode"):
            if group_mode["focal_seats"].empty:
                continue

            counts, bins, patches = ax.hist(
                group_mode["focal_seats"],
                bins=range(
                    int(group_mode["focal_seats"].min()),
                    int(group_mode["focal_seats"].max()) + 2
                ),
                align="left",
                edgecolor="gray",
                linewidth=0.5,
                color=mode_colors.get(mode, "xkcd:light gray"),
                alpha=0.5,
                label=mode,
            )

            if len(counts) > 0:
                max_bin_height = max(max_bin_height, counts.max())

        # styling
        for spine in ax.spines.values():
            spine.set_linewidth(0.5)

        total_seats = config["total_seats"]
        ylim = max_bin_height * 1.2 if max_bin_height > 0 else 1

        ax.set_xlim(-1, total_seats + 1)
        ax.set_ylim(0, ylim)
        ax.set_xticks(range(0, total_seats + 1, 1))
        ax.set_xticklabels([str(x) if x % 5 == 0 else "" for x in range(0, total_seats + 1)])
        ax.set_xlabel(f"Seats")
        ax.set_title(f"Representation for {focal_group}-preferred candidates, {num_dist} x {seats_per_district} {elm}")
        ax.tick_params(axis="both", which="major", labelsize=8)

        # legend (modes only, renamed + ordered)
        handles, labels = ax.get_legend_handles_labels()
        handle_map = {label: handle for handle, label in zip(handles, labels) if label in legend_mapping}

        ordered_handles, ordered_labels = [], []
        for mode_key in desired_order:
            if mode_key in handle_map:
                ordered_handles.append(handle_map[mode_key])
                ordered_labels.append(legend_mapping[mode_key])

        ax.legend(ordered_handles, ordered_labels, title="Mode", fontsize=8)

        # add reference lines for proportional representation benchmarks
        color_cs = "xkcd:brownish grey"
        color_iprop = "xkcd:purplish brown"

        i_cs_share = i_cs_turnout * total_seats
        i_share = iprop * total_seats

        if i_cs_share < i_share:
            i_cs_alignment = -0.3
            i_share_alignment = 0.3
            i_cs_ha = "right"
            i_share_ha = "left"
        else:
            i_cs_alignment = 0.3
            i_share_alignment = -0.3
            i_cs_ha = "left"
            i_share_ha = "right"            

        ax.axvline(i_cs_share, color=color_cs, linewidth=1)

        ax.text(
            i_cs_share + i_cs_alignment,
            ylim * 0.90,
            f"Combined support\n{i_cs_turnout*100:.2f}%\n({i_cs_share:.2f} seats)",
            va="center",
            ha=i_cs_ha,
            fontsize=8,
            color=color_cs,
        )

        
        ax.axvline(i_share, color=color_iprop, linestyle=":", linewidth=1)

        ax.text(
            i_share + i_share_alignment,
            ylim * 0.90,
            f"Focal group VAP\n{iprop*100:.2f}%\n({i_share:.2f} seats)",
            va="center",
            ha=i_share_ha,
            fontsize=8,
            color=color_iprop,
        )

        fig_path = figs_dir / f"{run_name}_{num_dist}x{seats_per_district}_{elm}_bymode.png"
        fig.savefig(fig_path, dpi=300, bbox_inches="tight")
        plt.close(fig)

    print(f"[summarize_results] Wrote CSV: {csv_path}")
    print(f"[summarize_results] Figures in: {figs_dir}")
    return summary_dir
