"""The name catalog against the lock (ADR 0017 sections 4 and 11).

The lock records every bundled model's labels, so this runs in CI without a
bundle: a model fetched without names turns the build red here.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / "App" / "Resources" / "StructureNames.json").read_text())
LOCK = json.loads((ROOT / "Models" / "manifest.lock.json").read_text())

spec = importlib.util.spec_from_file_location(
    "structure_names", ROOT / "Scripts" / "structure_names.py"
)
assert spec
assert spec.loader
structure_names = importlib.util.module_from_spec(spec)
spec.loader.exec_module(structure_names)


def test_catalog_is_current() -> None:
    assert structure_names.main(["--check"]) == 0


def test_every_model_in_the_lock_has_labels_and_every_label_a_name() -> None:
    assert set(LOCK["models"]) == set(CATALOG["models"])
    for model, entry in LOCK["models"].items():
        assert entry.get("labels"), model
        names = CATALOG["models"][model]
        assert set(entry["labels"]) == set(names), model
        for label_id, label in entry["labels"].items():
            assert names[label_id]["label"] == label, (model, label_id)


def test_names_are_unique_within_a_model_and_short_forms_fit() -> None:
    groups = {g["name"] for g in CATALOG["groups"]}
    for model, names in CATALOG["models"].items():
        for field in ("name", "short"):
            counts = Counter(n[field] for n in names.values())
            assert [k for k, v in counts.items() if v > 1] == [], (model, field)
        for n in names.values():
            assert len(n["short"]) <= 18, n
            assert n["group"] in groups, n
            assert n["de"], n
            assert n["name"][0].isupper(), n


def test_order_runs_right_before_left_and_cranial_to_caudal() -> None:
    ribs = CATALOG["models"]["clin_ct_ribs"]
    by_label = {n["label"]: n["order"] for n in ribs.values()}
    assert by_label["rib_right_1"] < by_label["rib_left_1"] < by_label["rib_right_2"]
    spine = {n["label"]: n["order"] for n in CATALOG["models"]["clin_ct_vertebrae"].values()}
    assert spine["vertebra_C1"] < spine["vertebra_C7"] < spine["vertebra_T1"] < spine["vertebra_L1"]


def test_names_adr_0017_fixes() -> None:
    def name(label: str) -> str:
        for names in CATALOG["models"].values():
            for n in names.values():
                if n["label"] == label:
                    return n["name"]
        raise KeyError(label)

    assert name("kidney_left") == "Left kidney"
    assert name("heart_myocardium") == "Left ventricular myocardium"
    assert name("pulmonary_artery") == "Pulmonary trunk and main branches"
    assert name("colon") == "Colon and rectum"
    assert name("vertebra_C1") == "C1 vertebra (atlas)"
    assert name("vertebra_L6") == "L6 vertebra (variant)"
    assert name("rib_left_13") == "Left 13th rib (accessory)"
    assert name("skeletal_muscle") == "Skeletal muscle (L3 level)"
