"""The intended-use statement every export carries (plan §2 rule 5, §13).

Identical to `IntendedUse` in BCOAKit; both suites compare against the plan.
"""

SHORT = "For research use only. Not for clinical use."

STATEMENT = (
    "Research software for the automated segmentation and quantification of "
    "anatomical structures in CT data. Not intended for diagnosis, treatment "
    "planning, or any other clinical decision-making."
)

# Plan §13: the case report's line on every page; the methods file and the
# provenance sheet carry it too.
NOT_MEDICAL_DEVICE = "Not a medical device; not for diagnosis or treatment decisions."
EVERY_PAGE = f"{SHORT} {NOT_MEDICAL_DEVICE}"

# Beside the intended use wherever it is explained at length (About, the
# reproducibility package, the DMG's Read Me); identical to BCOAKit's.
REGULATORY = (
    "It carries no CE marking and no FDA clearance, and its results are not validated for "
    "any clinical purpose."
)
WARRANTY = "Provided as is, without warranty of any kind, to the extent the law permits."
