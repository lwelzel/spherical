"""Product-location helpers must agree with the orchestrators and stay light to import."""

import ast
import sys
from pathlib import Path

import pytest

from spherical.pipeline import products
from spherical.pipeline.pipeline_config import (
    IFSReductionConfig,
    IRDISReductionConfig,
    config_from_dict,
    config_to_dict,
)
from spherical.pipeline.products import (
    converted_directory_for,
    observation_directory_for,
    target_folder_string,
    trap_result_folder_for,
)


class _Obs:
    def __init__(self, instrument, main_id="HD 1234  A", band="OBS_H", night="2020-01-01"):
        self.observation = {
            "INSTRUMENT": [instrument],
            "MAIN_ID": [main_id],
            "FILTER": [band],
            "NIGHT_START": [night],
        }


def test_products_imports_only_stdlib():
    tree = ast.parse(Path(products.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert not sorted(roots - sys.stdlib_module_names)


def test_target_folder_string_collapses_whitespace():
    assert target_folder_string(_Obs("ifs")) == "HD_1234_A/OBS_H/2020-01-01"


def test_ifs_layout_matches_ifs_reduction(tmp_path):
    pytest.importorskip("charis", reason="ifs_reduction needs the pipeline extra")
    from spherical.pipeline.ifs_reduction import output_directory_path

    config = IFSReductionConfig()
    config.directories.reduction_directory = tmp_path
    config.extraction = config.extraction.merge(method="apphot3")
    obs = _Obs("ifs")
    expected = Path(output_directory_path(str(tmp_path), obs, method="apphot3"))
    assert converted_directory_for(obs, config) == expected
    assert observation_directory_for(obs, tmp_path) == tmp_path / "IFS" / "observation" / "HD_1234_A" / "OBS_H" / "2020-01-01"
    assert trap_result_folder_for(obs, config) == tmp_path / "IFS" / "trap" / "HD_1234_A" / "OBS_H" / "2020-01-01"


def test_irdis_layout_matches_irdis_reduction(tmp_path):
    pytest.importorskip("dill", reason="irdis_reduction needs the pipeline extra")
    from spherical.pipeline.irdis_reduction import output_directory_path

    config = IRDISReductionConfig()
    config.directories.reduction_directory = tmp_path
    obs = _Obs("irdis", band="DB_K12")
    expected = Path(output_directory_path(str(tmp_path), obs)) / "converted"
    assert converted_directory_for(obs, config) == expected


def test_irdis_layout_has_no_method_segment(tmp_path):
    config = IRDISReductionConfig()
    config.directories.reduction_directory = tmp_path
    obs = _Obs("irdis", band="DB_K12")
    assert converted_directory_for(obs, config) == tmp_path / "IRDIS" / "observation" / "HD_1234_A" / "DB_K12" / "2020-01-01" / "converted"


def test_config_dict_round_trip(tmp_path):
    config = IFSReductionConfig()
    config.set_ncpu(6)
    config.directories.base_path = tmp_path
    config.extraction = config.extraction.merge(method="apphot5", R=35)
    config.steps = config.steps.merge(force={"extract_cubes"}, bundle_hexagons=True)
    data = config_to_dict(config)
    assert data["directories"]["base_path"] == str(tmp_path)
    assert data["steps"]["force"] == ["extract_cubes"]
    assert data["extraction"]["method"] == "apphot5"
    rebuilt = config_from_dict(data, "ifs")
    assert rebuilt.extraction.method == "apphot5" and rebuilt.extraction.R == 35
    assert rebuilt.steps.force == {"extract_cubes"} and rebuilt.steps.bundle_hexagons
    assert rebuilt.resources.ncpu == 6
    assert Path(rebuilt.directories.raw_directory) == Path(config.directories.raw_directory)
    assert config_to_dict(rebuilt) == data

    irdis = IRDISReductionConfig()
    irdis.irdis_preprocessing = irdis.irdis_preprocessing.merge(crop=True, crop_size=256)
    rebuilt_irdis = config_from_dict(config_to_dict(irdis), "irdis")
    assert rebuilt_irdis.irdis_preprocessing.crop and rebuilt_irdis.irdis_preprocessing.crop_size == 256
