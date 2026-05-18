"""Make top-level modules (app, webflow_crypto) importable from tests
regardless of pytest's import mode / rootdir handling."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
