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

# The home is a *child* of the throwaway directory, not the directory itself.
# CHECKPOINT_DB is derived as a sibling of AGENT_HOME -- deliberately outside
# it, so the agent cannot read its own transcript -- so a home with no parent of
# its own puts the database in the shared system temp root, where it survives
# this run and is shared with every other one. Nesting gives the sibling
# somewhere to land that the cleanup below actually removes.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="planner-tests-"))
_TEST_HOME = _TEST_ROOT / "home"
_TEST_HOME.mkdir()
os.environ["FINANCIAL_PLANNER_HOME"] = str(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_ROOT, ignore_errors=True)
