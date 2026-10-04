"""Stand-in for connected-components-3d (LGPL-3.0), which the bundle does not
ship (ADR 0014).

nnU-Net imports its trainer module to find the trainer class even for
inference; that module imports batchgeneratorsv2's training transforms, which
import acvl_utils.morphology, which imports cc3d at module level. Nothing on
the inference path calls it. Importing this module therefore succeeds, and
any use fails loudly instead of returning something plausible.
"""

from typing import NoReturn


def __getattr__(name: str) -> NoReturn:
    # Dunder probes come from the interpreter itself (pickle's whichmodule,
    # inspect, copy) walking sys.modules; they expect AttributeError and
    # would crash on anything else.
    if name.startswith("__"):
        raise AttributeError(name)
    raise RuntimeError(
        f"cc3d.{name} is not available in this app (ADR 0014); "
        "a model that needs connected components cannot run here"
    )
