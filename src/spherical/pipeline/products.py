"""Locations of pipeline products, resolvable without importing the pipeline itself.

External consumers (for example the ``reducer`` GPU post-processing package)
need to know where a given observation's ``converted/`` directory and TRAP
result folder are for a given configuration. ``run_trap`` already computes
both, but importing it pulls in ``trap`` and ``charis``; this module exposes the
same layout with standard-library imports only, so it works in a base install.

The layout is the one written by ``ifs_reduction.execute_target`` and
``irdis_reduction.execute_irdis_target``::

    {reduction_directory}/IFS/observation/{target}/{band}/{date}/{method}/converted/
    {reduction_directory}/IRDIS/observation/{target}/{band}/{date}/converted/
    {reduction_directory}/{INSTRUMENT}/trap/{target}/{band}/{date}/
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "instrument_of",
    "target_folder_string",
    "observation_directory_for",
    "converted_directory_for",
    "trap_result_folder_for",
]


def instrument_of(observation) -> str:
    """``"IFS"`` or ``"IRDIS"`` from an observation's metadata table."""
    return str(observation.observation["INSTRUMENT"][0]).upper()


def target_folder_string(observation) -> str:
    """``{target}/{band}/{date}`` exactly as ``toolbox.make_target_folder_string`` builds it."""
    target_name = str(observation.observation["MAIN_ID"][0])
    target_name = "_".join(target_name.split())
    obs_band = str(observation.observation["FILTER"][0])
    date = str(observation.observation["NIGHT_START"][0])
    return f"{target_name}/{obs_band}/{date}"


def observation_directory_for(observation, reduction_directory: str | Path) -> Path:
    """``{reduction_directory}/{INSTRUMENT}/observation/{target}/{band}/{date}``."""
    return Path(reduction_directory) / instrument_of(observation) / "observation" / target_folder_string(observation)


def converted_directory_for(observation, config) -> Path:
    """The ``converted/`` directory the pipeline writes for ``observation`` under ``config``.

    ``config`` is an ``IFSReductionConfig`` or ``IRDISReductionConfig``; for IFS
    the extraction method (``config.extraction.method``) is a path segment, for
    IRDIS it is not. Mirrors ``run_trap._data_directory_for``.
    """
    base = observation_directory_for(observation, config.directories.reduction_directory)
    if instrument_of(observation) == "IFS":
        return base / str(config.extraction.method) / "converted"
    return base / "converted"


def trap_result_folder_for(observation, config) -> Path:
    """``{reduction_directory}/{INSTRUMENT}/trap/{target}/{band}/{date}``."""
    return Path(config.directories.reduction_directory) / instrument_of(observation) / "trap" / target_folder_string(observation)
