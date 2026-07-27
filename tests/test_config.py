"""Configuration, path resolution, and .env loading.

The .env tests exist because of a real defect: python-dotenv was a declared
dependency that nothing ever called, so a key placed in .env was silently
ignored and surfaced as an opaque authentication error rather than an obvious
missing-key one. These are the regression tests for that.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ticketpilot.config import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODEL,
    Settings,
    api_key_status,
    data_dir,
    repo_root,
)


class TestPathResolution:
    def test_repo_root_contains_the_knowledge_base(self) -> None:
        assert (repo_root() / "data" / "kb.json").is_file()

    def test_paths_do_not_depend_on_the_working_directory(self, tmp_path, monkeypatch) -> None:
        # The suite must pass from any cwd on a clean machine, so nothing may be
        # resolved relative to where the runner happened to be invoked.
        before = data_dir()
        monkeypatch.chdir(tmp_path)
        assert data_dir() == before

    def test_data_dir_is_overridable(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TICKETPILOT_DATA_DIR", str(tmp_path))
        assert data_dir() == tmp_path


class TestSettings:
    def test_defaults_apply_when_nothing_is_set(self, monkeypatch) -> None:
        for name in (
            "ANTHROPIC_MODEL",
            "TICKETPILOT_PROVIDER",
            "TICKETPILOT_EFFORT",
            "TICKETPILOT_CONFIDENCE_THRESHOLD",
        ):
            monkeypatch.delenv(name, raising=False)
        settings = Settings.from_env()
        assert settings.model == DEFAULT_MODEL
        assert settings.provider == "anthropic"
        assert settings.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD

    def test_empty_value_falls_back_to_the_default(self, monkeypatch) -> None:
        # An empty assignment in a .env file (VAR=) sets an empty string, which
        # must not shadow the default with "".
        monkeypatch.setenv("ANTHROPIC_MODEL", "")
        assert Settings.from_env().model == DEFAULT_MODEL

    def test_overrides_are_honoured(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
        monkeypatch.setenv("TICKETPILOT_CONFIDENCE_THRESHOLD", "0.5")
        settings = Settings.from_env()
        assert settings.model == "claude-sonnet-5"
        assert settings.confidence_threshold == 0.5

    def test_malformed_numeric_override_does_not_crash(self, monkeypatch) -> None:
        # A typo in .env should not take the service down on startup.
        monkeypatch.setenv("TICKETPILOT_CONFIDENCE_THRESHOLD", "very high")
        assert Settings.from_env().confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD


class TestApiKeyStatus:
    def test_missing(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert api_key_status() == "missing"

    def test_empty_is_distinct_from_missing(self, monkeypatch) -> None:
        # The distinction matters: an empty key still occupies its precedence
        # slot and gets sent, producing an auth error that looks like a bad key
        # rather than an unfilled template.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert api_key_status() == "empty"

    def test_whitespace_only_counts_as_empty(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        assert api_key_status() == "empty"

    def test_present(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert api_key_status() == "present"


class TestDotenvIsActuallyLoaded:
    """Regression tests for the defect described in this module's docstring."""

    def test_dotenv_is_importable(self) -> None:
        # If the dependency were dropped, loading would silently no-op.
        import dotenv  # noqa: F401

    def test_config_calls_load_dotenv(self) -> None:
        # Asserts the wiring exists, not merely that the dependency is installed.
        source = (Path(repo_root()) / "src" / "ticketpilot" / "config.py").read_text(
            encoding="utf-8"
        )
        assert "load_dotenv" in source

    def test_a_key_in_dotenv_reaches_the_environment(self, tmp_path) -> None:
        """End-to-end: write a .env, import the package, read the value back.

        Run in a subprocess with the project root relocated, because config is
        imported once per process and this needs a clean import against a
        different root.
        """
        root = tmp_path / "proj"
        (root / "data").mkdir(parents=True)
        # repo_root() finds the project by looking for data/kb.json.
        (root / "data" / "kb.json").write_text('{"articles": []}', encoding="utf-8")
        (root / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-ant-from-dotenv\nANTHROPIC_MODEL=claude-from-dotenv\n",
            encoding="utf-8",
        )

        pkg_src = Path(repo_root()) / "src"
        # A package copy whose __file__ sits under the temporary root, so
        # repo_root() resolves there rather than to the real project.
        shim = root / "src" / "ticketpilot"
        shim.mkdir(parents=True)
        for name in ("__init__.py", "config.py"):
            (shim / name).write_text(
                (pkg_src / "ticketpilot" / name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        script = textwrap.dedent(
            """
            import os, sys
            sys.path.insert(0, sys.argv[1])
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_MODEL", None)
            from ticketpilot.config import Settings, api_key_status
            print(api_key_status())
            print(Settings.from_env().model)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(root / "src")],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        assert result.returncode == 0, result.stderr
        status, model = result.stdout.split()
        assert status == "present"
        assert model == "claude-from-dotenv"

    def test_real_environment_wins_over_dotenv(self, tmp_path) -> None:
        # override=False: an explicitly exported variable must beat the file, so
        # CI and shell overrides stay authoritative.
        root = tmp_path / "proj"
        (root / "data").mkdir(parents=True)
        (root / "data" / "kb.json").write_text('{"articles": []}', encoding="utf-8")
        (root / ".env").write_text("ANTHROPIC_MODEL=from-dotenv\n", encoding="utf-8")

        pkg_src = Path(repo_root()) / "src"
        shim = root / "src" / "ticketpilot"
        shim.mkdir(parents=True)
        for name in ("__init__.py", "config.py"):
            (shim / name).write_text(
                (pkg_src / "ticketpilot" / name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        env = dict(os.environ, ANTHROPIC_MODEL="from-real-environment")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]);"
                " from ticketpilot.config import Settings; print(Settings.from_env().model)",
                str(root / "src"),
            ],
            capture_output=True,
            text=True,
            cwd=str(root),
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "from-real-environment"


class TestShippedDotenvTemplate:
    """The committed .env.example must stay a template, never a populated file."""

    def test_example_contains_no_value(self) -> None:
        example = (Path(repo_root()) / ".env.example").read_text(encoding="utf-8")
        for line in example.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, value = line.partition("=")
            assert value.strip() == "", f"{name} has a value in .env.example"

    @pytest.mark.parametrize("marker", ["sk-ant-", "sk-"])
    def test_example_holds_no_key_like_string(self, marker: str) -> None:
        example = (Path(repo_root()) / ".env.example").read_text(encoding="utf-8")
        assert marker not in example
