"""Test session setup.

Redirects the agent's filesystem root to a throwaway directory **before**
``financial_planner.config`` is imported and freezes the path.

Without this the document tests write their fixture CSVs straight into
``agent_home/workspace/`` -- the directory that holds the household's real bank
statements. A run interrupted between the write and the fixture teardown leaves
``_pytest-*.csv`` sitting there, where the sidebar lists them and the agent is
offered them as genuine financial documents.

pytest imports conftest before any test module, so the environment variable is
in place by the time a test triggers the first ``financial_planner`` import.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_HOME = Path(tempfile.mkdtemp(prefix="planner-tests-"))
os.environ["FINANCIAL_PLANNER_HOME"] = str(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)
