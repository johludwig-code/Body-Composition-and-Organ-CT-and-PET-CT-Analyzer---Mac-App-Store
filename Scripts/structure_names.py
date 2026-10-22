#!/usr/bin/env python3
"""The structure name catalog (ADR 0017 section 4, OPEN_QUESTIONS #20).

    python3 Scripts/structure_names.py           # write App/Resources/StructureNames.json
    python3 Scripts/structure_names.py --check   # fail if it is stale

Every label of every bundled model gets the name an English report would
write ("Left kidney", "L3 vertebra"), a short form of at most 18 characters,
an anatomical group and an order (right before left, cranial to caudal), and
a German name kept for a later German interface. The rules are ADR 0017's;
the explicit names are the ones it fixes where the MOOSE word could mislead.
This file is a draft until the owner has reviewed it as a radiologist
(OPEN_QUESTIONS #20); the app, the report and the worker read the JSON it
writes, never the MOOSE words.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "App" / "Resources" / "StructureNames.json"
LOCK = ROOT / "Models" / "manifest.lock.json"

GROUPS = [
    "Organs",
    "Lungs and airways",
    "Digestive tract",
    "Heart and vessels",
    "Muscles",
    "Spine",
    "Sternum and ribs",
    "Other bones",
    "Body regions",
    "Body composition",
]
GROUPS_DE = {
    "Organs": "Organe",
    "Lungs and airways": "Lunge und Atemwege",
    "Digestive tract": "Verdauungstrakt",
    "Heart and vessels": "Herz und Gefäße",
    "Muscles": "Muskeln",
    "Spine": "Wirbelsäule",
    "Sternum and ribs": "Sternum und Rippen",
    "Other bones": "Übriges Skelett",
    "Body regions": "Körperregionen",
    "Body composition": "Körperzusammensetzung",
}

# Name, short, German, group, order: for labels without a rule below. The
# side comes first; the order within a group runs cranial to caudal, and the
# heart in the direction of flow.
NAMED: dict[str, tuple[str, str, str, str, int]] = {
    # Organs
    "brain": ("Brain", "Brain", "Gehirn", "Organs", 10),
    "thyroid_right": ("Right thyroid lobe", "R thyroid lobe", "Schilddrüse rechts", "Organs", 20),
    "thyroid_left": ("Left thyroid lobe", "L thyroid lobe", "Schilddrüse links", "Organs", 21),
    "liver": ("Liver", "Liver", "Leber", "Organs", 30),
    "gallbladder": ("Gallbladder", "Gallbladder", "Gallenblase", "Organs", 31),
    "spleen": ("Spleen", "Spleen", "Milz", "Organs", 32),
    "pancreas": ("Pancreas", "Pancreas", "Pankreas", "Organs", 33),
    "adrenal_gland_right": ("Right adrenal gland", "R adrenal", "Nebenniere rechts", "Organs", 40),
    "adrenal_gland_left": ("Left adrenal gland", "L adrenal", "Nebenniere links", "Organs", 41),
    "kidney_right": ("Right kidney", "R kidney", "Niere rechts", "Organs", 42),
    "kidney_left": ("Left kidney", "L kidney", "Niere links", "Organs", 43),
    "bladder": ("Urinary bladder", "Bladder", "Harnblase", "Organs", 50),
    # Lungs and airways
    "trachea": ("Trachea", "Trachea", "Trachea", "Lungs and airways", 10),
    "lung_upper_lobe_right": (
        "Right upper lobe",
        "RUL",
        "Oberlappen rechts",
        "Lungs and airways",
        20,
    ),
    "lung_middle_lobe_right": (
        "Right middle lobe",
        "RML",
        "Mittellappen rechts",
        "Lungs and airways",
        21,
    ),
    "lung_lower_lobe_right": (
        "Right lower lobe",
        "RLL",
        "Unterlappen rechts",
        "Lungs and airways",
        22,
    ),
    "lung_upper_lobe_left": ("Left upper lobe", "LUL", "Oberlappen links", "Lungs and airways", 23),
    "lung_lower_lobe_left": (
        "Left lower lobe",
        "LLL",
        "Unterlappen links",
        "Lungs and airways",
        24,
    ),
    # Digestive tract
    "esophagus": ("Esophagus", "Esophagus", "Ösophagus", "Digestive tract", 10),
    "stomach": ("Stomach", "Stomach", "Magen", "Digestive tract", 20),
    "duodenum": ("Duodenum", "Duodenum", "Duodenum", "Digestive tract", 30),
    "small_bowel": ("Small bowel", "Small bowel", "Dünndarm", "Digestive tract", 40),
    # The mask holds the rectum as well (ADR 0017 section 4).
    "colon": ("Colon and rectum", "Colon and rectum", "Kolon und Rektum", "Digestive tract", 50),
    # Heart and vessels, in the direction of flow
    "inferior_vena_cava": (
        "Inferior vena cava",
        "IVC",
        "V. cava inferior",
        "Heart and vessels",
        10,
    ),
    "heart_atrium_right": ("Right atrium", "RA", "Rechter Vorhof", "Heart and vessels", 20),
    "heart_ventricle_right": (
        "Right ventricle",
        "RV",
        "Rechter Ventrikel",
        "Heart and vessels",
        21,
    ),
    # Reaches into both hila (ADR 0017 section 4).
    "pulmonary_artery": (
        "Pulmonary trunk and main branches",
        "Pulmonary arteries",
        "Truncus pulmonalis und Hauptäste",
        "Heart and vessels",
        22,
    ),
    "heart_atrium_left": ("Left atrium", "LA", "Linker Vorhof", "Heart and vessels", 23),
    "heart_ventricle_left": ("Left ventricle", "LV", "Linker Ventrikel", "Heart and vessels", 24),
    # Only the wall of the left ventricle (ADR 0017 section 4).
    "heart_myocardium": (
        "Left ventricular myocardium",
        "LV myocardium",
        "Myokard des linken Ventrikels",
        "Heart and vessels",
        25,
    ),
    "aorta": ("Aorta", "Aorta", "Aorta", "Heart and vessels", 30),
    "iliac_artery_right": (
        "Right iliac artery",
        "R iliac artery",
        "A. iliaca rechts",
        "Heart and vessels",
        31,
    ),
    "iliac_artery_left": (
        "Left iliac artery",
        "L iliac artery",
        "A. iliaca links",
        "Heart and vessels",
        32,
    ),
    "portal_splenic_vein": (
        "Portal and splenic veins",
        "Portal/splenic v.",
        "V. portae und V. lienalis",
        "Heart and vessels",
        40,
    ),
    "iliac_vena_right": (
        "Right iliac vein",
        "R iliac vein",
        "V. iliaca rechts",
        "Heart and vessels",
        41,
    ),
    "iliac_vena_left": (
        "Left iliac vein",
        "L iliac vein",
        "V. iliaca links",
        "Heart and vessels",
        42,
    ),
    # Muscles
    "autochthon_right": (
        "Right paraspinal muscles",
        "R paraspinal",
        "Autochthone Rückenmuskulatur rechts",
        "Muscles",
        10,
    ),
    "autochthon_left": (
        "Left paraspinal muscles",
        "L paraspinal",
        "Autochthone Rückenmuskulatur links",
        "Muscles",
        11,
    ),
    "iliopsoas_right": ("Right iliopsoas", "R iliopsoas", "M. iliopsoas rechts", "Muscles", 20),
    "iliopsoas_left": ("Left iliopsoas", "L iliopsoas", "M. iliopsoas links", "Muscles", 21),
    "gluteus_minimus_right": (
        "Right gluteus minimus",
        "R glut. minimus",
        "M. glutaeus minimus rechts",
        "Muscles",
        30,
    ),
    "gluteus_minimus_left": (
        "Left gluteus minimus",
        "L glut. minimus",
        "M. glutaeus minimus links",
        "Muscles",
        31,
    ),
    "gluteus_medius_right": (
        "Right gluteus medius",
        "R glut. medius",
        "M. glutaeus medius rechts",
        "Muscles",
        32,
    ),
    "gluteus_medius_left": (
        "Left gluteus medius",
        "L glut. medius",
        "M. glutaeus medius links",
        "Muscles",
        33,
    ),
    "gluteus_maximus_right": (
        "Right gluteus maximus",
        "R glut. maximus",
        "M. glutaeus maximus rechts",
        "Muscles",
        34,
    ),
    "gluteus_maximus_left": (
        "Left gluteus maximus",
        "L glut. maximus",
        "M. glutaeus maximus links",
        "Muscles",
        35,
    ),
    # Spine and other bones
    "sacrum": ("Sacrum", "Sacrum", "Os sacrum", "Spine", 900),
    "skull": ("Skull", "Skull", "Schädel", "Other bones", 1),
    "hip_right": ("Right hip bone", "R hip bone", "Os coxae rechts", "Other bones", 60),
    "hip_left": ("Left hip bone", "L hip bone", "Os coxae links", "Other bones", 61),
    "sternum": ("Sternum", "Sternum", "Sternum", "Sternum and ribs", 1),
    # Body regions: "head" reaches down to C5/C6 (ADR 0017 section 4).
    "head": ("Head and neck", "Head and neck", "Kopf und Hals", "Body regions", 10),
    "body": ("Trunk", "Trunk", "Rumpf", "Body regions", 20),
    "arms": ("Arms", "Arms", "Arme", "Body regions", 30),
    "Legs": ("Legs", "Legs", "Beine", "Body regions", 40),
    # Body composition: MOOSE measures it at L3 only.
    "skeletal_muscle": (
        "Skeletal muscle (L3 level)",
        "Muscle (L3)",
        "Skelettmuskulatur (Höhe L3)",
        "Body composition",
        10,
    ),
    "subcutaneous_fat": (
        "Subcutaneous fat (L3 level)",
        "SAT (L3)",
        "Subkutanes Fett (Höhe L3)",
        "Body composition",
        20,
    ),
    "visceral_fat": (
        "Visceral fat (L3 level)",
        "VAT (L3)",
        "Viszerales Fett (Höhe L3)",
        "Body composition",
        30,
    ),
}

# Peripheral bones: English, German, order (cranial to caudal).
BONES = {
    "clavicle": ("clavicle", "Klavikula", 10),
    "scapula": ("scapula", "Skapula", 11),
    "humerus": ("humerus", "Humerus", 20),
    "radius": ("radius", "Radius", 21),
    "ulna": ("ulna", "Ulna", 22),
    "carpal": ("carpal bones", "Handwurzel", 23),
    "metacarpal": ("metacarpal bones", "Mittelhand", 24),
    "fingers": ("finger phalanges", "Fingerphalangen", 25),
    "femur": ("femur", "Femur", 70),
    "patella": ("patella", "Patella", 71),
    "tibia": ("tibia", "Tibia", 72),
    "fibula": ("fibula", "Fibula", 73),
    "tarsal": ("tarsal bones", "Fußwurzel", 74),
    "metatarsal": ("metatarsal bones", "Mittelfuß", 75),
    "toes": ("toe phalanges", "Zehenphalangen", 76),
}
SHORT_BONE = {
    "carpal bones": "carpals",
    "metacarpal bones": "metacarpals",
    "finger phalanges": "fingers",
    "tarsal bones": "tarsals",
    "metatarsal bones": "metatarsals",
    "toe phalanges": "toes",
}
ORDINAL = {1: "1st", 2: "2nd", 3: "3rd"}
VERTEBRA_NOTE = {"C1": " (atlas)", "C2": " (axis)", "L6": " (variant)"}
REGION_ORDER = {"C": 0, "T": 7, "L": 19}


def entry(label: str) -> dict[str, object]:
    if label in NAMED:
        name, short, german, group, order = NAMED[label]
    elif match := re.fullmatch(r"vertebra_([CTL])(\d+)", label):
        region, number = match.group(1), int(match.group(2))
        level = f"{region}{number}"
        name = f"{level} vertebra{VERTEBRA_NOTE.get(level, '')}"
        short, german = level, f"Wirbel {level}"
        group, order = "Spine", REGION_ORDER[region] * 10 + number * 10
    elif match := re.fullmatch(r"rib_(left|right)_(\d+)", label):
        side, number = match.group(1), int(match.group(2))
        nth = ORDINAL.get(number, f"{number}th")
        accessory = " (accessory)" if number == 13 else ""
        name = f"{side.capitalize()} {nth} rib{accessory}"
        short = f"{side[0].upper()} rib {number}"
        german = f"{number}. Rippe {'links' if side == 'left' else 'rechts'}"
        group, order = "Sternum and ribs", number * 10 + (1 if side == "left" else 0)
    elif match := re.fullmatch(r"([a-z]+)_(left|right)", label):
        bone, side = match.group(1), match.group(2)
        english, german_bone, base = BONES[bone]
        name = f"{side.capitalize()} {english}"
        short = f"{side[0].upper()} {SHORT_BONE.get(english, english)}"
        german = f"{german_bone} {'links' if side == 'left' else 'rechts'}"
        group, order = "Other bones", base * 10 + (1 if side == "left" else 0)
    else:
        raise KeyError(f"no name for {label}")
    return {"name": name, "short": short, "de": german, "group": group, "order": order}


def models_from_lock() -> dict[str, dict[str, str]]:
    lock = json.loads(LOCK.read_text())
    return {name: entry["labels"] for name, entry in lock["models"].items() if "labels" in entry}


def render(models: dict[str, dict[str, str]]) -> str:
    catalog = {
        "description": (
            "Display names of every structure of every bundled model (ADR 0017 section 4). "
            "Generated by Scripts/structure_names.py; a draft until the owner's review "
            "(OPEN_QUESTIONS #20)."
        ),
        "groups": [{"name": g, "de": GROUPS_DE[g]} for g in GROUPS],
        "models": {
            model: {
                label_id: {"label": label, **entry(label)}
                for label_id, label in sorted(labels.items(), key=lambda item: int(item[0]))
            }
            for model, labels in sorted(models.items())
        },
    }
    return json.dumps(catalog, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    rendered = render(models_from_lock())
    if "--check" in argv:
        if not OUTPUT.exists() or OUTPUT.read_text() != rendered:
            print("App/Resources/StructureNames.json is stale; run Scripts/structure_names.py")
            return 1
        return 0
    OUTPUT.write_text(rendered)
    print(f"[names] {sum(len(m) for m in models_from_lock().values())} structures")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
