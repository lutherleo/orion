"""`python -m orion ...`: the same entry point as the `orion` console script (orion.cli:main)."""
from .cli import main

raise SystemExit(main())
