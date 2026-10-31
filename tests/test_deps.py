"""Manifest -> (name, version) parsing for Dependency nodes. Token-free, no Joern, no DB.

Covers each supported manifest and the defensive contract: a malformed manifest yields [] rather
than crashing the build.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orion.graph.deps import parse_dependencies


def test_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"express": "^4.13.4", "marked": "0.3.5"},
        "devDependencies": {"mocha": "^3.0.0"},
    }))
    deps = dict(parse_dependencies(tmp_path))
    assert deps["express"] == "^4.13.4"
    assert deps["marked"] == "0.3.5"
    assert deps["mocha"] == "^3.0.0"


def test_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "Flask==2.0.1\nrequests>=2.20\n# a comment\n-e .\nDjango~=4.1\n"
    )
    deps = dict(parse_dependencies(tmp_path))
    assert deps["Flask"] == "==2.0.1"
    assert deps["requests"] == ">=2.20"
    assert deps["Django"] == "~=4.1"
    assert "#" not in " ".join(deps)  # the comment line produced no phantom dep


def test_pom_xml(tmp_path):
    (tmp_path / "pom.xml").write_text(
        """<project><dependencies>
             <dependency><groupId>org.springframework</groupId>
               <artifactId>spring-core</artifactId><version>5.3.0</version></dependency>
             <dependency><groupId>com.google</groupId>
               <artifactId>guava</artifactId><version>30.0</version></dependency>
           </dependencies></project>"""
    )
    deps = dict(parse_dependencies(tmp_path))
    assert deps["spring-core"] == "5.3.0"
    assert deps["guava"] == "30.0"


def test_requirements_keeps_packages_named_like_http(tmp_path):
    """The URL-line skip must match real schemes, not any package NAMED 'http...' (httpx/httplib2)."""
    (tmp_path / "requirements.txt").write_text(
        "httpx==0.27\nhttplib2==0.22\nrequests==2.31\nhttps://ex.com/pkg.whl\n"
    )
    deps = dict(parse_dependencies(tmp_path))
    assert deps["httpx"] == "==0.27"
    assert deps["httplib2"] == "==0.22"
    assert deps["requests"] == "==2.31"


def test_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\ngo 1.21\n\nrequire (\n"
        "\tgithub.com/gin-gonic/gin v1.9.1\n\tgithub.com/pkg/errors v0.9.1\n)\n"
    )
    deps = dict(parse_dependencies(tmp_path))
    assert deps["github.com/gin-gonic/gin"] == "v1.9.1"
    assert deps["github.com/pkg/errors"] == "v0.9.1"


def test_malformed_manifest_yields_empty_not_crash(tmp_path):
    (tmp_path / "package.json").write_text("{ not valid json ]")
    assert parse_dependencies(tmp_path) == []


@pytest.mark.skipif(not Path("fixtures/NodeGoat").exists(), reason="NodeGoat fixture not present")
def test_nodegoat_dependencies_are_populated():
    deps = dict(parse_dependencies("fixtures/NodeGoat"))
    assert "express" in deps          # the A9 data-gap is now closed for a real repo
    assert len(deps) > 5


def test_pyproject_pep621_and_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n'
        'dependencies = ["django>=3.2,<4; python_version > \'3.8\'", "requests[socks]==2.19.0", "pkg @ https://x/y.whl"]\n'
        '[project.optional-dependencies]\ntest = ["pytest>=8"]\n'
        '[tool.poetry.dependencies]\npython = "^3.10"\nflask = "^2.0"\nlxml = {version = "4.9.1", extras = ["x"]}\n'
        'mylib = {git = "https://example.com/mylib.git"}\n'
        '[tool.poetry.group.dev.dependencies]\nblack = "23.1"\n', encoding="utf-8")
    assert parse_dependencies(tmp_path) == [
        ("django", ">=3.2,<4"), ("requests", "==2.19.0"), ("pkg", ""), ("pytest", ">=8"),
        ("flask", "^2.0"), ("lxml", "4.9.1"), ("mylib", ""), ("black", "23.1")]


def test_requirements_pin_wins_over_a_pyproject_range(tmp_path):
    (tmp_path / "requirements.txt").write_text("django==3.2.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["django>=3"]\n',
                                             encoding="utf-8")
    assert parse_dependencies(tmp_path) == [("django", "==3.2.0")]


def test_malformed_pyproject_yields_empty(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project\ndependencies = [", encoding="utf-8")
    assert parse_dependencies(tmp_path) == []
