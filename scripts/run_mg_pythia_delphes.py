#!/usr/bin/env python3
"""Compatibility entry point. Install mg-pythia-delphes for the mgpd command."""
import sys
from pathlib import Path

try:
    import mgpd
except ModuleNotFoundError as exc:
    if exc.name != 'mgpd':
        raise
    # Retain source-checkout usage without requiring an editable installation.
    source = Path(__file__).resolve().parents[1] / 'src'
    if not (source / 'mgpd').is_dir():
        raise SystemExit('Install mg-pythia-delphes in this Python environment to use the runner')
    sys.path.insert(0, str(source))
    import mgpd

from mgpd.compat import *  # Preserve historical function/template imports.

if __name__ == '__main__':
    sys.exit(main())
