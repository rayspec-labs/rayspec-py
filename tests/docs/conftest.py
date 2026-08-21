"""Shared fixtures for the docs tests: repository paths and the markdown files under test."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def docs_dir() -> Path:
    return DOCS_DIR


@pytest.fixture(scope="session")
def markdown_files() -> list[Path]:
    """Every user-facing markdown page: ``docs/*.md`` plus the top-level ``README.md``."""
    return [README, *sorted(DOCS_DIR.glob("*.md"))]
