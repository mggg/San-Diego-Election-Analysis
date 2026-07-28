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
from typing import Any, Dict, List, Optional, Tuple
import geopandas as gpd

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from pipeline.utils.helpers import parse_district_configs, parse_plan_district_rep_from_path, count_focal_winners, load_json, find_settings_file, get_non_focal_group, get_voter_models


# --- Shared figure styling ---------------------------------------------------

# Fixed colors / labels so every figure (histograms, bubbles, cross-run) reads
# the same way. Unknown modes fall back to their raw name / a muted "other" ink.
MODE_COLORS = {
    "slate_pl": "#41B6E6",
    "slate_bt": "#E4002B",
    "cambridge": "#96690a",
}

LEGEND_MAPPING = {
    "slate_pl": "Impulsive",
    "slate_bt": "Deliberative",
    "cambridge": "Cambridge",
}

# Pseudo-mode pooling occurrences across every voter model into one row.
COMBINED_MODE = "combined"
LEGEND_MAPPING[COMBINED_MODE] = "Combined"

# Preferred display order for the known voter models; any others sort after.
DESIRED_ORDER = ["slate_pl", "slate_bt", "cambridge"]

# Bubble marker areas (points^2): most-frequent cell uses the max, a floor keeps
# rare cells visible.
BUBBLE_MAX_AREA = 150
BUBBLE_MIN_AREA = 10
BUBBLE_COLOR = "#898781"  # fallback fill for a mode not in MODE_COLORS
PROP_LINE_COLOR = "#52514e"  # focal-group proportional-representation reference line

X_TICK_STEP = 5   # seat-axis tick spacing
X_AXIS_PAD = 3    # seats of headroom past the largest relevant value


def _seat_axis_upper(max_seat: float, total_seats: int) -> int:
    """
    Upper limit for a seat x-axis: just past the largest relevant value (observed
    seats and reference lines), rounded up to a tick and capped at total_seats, so
    plots aren't mostly empty when no group comes close to winning every seat.
    """
    padded = max_seat + X_AXIS_PAD
    ticks_up = -(-int(padded) // X_TICK_STEP)  # ceil division to next whole tick
    return min(ticks_up * X_TICK_STEP, total_seats)


def _group_label(group: str) -> str:
    """Display name for a group/slate label. Generic: returns the code itself."""
    return str(group)


def _focal_population_share(config, gdf) -> float:
    """Focal group's share of population: pop_of_interest_column / population_column."""
    vap = gdf[config["population_column"]].sum()
    ivap = gdf[config["pop_of_interest_column"]].sum()
    return float(ivap / vap) if vap else 0.0


def aggregate_to_plan_level(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse the district-level summary table to one row per
    (plan, district config, mode, method, replicate), summing focal seats across
    the districts of each plan. This plan-level focal-seat total is what the
    histograms and bubble plots are distributions over.
    """
    return (
        df.groupby(
            ["plan", "num_districts", "seats_per_district", "mode", "election_method", "rep"],
            as_index=False,
        )
        .agg({"focal_seats": "sum"})
    )


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

    # "Combined support" (the share of votes going to focal candidates once
    # differential turnout and cross-bloc cohesion are folded in) is only defined
    # for the two-group focal-vs-non-focal model. Under the coalition / multi-bloc
    # model (turnout keyed by more than two blocs) there is no single non-focal
    # group, so we skip it and leave combined_support unset.
    if len(turnout) == 2:
        non_focal_group = get_non_focal_group(config)
        # adjust proportion by differential turnout
        iprop_turnout = iprop*turnout[focal_group] / (iprop*turnout[focal_group] + (1-iprop)*turnout[non_focal_group])
        # compute combined support: share of votes going to focal candidates
        focal_group_cohesion = cohesion_parameters[focal_group]
        non_focal_group_cohesion = cohesion_parameters[non_focal_group]
        i_cs_turnout = iprop_turnout*focal_group_cohesion[focal_group] + (1-iprop_turnout)*non_focal_group_cohesion[focal_group]
    else:
        iprop_turnout = None
        i_cs_turnout = None

    modes = get_voter_models(config)

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
    df_plan = aggregate_to_plan_level(df)

    # Shared styling (histograms + bubbles read the same); unknown modes fall
    # back to their raw name / a muted fill.
    mode_colors = MODE_COLORS
    legend_mapping = LEGEND_MAPPING
    # Order legends by the config's voter_models.
    desired_order = get_voter_models(config)

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
        handle_map = {label: handle for handle, label in zip(handles, labels)}

        ordered_handles, ordered_labels = [], []
        for mode_key in desired_order:
            if mode_key in handle_map:
                ordered_handles.append(handle_map[mode_key])
                ordered_labels.append(legend_mapping.get(mode_key, mode_key))

        ax.legend(ordered_handles, ordered_labels, title="Mode", fontsize=8)

        # add reference lines for proportional representation benchmarks
        color_cs = "xkcd:brownish grey"
        color_iprop = "xkcd:purplish brown"

        i_share = iprop * total_seats

        # The combined-support benchmark only exists for the two-group model
        # (see i_cs_turnout above); under the multi-bloc model just draw the
        # focal-group VAP line.
        if i_cs_turnout is not None:
            i_cs_share = i_cs_turnout * total_seats

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
        else:
            i_share_alignment = 0.3
            i_share_ha = "left"

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

    # Bubble grid (mode x seats, area = occurrence count) per districting config.
    plot_representation_bubbles(df_plan, config, focal_group, iprop, figs_dir, run_name)

    print(f"[summarize_results] Wrote CSV: {csv_path}")
    print(f"[summarize_results] Figures in: {figs_dir}")
    return summary_dir


# --- Bubble plots -------------------------------------------------------------


def _occurrence_counts(df_plan: pd.DataFrame) -> pd.DataFrame:
    """
    Count plan-level occurrences per (election_method, mode, focal_seats), plus a
    pooled COMBINED_MODE row that averages those counts across every voter model
    so the figure can show the combined distribution on the same scale as the
    individual models.
    """
    per_mode = (
        df_plan.groupby(["election_method", "mode", "focal_seats"])
        .size()
        .reset_index(name="count")
    )
    # Average across models: sum the counts then divide by the number of voter
    # models for that method, so seats where only some models landed aren't
    # over-counted (a missing (mode, seats) cell counts as zero, not absent).
    n_models = per_mode.groupby("election_method")["mode"].transform("nunique")
    combined = (
        per_mode.assign(count=per_mode["count"] / n_models)
        .groupby(["election_method", "focal_seats"], as_index=False)["count"]
        .sum()
    )
    combined["mode"] = COMBINED_MODE
    return pd.concat([per_mode, combined], ignore_index=True)


def _modes_in_display_order(present_modes) -> List[str]:
    """Individual modes in DESIRED_ORDER (unknown ones after), COMBINED pinned last."""
    present = set(present_modes)
    individual = [m for m in DESIRED_ORDER if m in present]
    individual += [m for m in present if m not in DESIRED_ORDER and m != COMBINED_MODE]
    return individual + ([COMBINED_MODE] if COMBINED_MODE in present else [])


def _draw_method_bubbles(ax, method_counts, modes_in_order, size_scale, iprop, config, x_upper):
    """
    Draw the bubble grid (mode x seats, area sized by occurrence count) for one
    election method, overlay the focal-group proportional-representation line, and
    style the axes.
    """
    y_index = {mode: i for i, mode in enumerate(modes_in_order)}
    for mode in modes_in_order:
        sub = method_counts[method_counts["mode"] == mode]
        if sub.empty:
            continue
        ax.scatter(
            sub["focal_seats"],
            [y_index[mode]] * len(sub),
            s=BUBBLE_MIN_AREA + sub["count"] * size_scale,
            color=MODE_COLORS.get(mode, BUBBLE_COLOR),
            alpha=0.7,
            edgecolor="gray",
            linewidth=0.5,
        )

    i_share = iprop * config["total_seats"]
    ax.axvline(i_share, color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2)

    ax.set_xlim(-1, x_upper + 1)
    ax.set_xticks(range(0, x_upper + 1, X_TICK_STEP))
    ax.set_xticklabels([str(x) for x in range(0, x_upper + 1, X_TICK_STEP)])
    ax.set_ylim(-0.5, len(modes_in_order) - 0.5)
    ax.set_yticks(range(len(modes_in_order)))
    ax.set_yticklabels([LEGEND_MAPPING.get(m, m) for m in modes_in_order])
    ax.tick_params(axis="both", which="major", labelsize=8)
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)


def _plot_bubbles_for_config(df_plan, config, iprop, figs_dir, run_name, num_dist, seats_per_district):
    """
    Single figure with one bubble subplot per election method. Focal seats on x,
    voter modes on y; bubble area encodes how many plans produced that focal-seat
    count under that mode. A dotted line marks the focal group's
    proportional-representation seat share; subplots share the y-axis.
    """
    counts = _occurrence_counts(df_plan)
    if counts.empty:
        return

    methods = sorted(counts["election_method"].unique())
    modes_in_order = _modes_in_display_order(counts["mode"].unique())

    # Scale bubble area from the per-model counts only; the pooled "Combined" row
    # sums those, so including it would shrink every individual bubble.
    per_model_counts = counts.loc[counts["mode"] != COMBINED_MODE, "count"]
    max_count = int(per_model_counts.max()) if not per_model_counts.empty else 0
    size_scale = (BUBBLE_MAX_AREA - BUBBLE_MIN_AREA) / max_count if max_count > 0 else 0

    fig, axes = plt.subplots(
        1, len(methods), figsize=(4 * len(methods), 3.5), sharey=True, squeeze=False
    )
    axes = axes[0]

    total_seats = config["total_seats"]
    seat_max = max(counts["focal_seats"].max(), iprop * total_seats)
    x_upper = _seat_axis_upper(seat_max, total_seats)

    for ax, method in zip(axes, methods):
        _draw_method_bubbles(
            ax, counts[counts["election_method"] == method], modes_in_order,
            size_scale, iprop, config, x_upper,
        )
        ax.set_title(method, fontsize=10)
        ax.set_xlabel("Seats won", fontsize=9)

    fig.subplots_adjust(top=0.78, bottom=0.15)
    fig.suptitle(
        f"Election outcomes for {_group_label(config['focal_group'])}-preferred candidates",
        fontsize=11, fontweight="bold", y=0.98,
    )
    fig.text(0.5, 0.88, run_name, ha="center", fontsize=8, color="gray", style="italic")

    prop_handle = Line2D(
        [0], [0], color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2,
        label=f"Proportional representation ({iprop * 100:.1f}%)",
    )
    fig.legend(handles=[prop_handle], loc="lower center", bbox_to_anchor=(0.5, 0.80), fontsize=7, frameon=True)

    fig_path = figs_dir / f"{run_name}_{num_dist}x{seats_per_district}_bubbles_by_method.png"
    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_representation_bubbles(df_plan, config, focal_group, iprop, figs_dir, run_name):
    """
    One bubble figure per districting configuration (district count x magnitude),
    each with one subplot per election method.
    """
    for (num_dist, seats_per_district), config_plans in df_plan.groupby(
        ["num_districts", "seats_per_district"]
    ):
        _plot_bubbles_for_config(
            config_plans, config, iprop, figs_dir, run_name, num_dist, seats_per_district
        )


# --- Cross-run summaries ------------------------------------------------------


def _per_mode_distribution_for_run(summary_csv: Path) -> Optional[pd.DataFrame]:
    """
    Read one run's summary CSV and return its per-mode focal-seat distribution
    (columns [mode, focal_seats, count], incl. the pooled COMBINED_MODE row),
    collapsing across election methods and district configs. None if empty.
    """
    df = pd.read_csv(summary_csv)
    if df.empty:
        return None
    counts = _occurrence_counts(aggregate_to_plan_level(df))
    if counts.empty:
        return None
    return counts.groupby(["mode", "focal_seats"], as_index=False)["count"].sum()


def plot_combined_bubbles_all_runs(config, output_dir=None, exclude_runs=None) -> Optional[Path]:
    """
    Compare every completed run in one stacked bubble figure: each run is a
    subplot (one y-row per voter mode plus a pooled "Combined" row), bubble area
    encodes how many plans produced each focal-seat count, and a dotted line marks
    the focal group's proportional-representation seat share.

    Scans outputs/*/summaries/*_summary.csv for finished runs.

    Args:
        config: Any run's parsed config; used for the seat-axis range and the
            population-share reference line (shared across runs).
        output_dir: Where to write the figure. Defaults to
            outputs/cross_run_summaries/figures.
        exclude_runs: Run names (matched case-insensitively as substrings) to omit.

    Returns:
        Path to the written figure, or None if no completed runs were found.
    """
    summary_paths = sorted(Path("outputs").glob("*/summaries/*_summary.csv"))
    exclude_lower = [e.lower() for e in (exclude_runs or [])]

    runs: List[Tuple[str, pd.DataFrame]] = []
    for path in summary_paths:
        label = str(pd.read_csv(path, usecols=["run_name"])["run_name"].iloc[0])
        if any(ex in label.lower() for ex in exclude_lower):
            print(f"[summarize_results] Excluding run from cross-run bubble plot: {label}")
            continue
        per_mode = _per_mode_distribution_for_run(path)
        if per_mode is not None:
            runs.append((label, per_mode))

    if not runs:
        print("[summarize_results] No completed runs found for cross-run bubble plot.")
        return None

    # "basic"-prefixed runs first, then alphabetical, for a stable panel order.
    runs.sort(key=lambda r: (not r[0].lower().startswith("basic"), r[0]))

    iprop = _focal_population_share(config, gpd.read_file(Path(config["geodata_path"])))
    observed_max_seats = max(int(c["focal_seats"].max()) for _, c in runs)
    total_seats = max(int(config["total_seats"]), observed_max_seats)
    i_share = iprop * total_seats

    all_modes: set = set()
    for _, c in runs:
        all_modes.update(c["mode"].unique())
    modes_in_order = _modes_in_display_order(all_modes)

    x_upper = _seat_axis_upper(max(observed_max_seats, i_share), total_seats)
    x_ticks = range(0, x_upper + 1, X_TICK_STEP)

    n_runs = len(runs)
    fig, axes = plt.subplots(
        n_runs, 1, figsize=(10, max(2.2 * n_runs, 2.5)), gridspec_kw={"hspace": 0.8}, squeeze=False
    )
    axes = [a[0] for a in axes]

    y_index = {mode: i for i, mode in enumerate(modes_in_order)}
    for ax, (label, per_mode) in zip(axes, runs):
        for mode in modes_in_order:
            sub = per_mode[per_mode["mode"] == mode]
            if sub.empty:
                continue
            # Scale each row independently so the most-common seat count in this
            # mode fills BUBBLE_MAX_AREA.
            row_max = sub["count"].max()
            row_scale = (BUBBLE_MAX_AREA - BUBBLE_MIN_AREA) / row_max if row_max > 0 else 0
            ax.scatter(
                sub["focal_seats"],
                [y_index[mode]] * len(sub),
                s=BUBBLE_MIN_AREA + sub["count"] * row_scale,
                color=MODE_COLORS.get(mode, BUBBLE_COLOR),
                alpha=0.7,
                edgecolor="gray",
                linewidth=0.5,
            )
        ax.axvline(i_share, color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2)
        ax.set_xlim(-1, x_upper + 1)
        ax.set_ylim(len(modes_in_order) - 0.5, -0.5)  # inverted: first mode on top
        ax.set_yticks(range(len(modes_in_order)))
        ax.set_yticklabels([LEGEND_MAPPING.get(m, m) for m in modes_in_order], fontsize=8)
        ax.set_xticks(x_ticks)
        ax.set_xticklabels([str(x) for x in x_ticks], fontsize=8)
        ax.tick_params(axis="both", which="major", labelsize=8)
        for spine in ax.spines.values():
            spine.set_linewidth(0.5)
        ax.set_title(label.replace("_", " "), fontsize=10, fontweight="bold", loc="left")
    axes[-1].set_xlabel("Seats won", fontsize=9, fontweight="bold")

    prop_handle = Line2D(
        [0], [0], color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2,
        label=f"Proportional representation ({iprop * 100:.1f}%)",
    )
    fig.suptitle(
        f"Election outcomes for {_group_label(config['focal_group'])}-preferred candidates",
        fontsize=12, fontweight="bold",
    )
    fig.legend(handles=[prop_handle], loc="upper right", fontsize=8, frameon=True)

    if output_dir is None:
        output_dir = Path("outputs") / "cross_run_summaries" / "figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    fig_path = output_dir / "combined_bubbles_all_runs.png"
    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)

    print(f"[summarize_results] Wrote cross-run figure: {fig_path}")
    return fig_path
