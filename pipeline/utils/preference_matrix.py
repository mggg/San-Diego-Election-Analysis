"""
Compute and persist per-bloc candidate preference matrices from BlocSlateConfig.

Wraps BlocSlateConfig.preference_df so the profile-generation stage can save one
preference matrix per (settings file, replicate) alongside the profile it
produced. The functions here are deliberately standalone (they take a
BlocSlateConfig and paths, not a settings dict) so they can be reused from
notebooks or other scripts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from votekit.ballot_generator import BlocSlateConfig


def compute_preference_matrix(config: "BlocSlateConfig") -> dict:
    """
    Extract the bloc x candidate preference matrix from a BlocSlateConfig.

    config.preference_df holds, for each (bloc, candidate) pair, the share of
    that bloc's within-slate support the candidate receives (populated by
    set_dirichlet_alphas / resample_preference_intervals_from_dirichlet_alphas).

    Args:
        config: A BlocSlateConfig with preference_df already populated (i.e.
            after set_dirichlet_alphas has been called).

    Returns:
        A JSON-serializable dict:
            {
              "blocs": [<bloc name>, ...],           # row order
              "candidates": [<candidate id>, ...],   # column order
              "matrix": [[float, ...], ...],
            }
    """
    df = config.preference_df
    return {
        "blocs": list(df.index),
        "candidates": list(df.columns),
        "matrix": df.to_numpy().tolist(),
    }


def preference_matrix_json(config: "BlocSlateConfig", *, indent: int = 2) -> str:
    """
    Compute the preference matrix for a BlocSlateConfig and serialize it to a
    JSON string.

    Useful when the caller wants the bytes to write itself (e.g. into a shared
    zip archive from the main process) rather than a file on disk.

    Args:
        config: A BlocSlateConfig with preference_df already populated.
        indent: json.dumps indentation.

    Returns:
        The matrix payload as a JSON string.
    """
    return json.dumps(compute_preference_matrix(config), indent=indent)


def write_preference_matrix(
    config: "BlocSlateConfig",
    output_path: str | Path,
) -> Path:
    """
    Compute the preference matrix for a BlocSlateConfig and write it to a JSON
    file, creating parent directories as needed.

    Args:
        config: A BlocSlateConfig with preference_df already populated.
        output_path: Destination .json path.

    Returns:
        The output Path that was written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = compute_preference_matrix(config)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return output_path


def preference_matrix_arcname(profile_filename: str) -> str:
    """
    Build the preference-matrix entry name for a profile filename, mirroring
    profiles.zip's naming so the two archives' entries line up 1:1 once the
    caller prefixes both with the same "<mode>/<district_count>/" path.

    Args:
        profile_filename: The profile's filename as written into profiles.zip,
            e.g. "<run>_..._district_00_v0.csv".

    Returns:
        The same stem with a ".json" extension, e.g. "<run>_..._district_00_v0.json".
    """
    return f"{Path(profile_filename).stem}.json"
