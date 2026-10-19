#!/usr/bin/env python3
"""DJI Pano Builder — command-line entry point.

The implementation lives in the :mod:`djipano` package so that the CLI and
the GUI share exactly the same stitching code.

    python3 dji_pano.py PANORAMA/006_0004 -o pano.jpg
"""

from djipano.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
