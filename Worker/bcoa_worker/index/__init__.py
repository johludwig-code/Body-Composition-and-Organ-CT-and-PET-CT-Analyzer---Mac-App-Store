"""The index job (ADR 0020, ADR 0022): walk the source folders, read headers
into the catalog, group the series, choose automatically, make previews.

Nothing in this package may reach the network or start a program; the
worker's guard (ADR 0016) refuses both, and a refusal here is a bug.
"""

import sys

if "requests" not in sys.modules:
    # pydicom imports `requests` when it is installed (only to download its
    # test data), and importing requests loads urllib3, which opens an IPv6
    # socket at import time to probe for IPv6 support. The guard refuses that
    # socket and logs a refusal in every index job. Measured with requests
    # 2.34.2 and urllib3 2.8.0: with this block urllib3 is never loaded, no
    # socket is attempted, and pydicom still reads and decodes. A requests
    # that some other code already imported is left alone rather than broken.
    sys.modules["requests"] = None  # type: ignore[assignment]
