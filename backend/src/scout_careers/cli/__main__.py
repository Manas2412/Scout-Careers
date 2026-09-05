"""``python -m scout_careers.cli`` — the same app as the console script.

Exists because a container does not always have the console script on its PATH,
and ``DEPLOYMENT_ENV_RUNBOOK.md`` §6.2 documents the headless authorisation as
``python -m scout_careers.cli auth gmail --port 8765``. A documented command
that does not run is worse than no documentation.
"""

from __future__ import annotations

from scout_careers.cli.main import main

if __name__ == "__main__":
    main()
