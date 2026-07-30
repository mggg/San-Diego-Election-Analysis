"""
Block-level VAP/CVAP Data Generator
=============================================

Builds a block-level GeoPackage for a given city with Voting Age Population
(VAP) broken down into six mutually-exclusive race/ethnicity categories:

    BVAP / HVAP / AVAP / AMINVAP / OVAP / WVAP

Citizen Voting Age Population (CVAP) can also be estimated, but is off by
default: it is a modelled quantity (VAP discounted by ACS citizenship rates)
rather than a published count, and no pipeline stage consumes it. Set
geometry_data.include_cvap to true to compute and export the *CVAP columns.

Blocks and their VAP come from one of two interchangeable sources:

    * a local GerryDB block view GeoPackage (`block_source_path` in the config),
      which already carries the six VAP categories, total population by race, and
      precinct-level election results, plus a published dual graph; or
    * TIGER/Line geometries joined to PL 94-171 tables pulled from the Census API.

Either way the output schema is identical.

Which of those blocks count as "in the city" is decided either by a spatial rule
against the city boundary (`selection_predicate`) or, when `districtr_plan_path`
is configured, by a Districtr plan's own assignment — the plan's block set, with
each block's plan district carried through to the output.

Pipeline:
    1. download_blocks()          TIGER/Line 2020 block geometries (county-filtered)
       load_gerrydb_blocks()      ... or blocks read from a GerryDB view
    2. download_boundary()        City "place" polygon from TIGER, or, when the
                                  config names a council district layer,
                                  load_city_districts() dissolved into one polygon
    3. download_pl_blocks()       PL 94-171 P1 + P3 + P4 tables per block
    4. download_acs_citizenship() ACS 5-year B05003 citizenship rates per tract
    5. build_vap_categories()     partition VAP into the six categories
    6. build_cvap_categories()    per-block citizenship rate lookup (by tract)
    7. estimate_cvap_by_block()   discount each VAP category by its tract rate
    8. select_blocks_in_city()    keep the blocks that fall inside the city
       select_blocks_by_plan()    ... or exactly the blocks a Districtr plan uses
    8b. assign_districts()        tag each block with its council district (optional)
    9. export_to_gpkg()           write the final GeoPackage

Every download step uses lazy caching: if the cache file already exists it is
loaded from disk instead of being re-downloaded.

Sources:
    - GerryDB block view: geometries, VAP, race, elections, dual graph (local file)
    - TIGER/Line 2020: block & place geometries (census.gov)
    - Census API PL 94-171 (Tables P1, P3, P4): total population + VAP at block level
    - Census API ACS 5-year (Table B05003): citizenship rates at tract level

Methodology follows VAP-CVAP.pdf: VAP is partitioned exactly into six groups,
then each group is multiplied by its ACS tract-level citizenship rate (falling
back to the statewide rate where the tract denominator is too small).
"""

import argparse
import os
import json
import sqlite3
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import geopandas as gpd
import requests
import shapely
from census import Census
from dotenv import load_dotenv
from gerrychain import Graph

from pipeline.utils.helpers import load_run_config


# --------------------------------------------------------------------------- #
# Project paths (fixed)
# --------------------------------------------------------------------------- #
PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PIPELINE_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

# --------------------------------------------------------------------------- #
# TIGER/Line base URL
# --------------------------------------------------------------------------- #
TIGER = "https://www2.census.gov/geo/tiger/TIGER2020"

# --------------------------------------------------------------------------- #
# Census vintages
# --------------------------------------------------------------------------- #
DECENNIAL_YEAR = 2020
ACS_YEAR = 2020

# --------------------------------------------------------------------------- #
# PL 94-171 variable inventory
# P1_001N = total population
# P3/P4 = voting-age population by race/ethnicity
# --------------------------------------------------------------------------- #
P1_ALL = ["p1_001n"]
P3_ALL = [f"p3_{i:03d}n" for i in range(1, 72)]
P4_ALL = [f"p4_{i:03d}n" for i in range(1, 72)]
RAW_VARS = P1_ALL + P3_ALL + P4_ALL        # 143 variables total

# --------------------------------------------------------------------------- #
# VAP category definitions (from VAP-CVAP.pdf)
# --------------------------------------------------------------------------- #
BVAP_VARS = [
    "p3_004n", "p3_011n", "p3_016n", "p3_017n", "p3_018n", "p3_019n",
    "p3_027n", "p3_028n", "p3_029n", "p3_030n", "p3_037n", "p3_038n",
    "p3_039n", "p3_040n", "p3_041n", "p3_042n", "p3_048n", "p3_049n",
    "p3_050n", "p3_051n", "p3_052n", "p3_053n", "p3_058n", "p3_059n",
    "p3_060n", "p3_061n", "p3_064n", "p3_065n", "p3_066n", "p3_067n",
    "p3_069n", "p3_071n",
]
assert len(BVAP_VARS) == 32

HVAP_PAIRS = [
    ("p3_003n", "p4_005n"), ("p3_005n", "p4_007n"), ("p3_006n", "p4_008n"),
    ("p3_007n", "p4_009n"), ("p3_008n", "p4_010n"), ("p3_012n", "p4_014n"),
    ("p3_013n", "p4_015n"), ("p3_014n", "p4_016n"), ("p3_015n", "p4_017n"),
    ("p3_020n", "p4_022n"), ("p3_021n", "p4_023n"), ("p3_022n", "p4_024n"),
    ("p3_023n", "p4_025n"), ("p3_024n", "p4_026n"), ("p3_025n", "p4_027n"),
    ("p3_031n", "p4_033n"), ("p3_032n", "p4_034n"), ("p3_033n", "p4_035n"),
    ("p3_034n", "p4_036n"), ("p3_035n", "p4_037n"), ("p3_036n", "p4_038n"),
    ("p3_043n", "p4_045n"), ("p3_044n", "p4_046n"), ("p3_045n", "p4_047n"),
    ("p3_046n", "p4_048n"), ("p3_054n", "p4_056n"), ("p3_055n", "p4_057n"),
    ("p3_056n", "p4_058n"), ("p3_057n", "p4_059n"), ("p3_062n", "p4_064n"),
    ("p3_068n", "p4_070n"),
]
assert len(HVAP_PAIRS) == 31

AVAP_VARS = [
    "p4_008n", "p4_009n", "p4_015n", "p4_016n", "p4_022n", "p4_023n",
    "p4_025n", "p4_026n", "p4_027n", "p4_033n", "p4_034n", "p4_036n",
    "p4_037n", "p4_038n", "p4_045n", "p4_046n", "p4_047n", "p4_048n",
    "p4_056n", "p4_057n", "p4_058n", "p4_059n", "p4_064n", "p4_070n",
]
AMINVAP_VARS = ["p4_007n", "p4_014n", "p4_024n", "p4_035n"]
OVAP_VARS = ["p4_010n", "p4_017n"]
WVAP_VARS = ["p4_005n"]
assert (len(AVAP_VARS), len(AMINVAP_VARS), len(OVAP_VARS), len(WVAP_VARS)) == (24, 4, 2, 1)

CATEGORIES = ["BVAP", "HVAP", "AVAP", "AMINVAP", "OVAP", "WVAP"]
CVAP_CATEGORIES = ["BCVAP", "HCVAP", "ACVAP", "AMINCVAP", "OCVAP", "WCVAP"]

# --------------------------------------------------------------------------- #
# ACS B05003 citizenship-rate definitions
# --------------------------------------------------------------------------- #
ACS_RATE_TABLES = {
    "B": "BVAP",
    "I": "HVAP",
    "D": "AVAP",
    "C": "AMINVAP",
    "H": "WVAP",
}
CVAP_ROWS = ["009", "011", "020", "022"]
VAP_ROWS = ["008", "019"]

DISCOUNT_MAP = {
    "BVAP": "BVAP",
    "HVAP": "HVAP",
    "AVAP": "AVAP",
    "AMINVAP": "AMINVAP",
    "WVAP": "WVAP",
    "OVAP": "WVAP",
}

VAP_FLOOR = 20

# --------------------------------------------------------------------------- #
# GerryDB block view
#
# A GerryDB view already publishes the same six-category VAP partition this
# module builds from PL 94-171, under the snake_case names used downstream, so
# loading one just means renaming into the internal schema. `path` is the
# 15-digit block GEOID.
# --------------------------------------------------------------------------- #
GERRYDB_ID_COLUMN = "path"
GERRYDB_GRAPH_TABLE = "gerrydb_graph_edge"
GERRYDB_VAP_COLUMNS = {
    "total_vap_20": "VAP",
    "bvap_20": "BVAP",
    "hvap_20": "HVAP",
    "asian_nhpi_vap_20": "AVAP",
    "amin_vap_20": "AMINVAP",
    "other_vap_20": "OVAP",
    "white_vap_20": "WVAP",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _chunks(seq, n):
    """Yield successive n-sized chunks (the API allows <= 50 variables per call)."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def b05003_vars(suffix):
    """Return the B05003<suffix> variable names needed for CVAP and VAP."""
    rows = sorted(set(CVAP_ROWS + VAP_ROWS))
    return [f"B05003{suffix}_{r}E" for r in rows]


def _place_paths(place_name):
    """Build place-specific cache paths derived from the place name."""
    geom = place_name.replace(" ", "_").lower()
    return {
        "blocks_cache": DATA_DIR / f"{geom}_blocks_raw.gpkg",
        "boundary_cache": DATA_DIR / f"{geom}_boundary.gpkg",
        "pl_cache": DATA_DIR / f"{geom}_pl_blocks_p1p3p4.parquet",
        "acs_cache_dir": DATA_DIR / "acs_tracts",
    }


# --------------------------------------------------------------------------- #
# 1. Block geometries
# --------------------------------------------------------------------------- #
def download_blocks(state_fips, counties, cache_path):
    """Download (or load from cache) TIGER block geometries filtered to the given counties.

    Args:
        state_fips: Two-digit state FIPS code (e.g. "06" for California).
        counties: List of three-digit county FIPS codes to keep.
        cache_path: GeoPackage path used for lazy caching.

    Returns:
        GeoDataFrame indexed by GEOID with COUNTYFP20, TRACTCE20, ALAND20, geometry.
    """
    cache_path = Path(cache_path)
    if cache_path.exists():
        print("Loading blocks from cache …")
        block_gdf = gpd.read_file(cache_path)
        if "GEOID" in block_gdf.columns:
            block_gdf = block_gdf.set_index("GEOID")
        return block_gdf

    blocks_url = f"{TIGER}/TABBLOCK20/tl_2020_{state_fips}_tabblock20.zip"
    print("Downloading statewide blocks (this is the big one) …")
    state_blocks = gpd.read_file(blocks_url)

    block_gdf = state_blocks[state_blocks["COUNTYFP20"].isin(counties)].copy()
    block_gdf = block_gdf.rename(columns={"GEOID20": "GEOID"}).set_index("GEOID")
    block_gdf = block_gdf[["COUNTYFP20", "TRACTCE20", "ALAND20", "geometry"]]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    block_gdf.to_file(cache_path, driver="GPKG")
    print(f"✓ Saved {len(block_gdf):,} blocks to {cache_path}")
    return block_gdf


# --------------------------------------------------------------------------- #
# 1b. Blocks from a GerryDB view
# --------------------------------------------------------------------------- #
def load_gerrydb_blocks(geo):
    """Load blocks for the configured counties from a local GerryDB block view.

    Replaces steps 1, 3, and 5 of the TIGER route in one read: the view already
    carries geometries, the six-category VAP partition, population by race, and
    election results. Columns are renamed into this module's internal schema so
    everything downstream (CVAP estimation, export) is unchanged.

    Args:
        geo: The config["geometry_data"] block. Reads "block_source_path",
            optional "block_source_layer" (defaults to the file stem),
            "state_fips", and "counties".

    Returns:
        (blocks, extra_columns) where blocks is a GeoDataFrame indexed by the
        15-digit block GEOID carrying geometry, total_pop_20, VAP and the six
        VAP categories, plus any pass-through columns; extra_columns lists those
        pass-through column names (race population, elections).

    Raises:
        FileNotFoundError: If the view file is missing.
        ValueError: If expected columns are absent, the county filter is empty,
            or the six categories do not partition VAP.
    """
    source = Path(geo["block_source_path"])
    if not source.exists():
        raise FileNotFoundError(f"GerryDB block view not found at {source}.")

    layer = geo.get("block_source_layer", source.stem)
    state_fips = geo["state_fips"]
    counties = geo["counties"]

    # `path` is state+county+tract+block, so a prefix match is the county filter;
    # pushing it into the driver avoids reading a statewide view into memory.
    where = " OR ".join(
        f"{GERRYDB_ID_COLUMN} LIKE '{state_fips}{county}%'" for county in counties
    )
    blocks = gpd.read_file(source, layer=layer, where=where)
    if blocks.empty:
        raise ValueError(
            f"No blocks in {source} matched counties {counties} in state {state_fips}."
        )

    missing = [c for c in (*GERRYDB_VAP_COLUMNS, "total_pop_20") if c not in blocks.columns]
    if missing:
        raise ValueError(f"GerryDB view {source} is missing expected columns: {missing}")

    blocks = blocks.rename(columns={GERRYDB_ID_COLUMN: "GEOID"}).set_index("GEOID")
    blocks = blocks.rename(columns=GERRYDB_VAP_COLUMNS)

    # Same invariant build_vap_categories() asserts on the PL route.
    max_error = (blocks[CATEGORIES].sum(axis=1) - blocks["VAP"]).abs().max()
    if max_error > 1e-6:
        raise ValueError(
            f"Categories do NOT partition VAP in {source} (max error {max_error})."
        )

    known = {"geometry", "total_pop_20", "VAP", *CATEGORIES}
    extra_columns = [c for c in blocks.columns if c not in known]

    print(f"Loaded {len(blocks):,} blocks from {source.name} (layer '{layer}')")
    print(f"  carrying {len(extra_columns)} extra columns (race population, elections)")
    print(f"  total population = {blocks['total_pop_20'].sum():,.0f}, VAP = {blocks['VAP'].sum():,.0f}")
    return blocks, extra_columns


def load_gerrydb_graph(geo, geoids):
    """Build the dual graph published with a GerryDB view, restricted to `geoids`.

    Preferred over deriving adjacency from the exported geometries: it is the
    graph the view was built with, and it carries shared-perimeter weights.
    Nodes are positional (node i is row i of `geoids`) because the district and
    settings stages line assignments up against geodata rows.

    Args:
        geo: The config["geometry_data"] block.
        geoids: Block GEOIDs, in the row order of the exported geodata.

    Returns:
        A networkx Graph over range(len(geoids)), or None when the view ships no
        graph table.
    """
    source = Path(geo["block_source_path"])
    with sqlite3.connect(source) as connection:
        tables = pd.read_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            connection,
            params=(GERRYDB_GRAPH_TABLE,),
        )
        if tables.empty:
            print(f"  {source.name} ships no '{GERRYDB_GRAPH_TABLE}' table; "
                  "falling back to geometry-derived adjacency.")
            return None
        edges = pd.read_sql(f"SELECT path_1, path_2, weights FROM {GERRYDB_GRAPH_TABLE}", connection)

    position = {geoid: i for i, geoid in enumerate(geoids)}
    inside = edges["path_1"].isin(position) & edges["path_2"].isin(position)
    edges = edges[inside]

    graph = nx.Graph()
    graph.add_nodes_from(range(len(geoids)))
    for path_1, path_2, weights in edges.itertuples(index=False):
        attrs = json.loads(weights) if isinstance(weights, str) else {}
        graph.add_edge(position[path_1], position[path_2], **attrs)

    print(f"  published dual graph: {graph.number_of_edges():,} edges among the selected blocks")
    return graph


# --------------------------------------------------------------------------- #
# 1c. Blocks from a Districtr plan
# --------------------------------------------------------------------------- #
def load_districtr_plan(geo):
    """Load a Districtr plan's block-to-district assignment.

    A plan carries its own answer to "which blocks are in this city" — the keys
    of its assignment — which sidesteps choosing a spatial predicate against a
    boundary. Prefers a local export, falling back to the Districtr API, because
    a browser export that fails writes the literal string "undefined".

    Args:
        geo: The config["geometry_data"] block. Reads "districtr_plan_path" and
            optionally "districtr_plan_id" for the API fallback.

    Returns:
        Series mapping 15-digit block GEOID to the district number the plan
        displays, or None when no plan is configured.

    Raises:
        FileNotFoundError: If no usable export exists and no plan id is given.
    """
    plan_path = geo.get("districtr_plan_path")
    plan_id = geo.get("districtr_plan_id")
    if not plan_path and not plan_id:
        return None

    payload = None
    if plan_path:
        plan_path = Path(plan_path)
        # A failed export is 9 bytes of "undefined", so require a plausible size.
        if plan_path.exists() and plan_path.stat().st_size > 1000:
            payload = json.loads(plan_path.read_text())
        else:
            print(f"No usable Districtr export at {plan_path}.")

    if payload is None:
        if not plan_id:
            raise FileNotFoundError(
                f"Districtr plan not readable at {plan_path} and no "
                "'districtr_plan_id' configured to fetch it instead."
            )
        print(f"Fetching Districtr plan {plan_id} …")
        response = requests.get(
            "https://districtr.org/.netlify/functions/planRead",
            params={"id": plan_id}, timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if plan_path:
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps(payload.get("plan", payload)))

    plan = payload.get("plan", payload)

    # Part ids are zero-based; `parts` carries the number each displays as.
    # Assignment values are usually a one-element list, occasionally a bare int.
    display_number = {part["id"]: part["displayNumber"] for part in plan["parts"]}
    assignment = pd.Series({
        geoid: display_number[value[0] if isinstance(value, list) else value]
        for geoid, value in plan["assignment"].items()
    }, name="plan_district").astype("Int64")
    assignment.index.name = "GEOID"

    print(f"Districtr plan {plan['id']} ({plan['place']['name']}): "
          f"{len(assignment):,} blocks across {plan['problem']['numberOfParts']} "
          f"{plan['problem']['pluralNoun']}, keyed by {plan['idColumn']['key']}")
    return assignment


def select_blocks_by_plan(sc_blocks, assignment, place_name, crs_webmap):
    """Restrict blocks to the set a Districtr plan assigns, and tag each one.

    Args:
        sc_blocks: GeoDataFrame of county blocks, indexed by block GEOID.
        assignment: Series from load_districtr_plan().
        place_name: City name used in log output.
        crs_webmap: Web-map CRS string for the output.

    Returns:
        GeoDataFrame of the plan's blocks with a "plan_district" column.

    Raises:
        ValueError: If the plan references blocks the source does not have,
            which usually means "counties" is too narrow.
    """
    missing = assignment.index.difference(sc_blocks.index)
    if len(missing):
        counties = sorted({geoid[2:5] for geoid in missing})
        raise ValueError(
            f"{len(missing):,} block(s) in the plan are absent from the block "
            f"source; they sit in counties {counties}. Add them to 'counties' "
            "in geometry_data."
        )

    selected = sc_blocks.loc[assignment.index].copy()
    selected.index.name = sc_blocks.index.name  # .loc with an unnamed index drops it
    selected["plan_district"] = assignment.to_numpy()
    selected = selected.to_crs(crs_webmap)

    print(f"{len(selected):,} of {len(sc_blocks):,} county blocks selected for "
          f"{place_name} (from the Districtr plan).")
    print(f"  {place_name} population = {selected['total_pop_20'].sum():,.0f}")
    print(f"  {place_name} VAP  = {selected['VAP'].sum():,.0f}")
    if "CVAP" in selected.columns:
        print(f"  {place_name} CVAP = {selected['CVAP'].sum():,.0f}")
    return selected


# --------------------------------------------------------------------------- #
# 2. City boundary
# --------------------------------------------------------------------------- #
def download_boundary(state_fips, place_geoid, cache_path, crs_equal):
    """Download (or load from cache) the city "place" polygon from TIGER.

    Args:
        state_fips: Two-digit state FIPS code.
        place_geoid: Seven-digit place GEOID (state_fips + place_fips).
        cache_path: GeoPackage path used for lazy caching.
        crs_equal: Equal-area CRS string for spatial operations (e.g. "EPSG:26910").

    Returns:
        Single-row GeoDataFrame in the equal-area CRS.
    """
    cache_path = Path(cache_path)
    if cache_path.exists():
        print("Loading boundary from cache …")
        return gpd.read_file(cache_path).to_crs(crs_equal)

    place_url = f"{TIGER}/PLACE/tl_2020_{state_fips}_place.zip"
    print("Downloading place layer for the city boundary …")
    state_places = gpd.read_file(place_url)
    boundary = state_places[state_places["GEOID"] == place_geoid].to_crs(crs_equal)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    boundary.to_file(cache_path, driver="GPKG")
    print(f"✓ Saved boundary to {cache_path}")

    assert len(boundary) == 1, (
        f"Expected exactly one place polygon for GEOID {place_geoid}, found {len(boundary)}"
    )
    return boundary


# --------------------------------------------------------------------------- #
# 2b. City council districts (optional)
# --------------------------------------------------------------------------- #
def load_city_districts(geo, crs_equal):
    """Load the enacted council district boundaries for one city.

    The published layer is regional (San Diego County's covers El Cajon, Chula
    Vista, and so on alongside the City of San Diego, with district numbers
    repeating across cities), so it is filtered to a single jurisdiction. The
    union of the result is the city boundary, and its individual polygons label
    each block with the district it currently sits in.

    Args:
        geo: The config["geometry_data"] block. Reads "districts_path" (local
            path or URL), "districts_jurisdiction" and its column, and
            "district_column".
        crs_equal: Equal-area CRS string for spatial operations.

    Returns:
        GeoDataFrame of the city's districts, in the equal-area CRS, sorted by
        district number.

    Raises:
        FileNotFoundError: If a local districts_path does not exist.
        ValueError: If the jurisdiction filter leaves no districts.
    """
    districts_path = geo["districts_path"]
    if "://" not in str(districts_path) and not Path(districts_path).exists():
        raise FileNotFoundError(
            f"Council district boundaries not found at {districts_path}. "
            "Set 'districts_path' in geometry_data to the regional council "
            "district layer, or remove it to use the TIGER place boundary."
        )

    districts = gpd.read_file(districts_path).to_crs(crs_equal)

    jurisdiction = geo.get("districts_jurisdiction")
    if jurisdiction:
        jurisdiction_column = geo.get("districts_jurisdiction_column", "JUR_NAME")
        districts = districts[districts[jurisdiction_column] == jurisdiction].copy()
        if districts.empty:
            raise ValueError(
                f"No districts with {jurisdiction_column} == '{jurisdiction}' "
                f"in {districts_path}."
            )

    district_column = geo.get("district_column", "DISTRICT")
    districts = districts.dropna(subset=[district_column]).copy()
    districts[district_column] = districts[district_column].astype(int)
    districts = districts.sort_values(district_column).reset_index(drop=True)

    print(f"Loaded {len(districts)} council districts for {jurisdiction or districts_path}.")
    return districts


def assign_districts(sc_blocks, districts, district_column, crs_equal):
    """Tag each block with a single council district.

    A block's representative point decides, which is exact for any block wholly
    inside the city. Blocks selected by the "intersects" rule can sit mostly
    outside, putting that point in no district at all; those fall back to the
    district they overlap most. Every block still gets exactly one district, so
    district totals stay additive.

    Args:
        sc_blocks: GeoDataFrame of the blocks inside the city.
        districts: GeoDataFrame of council districts.
        district_column: Name of the district number column in `districts`.
        crs_equal: Equal-area CRS string for spatial operations.

    Returns:
        Copy of sc_blocks with an added "council_district" column.
    """
    blocks_equal = sc_blocks.to_crs(crs_equal)
    districts_equal = districts.to_crs(crs_equal)[[district_column, "geometry"]]

    points = gpd.GeoDataFrame(
        geometry=blocks_equal.geometry.representative_point(), crs=crs_equal
    )
    tagged = gpd.sjoin(points, districts_equal, predicate="within", how="left")
    # A point on a shared district edge can match twice; keep the first match so
    # the result stays one row per block.
    tagged = tagged[~tagged.index.duplicated(keep="first")]

    sc_blocks = sc_blocks.copy()
    sc_blocks["council_district"] = tagged[district_column].reindex(sc_blocks.index).to_numpy()

    stragglers = sc_blocks.index[sc_blocks["council_district"].isna()]
    if len(stragglers):
        overlaps = gpd.sjoin(
            blocks_equal.loc[stragglers, ["geometry"]], districts_equal,
            predicate="intersects", how="inner",
        )
        if not overlaps.empty:
            areas = shapely.area(shapely.intersection(
                overlaps.geometry.to_numpy(),
                districts_equal.geometry.iloc[overlaps["index_right"]].to_numpy(),
            ))
            best = (
                pd.DataFrame({
                    "block": overlaps.index,
                    "district": overlaps[district_column].to_numpy(),
                    "area": areas,
                })
                .sort_values("area")
                .drop_duplicates("block", keep="last")
                .set_index("block")["district"]
            )
            best.index.name = None
            sc_blocks.loc[best.index, "council_district"] = best
        print(f"  {len(stragglers):,} block(s) had no interior point in a district; "
              "assigned by largest overlap.")

    unassigned = int(sc_blocks["council_district"].isna().sum())
    if unassigned:
        print(f"  Warning: {unassigned:,} block(s) touch no district polygon at all.")
    else:
        # Only integral once nothing is missing; keeps district labels out of
        # float formatting downstream.
        sc_blocks["council_district"] = sc_blocks["council_district"].astype(int)
    counts = sc_blocks["council_district"].value_counts().sort_index()
    print("  Blocks per council district: " + ", ".join(
        f"{int(d)}: {n:,}" for d, n in counts.items()
    ))
    return sc_blocks


# --------------------------------------------------------------------------- #
# 3. PL 94-171 block data (P1 + P3 + P4)
# --------------------------------------------------------------------------- #
def fetch_pl_blocks(client, variables, state_fips, counties, chunk_size=49):
    """Download PL block data for `variables` across several counties.

    The API caps each request at 50 variables, so variables are fetched in
    chunks and merged on the geography keys.

    Args:
        client: Census API client.
        variables: List of PL variable names to fetch.
        state_fips: Two-digit state FIPS code.
        counties: List of three-digit county FIPS codes.
        chunk_size: Max variables per API call (default 49 to stay under the 50-var cap).

    Returns:
        DataFrame indexed by the 15-digit block GEOID with float value columns.
    """
    geo_keys = ["state", "county", "tract", "block"]
    county_frames = []
    for cty in counties:
        chunk_frames = []
        for chunk in _chunks(variables, chunk_size):
            raw = client.pl.get(
                [v.upper() for v in chunk],
                geo={"for": "block:*", "in": f"state:{state_fips} county:{cty}"},
            )
            chunk_frames.append(pd.DataFrame(raw))
        df = chunk_frames[0]
        for extra in chunk_frames[1:]:
            df = df.merge(extra, on=geo_keys)
        county_frames.append(df)

    out = pd.concat(county_frames, ignore_index=True)
    out.columns = [c.lower() for c in out.columns]
    out["GEOID"] = out["state"] + out["county"] + out["tract"] + out["block"]
    value_cols = [v.lower() for v in variables]
    out[value_cols] = out[value_cols].astype(float)
    return out.set_index("GEOID")[value_cols]


def download_pl_blocks(state_fips, counties, client, cache_path):
    """Download (or load from cache) the P1/P3/P4 PL table for every block in the given counties.

    Args:
        state_fips: Two-digit state FIPS code.
        counties: List of three-digit county FIPS codes.
        client: Census API client.
        cache_path: Parquet path used for lazy caching.

    Returns:
        DataFrame indexed by block GEOID with the 143 P1/P3/P4 variables.
    """
    cache_path = Path(cache_path)
    if cache_path.exists():
        print("Loading P1/P3/P4 data from cache …")
        return pd.read_parquet(cache_path)

    print("Downloading P1/P3/P4 data from Census API (this takes ~2 minutes) …")
    pl_blocks = fetch_pl_blocks(client, RAW_VARS, state_fips, counties)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pl_blocks.to_parquet(cache_path)
    print(f"✓ Saved {len(pl_blocks):,} blocks to {cache_path}")
    return pl_blocks


# --------------------------------------------------------------------------- #
# 4. ACS citizenship rates (tract level)
# --------------------------------------------------------------------------- #
def citizenship_rates(client, suffix, geo, year):
    """Return CVAP and VAP (ACS) for one B05003 race iteration.

    Args:
        client: Census client.
        suffix: B05003 race-iteration suffix (e.g. "B", "H", "I", …).
        geo: Census API geography dict.
        year: ACS 5-year vintage.

    Returns:
        DataFrame with acs_cvap and acs_vap; indexed by 11-digit tract GEOID
        when the geography is tract-level.
    """
    raw = client.acs5.get(b05003_vars(suffix), geo=geo, year=year)
    df = pd.DataFrame(raw)
    val_cols = b05003_vars(suffix)
    df[val_cols] = df[val_cols].astype(float)
    cvap = df[[f"B05003{suffix}_{r}E" for r in CVAP_ROWS]].sum(axis=1)
    vap = df[[f"B05003{suffix}_{r}E" for r in VAP_ROWS]].sum(axis=1)
    out = pd.DataFrame({"acs_cvap": cvap, "acs_vap": vap})
    if "tract" in df.columns:
        out["GEOID"] = df["state"] + df["county"] + df["tract"]
        out = out.set_index("GEOID")
    return out


def download_acs_citizenship(state_fips, client, cache_dir):
    """Download (or load from cache) ACS B05003 citizenship rates.

    For every race iteration in ACS_RATE_TABLES this fetches tract-level CVAP/VAP
    for all tracts in the state (cached per category) plus the statewide totals
    used as a fallback when a tract denominator is too small.

    Args:
        state_fips: Two-digit state FIPS code.
        client: Census client.
        cache_dir: Directory for per-category tract parquet caches.

    Returns:
        (acs_tract, acs_state) where
            acs_tract: dict category -> tract DataFrame (acs_cvap, acs_vap)
            acs_state: dict category -> (statewide_cvap, statewide_vap)
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    acs_tract = {}
    acs_state = {}

    for suffix, category in ACS_RATE_TABLES.items():
        cache_file = cache_dir / f"acs_{state_fips}_{category}_tracts.parquet"

        if cache_file.exists():
            print(f"Loading {category} tract data from cache …")
            acs_tract[category] = pd.read_parquet(cache_file)
        else:
            tract_geo = {"for": "tract:*", "in": f"state:{state_fips}"}
            acs_tract[category] = citizenship_rates(client, suffix, tract_geo, ACS_YEAR)
            acs_tract[category].to_parquet(cache_file)
            print(f"✓ Saved {category} tracts to {cache_file}")

        st = citizenship_rates(client, suffix, {"for": f"state:{state_fips}"}, ACS_YEAR)
        acs_state[category] = (float(st["acs_cvap"].iloc[0]), float(st["acs_vap"].iloc[0]))
        rate = acs_state[category][0] / acs_state[category][1]
        print(f"{category:8s} (B05003{suffix})  statewide rate = {rate:.3f}")

    return acs_tract, acs_state


# --------------------------------------------------------------------------- #
# 5. Partition VAP into the six categories
# --------------------------------------------------------------------------- #
def build_vap_categories(vap_raw):
    """Partition total VAP into the six mutually-exclusive categories.

    Builds BVAP, HVAP, AVAP, AMINVAP, OVAP, WVAP from the raw P3/P4 variables
    and verifies that they sum exactly to total VAP (P3_001N).

    Args:
        vap_raw: DataFrame of raw P3/P4 variables indexed by block GEOID.

    Returns:
        DataFrame indexed by GEOID with total_pop_20, VAP, and the six categories.
    """
    vap = pd.DataFrame(index=vap_raw.index)
    vap["total_pop_20"] = vap_raw["p1_001n"]
    vap["VAP"] = vap_raw["p3_001n"]

    vap["BVAP"] = vap_raw[BVAP_VARS].sum(axis=1)

    p3_side = [p3 for p3, _ in HVAP_PAIRS]
    p4_side = [p4 for _, p4 in HVAP_PAIRS]
    vap["HVAP"] = vap_raw[p3_side].sum(axis=1).values - vap_raw[p4_side].sum(axis=1).values

    vap["AVAP"] = vap_raw[AVAP_VARS].sum(axis=1)
    vap["AMINVAP"] = vap_raw[AMINVAP_VARS].sum(axis=1)
    vap["OVAP"] = vap_raw[OVAP_VARS].sum(axis=1)
    vap["WVAP"] = vap_raw[WVAP_VARS].sum(axis=1)

    recomputed = vap[CATEGORIES].sum(axis=1)
    max_err = (recomputed - vap["VAP"]).abs().max()
    assert max_err < 1e-6, (
        f"Categories do NOT partition VAP (max error {max_err}) — check the variable tables!"
    )

    bad_vap = vap["VAP"] > vap["total_pop_20"]
    assert not bad_vap.any(), (
        f"Found {bad_vap.sum()} blocks where VAP > total population."
    )

    print(f"Blocks with total population = 0: {(vap['total_pop_20'] == 0).sum():,}")
    print(f"Blocks with VAP = 0: {(vap['VAP'] == 0).sum():,}")
    print(f"VAP partitioned into {len(CATEGORIES)} categories for {len(vap):,} blocks.")
    return vap


# --------------------------------------------------------------------------- #
# 6. Per-block citizenship rate lookup
# --------------------------------------------------------------------------- #
def build_cvap_categories(category, block_index, acs_tract, acs_state):
    """Per-block citizenship rate for one category, looked up by tract.

    Uses the tract rate when the tract ACS VAP >= VAP_FLOOR, otherwise the
    statewide rate.

    Args:
        category: ACS rate category (one of ACS_RATE_TABLES values).
        block_index: Index of block GEOIDs to produce rates for.
        acs_tract: dict category -> tract DataFrame.
        acs_state: dict category -> (cvap, vap) statewide totals.

    Returns:
        Series of citizenship rates aligned to block_index.
    """
    tdf = acs_tract[category]
    state_cvap, state_vap = acs_state[category]
    state_rate = state_cvap / state_vap if state_vap else 0.0

    with np.errstate(divide="ignore", invalid="ignore"):
        rate = tdf["acs_cvap"] / tdf["acs_vap"]
    rate = rate.where(tdf["acs_vap"] >= VAP_FLOOR, other=state_rate)
    rate = rate.fillna(state_rate)

    block_tract = pd.Series(block_index.str[:11], index=block_index)
    return block_tract.map(rate).fillna(state_rate)


# --------------------------------------------------------------------------- #
# 7. Estimate CVAP per block
# --------------------------------------------------------------------------- #
def estimate_cvap_by_block(vap, acs_tract, acs_state):
    """Discount each VAP category by its group's tract-level citizenship rate.

    Args:
        vap: DataFrame with the six VAP categories (from build_vap_categories).
        acs_tract: dict category -> tract DataFrame.
        acs_state: dict category -> (cvap, vap) statewide totals.

    Returns:
        DataFrame indexed by GEOID with the six *CVAP columns and total CVAP.
    """
    cvap = pd.DataFrame(index=vap.index)
    for vap_col, rate_cat in DISCOUNT_MAP.items():
        rate = build_cvap_categories(rate_cat, vap.index, acs_tract, acs_state)
        cvap[vap_col.replace("VAP", "CVAP")] = vap[vap_col].values * rate.values

    cvap["CVAP"] = cvap[CVAP_CATEGORIES].sum(axis=1)
    print(f"CVAP estimated for {len(cvap):,} blocks (total CVAP = {cvap['CVAP'].sum():,.0f}).")
    return cvap


# --------------------------------------------------------------------------- #
# 8. Select blocks whose centroid falls inside the city
# --------------------------------------------------------------------------- #
def select_blocks_in_city(sc_blocks, sc_boundary, place_name, crs_equal, crs_webmap,
                          predicate="representative_point"):
    """Keep the blocks that belong to the city under the configured rule.

    Blocks are never split, so counts stay additive under every rule, but each
    answers a different question at the city limit:

    * "within" keeps a block only when its whole geometry lies inside the city.
      Nothing outside the line is ever counted, at the cost of dropping
      straddling blocks entirely, so totals run below the published count.
    * "representative_point" keeps a block when its interior point is inside the
      city. Blocks partition the city, and totals reproduce the published count.
    * "intersects" keeps a block when it touches the city at all. Nothing inside
      the city is missed, but every straddling block is counted whole, so totals
      run above the published count.

    Args:
        sc_blocks: GeoDataFrame of county blocks with VAP/CVAP attributes.
        sc_boundary: Single-row GeoDataFrame of the city boundary.
        place_name: City name used in log output.
        crs_equal: Equal-area CRS string for spatial operations.
        crs_webmap: Web-map CRS string for the output (e.g. "EPSG:4326").
        predicate: "within", "representative_point", or "intersects".

    Returns:
        GeoDataFrame of the selected blocks, in crs_webmap.

    Raises:
        ValueError: If predicate is not one of the supported rules.
    """
    sc_blocks = sc_blocks.to_crs(crs_equal)
    sc_boundary = sc_boundary.to_crs(crs_equal)

    if hasattr(sc_boundary.geometry, "union_all"):
        city_poly = sc_boundary.geometry.union_all()
    else:
        city_poly = sc_boundary.geometry.unary_union

    if predicate == "within":
        # covered_by rather than within: a block sharing boundary with the city
        # limit is still wholly inside, and within() excludes it.
        in_city = sc_blocks.geometry.covered_by(city_poly)
    elif predicate == "representative_point":
        in_city = sc_blocks.geometry.representative_point().within(city_poly)
    elif predicate == "intersects":
        in_city = sc_blocks.geometry.intersects(city_poly)
    else:
        raise ValueError(
            f"Unknown selection predicate '{predicate}'; expected 'within', "
            "'representative_point', or 'intersects'."
        )

    selected = sc_blocks[in_city].copy().to_crs(crs_webmap)

    print(f"{in_city.sum():,} of {len(in_city):,} county blocks selected for "
          f"{place_name} (predicate: {predicate}).")
    if predicate == "intersects":
        # Straddling blocks are kept whole, so these totals exceed the city's
        # published counts by whatever those blocks hold outside the line.
        straddling = ~sc_blocks.loc[in_city].geometry.covered_by(city_poly)
        print(f"  {straddling.sum():,} of them straddle the city line, holding "
              f"{selected.loc[straddling.to_numpy(), 'total_pop_20'].sum():,.0f} people "
              "counted in full.")
    elif predicate == "within":
        # Dropped straddlers hold real in-city population that no longer counts.
        dropped = sc_blocks[sc_blocks.geometry.intersects(city_poly) & ~in_city]
        print(f"  {len(dropped):,} block(s) touch the city but are not wholly inside; "
              f"dropped, along with the {dropped['total_pop_20'].sum():,.0f} people in them.")
    print(f"  {place_name} population = {selected['total_pop_20'].sum():,.0f}")
    print(f"  {place_name} VAP  = {selected['VAP'].sum():,.0f}")
    if "CVAP" in selected.columns:
        print(f"  {place_name} CVAP = {selected['CVAP'].sum():,.0f}")
    return selected


# --------------------------------------------------------------------------- #
# 9. Export
# --------------------------------------------------------------------------- #
def export_to_gpkg(sc_blocks, output_path, place_name, crs_tiger,
                   extra_columns=(), graph=None):
    """Write the final block-level VAP/CVAP table to a GeoPackage.

    Columns are renamed to the snake_case schema used by the pipeline so the
    output is interchangeable with other geodata sources downstream.

    Args:
        sc_blocks: GeoDataFrame of the selected city blocks.
        output_path: Destination file path (GeoPackage or GeoJSON).
        place_name: City name used as the GeoPackage layer name.
        crs_tiger: CRS string matching the TIGER/Line source (e.g. "EPSG:4269").
        extra_columns: Source columns to carry through unchanged (race
            population, election results from a GerryDB view).
        graph: Pre-built dual graph over range(len(sc_blocks)). When given, its
            edges replace the geometry-derived adjacency; node attributes still
            come from the exported columns.

    Returns:
        (exported GeoDataFrame, GerryChain Graph).
    """
    output_path = Path(output_path)
    export_cols = [c for c in ("COUNTYFP20", "TRACTCE20") if c in sc_blocks.columns]
    for column in ("plan_district", "council_district"):
        if column in sc_blocks.columns:
            export_cols.append(column)
    # CVAP columns are only present when geometry_data.include_cvap is set.
    cvap_cols = [c for c in ("CVAP", *CVAP_CATEGORIES) if c in sc_blocks.columns]
    export_cols += (["total_pop_20", "VAP"] + CATEGORIES + cvap_cols
                    + [c for c in extra_columns if c in sc_blocks.columns]
                    + ["geometry"])

    sc_export = sc_blocks.to_crs(crs_tiger)[export_cols].copy()

    rename_dict = {
        "VAP": "total_vap_20",
        "CVAP": "total_cvap_20",
        "BVAP": "bvap_20",
        "HVAP": "hvap_20",
        "AVAP": "asian_nhpi_vap_20",
        "AMINVAP": "amin_vap_20",
        "OVAP": "other_vap_20",
        "WVAP": "white_vap_20",
        "BCVAP": "bcvap_20",
        "HCVAP": "hcvap_20",
        "ACVAP": "asian_nhpi_cvap_20",
        "AMINCVAP": "amin_cvap_20",
        "OCVAP": "other_cvap_20",
        "WCVAP": "white_cvap_20",
    }
    sc_export = sc_export.rename(columns=rename_dict)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    layer_name = place_name.replace(" ", "_").lower() + "_blocks"

    suffix = output_path.suffix.lower()
    if suffix == ".gpkg":
        sc_export.to_file(output_path, layer=layer_name, driver="GPKG")
    else:
        sc_export.to_file(output_path)

    n_cols = len([c for c in sc_export.columns if c != "geometry"])
    print(f"Wrote {len(sc_export):,} blocks x {n_cols} columns -> {output_path}")

    # Build and save the GerryChain graph so district_generator can load it from cache.
    graph_path = output_path.parent / (output_path.stem + "_graph.json")
    derived = Graph.from_file(str(output_path))
    derived = Graph.from_networkx(nx.convert_node_labels_to_integers(derived, first_label=0))

    if graph is None:
        final = derived
    else:
        # Keep the node attributes the exported columns give us, but take
        # adjacency from the published graph.
        shared = len(set(map(frozenset, derived.edges)) & set(map(frozenset, graph.edges)))
        print(f"  published graph vs geometry-derived: {graph.number_of_edges():,} vs "
              f"{derived.number_of_edges():,} edges, {shared:,} shared")
        derived.remove_edges_from(list(derived.edges))
        derived.add_edges_from(graph.edges(data=True))
        final = derived

    final.to_json(str(graph_path))
    print(f"Wrote graph ({len(final.nodes):,} nodes, {final.number_of_edges():,} edges) -> {graph_path}")

    return sc_export, final


# --------------------------------------------------------------------------- #
# Main orchestration
# --------------------------------------------------------------------------- #
def generate_data(config):
    """Run the full block-level VAP/CVAP pipeline end to end.

    Reads geographic parameters from config["geometry_data"] and writes the
    output to config["geodata_path"].

    Args:
        config: Parsed pipeline config dict.

    Returns:
        GeoDataFrame of the exported city blocks.
    """
    geo = config["geometry_data"]
    state_fips = geo["state_fips"]
    place_fips = geo["place_fips"]
    place_name = geo["place_name"]
    counties = geo["counties"]
    crs_tiger = geo["crs_tiger"]
    crs_equal = geo["crs_equal"]
    crs_webmap = geo["crs_webmap"]
    place_geoid = state_fips + place_fips

    output_path = Path(config["geodata_path"])
    paths = _place_paths(place_name)
    block_source = geo.get("block_source_path")

    load_dotenv(".env")
    api_key = os.getenv("CENSUS_API_KEY")
    if not api_key:
        raise ValueError(
            "CENSUS_API_KEY not found. Add it to .env in root "
            "(get a free key at https://api.census.gov/data/key_signup.html)."
        )

    print("-" * 60)
    print(f"Target: {place_name} (GEOID {place_geoid}) in {geo.get('state_name', state_fips)}")
    print("-" * 60)

    client = Census(api_key, year=DECENNIAL_YEAR)

    # 1-2. Geometries. When council districts are configured they define the
    # city boundary (their union) as well as the per-block district labels;
    # otherwise the TIGER place polygon is the boundary.
    extra_columns = ()
    if block_source:
        block_gdf, extra_columns = load_gerrydb_blocks(geo)
        vap = block_gdf[["total_pop_20", "VAP", *CATEGORIES]]
    else:
        block_gdf = download_blocks(state_fips, counties, paths["blocks_cache"])
        vap = None

    plan_assignment = load_districtr_plan(geo)

    districts = load_city_districts(geo, crs_equal) if geo.get("districts_path") else None
    if districts is not None:
        sc_boundary = gpd.GeoDataFrame(geometry=[districts.union_all()], crs=crs_equal)
    else:
        sc_boundary = download_boundary(state_fips, place_geoid, paths["boundary_cache"], crs_equal)

    # 3-5. VAP. A GerryDB view publishes the six categories directly; otherwise
    # they are assembled from the PL 94-171 tables.
    if vap is None:
        vap_raw = download_pl_blocks(state_fips, counties, client, paths["pl_cache"])
        vap = build_vap_categories(vap_raw)
        block_gdf = block_gdf.join(vap)

    # 4, 6-7. CVAP is a modelled estimate (VAP discounted by ACS citizenship
    # rates), not a published count, and nothing downstream consumes it. It is
    # off by default so it cannot be picked up by accident; set
    # "include_cvap": true in geometry_data to estimate and export it.
    include_cvap = bool(geo.get("include_cvap", False))
    if include_cvap:
        acs_tract, acs_state = download_acs_citizenship(state_fips, client, paths["acs_cache_dir"])
        sc_blocks = block_gdf.join(estimate_cvap_by_block(vap, acs_tract, acs_state))
        print(f"  Total CVAP (county) = {sc_blocks['CVAP'].sum():,.0f}")
    else:
        sc_blocks = block_gdf
        print("  CVAP not estimated (set geometry_data.include_cvap to enable).")
    print(f"  Total VAP  (county) = {sc_blocks['VAP'].sum():,.0f}")

    # 8. Restrict to the city's blocks. Blocks are kept whole, never clipped.
    # A Districtr plan defines its own block set, which takes precedence over
    # any spatial predicate against the boundary.
    if plan_assignment is not None:
        if geo.get("selection_predicate"):
            print("  ('selection_predicate' is ignored: the Districtr plan defines "
                  "the block set.)")
        sc_blocks = select_blocks_by_plan(sc_blocks, plan_assignment, place_name, crs_webmap)
    else:
        sc_blocks = select_blocks_in_city(
            sc_blocks, sc_boundary, place_name, crs_equal, crs_webmap,
            predicate=geo.get("selection_predicate", "representative_point"),
        )

    # 8b. Label each block with its enacted council district.
    if districts is not None:
        sc_blocks = assign_districts(
            sc_blocks, districts, geo.get("district_column", "DISTRICT"), crs_equal
        )
        districts_output = geo.get("districts_output_path")
        if districts_output:
            districts_output = Path(districts_output)
            districts_output.parent.mkdir(parents=True, exist_ok=True)
            districts.to_crs(crs_tiger).to_file(districts_output)
            print(f"Wrote {len(districts)} council districts -> {districts_output}")

    # 9. Export GeoPackage and GerryChain graph. A GerryDB view ships the dual
    # graph it was built with, so prefer that over deriving adjacency here.
    published_graph = load_gerrydb_graph(geo, list(sc_blocks.index)) if block_source else None
    sc_blocks, graph = export_to_gpkg(
        sc_blocks, output_path, place_name, crs_tiger,
        extra_columns=extra_columns, graph=published_graph,
    )

    # Write a metadata sidecar so future runs can detect geodata/config mismatches.
    meta_path = output_path.parent / (output_path.stem + "_meta.json")
    with open(meta_path, "w") as f:
        json.dump({"geometry_data": geo}, f, indent=2)

    print("-" * 60)
    print("Done.")
    print("-" * 60)
    return sc_blocks, graph


def main():
    parser = argparse.ArgumentParser(description="Generate block-level VAP/CVAP geodata")
    parser.add_argument(
        "--config",
        default="configs/basic.json",
        help="Path to a run config JSON file (default: configs/basic.json)",
    )
    args = parser.parse_args()

    # Same layering the pipeline uses, so geometry_data can live in the project
    # config rather than in every run config.
    generate_data(load_run_config(args.config))


if __name__ == '__main__':
    # Goes through main() so the run config is layered over project-settings.json;
    # loading the run config alone would miss geometry_data and the shared columns.
    main()