"""Worker process of the Body Composition and Organ CT and PET-CT Analyzer.

Started by the app once per job as ``python -I -m bcoa_worker run --job <file>``.
"""

__version__ = "0.1.0"

# Raised whenever an event or job field changes meaning; both test suites parse
# every fixture in /Protocol, so a mismatch fails on whichever side lags.
PROTOCOL_VERSION = 1
