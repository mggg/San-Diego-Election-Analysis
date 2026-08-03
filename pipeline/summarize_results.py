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

from pipeline.utils.helpers import parse_district_configs, parse_plan_district_rep_from_path, count_focal_winners, load_run_config, load_json, find_settings_file, get_voter_models
from pipeline.settings_generator import get_group_vap_columns


# --- Shared figure styling ---------------------------------------------------

# Fixed colors / labels so every figure (histograms, bubbles, cross-run) reads
# the same way. Unknown modes fall back to their raw name / a muted "other" ink.
# City of San Diego colors. The pair separates strongly (worst-case
# colour-vision-deficiency dE 40.4, normal-vision 44.3), but the gold sits at
# 1.47:1 against a white page, so bars carry a dark edge (BAR_EDGE_COLOR) and
# every series is named in the legend rather than relying on fill alone.
MODE_COLORS = {
    "slate_pl": "#FFCC00",
    "slate_bt": "#AA0000",
    "cambridge": "#2a78d6",
}

# Dark ink outline: the only thing holding the gold bars against the page.
BAR_EDGE_COLOR = "#52514e"

# Bar fill opacity. Transparency reads nicely where the dodged bars overlap, but
# it composites the fill toward white: at 0.7 the city gold renders around
# 1.3:1 against the page, so the dark edge below does much of the work of
# defining those bars. Raise this if the gold ever looks like empty space.
BAR_ALPHA = 0.7

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

# Seat-axis tick spacing. Seats are integers and these plans are small, so every
# seat count gets its own labelled tick and a bar can be read straight off the
# axis. Raise this for plans with many more seats, where a label per seat would
# crowd (Chicago's 50-seat runs use 5).
X_TICK_STEP = 1
X_AXIS_PAD = 3    # seats of headroom past the largest relevant value

# One type scale for every figure, so histograms, bubble grids, and the cross-run
# comparison can be read side by side without re-learning what a size means.
FIG_TITLE_SIZE = 12
PANEL_TITLE_SIZE = 10
AXIS_LABEL_SIZE = 9
TICK_SIZE = 8
LEGEND_SIZE = 8
SUBTITLE_SIZE = 8

# Every seat axis is the same quantity, so it gets the same name everywhere.
SEAT_AXIS_LABEL = "Seats won"

# Vertical space reserved at the top for the title block and at the bottom for the
# shared legend. Held in INCHES rather than axes fractions: a 3.5" bubble row and
# an 11" cross-run stack then get the same visual gap instead of the tall figure
# reserving several empty inches.
TITLE_BAND_IN = 0.85
LEGEND_BAND_IN = 0.55


def _seat_axis_upper(max_seat: float, total_seats: int) -> int:
    """
    Upper limit for a seat x-axis: just past the largest relevant value (observed
    seats and reference lines), rounded up to a tick and capped at total_seats, so
    plots aren't mostly empty when no group comes close to winning every seat.
    """
    padded = max_seat + X_AXIS_PAD
    ticks_up = -(-int(padded) // X_TICK_STEP)  # ceil division to next whole tick
    return min(ticks_up * X_TICK_STEP, total_seats)


def _prop_line_label(group_label: str, iprop: float, total_seats: int) -> str:
    """
    One wording for the proportional-representation reference line, shared by
    every figure that draws it, so the same dotted line never gets described two
    different ways across the report.
    """
    return f"{group_label} share of VAP: {iprop * 100:.1f}% ({iprop * total_seats:.1f} seats)"


def _layout_title_and_legend_bands(fig, title: str, subtitle: Optional[str] = None) -> None:
    """
    Lay the axes out inside a reserved title band and legend band, then draw the
    title block. Panels are packed by tight_layout within that rect, so a figure
    level legend placed by _bottom_legend cannot land on the axes, the x labels,
    or the panel titles no matter how many panels there are.
    """
    height = fig.get_figheight()
    fig.tight_layout(rect=(0, LEGEND_BAND_IN / height, 1, 1 - TITLE_BAND_IN / height))
    fig.suptitle(title, fontsize=FIG_TITLE_SIZE, fontweight="bold", y=1 - 0.22 / height)
    if subtitle:
        # Run name as an italic grey line under the title, inside the same band.
        fig.text(
            0.5, 1 - 0.52 / height, subtitle, ha="center", va="center",
            fontsize=SUBTITLE_SIZE, color="gray", style="italic",
        )


def _bottom_legend(fig, handles, labels, ncol: int = 2) -> None:
    """Draw the one figure-level legend, centered in the reserved bottom band."""
    if not handles:
        return
    fig.legend(
        handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.01),
        ncol=ncol, fontsize=LEGEND_SIZE, frameon=False,
    )


def _group_label(group: str) -> str:
    """Display name for a group/slate label. Generic: returns the code itself."""
    return str(group)


def _focal_population_share(config, gdf) -> float:
    """
    Focal group's share of the voting-age population.

    Both sides of this ratio must be VAP: pop_of_interest_column is a VAP column,
    so the denominator is population_vap_column, not the total-population column.
    No turnout adjustment is applied -- this is the plain demographic share the
    reference lines are drawn against.
    """
    vap = gdf[config["population_vap_column"]].sum()
    ivap = gdf[config["pop_of_interest_column"]].sum()
    return float(ivap / vap) if vap else 0.0


def _slate_baselines(config, gdf) -> Dict[str, float]:
    """
    Each candidate slate's demographic share of citywide VAP, for the by-slate
    representation panel's reference lines.
    """
    total_vap = float(gdf[config["population_vap_column"]].sum())
    if total_vap <= 0:
        return {slate: 0.0 for slate in config["slate_to_candidates"]}

    group_columns = get_group_vap_columns(config, config["slate_to_candidates"].keys())
    return {
        slate: sum(float(gdf[c].sum()) for c in cols) / total_vap
        for slate, cols in group_columns.items()
    }


def _draw_mode_histograms(ax, group_distn: pd.DataFrame, seat_col: str = "focal_seats") -> float:
    """
    Draw a grouped (dodged) bar histogram with one series per voter model.

    For each integer focal-seat count, each mode gets its own bar placed
    side-by-side, so the series are read by comparison rather than overlapping
    translucently. Modes are ordered by DESIRED_ORDER (with any unexpected modes
    appended) so colors line up left-to-right with the legend.

    Returns:
        The tallest bar height across all modes, so the caller can scale the
        y-axis consistently.
    """
    present_modes = set(group_distn["mode"].unique())
    modes_in_order = [m for m in DESIRED_ORDER if m in present_modes]
    modes_in_order += [m for m in present_modes if m not in DESIRED_ORDER]

    n_modes = len(modes_in_order)
    if n_modes == 0:
        return 0

    # Bars overlap each other by 50%: centres are spaced half a bar width apart,
    # so a group spans 0.84 of a tick and leaves a visible gap to the next seat.
    bar_width = 0.42
    step = bar_width / 2
    max_bin_height = 0

    for i, mode in enumerate(modes_in_order):
        seats = group_distn.loc[group_distn["mode"] == mode, seat_col]
        if seats.empty:
            continue

        counts = seats.value_counts().sort_index()
        offset = (i - (n_modes - 1) / 2) * step

        # Bars overlap by half their width, so some transparency lets the
        # occluded series show through. Kept high (and paired with the dark
        # edge) because the city gold loses contrast fast as alpha drops.
        ax.bar(
            counts.index + offset,
            counts.values,
            width=bar_width,
            edgecolor=BAR_EDGE_COLOR,
            linewidth=0.9,
            color=MODE_COLORS.get(mode, "xkcd:light gray"),
            alpha=BAR_ALPHA,
            label=mode,
        )

        if len(counts) > 0:
            max_bin_height = max(max_bin_height, counts.values.max())

    return max_bin_height


def _style_method_axis(ax, method: str, ylim: float, x_upper: int) -> None:
    """
    Apply spines, limits, ticks, and labels to one election-method panel of the
    by-mode figure. The panel title is the voting rule; the figure-level title
    and run name are drawn once by the caller.
    """
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)

    # x-axis spans 0..x_upper (capped near the data); 20% y headroom.
    ax.set_xlim(-1, x_upper + 1)
    ax.set_ylim(0, ylim)
    ax.set_xticks(range(0, x_upper + 1, X_TICK_STEP))
    ax.set_xticklabels([str(x) for x in range(0, x_upper + 1, X_TICK_STEP)])
    ax.set_xlabel(SEAT_AXIS_LABEL, fontsize=AXIS_LABEL_SIZE)
    ax.set_title(method, fontsize=PANEL_TITLE_SIZE)
    ax.tick_params(axis="both", which="major", labelsize=TICK_SIZE)


def _ordered_mode_handles(ax):
    """Return (handles, labels) for the mode legend in DESIRED_ORDER, renamed via LEGEND_MAPPING."""
    handles, labels = ax.get_legend_handles_labels()
    handle_map = {label: handle for handle, label in zip(handles, labels) if label in LEGEND_MAPPING}

    ordered_handles, ordered_labels = [], []
    for mode_key in DESIRED_ORDER:
        if mode_key in handle_map:
            ordered_handles.append(handle_map[mode_key])
            ordered_labels.append(LEGEND_MAPPING[mode_key])
    return ordered_handles, ordered_labels


def _build_mode_legend(fig, ax, ref_handles=None, ref_labels=None) -> None:
    """
    Draw one figure-level legend of modes (renamed via LEGEND_MAPPING, in
    DESIRED_ORDER), followed by the reference-line entries so their descriptions
    live in the legend instead of as free text overlapping the bars. Handles are
    taken from a single panel -- every panel plots the same series -- and the
    legend sits in the reserved band below the row of panels.
    """
    ordered_handles, ordered_labels = _ordered_mode_handles(ax)
    handles = ordered_handles + list(ref_handles or [])
    labels = ordered_labels + list(ref_labels or [])
    # Two columns: the modes stack in the first, the reference line sits in the
    # second, which keeps the block narrower than the panels it sits under.
    _bottom_legend(fig, handles, labels, ncol=2)


def _draw_reference_lines(ax, config, iprop, label=None):
    """
    Draw the proportional-representation reference line: the seats implied by the
    focal group's share of VAP (no turnout adjustment).

    The description rides on the line's label so it appears in the legend rather
    than as free text over the histogram. Returns (handles, labels) for it.
    """
    group_label = label if label is not None else _group_label(config["focal_group"])
    # Reference lines are annotations, not series identity, so they stay off hue
    # entirely (ink tones, distinguished by linestyle) rather than risking a
    # CVD-ambiguous pair with the mode bars.
    color_iprop = "#52514e"

    if iprop is None:
        return [], []

    i_share = iprop * config["total_seats"]
    iprop_label = _prop_line_label(group_label, iprop, config["total_seats"])
    iprop_line = ax.axvline(
        i_share, color=color_iprop, linestyle=":", linewidth=1, label=iprop_label
    )
    return [iprop_line], [iprop_label]


def _style_slate_axis(ax, config, slate: str, ylim: float, x_upper: int) -> None:
    """Spines, limits, ticks, and a per-slate subplot title for the by-slate panel."""
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)
    ax.set_xlim(-1, x_upper + 1)
    ax.set_ylim(0, ylim)
    ax.set_xticks(range(0, x_upper + 1, X_TICK_STEP))
    ax.set_xticklabels([str(x) for x in range(0, x_upper + 1, X_TICK_STEP)], fontsize=TICK_SIZE)
    ax.set_xlabel(SEAT_AXIS_LABEL, fontsize=AXIS_LABEL_SIZE)
    ax.set_title(_group_label(slate), fontsize=PANEL_TITLE_SIZE)
    ax.tick_params(axis="both", which="major", labelsize=TICK_SIZE)


def _plot_slate_panel(
    group_distn: pd.DataFrame,
    num_dist,
    seats_per_district,
    elm,
    config,
    slate_baselines: Dict[str, float],
    figs_dir: Path,
    run_name: str,
) -> None:
    """
    Create and save one paneled by-slate representation figure: a grid of
    histograms (2x2 for the four San Diego slates), one per candidate slate,
    each showing that slate's seat distribution across modes with its own
    proportional-representation reference line.
    """
    slates = list(config["slate_to_candidates"])
    n = len(slates)
    ncols = 2 if n > 1 else 1
    nrows = (n + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 3.2 * nrows), squeeze=False)
    flat = [ax for row in axes for ax in row]

    # Shared x cap across all slate subplots so they stay comparable: the largest
    # observed seat count or reference line over every slate, padded and capped.
    total_seats = config["total_seats"]
    seat_max = max((group_distn[f"seats_{s}"].max() for s in slates), default=0)
    ref_max = max((slate_baselines.get(s, 0.0) * total_seats for s in slates), default=0)
    x_upper = _seat_axis_upper(max(seat_max, ref_max), total_seats)

    for ax, slate in zip(flat, slates):
        max_bin_height = _draw_mode_histograms(ax, group_distn, seat_col=f"seats_{slate}")
        ylim = max_bin_height * 1.2 if max_bin_height > 0 else 1
        _style_slate_axis(ax, config, slate, ylim, x_upper)
        ref_handles, ref_labels = _draw_reference_lines(
            ax, config, slate_baselines.get(slate), label=_group_label(slate)
        )
        # Per-slate reference values differ, so each subplot names its own line.
        # Pinned to the upper right (not "best", which drifts panel to panel) and
        # given a translucent frame so it never reads as part of the bars.
        ax.legend(
            ref_handles, ref_labels, fontsize=7, loc="upper right",
            frameon=True, framealpha=0.85, borderpad=0.3,
        )

    # Hide any unused cells in the grid.
    for ax in flat[n:]:
        ax.axis("off")

    # The mode legend is the only figure-level one, and it sits in the reserved
    # bottom band like every other figure's.
    handles, labels = _ordered_mode_handles(flat[0])
    _layout_title_and_legend_bands(fig, "Election outcomes by slate", run_name)
    _bottom_legend(fig, handles, labels, ncol=max(1, len(handles)))

    fig_path = figs_dir / f"{run_name}_{num_dist}x{seats_per_district}_{elm}_byslate.png"
    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_slate_representation_panels(
    df_plan: pd.DataFrame,
    config,
    slate_baselines: Dict[str, float],
    figs_dir: Path,
    run_name: str,
) -> None:
    """Produce one by-slate representation panel per (district count, seats, method)."""
    for (num_dist, seats_per_district, elm), group_distn in df_plan.groupby(
        ["num_districts", "seats_per_district", "election_method"]
    ):
        _plot_slate_panel(
            group_distn, num_dist, seats_per_district, elm,
            config, slate_baselines, figs_dir, run_name,
        )


def _plot_method_histogram_panel(
    df_plan: pd.DataFrame,
    num_dist,
    seats_per_district,
    config,
    focal_group: str,
    iprop: Optional[float],
    figs_dir: Path,
    run_name: str,
) -> None:
    """
    Create and save one paneled by-mode figure: a single row of seat-distribution
    histograms, one panel per election method (voting rule), laid out like the
    bubble figures. Panels share x and y limits so the rules are read by direct
    comparison, and the figure carries one title, one run-name subtitle, and one
    legend for the whole row.
    """
    methods = sorted(df_plan["election_method"].unique())
    if not methods:
        return

    fig, axes = plt.subplots(
        1, len(methods), figsize=(4 * len(methods), 3.6), sharey=True, squeeze=False
    )
    axes = axes[0]

    total_seats = config["total_seats"]
    max_seat = max(
        df_plan["focal_seats"].max(),
        iprop * total_seats if iprop is not None else 0,
    )
    x_upper = _seat_axis_upper(max_seat, total_seats)

    # Draw every panel first, then apply the tallest bar across the whole row as a
    # shared y limit -- with sharey the panels must agree anyway, and a common
    # scale is what makes the voting rules comparable.
    bin_heights = [
        _draw_mode_histograms(ax, df_plan[df_plan["election_method"] == method])
        for ax, method in zip(axes, methods)
    ]
    max_bin_height = max(bin_heights, default=0)
    ylim = max_bin_height * 1.2 if max_bin_height > 0 else 1

    ref_handles: List[Any] = []
    ref_labels: List[str] = []
    for ax, method in zip(axes, methods):
        _style_method_axis(ax, method, ylim, x_upper)
        ref_handles, ref_labels = _draw_reference_lines(ax, config, iprop)

    axes[0].set_ylabel("Number of plans", fontsize=AXIS_LABEL_SIZE)

    _layout_title_and_legend_bands(
        fig,
        f"Election outcomes for {_group_label(focal_group)}-preferred candidates",
        run_name,
    )
    _build_mode_legend(fig, axes[0], ref_handles, ref_labels)

    fig_path = figs_dir / f"{run_name}_{num_dist}x{seats_per_district}_bymode.png"
    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def aggregate_to_plan_level(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse the district-level summary table to one row per
    (plan, district config, mode, method, replicate), summing focal seats across
    the districts of each plan. This plan-level focal-seat total is what the
    histograms and bubble plots are distributions over.

    Also sums each per-slate seat column (seats_<slate>) up to the plan, which
    feeds the by-slate representation panel.
    """
    group_keys = ["plan", "num_districts", "seats_per_district", "mode", "election_method", "rep"]
    # "seats_per_district" also matches the seats_<slate> prefix, but it is a
    # grouping key describing the plan -- summing it would report a 9x1 plan as
    # 9 seats per district and poison every figure keyed on that value.
    seat_cols = [
        c for c in df.columns
        if (c == "focal_seats" or c.startswith("seats_")) and c not in group_keys
    ]
    return df.groupby(group_keys, as_index=False).agg({c: "sum" for c in seat_cols})


def summarize_results(config) -> Path:
    """
    Aggregate election results into a summary csv and produce histogram figures.

    Args:
        config: Parsed config dict.

    Outputs:
        - outputs/summaries/<run_name>_summary/<run_name>_summary.csv: one row per
          (replicate, plan, district) triple, with columns for plan, mode, district_id,
          rep, focal_seats, the population columns named in the config, and
          focal_vap_share (the district's focal share of VAP, turnout-free).
        - outputs/summaries/<run_name>_summary/figures/*_bymode.png: one panel per
          (district_count, seats_per_district), a single row of histograms with one
          column per election method, each showing the distribution of focal-group
          seats across modes under that voting rule.
        - outputs/summaries/<run_name>_summary/figures/*_byslate.png: one panel per
          (district_count, seats_per_district, election_method), a grid (2x2 for
          San Diego's four slates) of per-slate seat-distribution histograms with
          each slate's own proportional-representation reference line.

    Returns:
        Path to the summary directory.
    """

    run_name = str(config["run_name"])
    district_configs = parse_district_configs(config["district_configs"])
    focal_group = str(config["focal_group"])
    slate_to_candidates = config.get("slate_to_candidates", {}) or {}

    geodata_path = Path(config["geodata_path"])
    gdf = gpd.read_file(geodata_path)
    # Citywide focal-group share of the voting-age population. Deliberately not
    # turnout-adjusted: the benchmark the plots compare seats against is the
    # group's demographic share of VAP, not its modelled share of the electorate.
    iprop = _focal_population_share(config, gdf)

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
                    total_pop = settings_data.get(config["population_column"], None)
                    total_vap = settings_data.get(config["population_vap_column"], None)
                    total_ivap = settings_data.get(config["pop_of_interest_column"], None)
                    # partisan has p_prop_census -- add?

                    for method_key, winners in result.items():
                        focal_seats = count_focal_winners(
                            winners,
                            focal_group,
                            slate_to_candidates,
                        )
                        row = {
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
                            config["population_column"]: total_pop,
                            config["population_vap_column"]: total_vap,
                            config["pop_of_interest_column"]: total_ivap,
                            # Focal share of VAP for this district, the same
                            # (turnout-free) basis the reference lines use.
                            "focal_vap_share": (
                                total_ivap / total_vap
                                if total_ivap is not None and total_vap else None
                            ),
                        }
                        # Per-slate seat counts feed the by-slate representation panel.
                        for slate in slate_to_candidates:
                            row[f"seats_{slate}"] = count_focal_winners(winners, slate, slate_to_candidates)
                        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.sort_values(['mode','rep','num_districts','plan','district_id'])

    # Save dataframe
    csv_path = summary_dir / f"{run_name}_summary.csv"
    df.to_csv(csv_path, index=False)

    # aggregate focal seats to the plan level (sum across districts)
    df_plan = aggregate_to_plan_level(df)

    # one paneled histogram figure per (district count, seats), a column per method
    for (num_dist, seats_per_district), config_plans in df_plan.groupby(
        ["num_districts", "seats_per_district"]
    ):
        _plot_method_histogram_panel(
            config_plans, num_dist, seats_per_district,
            config, focal_group, iprop, figs_dir, run_name,
        )

    # By-slate proportional-representation panel (one histogram per candidate slate).
    if config.get("slate_to_candidates"):
        plot_slate_representation_panels(
            df_plan, config, _slate_baselines(config, gdf), figs_dir, run_name
        )

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
    # Inverted so the first mode is the TOP row, matching the cross-run figure and
    # the left-to-right order of the histogram bars and legend.
    ax.set_ylim(len(modes_in_order) - 0.5, -0.5)
    ax.set_yticks(range(len(modes_in_order)))
    ax.set_yticklabels([LEGEND_MAPPING.get(m, m) for m in modes_in_order])
    ax.tick_params(axis="both", which="major", labelsize=TICK_SIZE)
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
        ax.set_title(method, fontsize=PANEL_TITLE_SIZE)
        ax.set_xlabel(SEAT_AXIS_LABEL, fontsize=AXIS_LABEL_SIZE)

    _layout_title_and_legend_bands(
        fig,
        f"Election outcomes for {_group_label(config['focal_group'])}-preferred candidates",
        run_name,
    )
    # The modes are named on the y-axis, so the only legend entry is the
    # reference line -- and it goes in the bottom band with everything else's,
    # not floating over the panel titles.
    prop_label = _prop_line_label(_group_label(config["focal_group"]), iprop, config["total_seats"])
    prop_handle = Line2D([0], [0], color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2, label=prop_label)
    _bottom_legend(fig, [prop_handle], [prop_label], ncol=1)

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
        ax.set_yticklabels([LEGEND_MAPPING.get(m, m) for m in modes_in_order], fontsize=TICK_SIZE)
        ax.set_xticks(x_ticks)
        ax.set_xticklabels([str(x) for x in x_ticks], fontsize=TICK_SIZE)
        ax.tick_params(axis="both", which="major", labelsize=TICK_SIZE)
        for spine in ax.spines.values():
            spine.set_linewidth(0.5)
        ax.set_title(label.replace("_", " "), fontsize=PANEL_TITLE_SIZE, fontweight="bold", loc="left")
    axes[-1].set_xlabel(SEAT_AXIS_LABEL, fontsize=AXIS_LABEL_SIZE)

    prop_label = _prop_line_label(_group_label(config["focal_group"]), iprop, total_seats)
    prop_handle = Line2D(
        [0], [0], color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2, label=prop_label,
    )
    _layout_title_and_legend_bands(
        fig,
        f"Election outcomes for {_group_label(config['focal_group'])}-preferred candidates",
    )
    # Same bottom-band legend as the per-run figures; the old upper-right box
    # collided with the first run's panel title.
    _bottom_legend(fig, [prop_handle], [prop_label], ncol=1)

    if output_dir is None:
        output_dir = Path("outputs") / "cross_run_summaries" / "figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    fig_path = output_dir / "combined_bubbles_all_runs.png"
    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)

    print(f"[summarize_results] Wrote cross-run figure: {fig_path}")
    return fig_path

if __name__ == '__main__':
    config = load_run_config("configs/basic.json")
    summarize_results(config)
    plot_combined_bubbles_all_runs(config)