from __future__ import annotations

from datetime import date

import pytest

from bcoa_worker import naming


def test_plan_examples() -> None:
    assert naming.wide_column("clin_ct_organs", "liver", "volume_ml") == "organs__liver__volume_ml"
    assert (
        naming.wide_column("clin_ct_vertebrae", "vertebra_L3", "hu_mean", timepoint=2)
        == "vertebrae__vertebra_l3__hu_mean__t2"
    )
    assert naming.status_column("clin_ct_organs", timepoint=1) == "organs__status__t1"


def test_sanitize() -> None:
    assert naming.sanitize("Vertebra L3") == "vertebra_l3"
    assert naming.sanitize("  heart--left atrium ") == "heart_left_atrium"
    with pytest.raises(ValueError):
        naming.sanitize("---")


def test_other_prefixes_are_kept() -> None:
    assert naming.model_key("preclin_ct_legs") == "preclin_ct_legs"
    assert naming.model_key("clin_pt_fdg_brain_v1") == "clin_pt_fdg_brain_v1"


def test_display_names() -> None:
    assert naming.display_name("kidney_left") == "Kidney left"
    assert naming.display_name("vertebra_L3") == "Vertebra L3"


def test_timepoints_by_date_with_stable_ties() -> None:
    dates = [date(2024, 5, 1), date(2023, 1, 1), date(2024, 5, 1)]
    assert naming.timepoints(dates) == [2, 1, 3]


def test_series_suffix() -> None:
    assert naming.wide_column("clin_ct_organs", "liver", "volume_ml", timepoint=1, series=2) == (
        "organs__liver__volume_ml__t1__s2"
    )


def test_excel_limits() -> None:
    naming.check_dimensions(1000, 16_384)
    with pytest.raises(ValueError, match="long layout"):
        naming.check_dimensions(10, 16_385)
    assert naming.check_sheet_name("data_dictionary") == "data_dictionary"
    with pytest.raises(ValueError):
        naming.check_sheet_name("x" * 32)
