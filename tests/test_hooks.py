"""The edit guard, `.claude/hooks/guard-sensitive-paths.sh`, run as Claude Code runs it.

Each test pipes one PreToolUse payload into the real script, with
``CLAUDE_PROJECT_DIR`` pointed at a throwaway project, and reads the verdict from
the exit code: 0 allows the write, 2 refuses it. Nothing else pinned the guard,
and it fails open quietly -- a wrong case pattern lets the write through with no
message -- so the allowlist under the agent's home is checked from both sides.

The script reads ``FINANCIAL_PLANNER_HOME`` from the environment and then from
the project's ``.env``. ``conftest.py`` sets the variable for this whole session,
so every run here starts from an environment with it removed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard-sensitive-paths.sh"

ALLOW, DENY = 0, 2

# Without jq the guard refuses every write (it fails closed), so every test would
# fail for a reason that has nothing to do with the allowlist.
pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the guard needs jq")


def run_guard(
    project: Path,
    file_path: str | Path,
    *,
    home: str | Path | None = None,
    key: str = "file_path",
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "FINANCIAL_PLANNER_HOME"}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    if home is not None:
        env["FINANCIAL_PLANNER_HOME"] = str(home)
    payload = {"tool_name": "Write", "tool_input": {key: str(file_path)}}
    return subprocess.run(
        ["sh", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=project,
        timeout=30,
    )


def verdict(project: Path, file_path: str | Path, **kwargs) -> int:
    return run_guard(project, file_path, **kwargs).returncode


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project laid out like this repo, as far as the guard looks.

    Resolved, because the guard compares symlink-resolved paths and macOS's temp
    directory sits behind the ``/var`` -> ``/private/var`` link.
    """
    root = (tmp_path / "project").resolve()
    skill = root / "agent_home" / "skills" / "budget-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: budget-review\n---\n")
    (root / "agent_home" / "workspace").mkdir()
    (root / "agent_home" / "workspace" / ".gitkeep").touch()
    (root / "agent_home" / "AGENTS.md").write_text("# Household profile\n")
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / "src").mkdir()
    return root


class TestOutsideTheAgentHome:
    @pytest.mark.parametrize(
        "rel",
        [
            "src/financial_planner/finance.py",
            "README.md",
            ".env.example",
            "tests/fixtures/scratch.sqlite",
        ],
    )
    def test_ordinary_files_are_editable(self, project, rel):
        assert verdict(project, project / rel) == ALLOW

    @pytest.mark.parametrize("rel", [".env", ".streamlit/secrets.toml"])
    def test_secrets_are_refused(self, project, rel):
        result = run_guard(project, project / rel)
        assert result.returncode == DENY
        assert "Blocked by .claude/hooks/guard-sensitive-paths.sh" in result.stderr

    def test_the_checkpoint_database_is_refused(self, project):
        assert verdict(project, project / "planner_state.sqlite") == DENY

    @pytest.mark.parametrize(
        "rel",
        [".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/verify.sh"],
    )
    def test_the_hook_configuration_is_refused(self, project, rel):
        assert verdict(project, project / rel) == DENY

    def test_a_payload_without_a_path_is_let_through(self, project):
        # The matcher covers tools that may carry no path; there is nothing to judge.
        result = subprocess.run(
            ["sh", str(GUARD)],
            input=json.dumps({"tool_name": "Write", "tool_input": {}}),
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
            timeout=30,
        )
        assert result.returncode == ALLOW


class TestTheAgentHomeIsAnAllowlist:
    @pytest.mark.parametrize(
        "rel",
        [
            "skills/budget-review/SKILL.md",
            "skills/new-skill/SKILL.md",
            "workspace/.gitkeep",
        ],
    )
    def test_the_tracked_files_are_editable(self, project, rel):
        assert verdict(project, project / "agent_home" / rel) == ALLOW

    def test_the_household_profile_is_refused(self, project):
        result = run_guard(project, project / "agent_home" / "AGENTS.md")
        assert result.returncode == DENY
        assert "DEFAULT_PROFILE" in result.stderr

    @pytest.mark.parametrize(
        "rel",
        [
            "workspace/statement.csv",
            "conversation_history/thread.md",
            "large_tool_results/call_1",
            "blobs/abc",
            # A folder no library writes yet: refused because it is not allowed,
            # not because anyone listed it.
            "some_future_runtime_folder/state.json",
            "notes.txt",
        ],
    )
    def test_everything_else_under_the_home_is_refused(self, project, rel):
        assert verdict(project, project / "agent_home" / rel) == DENY

    @pytest.mark.parametrize(
        "path",
        [
            "agent_home/skills/../AGENTS.md",
            # Through a directory that does not exist yet, so the walk cannot
            # lean on the filesystem to collapse the "..".
            "agent_home/skills/not-yet/../../workspace/statement.csv",
        ],
    )
    def test_a_traversal_out_of_skills_is_refused(self, project, path):
        assert verdict(project, f"{project}/{path}") == DENY

    def test_a_relative_path_is_resolved_against_the_project(self, project):
        assert verdict(project, "agent_home/AGENTS.md") == DENY
        assert verdict(project, "agent_home/skills/budget-review/SKILL.md") == ALLOW

    def test_notebook_edits_are_judged_by_notebook_path(self, project):
        target = project / "agent_home" / "workspace" / "analysis.ipynb"
        assert verdict(project, target, key="notebook_path") == DENY


class TestARedirectedHome:
    """`FINANCIAL_PLANNER_HOME` moves the home, and the guard has to move with it."""

    @pytest.fixture
    def home(self, tmp_path: Path) -> Path:
        home = (tmp_path / "homes" / "alice").resolve()
        (home / "skills").mkdir(parents=True)
        return home

    def test_the_allowlist_follows_the_home(self, project, home):
        assert verdict(project, home / "AGENTS.md", home=home) == DENY
        assert verdict(project, home / "workspace" / "statement.csv", home=home) == DENY
        assert verdict(project, home / "skills" / "x" / "SKILL.md", home=home) == ALLOW

    def test_the_database_is_the_sibling_named_after_the_home(self, project, home):
        assert verdict(project, home.parent / "alice-state.sqlite", home=home) == DENY
        # Exact match, not *.sqlite: the name is what separates households.
        assert verdict(project, home.parent / "bob-state.sqlite", home=home) == ALLOW

    def test_the_old_default_home_is_no_longer_special(self, project, home):
        assert verdict(project, project / "agent_home" / "AGENTS.md", home=home) == ALLOW

    def test_the_home_can_come_from_dotenv(self, project, home):
        (project / ".env").write_text(f'ANTHROPIC_API_KEY=x\nFINANCIAL_PLANNER_HOME="{home}"\n')
        assert verdict(project, home / "AGENTS.md") == DENY
        assert verdict(project, home.parent / "alice-state.sqlite") == DENY

    def test_the_environment_wins_over_dotenv(self, project, home, tmp_path):
        (project / ".env").write_text(f"FINANCIAL_PLANNER_HOME={tmp_path / 'elsewhere'}\n")
        assert verdict(project, home / "AGENTS.md", home=home) == DENY


class TestAHomeAtOrAboveTheProjectRoot:
    """The allowlist would refuse every edit in the repository, so it stands down.

    That layout already breaks the agent's filesystem boundary and is not
    supported; the guard only declines to make it worse by blocking all work.
    """

    @pytest.mark.parametrize("level", ["root", "parent"])
    def test_ordinary_files_stay_editable(self, project, level):
        home = project if level == "root" else project.parent
        assert verdict(project, project / "src" / "app.py", home=home) == ALLOW

    @pytest.mark.parametrize("level", ["root", "parent"])
    def test_dotenv_stays_refused(self, project, level):
        home = project if level == "root" else project.parent
        assert verdict(project, project / ".env", home=home) == DENY
