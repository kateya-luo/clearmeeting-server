"""Create a transactionally consistent SQLite backup while the server is online."""
from __future__ import annotations

import argparse
import sqlite3


parser = argparse.ArgumentParser()
parser.add_argument("source")
parser.add_argument("destination")
args = parser.parse_args()

source = sqlite3.connect(args.source)
destination = sqlite3.connect(args.destination)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
