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

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import geopandas as gpd
import numpy as np

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from pipeline.utils.helpers import parse_district_configs, parse_plan_district_rep_from_path, count_focal_winners, load_run_config, load_json, find_settings_file, get_voter_models, DistrictConfig, slate_names
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
    "name_cumulative": "#2a78d6",
    # The pooled average, in a hue no single model uses so a reader never has to
    # check the legend to know whether a bar is one model or all of them. It was
    # falling through to DEFAULT_MODE_COLOR here, which is slate_bt's red -- the
    # figures drew the average and one of its inputs in the same colour.
    "combined": "#6a3d9a",  # COMBINED_MODE, which is defined below this block
}

# Dark ink outline: the only thing holding the gold bars against the page.
BAR_EDGE_COLOR = "#52514e"

# Bar fill opacity. Transparency reads nicely where the dodged bars overlap, but
# it composites the fill toward white: at 0.7 the city gold renders around
# 1.3:1 against the page, so the dark edge below does much of the work of
# defining those bars. Raise this if the gold ever looks like empty space.
BAR_ALPHA = 0.7

# How much of one seat tick a group of overlapping bars spans, whatever the
# number of voter models in it (see _draw_mode_histograms). The gap to the
# next seat is what keeps neighbouring groups readable as separate.
BAR_GROUP_SPAN = 0.84

LEGEND_MAPPING = {
    "slate_pl": "Impulsive",
    "slate_bt": "Deliberative",
    "cambridge": "Cambridge",
    "name_cumulative": "Name Cumulative",
}

# Pseudo-mode pooling occurrences across every voter model into one row.
COMBINED_MODE = "combined"
LEGEND_MAPPING[COMBINED_MODE] = "Combined"

# Synthetic election_method tag for a hybrid_election run's pooled citywide
# total (see aggregate_hybrid_totals / plot_hybrid_combined_totals): a hybrid
# run pairs exactly one voting rule per contest, so there is no real method to
# vary here, just this one pooled system standing in for all of them summed.
HYBRID_COMBINED_METHOD = "Combined"

# Preferred display order for the known voter models; any others sort after.
DESIRED_ORDER = ["slate_pl", "slate_bt", "cambridge", "name_cumulative"]

# Bubble marker areas (points^2): most-frequent cell uses the max, a floor keeps
# rare cells visible.
BUBBLE_MAX_AREA = 150
BUBBLE_MIN_AREA = 10
# San Diego red -- the fallback fill for any voter model with no MODE_COLORS
# entry of its own.
DEFAULT_MODE_COLOR = "#AA0000"
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

# Readable names for the election methods. Keys are the uppercased method names
# the summary table carries (VoteKit class names, so "FASTSTV" for the fast STV
# implementation); values are how a reader should see them. Anything missing
# falls back to a title-cased version of the raw name.
RULE_DISPLAY_NAMES = {
    "STV": "STV",
    "FASTSTV": "STV",
    "IRV": "IRV",
    "PLURALITY": "Plurality",
    "ALASKA": "Alaska",
    "TOPTWO": "Top Two",
    "ALASKATWOPROFILE": "Alaska (two-profile)",
    "TOPTWOTWOPROFILE": "Top Two (two-profile)",
    "PSMD": "PSMD",
}

# The report page's chart for a run defaults to that run's first "system" (see
# docs/js/report.js and the per-chart modules, all of which read systems[0]).
# Sorting plain-alphabetically would default a run like Basic's Cumulative/STV/
# Limited mix to Cumulative, since it precedes "STV" alphabetically -- STV is
# this project's original focus, so it sorts first wherever a run includes it,
# with every other rule alphabetical after it.
SYSTEM_SORT_PRIORITY = {"STV": 0, "FASTSTV": 0}


def _system_sort_key(method: str) -> Tuple[int, str]:
    return (SYSTEM_SORT_PRIORITY.get(str(method).upper(), 1), str(method))


def _rule_display_name(method: str) -> str:
    """Readable name for one election method, e.g. FASTSTV -> "STV"."""
    return RULE_DISPLAY_NAMES.get(str(method).upper(), str(method).title())


def _method_label(method: str, num_districts, seats_per_district) -> str:
    """
    How an election system is named in every figure: the districting shape it was
    run under, then the rule -- "3 X 3 STV", "9 X 1 IRV". The shape is part of the
    name because the same rule under a different magnitude is a different system
    as far as these results are concerned.

    HYBRID_COMBINED_METHOD is the one exception: it pools every contest in a
    hybrid_election run rather than running under one shape, so it's named by
    the run's total seat count instead of a num_districts x seats_per_district
    pair that wouldn't mean anything for it.
    """
    if method == HYBRID_COMBINED_METHOD:
        return f"Combined ({seats_per_district} seats)"
    return f"{num_districts} X {seats_per_district} {_rule_display_name(method)}"


def _figure_subtitle(run_name: str, system_label: str, n_run_systems: int) -> Optional[str]:
    """
    The italic line under a figure's title: the election system it shows, not the
    run name.

    A run that simulated several systems (the alternative-systems config) names
    them on the panels instead, so a figure spanning all of them gets no
    subtitle. The run name comes back only as a scenario tag after a dash, and
    only when the system alone would not identify the figure -- a single-system
    run whose name is not just its system ("Low AAPI Turnout" -> "3 X 3 STV -
    Low AAPI Turnout", while "Basic - 3 X 3" -> "3 X 3 STV").
    """
    scenario = ""
    if n_run_systems == 1 and system_label and system_label.lower() not in run_name.lower():
        scenario = run_name

    if system_label and scenario:
        return f"{system_label} - {scenario}"
    return system_label or scenario or None


def _method_labels_from_summary(df: pd.DataFrame) -> Dict[str, str]:
    """
    {raw method -> display label} for a summary table, reading each method's
    districting shape off its own rows. A method that somehow spans several
    shapes falls back to the bare rule name rather than claiming one of them.
    """
    labels = {}
    for method, sub in df.groupby("election_method"):
        shapes = sub[["num_districts", "seats_per_district"]].drop_duplicates()
        if len(shapes) == 1:
            num_dist, seats = shapes.iloc[0]
            labels[str(method)] = _method_label(method, num_dist, seats)
        else:
            labels[str(method)] = _rule_display_name(method)
    return labels

# Vertical space reserved at the top for the title block and at the bottom for the
# shared legend. Held in INCHES rather than axes fractions: a 3.5" bubble row and
# an 11" cross-run stack then get the same visual gap instead of the tall figure
# reserving several empty inches.
TITLE_BAND_IN = 0.85
LEGEND_BAND_IN = 0.55

# Panel grid for the per-method figures. Four across is about as wide as a page
# can carry before the panels stop being readable, so runs with more voting rules
# than this wrap onto a second row instead of stretching into a 24-inch strip.
MAX_PANEL_COLS = 4
PANEL_W_IN = 4.0
PANEL_H_IN = 3.4


def _panel_grid(n_panels: int) -> Tuple[int, int]:
    """
    (rows, cols) for n panels: one row up to MAX_PANEL_COLS, then wrapped and
    rebalanced so the rows are even (6 panels go 3+3, not 4+2).
    """
    if n_panels <= MAX_PANEL_COLS:
        return 1, max(n_panels, 1)
    nrows = -(-n_panels // MAX_PANEL_COLS)
    return nrows, -(-n_panels // nrows)


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

    Both sides of this ratio must be VAP: the denominator is
    population_vap_column, not the total-population column. No turnout
    adjustment is applied -- this is the plain demographic share the reference
    lines are drawn against.

    The numerator is the focal group's own VAP columns, summed, which is the
    only definition that survives a change of bloc model: pop_of_interest_column
    names one column, and a group spanning several (POC = Black + Hispanic +
    Asian/NHPI, WAIO = White + American Indian + other) cannot be expressed as
    one. Reading it from that field instead drew every reference line at the
    share of whichever single group the project happened to be configured
    around, however the run defined its focal group. Falls back to
    pop_of_interest_column for the legacy two-group model, which has no groups
    to resolve.
    """
    vap = gdf[config["population_vap_column"]].sum()
    if not vap:
        return 0.0

    focal = config.get("focal_group")
    try:
        columns = get_group_vap_columns(config, [focal])[focal]
    except (KeyError, ValueError):
        columns = [config["pop_of_interest_column"]]
    ivap = sum(float(gdf[column].sum()) for column in columns)
    return float(ivap / vap)


def _slate_baselines(config, gdf) -> Dict[str, float]:
    """
    Each candidate slate's demographic share of citywide VAP, for the by-slate
    representation panel's reference lines.
    """
    total_vap = float(gdf[config["population_vap_column"]].sum())
    slates = slate_names(config)
    if total_vap <= 0:
        return {slate: 0.0 for slate in slates}

    group_columns = get_group_vap_columns(config, slates)
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
    # and the width follows from how many there are, so the group always spans
    # BAR_GROUP_SPAN of a tick and leaves a visible gap to the next seat. Fixing
    # the width instead makes the group grow with the series count -- four
    # series at 0.42 would span 1.05 of a tick and collide with the next seat's
    # group. Three series still come out at exactly 0.42.
    bar_width = 2 * BAR_GROUP_SPAN / (n_modes + 1)
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
            color=MODE_COLORS.get(mode, DEFAULT_MODE_COLOR),
            alpha=BAR_ALPHA,
            label=mode,
        )

        if len(counts) > 0:
            max_bin_height = max(max_bin_height, counts.values.max())

    return max_bin_height


def _max_mode_bin_height(group_distn: pd.DataFrame, seat_col: str = "focal_seats") -> float:
    """
    Tallest bar the grouped histogram of this slice would draw, without drawing
    it -- one bar is one (mode, seat count) cell. Lets a caller settle a shared
    y limit across several figures before any of them is rendered.
    """
    if group_distn.empty:
        return 0
    return float(group_distn.groupby("mode")[seat_col].value_counts().max())


def _style_method_axis(ax, method_label: Optional[str], ylim: float, x_upper: int) -> None:
    """
    Apply spines, limits, ticks, and labels to one election-method panel of the
    by-mode figure. The panel title is the readable system name (see
    _method_label), or nothing when method_label is None -- a single-panel figure
    names its system in the subtitle instead of labelling it twice.
    """
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)

    # x-axis spans 0..x_upper (capped near the data); 20% y headroom.
    ax.set_xlim(-1, x_upper + 1)
    ax.set_ylim(0, ylim)
    ax.set_xticks(range(0, x_upper + 1, X_TICK_STEP))
    ax.set_xticklabels([str(x) for x in range(0, x_upper + 1, X_TICK_STEP)])
    ax.set_xlabel(SEAT_AXIS_LABEL, fontsize=AXIS_LABEL_SIZE)
    if method_label:
        ax.set_title(method_label, fontsize=PANEL_TITLE_SIZE)
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


def _method_figs_dir(figs_dir: Path, elm: str) -> Path:
    """
    Per-election-method figures subfolder (figures/<run_name>/<election_method>/),
    created on demand. Figures that span every method for a run (e.g. the
    bubbles-by-method chart) stay at figs_dir's root instead.
    """
    method_dir = figs_dir / elm
    method_dir.mkdir(parents=True, exist_ok=True)
    return method_dir


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
    # One file per election method, so the subtitle names the system (plus the
    # scenario, where the system alone would not identify the run).
    n_systems = int(group_distn["election_method"].nunique())
    subtitle = _figure_subtitle(
        run_name, _method_label(elm, num_dist, seats_per_district), n_systems
    )
    _layout_title_and_legend_bands(fig, "Election outcomes by slate", subtitle)
    _bottom_legend(fig, handles, labels, ncol=max(1, len(handles)))

    fig_path = _method_figs_dir(figs_dir, elm) / f"{run_name}_{num_dist}x{seats_per_district}_byslate.png"
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


def _render_method_histograms(
    df_plan: pd.DataFrame,
    methods: List[str],
    config,
    focal_group: str,
    iprop: Optional[float],
    subtitle: Optional[str],
    fig_path: Path,
    ylim: float,
    x_upper: int,
    method_labels: Dict[str, str],
) -> None:
    """
    Draw and save a by-mode histogram figure covering `methods`: a grid with one
    panel per voting rule (a single row up to MAX_PANEL_COLS, wrapped beyond
    that), one title, one subtitle, and one legend. Panels are titled from
    method_labels, so readers see "9 X 1 IRV" rather than the raw key -- except
    on a single-panel figure, where the subtitle already names the system.

    ylim and x_upper are passed in rather than derived from `methods`, so a
    single-rule figure is drawn on exactly the axes its panel has in the combined
    figure -- the standalone files stay comparable with each other and with the
    panel instead of each rescaling to its own data.
    """
    nrows, ncols = _panel_grid(len(methods))
    fig, axes_grid = plt.subplots(
        nrows, ncols, figsize=(PANEL_W_IN * ncols, PANEL_H_IN * nrows),
        sharey=True, squeeze=False,
    )
    axes = [ax for row in axes_grid for ax in row]

    ref_handles: List[Any] = []
    ref_labels: List[str] = []
    single_panel = len(methods) == 1
    for ax, method in zip(axes, methods):
        _draw_mode_histograms(ax, df_plan[df_plan["election_method"] == method])
        panel_title = None if single_panel else method_labels.get(method, method)
        _style_method_axis(ax, panel_title, ylim, x_upper)
        ref_handles, ref_labels = _draw_reference_lines(ax, config, iprop)

    # Blank out any cells the wrap leaves over in the last row.
    for ax in axes[len(methods):]:
        ax.axis("off")

    # One y label per row: with sharey the tick labels only appear on the left
    # panel anyway, so that is where the row's axis gets named.
    for row in axes_grid:
        row[0].set_ylabel("Number of plans", fontsize=AXIS_LABEL_SIZE)

    _layout_title_and_legend_bands(
        fig,
        f"Election outcomes for {_group_label(focal_group)}-preferred candidates",
        subtitle,
    )
    _build_mode_legend(fig, axes[0], ref_handles, ref_labels)

    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


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
    Write the focal group's by-mode histograms for one districting config: the
    combined panel spanning every voting rule at the run's figure root, plus a
    standalone single-rule figure under each rule's folder.

    The per-rule files are skipped when the run only has one rule, since the
    combined panel is already that figure.
    """
    methods = sorted(df_plan["election_method"].unique())
    if not methods:
        return

    total_seats = config["total_seats"]
    max_seat = max(
        df_plan["focal_seats"].max(),
        iprop * total_seats if iprop is not None else 0,
    )
    x_upper = _seat_axis_upper(max_seat, total_seats)

    # Tallest bar across every rule sets one shared count axis, which is what
    # makes the rules comparable panel-to-panel and file-to-file.
    max_bin_height = max(
        (
            _max_mode_bin_height(df_plan[df_plan["election_method"] == method])
            for method in methods
        ),
        default=0,
    )
    ylim = max_bin_height * 1.2 if max_bin_height > 0 else 1

    method_labels = {m: _method_label(m, num_dist, seats_per_district) for m in methods}
    # A figure spanning several systems names them on its panels, so it carries
    # no system in the subtitle; a single-system figure carries it there.
    combined_system = method_labels[methods[0]] if len(methods) == 1 else ""

    stem = f"{run_name}_{num_dist}x{seats_per_district}_bymode.png"
    _render_method_histograms(
        df_plan, methods, config, focal_group, iprop,
        _figure_subtitle(run_name, combined_system, len(methods)),
        figs_dir / stem, ylim, x_upper, method_labels,
    )

    if len(methods) > 1:
        for method in methods:
            _render_method_histograms(
                df_plan, [method], config, focal_group, iprop,
                _figure_subtitle(run_name, method_labels[method], len(methods)),
                _method_figs_dir(figs_dir, method) / stem, ylim, x_upper, method_labels,
            )


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


def aggregate_hybrid_totals(df_plan: pd.DataFrame, district_configs: List[DistrictConfig]) -> pd.DataFrame:
    """
    Sum focal_seats and per-slate seat columns across every contest in a
    hybrid_election run, to get one combined citywide total per (mode, rep,
    plan). Each contest contributes exactly one election_method
    (hybrid_election requires one voting rule per district_configs entry, see
    match_hybrid_contests), so this is a plain join-and-sum -- no method
    cross-product to resolve.

    A contest with a single sampled "plan" (an at-large entry, num_districts
    == 1, whose district assignment is trivial) is broadcast across every
    sampled district plan sharing its (mode, rep): the same at-large outcome
    paired with each district-plan draw for that replicate.

    Args:
        df_plan: Plan-level summary from aggregate_to_plan_level, covering
            every district_configs entry in the run.
        district_configs: This run's parsed district_configs entries.

    Returns:
        One row per (mode, rep, plan-of-the-largest-entry), with seat columns
        summed across every contest and a "contests" column describing which
        election_method served each entry (e.g. "9x1:IRV; 1x6:FastSTV").
    """
    seat_cols = [c for c in df_plan.columns if c == "focal_seats" or c.startswith("seats_")]
    join_cols = ["mode", "rep"]
    # Broadcasting requires starting from the entry with the most rows (the
    # one with multiple sampled plans); every other entry is joined onto it.
    ordered = sorted(district_configs, key=lambda dc: -dc.num_districts)

    def _entry_rows(dc):
        return df_plan[
            (df_plan["num_districts"] == dc.num_districts)
            & (df_plan["seats_per_district"] == dc.winners)
        ].copy()

    base_dc = ordered[0]
    combined = _entry_rows(base_dc)
    combined["contests"] = combined["election_method"].apply(
        lambda m, dc=base_dc: f"{dc.num_districts}x{dc.winners}:{m}"
    )
    combined = combined.drop(columns=["election_method"])

    for dc in ordered[1:]:
        rows = _entry_rows(dc)
        addend = rows[join_cols + seat_cols + ["election_method"]].rename(
            columns={c: f"{c}__add" for c in seat_cols}
        )
        combined = combined.merge(addend, on=join_cols, how="inner")
        for c in seat_cols:
            combined[c] = combined[c] + combined[f"{c}__add"]
        combined = combined.drop(columns=[f"{c}__add" for c in seat_cols])
        combined["contests"] = combined["contests"] + combined["election_method"].apply(
            lambda m, dc=dc: f"; {dc.num_districts}x{dc.winners}:{m}"
        )
        combined = combined.drop(columns=["election_method"])

    return combined


def expected_figure_count(df_plan: pd.DataFrame, config: dict) -> int:
    """
    Number of figures summarize_results should produce for this run's df_plan.

    Per (district count, seats) shape: one combined bymode histogram and one
    combined bubbles-by-method figure (both span every method in the shape).
    When the shape has more than one election method, _plot_method_histogram_panel
    and _plot_bubbles_for_config each *also* write a standalone bymode/bubbles
    pair per method -- a single-method shape doesn't duplicate itself. On top
    of that, slate_to_candidates being configured adds one byslate panel per
    (shape, method) group, unconditionally (not gated by method count).

    A hybrid_election run also gets a pooled bymode histogram and bubble grid
    over its combined seat total (plus a combined byslate panel when
    slate_to_candidates is set) -- see plot_hybrid_combined_totals.

    Shared with run.py's has_valid_summaries, so both sides agree on what
    "complete" means for the figures/<run_name>/ tree.
    """
    has_slate_figs = bool(config.get("slate_to_candidates"))
    total = 0
    for _shape, group in df_plan.groupby(["num_districts", "seats_per_district"]):
        n_methods = group["election_method"].nunique()
        total += 2  # combined bymode + combined bubbles_by_method
        if n_methods > 1:
            total += n_methods * 2  # per-method standalone bymode + bubbles
        if has_slate_figs:
            total += n_methods  # byslate panel per (shape, method), always
    if config.get("hybrid_election"):
        total += 3 if has_slate_figs else 2
    return total


def summarize_results(config) -> Path:
    """
    Aggregate election results into a summary csv and produce histogram figures.

    Args:
        config: Parsed config dict.

    Outputs:
        - outputs/<run_name>/summaries/<run_name>_summary.csv: one row per
          (replicate, plan, district) triple, with columns for plan, mode, district_id,
          rep, focal_seats, the population columns named in the config, and
          focal_vap_share (the district's focal share of VAP, turnout-free).
        - figures/<run_name>/*_bymode.png: one panel per (district_count,
          seats_per_district), a single row of histograms with one column per
          election method, each showing the distribution of focal-group seats
          across modes under that voting rule. It spans every method, so it sits
          at the run's figure root.
        - figures/<run_name>/<election_method>/*_byslate.png: one panel per
          (district_count, seats_per_district, election_method), a grid (2x2 for
          San Diego's four slates) of per-slate seat-distribution histograms with
          each slate's own proportional-representation reference line.
        - figures/<run_name>/*_bubbles_by_method.png: one bubble-grid figure per
          (district_count, seats_per_district), spanning every election method,
          so it lives at the run's figure root rather than under one method's
          subfolder.

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

    # Output roots. The summary CSV stays under outputs/<run_name>/, but figures
    # live in a separate top-level figures/<run_name>/ tree (organized by
    # election method) so every run's figures can be browsed independently of
    # the (gitignored, disposable) outputs/ working directory.
    summary_dir = Path("outputs") / f'{run_name}' / "summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    figs_dir = Path("figures") / run_name
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
                    # The focal group's own VAP in this district. Every settings
                    # file records group_vap per demographic group, which is the
                    # per-district counterpart of _focal_population_share: a
                    # focal group spanning several columns has no single column
                    # to read, so pop_of_interest_column is only the fallback.
                    focal_vap = (settings_data.get("group_vap") or {}).get(focal_group, total_ivap)
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
                                focal_vap / total_vap
                                if focal_vap is not None and total_vap else None
                            ),
                        }
                        # Per-slate seat counts feed the by-slate representation panel.
                        # Slate names come from slate_names (falls back to blocs when
                        # slate_to_candidates isn't set) -- count_focal_winners still
                        # matches correctly against an empty slate_to_candidates dict,
                        # since is_focal_candidate falls back to id-prefix matching.
                        for slate in slate_names(config):
                            row[f"seats_{slate}"] = count_focal_winners(winners, slate, slate_to_candidates)
                        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.sort_values(['mode','rep','num_districts','plan','district_id'])

    # Save dataframe
    csv_path = summary_dir / f"{run_name}_summary.csv"
    df.to_csv(csv_path, index=False)

    # aggregate focal seats to the plan level (sum across districts)
    df_plan = aggregate_to_plan_level(df)

    # A hybrid_election run's district_configs entries are complementary
    # contests of one government (e.g. 9 IRV districts + a 6-seat STV
    # at-large block), so on top of each contest's own distribution, also
    # write their combined citywide seat total.
    if config.get("hybrid_election"):
        df_combined = aggregate_hybrid_totals(df_plan, district_configs)
        df_combined.insert(0, "run_name", run_name)
        # Named so it can never be swept up by the outputs/*/summaries/*_summary.csv
        # glob that plot_combined_bubbles_all_runs scans for per-run summaries --
        # this file's rows are combined across district_configs entries and don't
        # match that shape (no single election_method/num_districts per row).
        combined_path = summary_dir / f"{run_name}_hybrid_combined_seats.csv"
        df_combined.to_csv(combined_path, index=False)
        print(f"[summarize_results] Wrote combined CSV: {combined_path}")

    # One paneled histogram figure per (district count, seats), a column per
    # method. It spans every voting rule, so like the bubble figure it lives at
    # the run's figure root rather than under any one method's folder.
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

    # Pooled citywide figures for a hybrid_election run -- the combined seat
    # total across every contest, on top of each contest's own figures above.
    if config.get("hybrid_election"):
        plot_hybrid_combined_totals(
            df_combined, config, focal_group, iprop,
            _slate_baselines(config, gdf), figs_dir, run_name,
        )

    # Same aggregates again, as JSON for the report page (pipeline.report_generator).
    write_report_artifacts(
        df, df_plan, config, _slate_baselines(config, gdf), iprop,
        df_combined=df_combined if config.get("hybrid_election") else None,
    )

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


def _method_modes_in_order(mode_values) -> List[str]:
    """
    Row order for a bubble panel showing a single election method: that method's
    own real voter models, plus the pooled Combined row only when there is more
    than one of them to pool.

    _occurrence_counts always synthesizes a Combined row per method, even when
    only one voter model produced it (Cumulative and Limited both run on the
    single name_cumulative model) -- pooling one model with itself is a no-op,
    so that row is dropped rather than shown as a redundant duplicate. Without
    this, a single-model method's panel would also inherit whichever other
    modes happened to run under other methods in the same figure (Impulsive,
    Deliberative, ...), showing up as rows with no bubbles at all.
    """
    real_modes = set(mode_values) - {COMBINED_MODE}
    if len(real_modes) > 1:
        real_modes.add(COMBINED_MODE)
    return _modes_in_display_order(real_modes)


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
            color=MODE_COLORS.get(mode, DEFAULT_MODE_COLOR),
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


def _render_method_bubbles_figure(
    counts, methods, modes_in_order, size_scale, iprop, config, x_upper, subtitle, fig_path,
    method_labels,
):
    """
    Draw and save a bubble figure covering `methods`: focal seats on x, voter
    modes on y, bubble area encoding how many plans produced that focal-seat
    count under that mode, with the proportional-representation line overlaid.

    size_scale and x_upper come from the caller so a standalone single-rule
    figure keeps the run's bubble scale and seat axis, and can be compared with
    the other rules' files directly. Panels are titled from method_labels, so
    readers see "9 X 1 IRV" rather than the raw key -- except on a single-panel
    figure, where the subtitle already names the system.
    """
    nrows, ncols = _panel_grid(len(methods))
    fig, axes_grid = plt.subplots(
        nrows, ncols, figsize=(PANEL_W_IN * ncols, PANEL_H_IN * nrows),
        sharey=True, squeeze=False,
    )
    axes = [ax for row in axes_grid for ax in row]

    single_panel = len(methods) == 1
    for ax, method in zip(axes, methods):
        _draw_method_bubbles(
            ax, counts[counts["election_method"] == method], modes_in_order,
            size_scale, iprop, config, x_upper,
        )
        if not single_panel:
            ax.set_title(method_labels.get(method, method), fontsize=PANEL_TITLE_SIZE)
        ax.set_xlabel(SEAT_AXIS_LABEL, fontsize=AXIS_LABEL_SIZE)

    # Blank out any cells the wrap leaves over in the last row.
    for ax in axes[len(methods):]:
        ax.axis("off")

    _layout_title_and_legend_bands(
        fig,
        f"Election outcomes for {_group_label(config['focal_group'])}-preferred candidates",
        subtitle,
    )
    # The modes are named on the y-axis, so the only legend entry is the
    # reference line -- and it goes in the bottom band with everything else's,
    # not floating over the panel titles.
    prop_label = _prop_line_label(_group_label(config["focal_group"]), iprop, config["total_seats"])
    prop_handle = Line2D([0], [0], color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2, label=prop_label)
    _bottom_legend(fig, [prop_handle], [prop_label], ncol=1)

    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _plot_bubbles_for_config(df_plan, config, iprop, figs_dir, run_name, num_dist, seats_per_district):
    """
    Write the focal group's bubble figures for one districting config: the
    combined figure spanning every voting rule at the run's figure root, plus a
    standalone single-rule figure under each rule's folder.

    The per-rule files are skipped when the run only has one rule, since the
    combined figure is already that figure.
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

    total_seats = config["total_seats"]
    seat_max = max(counts["focal_seats"].max(), iprop * total_seats)
    x_upper = _seat_axis_upper(seat_max, total_seats)

    method_labels = {m: _method_label(m, num_dist, seats_per_district) for m in methods}
    combined_system = method_labels[methods[0]] if len(methods) == 1 else ""

    _render_method_bubbles_figure(
        counts, methods, modes_in_order, size_scale, iprop, config, x_upper,
        _figure_subtitle(run_name, combined_system, len(methods)),
        figs_dir / f"{run_name}_{num_dist}x{seats_per_district}_bubbles_by_method.png",
        method_labels,
    )

    if len(methods) > 1:
        for method in methods:
            # This method's own rows, not the whole run's -- a single-model
            # method (Cumulative, Limited) would otherwise inherit empty rows
            # left over from other methods sharing the combined figure's axis.
            method_counts = counts[counts["election_method"] == method]
            method_modes = _method_modes_in_order(method_counts["mode"].unique())
            _render_method_bubbles_figure(
                counts, [method], method_modes, size_scale, iprop, config, x_upper,
                _figure_subtitle(run_name, method_labels[method], len(methods)),
                _method_figs_dir(figs_dir, method)
                / f"{run_name}_{num_dist}x{seats_per_district}_bubbles.png",
                method_labels,
            )


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


# --- Hybrid combined totals ----------------------------------------------------


def _plot_combined_slate_panel(
    combined: pd.DataFrame,
    contests_label: str,
    config,
    slate_baselines: Dict[str, float],
    figs_dir: Path,
    run_name: str,
) -> None:
    """
    By-slate panel for a hybrid run's pooled citywide total.

    Same grid of per-slate histograms as _plot_slate_panel, but titled and
    named for the combined total rather than any one contest's own shape (that
    labelling depends on a single num_districts x seats_per_district, which
    the pooled total doesn't have).
    """
    slates = list(config["slate_to_candidates"])
    n = len(slates)
    ncols = 2 if n > 1 else 1
    nrows = (n + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 3.2 * nrows), squeeze=False)
    flat = [ax for row in axes for ax in row]

    total_seats = config["total_seats"]
    seat_max = max((combined[f"seats_{s}"].max() for s in slates), default=0)
    ref_max = max((slate_baselines.get(s, 0.0) * total_seats for s in slates), default=0)
    x_upper = _seat_axis_upper(max(seat_max, ref_max), total_seats)

    for ax, slate in zip(flat, slates):
        max_bin_height = _draw_mode_histograms(ax, combined, seat_col=f"seats_{slate}")
        ylim = max_bin_height * 1.2 if max_bin_height > 0 else 1
        _style_slate_axis(ax, config, slate, ylim, x_upper)
        ref_handles, ref_labels = _draw_reference_lines(
            ax, config, slate_baselines.get(slate), label=_group_label(slate)
        )
        ax.legend(
            ref_handles, ref_labels, fontsize=7, loc="upper right",
            frameon=True, framealpha=0.85, borderpad=0.3,
        )

    for ax in flat[n:]:
        ax.axis("off")

    handles, labels = _ordered_mode_handles(flat[0])
    subtitle = _figure_subtitle(run_name, contests_label, 1)
    _layout_title_and_legend_bands(fig, "Election outcomes by slate", subtitle)
    _bottom_legend(fig, handles, labels, ncol=max(1, len(handles)))

    fig_path = figs_dir / f"{run_name}_combined_byslate.png"
    fig.savefig(fig_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_hybrid_combined_totals(
    df_combined: pd.DataFrame,
    config: dict,
    focal_group: str,
    iprop: Optional[float],
    slate_baselines: Dict[str, float],
    figs_dir: Path,
    run_name: str,
) -> None:
    """
    Write the pooled citywide figures for a hybrid_election run: a by-mode
    histogram and a bubble grid over the combined seat total from
    aggregate_hybrid_totals (each district_configs entry's own contest summed
    into one number per (mode, rep, plan)), plus a by-slate panel when the run
    configures slate_to_candidates. These sit alongside each contest's own
    figures, which summarize_results still draws separately -- the combined
    figures are the citywide total on top, not a replacement.

    Reuses the exact panel-drawing code the per-contest figures use, by
    tagging df_combined with the one synthetic HYBRID_COMBINED_METHOD, so the
    combined figures read as one more system in the same visual language
    rather than a different chart style.

    Outputs:
        figures/<run_name>/<run_name>_combined_bymode.png
        figures/<run_name>/<run_name>_combined_bubbles.png
        figures/<run_name>/<run_name>_combined_byslate.png (if slate_to_candidates is set)
    """
    if df_combined.empty:
        return

    contests_label = str(df_combined["contests"].iloc[0]) if "contests" in df_combined.columns else ""
    combined = df_combined.assign(election_method=HYBRID_COMBINED_METHOD)
    method_labels = {HYBRID_COMBINED_METHOD: _method_label(HYBRID_COMBINED_METHOD, 0, config["total_seats"])}
    subtitle = _figure_subtitle(run_name, contests_label, 1)

    total_seats = config["total_seats"]
    max_seat = max(
        combined["focal_seats"].max(),
        iprop * total_seats if iprop is not None else 0,
    )
    x_upper = _seat_axis_upper(max_seat, total_seats)
    max_bin_height = _max_mode_bin_height(combined)
    ylim = max_bin_height * 1.2 if max_bin_height > 0 else 1

    _render_method_histograms(
        combined, [HYBRID_COMBINED_METHOD], config, focal_group, iprop,
        subtitle, figs_dir / f"{run_name}_combined_bymode.png", ylim, x_upper, method_labels,
    )

    counts = _occurrence_counts(combined)
    if not counts.empty:
        modes_in_order = _modes_in_display_order(counts["mode"].unique())
        per_model_counts = counts.loc[counts["mode"] != COMBINED_MODE, "count"]
        max_count = int(per_model_counts.max()) if not per_model_counts.empty else 0
        size_scale = (BUBBLE_MAX_AREA - BUBBLE_MIN_AREA) / max_count if max_count > 0 else 1
        _render_method_bubbles_figure(
            counts, [HYBRID_COMBINED_METHOD], modes_in_order, size_scale, iprop, config,
            x_upper, subtitle, figs_dir / f"{run_name}_combined_bubbles.png", method_labels,
        )

    if config.get("slate_to_candidates"):
        _plot_combined_slate_panel(combined, contests_label, config, slate_baselines, figs_dir, run_name)



# --- Report artifacts ---------------------------------------------------------
#
# The report page draws the same charts as the figures above, but in the browser
# from JSON rather than from matplotlib. Both read the same aggregates, so the
# two cannot drift: everything written here comes from _occurrence_counts and
# friends, not from a parallel calculation.
#
# Records are flat arrays of objects -- the shape d3.group / d3.rollup expect --
# rather than nested by system or mode, so a chart can regroup them however it
# needs without the artifact committing to one layout.


def run_slug(run_name: str) -> str:
    """URL-safe id for a run, used for its data directory and DOM ids."""
    return re.sub(r"[^a-z0-9]+", "-", str(run_name).lower()).strip("-")


def _focal_seat_records(df_plan: pd.DataFrame, config) -> List[Dict[str, Any]]:
    """
    One record per (system, voter model, seat count): how many sampled plans
    elected that many focal-preferred candidates.

    Straight from _occurrence_counts, so it carries the pooled COMBINED_MODE row
    the bubble figures show -- an average across voter models, not a sum, which
    the "pooled" flag marks so the page can say so.
    """
    counts = _occurrence_counts(df_plan)
    if counts.empty:
        return []

    shapes = (
        df_plan[["election_method", "num_districts", "seats_per_district"]]
        .drop_duplicates()
        .set_index("election_method")
    )
    records = []
    for method, group in counts.groupby("election_method"):
        num_dist = int(shapes.loc[method, "num_districts"])
        winners = int(shapes.loc[method, "seats_per_district"])
        label = _method_label(method, num_dist, winners)
        for mode, mode_rows in group.groupby("mode"):
            total = float(mode_rows["count"].sum())
            for _, row in mode_rows.sort_values("focal_seats").iterrows():
                records.append({
                    "system": str(method),
                    "systemLabel": label,
                    "numDistricts": num_dist,
                    "winners": winners,
                    "mode": str(mode),
                    "modeLabel": LEGEND_MAPPING.get(mode, str(mode)),
                    "pooled": mode == COMBINED_MODE,
                    "seats": int(row["focal_seats"]),
                    "plans": float(row["count"]),
                    "share": float(row["count"]) / total if total else 0.0,
                })
    return records


def _slate_seat_records(df_plan: pd.DataFrame, config, slate_baselines) -> List[Dict[str, Any]]:
    """
    The same shape as _focal_seat_records, but per candidate slate rather than
    the focal group alone -- the data behind the by-slate panels.

    Carries the pooled COMBINED_MODE row too, averaged across voter models the
    way _occurrence_counts does it, so a chart can be switched from the focal
    group to any slate without losing the combined reading. The focal group is one
    of the slates, and its rows here reproduce _focal_seat_records exactly.
    """
    slates = slate_names(config)
    records = []
    for (num_dist, winners, method), group in df_plan.groupby(
        ["num_districts", "seats_per_district", "election_method"]
    ):
        label = _method_label(method, num_dist, winners)
        for slate in slates:
            column = f"seats_{slate}"
            if column not in group.columns:
                continue
            pooled: Dict[int, float] = {}
            n_modes = group["mode"].nunique() or 1
            for mode, mode_rows in group.groupby("mode"):
                counts = mode_rows[column].value_counts().sort_index()
                total = float(counts.sum())
                for seats, plans in counts.items():
                    records.append({
                        "system": str(method),
                        "systemLabel": label,
                        "numDistricts": int(num_dist),
                        "winners": int(winners),
                        "mode": str(mode),
                        "modeLabel": LEGEND_MAPPING.get(mode, str(mode)),
                        "pooled": False,
                        "slate": str(slate),
                        "slateLabel": _group_label(slate),
                        "slateVapShare": float(slate_baselines.get(slate, 0.0)),
                        "seats": int(seats),
                        "plans": float(plans),
                        "share": float(plans) / total if total else 0.0,
                    })
                    # A (mode, seats) cell no model reached counts as zero for it,
                    # not as absent, so divide by the model count rather than by
                    # however many models happened to land on that seat total.
                    pooled[int(seats)] = pooled.get(int(seats), 0.0) + float(plans) / n_modes

            pooled_total = sum(pooled.values())
            for seats, plans in sorted(pooled.items()):
                records.append({
                    "system": str(method),
                    "systemLabel": label,
                    "numDistricts": int(num_dist),
                    "winners": int(winners),
                    "mode": COMBINED_MODE,
                    "modeLabel": LEGEND_MAPPING[COMBINED_MODE],
                    "pooled": True,
                    "slate": str(slate),
                    "slateLabel": _group_label(slate),
                    "slateVapShare": float(slate_baselines.get(slate, 0.0)),
                    "seats": int(seats),
                    "plans": plans,
                    "share": plans / pooled_total if pooled_total else 0.0,
                })
    return records


def _run_metadata(df: pd.DataFrame, df_plan: pd.DataFrame, config, iprop: float,
                  slate_baselines: Dict[str, float]) -> Dict[str, Any]:
    """
    Everything a chart needs that isn't a data point: labels, axis bounds, the
    proportional-representation line, and the shape of the run behind it.
    """
    run_name = str(config["run_name"])
    total_seats = int(config["total_seats"])
    seat_max = max(int(df_plan["focal_seats"].max()), 0)
    seat_upper = _seat_axis_upper(max(seat_max, iprop * total_seats), total_seats)

    district_configs = []
    for (num_dist, winners), group in df_plan.groupby(["num_districts", "seats_per_district"]):
        systems = [
            {"id": str(m), "label": _method_label(m, num_dist, winners)}
            for m in sorted(group["election_method"].unique(), key=_system_sort_key)
        ]
        source = next(
            (d for d in config["district_configs"] if d["num_districts"] == num_dist), {}
        )
        district_configs.append({
            "numDistricts": int(num_dist),
            "winners": int(winners),
            "systems": systems,
            "candidatePoolMax": source.get("candidate_pool_max"),
            "candidatePoolMean": source.get("candidate_pool_mean"),
            "plans": int(group["plan"].nunique()),
            "electionsPerSystem": int(len(df) / max(df["election_method"].nunique(), 1)),
        })

    # The pooled row is added by _occurrence_counts, so it is not in df_plan's
    # modes -- but it is in the records, and the page has to know how to label it.
    modes = _modes_in_display_order(set(df_plan["mode"].unique()) | {COMBINED_MODE})
    return {
        "runName": run_name,
        "slug": run_slug(run_name),
        "focalGroup": str(config["focal_group"]),
        "focalGroupLabel": _group_label(config["focal_group"]),
        "totalSeats": total_seats,
        "focalVapShare": float(iprop),
        "proportionalSeats": float(iprop) * total_seats,
        "proportionalLabel": _prop_line_label(
            _group_label(config["focal_group"]), iprop, total_seats
        ),
        "seatMax": int(seat_upper),
        "seatTicks": list(range(0, seat_upper + 1, X_TICK_STEP)),
        "districtConfigs": district_configs,
        "voterModels": [
            {"id": m, "label": LEGEND_MAPPING.get(m, m), "pooled": m == COMBINED_MODE}
            for m in modes
        ],
        "slates": [
            {"id": s, "label": _group_label(s), "vapShare": float(slate_baselines.get(s, 0.0))}
            for s in slate_names(config)
        ],
        "turnout": config.get("turnout"),
        "primaryTurnout": config.get("primary_turnout"),
        "replicates": int(config.get("num_reps", 0)),
        "subsamples": int(config.get("num_subsamples", 0)),
    }


def report_artifacts_dir(run_name: str) -> Path:
    """Where a run's report artifacts live; read by pipeline.report_generator."""
    return Path("outputs") / str(run_name) / "summaries" / "report"



# --- Candidate-availability boxplots ------------------------------------------
#
# The browser data behind the coalition boxplot this project and the Chicago one
# both use. Districts are not comparable across plans by id -- district 5 in plan
# 0 is not the same geography as district 5 in plan 200 -- so each plan's
# districts are ranked by the slate's share of district VAP, low to high, and the
# ranks pool across plans. Each box is one rank's distribution of that share.
#
# The fill is one of two readings of the same boxes:
#
#   availability  every (plan, district) pair, coloured by the average number of
#                 that slate's candidates on the ballot. settings_generator drops
#                 a slate from slate_to_candidates when it draws zero candidates,
#                 so the absence is the datum.
#   win rate      Chicago's original reading: restricted to districts where the
#                 slate actually had a candidate, coloured by the share of those
#                 districts it won. Restricting matters -- a rank the slate never
#                 contested would otherwise read as a rank it always lost.
#
# Ranks are computed before the restriction, so rank 3 means the same district
# position under both readings and the two colourings sit on comparable boxes.
# Win rate pools voter models and replicates, as the notebook does.

BOX_WHISKER_IQR = 1.5


def _box_stats(values: np.ndarray) -> Optional[Dict[str, Any]]:
    """
    Tukey box statistics, matching matplotlib's boxplot defaults exactly.

    Computed here rather than in the browser so a box on the page and its
    matplotlib counterpart cannot disagree about where a whisker ends.
    """
    arr = np.sort(np.asarray(values, dtype=float))
    if arr.size == 0:
        return None

    q1, median, q3 = (float(v) for v in np.percentile(arr, [25, 50, 75]))
    iqr = q3 - q1
    inside = arr[(arr >= q1 - BOX_WHISKER_IQR * iqr) & (arr <= q3 + BOX_WHISKER_IQR * iqr)]
    low = float(inside.min()) if inside.size else q1
    high = float(inside.max()) if inside.size else q3

    return {
        "n": int(arr.size),
        "q1": round(q1, 4),
        "median": round(median, 4),
        "q3": round(q3, 4),
        "low": round(low, 4),
        "high": round(high, 4),
        "mean": round(float(arr.mean()), 4),
        "outliers": [round(float(v), 4) for v in arr[(arr < low) | (arr > high)]],
    }


def _district_facts(config, num_districts: int) -> pd.DataFrame:
    """
    Per-(plan, district) VAP share and candidate count for every slate, read out
    of the settings files that produced the elections.

    Returns an empty frame when the settings are gone: they are the only record
    of which slates were on which ballot, so a run whose settings have been
    cleaned up cannot have this figure rebuilt from anything else.
    """
    settings_dir = Path("outputs") / config["run_name"] / "settings" / str(num_districts)
    if not settings_dir.is_dir():
        return pd.DataFrame()

    total_vap_col = config["population_vap_column"]
    slates = slate_names(config)
    rows = []
    for path in sorted(settings_dir.rglob("*.json")):
        plan, district, _ = parse_plan_district_rep_from_path(path.name)
        if plan is None or district is None:
            continue
        data = load_json(path)
        total_vap = data.get(total_vap_col) or 0
        available = data.get("slate_to_candidates") or {}
        row = {"plan": plan, "district_id": district}
        for slate in slates:
            group_vap = (data.get("group_vap") or {}).get(slate, 0.0)
            row[f"vap_{slate}"] = (group_vap / total_vap) if total_vap else 0.0
            row[f"cands_{slate}"] = len(available.get(slate, []))
        rows.append(row)
    return pd.DataFrame(rows)


def _ranked_by_vap(facts: pd.DataFrame, slate: str) -> pd.DataFrame:
    """Each plan's districts ranked by this slate's VAP share, 1 = lowest."""
    ranked = facts.copy()
    ranked["rank"] = ranked.groupby("plan")[f"vap_{slate}"].rank(method="first").astype(int)
    return ranked


def _rank_boxes(ranked: pd.DataFrame, slate: str) -> Dict[str, Any]:
    """VAP-share percentages pooled by rank, as box statistics."""
    column = f"vap_{slate}"
    ranks = []
    for rank, group in ranked.groupby("rank"):
        stats = _box_stats(group[column].to_numpy() * 100)
        if stats:
            ranks.append({"rank": int(rank), **stats})
    if not ranks:
        return {}
    return {
        "ranks": ranks,
        "overallMean": round(float(ranked[column].mean() * 100), 4),
        "plans": int(ranked["plan"].nunique()),
    }


def _availability_artifact(df: pd.DataFrame, config,
                           slate_baselines: Dict[str, float]) -> Dict[str, Any]:
    """
    Boxes and colourings for every (slate, districting shape) in the run.

    The boxes come in two variants -- all districts, and only those the slate
    contested -- and are shared by every colouring that sits on them, so a system
    contributes a colour array rather than a second copy of the same distribution.
    """
    slates = slate_names(config)
    boxes: List[Dict[str, Any]] = []
    colors: List[Dict[str, Any]] = []

    shapes = df[["num_districts", "seats_per_district"]].drop_duplicates().astype(int)
    for num_dist, winners in shapes.itertuples(index=False):
        facts = _district_facts(config, int(num_dist))
        if facts.empty:
            print(
                f"[summarize_results] No settings for {num_dist} districts; "
                "skipping candidate-availability data."
            )
            continue

        shaped = df[(df["num_districts"] == num_dist) & (df["seats_per_district"] == winners)]
        for slate in slates:
            ranked = _ranked_by_vap(facts, slate)
            contested = ranked[ranked[f"cands_{slate}"] > 0]
            common = {
                "slate": str(slate),
                "slateLabel": _group_label(slate),
                "slateVapShare": float(slate_baselines.get(slate, 0.0)),
                "numDistricts": int(num_dist),
                "winners": int(winners),
            }

            for restricted, subset in ((False, ranked), (True, contested)):
                stats = _rank_boxes(subset, slate)
                if stats:
                    boxes.append({**common, "restricted": restricted, **stats})

            # Availability: every district, coloured by candidates fielded. The
            # totals travel with the average so a reader can see what it is an
            # average of -- 0.42 per district is 42 candidates over 100 of them.
            by_rank = ranked.groupby("rank")[f"cands_{slate}"]
            counts, totals, districts = by_rank.mean(), by_rank.sum(), by_rank.size()
            colors.append({
                **common,
                "metric": "availability",
                "label": f"Avg. {_group_label(slate)} candidates per district",
                "format": "count",
                "zeroLabel": f"No {_group_label(slate)} candidate at this rank",
                "restricted": False,
                "values": [{
                    "rank": int(r),
                    "value": round(float(v), 4),
                    "candidates": int(totals.loc[r]),
                    "districts": int(districts.loc[r]),
                } for r, v in counts.items()],
            })

            # Win rate: contested districts only, one colouring per system.
            seat_col = f"seats_{slate}"
            if seat_col not in shaped.columns or contested.empty:
                continue
            keys = contested[["plan", "district_id", "rank"]]
            for method, rows in shaped.groupby("election_method"):
                merged = rows[["plan", "district_id", seat_col]].merge(
                    keys, on=["plan", "district_id"], how="inner"
                )
                if merged.empty:
                    continue
                # The denominator is district elections, not districts: every
                # (plan, district) the slate contested appears once per voter
                # model and replicate. Both counts are carried so the percentage
                # can be shown as the fraction it actually is.
                won = merged.groupby("rank")[seat_col].apply(lambda s: int((s > 0).sum()))
                contests = merged.groupby("rank")[seat_col].size()
                rates = (won / contests * 100)
                colors.append({
                    **common,
                    "metric": "winRate",
                    "system": str(method),
                    "systemLabel": _method_label(method, num_dist, winners),
                    "label": "Win rate",
                    "format": "percent",
                    "zeroLabel": f"No {_group_label(slate)} winner at this rank",
                    "restricted": True,
                    "values": [{
                        "rank": int(r),
                        "value": round(float(v), 4),
                        "won": int(won.loc[r]),
                        "contests": int(contests.loc[r]),
                    } for r, v in rates.items()],
                })

    return {"boxes": boxes, "colors": colors}


def _tag_hybrid_combined(df_combined: pd.DataFrame, total_seats: int) -> pd.DataFrame:
    """
    df_combined (aggregate_hybrid_totals' output) reshaped to df_plan's columns,
    tagged as one synthetic system (HYBRID_COMBINED_METHOD) under a sentinel
    shape (num_districts=0).

    Concatenating this onto df_plan before building the report records makes
    the combined total flow through _run_metadata, _focal_seat_records, and
    _slate_seat_records exactly like any real contest: it becomes one more
    entry in districtConfigs, with its own systems[] entry -- which is all
    report.js's dropdown needs to offer it alongside each contest's own system
    (see docs/js/report.js's buildControls, which flattens every
    districtConfigs entry's systems into one <select>).
    """
    if df_combined.empty:
        return df_combined
    return df_combined.assign(
        election_method=HYBRID_COMBINED_METHOD,
        num_districts=0,
        seats_per_district=total_seats,
    )


def write_report_artifacts(df: pd.DataFrame, df_plan: pd.DataFrame, config,
                           slate_baselines: Dict[str, float], iprop: float,
                           df_combined: Optional[pd.DataFrame] = None) -> Path:
    """
    Write this run's JSON artifacts for the report page.

    Args:
        df: The district-level summary table.
        df_plan: Its plan-level aggregate (aggregate_to_plan_level).
        config: Parsed config dict.
        slate_baselines: Each slate's share of citywide VAP (_slate_baselines).
        iprop: The focal group's share of citywide VAP.
        df_combined: A hybrid_election run's pooled citywide seat total
            (aggregate_hybrid_totals), when the run has one. Folded into
            df_plan as one more system before the records are built -- see
            _tag_hybrid_combined.

    Outputs:
        outputs/<run_name>/summaries/report/{run,focal_seats,slate_seats}.json, and
        availability.json when the run's settings are still on disk to build it
        from.
    """
    out_dir = report_artifacts_dir(config["run_name"])
    out_dir.mkdir(parents=True, exist_ok=True)

    report_df_plan = df_plan
    if df_combined is not None and not df_combined.empty:
        report_df_plan = pd.concat(
            [df_plan, _tag_hybrid_combined(df_combined, config["total_seats"])],
            ignore_index=True,
        )

    payloads = {
        "run.json": _run_metadata(df, report_df_plan, config, iprop, slate_baselines),
        "focal_seats.json": _focal_seat_records(report_df_plan, config),
        "slate_seats.json": _slate_seat_records(report_df_plan, config, slate_baselines),
    }

    # Optional, not part of the required set: it needs the settings files, and a
    # run whose settings have been cleaned up should still publish everything
    # else rather than dropping off the page entirely.
    availability = _availability_artifact(df, config, slate_baselines)
    if availability["boxes"]:
        payloads["availability.json"] = availability
    for name, payload in payloads.items():
        with open(out_dir / name, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)

    print(f"[summarize_results] Report artifacts in: {out_dir}")
    return out_dir


# --- Cross-run summaries ------------------------------------------------------


def _per_mode_distributions_for_run(summary_csv: Path) -> List[Tuple[str, pd.DataFrame]]:
    """
    Read one run's summary CSV and return its per-mode focal-seat distributions
    (each a frame of [mode, focal_seats, count], incl. the pooled COMBINED_MODE
    row), collapsed across district configs.

    A run that simulated several voting rules yields one entry per rule, each
    tagged with a " - <RULE>" label suffix, so a multi-rule run like the
    alternative-systems config is compared rule by rule in the cross-run figure
    rather than pooled into a single panel that hides the differences between
    them. A single-rule run yields one entry with an empty suffix.

    Returns an empty list if the CSV has nothing to plot.
    """
    df = pd.read_csv(summary_csv)
    if df.empty:
        return []
    counts = _occurrence_counts(aggregate_to_plan_level(df))
    if counts.empty:
        return []

    methods = sorted(counts["election_method"].unique())
    if len(methods) <= 1:
        pooled = counts.groupby(["mode", "focal_seats"], as_index=False)["count"].sum()
        return [("", pooled)]

    labels = _method_labels_from_summary(df)
    return [
        (
            f" - {labels.get(method, _rule_display_name(method))}",
            counts[counts["election_method"] == method]
            .groupby(["mode", "focal_seats"], as_index=False)["count"]
            .sum(),
        )
        for method in methods
    ]


def plot_combined_bubbles_all_runs(config, output_dir=None, exclude_runs=None) -> Optional[Path]:
    """
    Compare every completed run in one stacked bubble figure: each panel is a run
    (one y-row per voter mode plus a pooled "Combined" row), bubble area encodes
    how many plans produced each focal-seat count, and a dotted line marks the
    focal group's proportional-representation seat share.

    Runs that simulated several voting rules contribute one panel per rule,
    labelled "<run> - <RULE>", so the rules are compared against each other and
    against the other runs on one shared seat axis.

    Scans outputs/*/summaries/*_summary.csv for finished runs.

    Args:
        config: Any run's parsed config; used for the seat-axis range and the
            population-share reference line (shared across runs).
        output_dir: Where to write the figure. Defaults to
            figures/cross_run_summaries.
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
        # One panel per voting rule for multi-rule runs, one for the run otherwise.
        for suffix, per_mode in _per_mode_distributions_for_run(path):
            runs.append((label + suffix, per_mode))

    if not runs:
        print("[summarize_results] No completed runs found for cross-run bubble plot.")
        return None

    # "basic"-prefixed runs first, then alphabetical. Because a split run's panels
    # all share the run name as their label prefix, this also keeps each run's
    # rules together and in rule order.
    runs.sort(key=lambda r: (not r[0].lower().startswith("basic"), r[0]))

    iprop = _focal_population_share(config, gpd.read_file(Path(config["geodata_path"])))
    observed_max_seats = max(int(c["focal_seats"].max()) for _, c in runs)
    total_seats = max(int(config["total_seats"]), observed_max_seats)
    i_share = iprop * total_seats

    x_upper = _seat_axis_upper(max(observed_max_seats, i_share), total_seats)
    x_ticks = range(0, x_upper + 1, X_TICK_STEP)

    # One column, always: the whole point of this figure is that every panel's
    # seat axis lines up vertically. Panels get shorter once a split run pushes
    # the count up, so the stack stays a readable shape instead of running to two
    # feet of canvas.
    n_runs = len(runs)
    panel_h = 2.2 if n_runs <= 6 else 1.7
    fig, axes = plt.subplots(
        n_runs, 1, figsize=(10, max(panel_h * n_runs, 2.5)),
        gridspec_kw={"hspace": 0.8 if n_runs <= 6 else 0.6}, squeeze=False,
    )
    axes = [a[0] for a in axes]

    for ax, (label, per_mode) in zip(axes, runs):
        # This panel's own rows, not every run/method's -- a single-model
        # method (Cumulative, Limited) would otherwise inherit empty rows left
        # over from other panels in the same stacked figure.
        panel_modes = _method_modes_in_order(per_mode["mode"].unique())
        y_index = {mode: i for i, mode in enumerate(panel_modes)}
        for mode in panel_modes:
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
                color=MODE_COLORS.get(mode, DEFAULT_MODE_COLOR),
                alpha=0.7,
                edgecolor="gray",
                linewidth=0.5,
            )
        ax.axvline(i_share, color=PROP_LINE_COLOR, linestyle=":", linewidth=1.2)
        ax.set_xlim(-1, x_upper + 1)
        ax.set_ylim(len(panel_modes) - 0.5, -0.5)  # inverted: first mode on top
        ax.set_yticks(range(len(panel_modes)))
        ax.set_yticklabels([LEGEND_MAPPING.get(m, m) for m in panel_modes], fontsize=TICK_SIZE)
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
        output_dir = Path("figures") / "cross_run_summaries"
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