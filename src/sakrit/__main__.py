# SPDX-License-Identifier: Apache-2.0
"""Enable ``python -m sakrit`` (G-8) — delegate to the same CLI as the console script."""

import sys

from sakrit.cli import main

if __name__ == "__main__":
    sys.exit(main())
