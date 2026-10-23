"""The app's texts for the import's codes are generated from the registry;
a registry change without regenerating would leave a code without its text."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import index_code_texts


def test_the_generated_swift_file_is_current() -> None:
    assert index_code_texts.main(["--check"]) == 0


def test_every_code_with_a_text_has_a_case() -> None:
    registry = json.loads(index_code_texts.REGISTRY.read_text())
    rendered = index_code_texts.render(registry)
    for group, entry in registry["groups"].items():
        for item in entry["codes"]:
            if item.get("text"):
                assert f'"index.{group}.{item["code"]}"' in rendered
