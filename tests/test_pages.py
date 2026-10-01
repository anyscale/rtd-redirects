"""Tests for rtd_redirects.pages."""

from __future__ import annotations

import subprocess
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from rtd_redirects.pages import PageSourceError, load_pages, source_to_page


def _inventory(lines: list[str]) -> bytes:
    header = (
        b"# Sphinx inventory version 2\n# Project: X\n# Version: \n"
        b"# The remainder of this file is compressed using zlib.\n"
    )
    return header + zlib.compress("\n".join(lines).encode())


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class TestHtmlDir:
    def test_every_html_file_is_a_page(self, tmp_path: Path):
        (tmp_path / "core").mkdir()
        (tmp_path / "index.html").write_text("")
        (tmp_path / "core" / "actors.html").write_text("")
        (tmp_path / "core" / "style.css").write_text("")
        assert load_pages(f"html:{tmp_path}") == {"/index.html", "/core/actors.html"}

    def test_bare_directory_is_inferred(self, tmp_path: Path):
        (tmp_path / "a.html").write_text("")
        assert load_pages(str(tmp_path)) == {"/a.html"}

    def test_missing_directory(self, tmp_path: Path):
        with pytest.raises(PageSourceError, match="not a directory"):
            load_pages(f"html:{tmp_path / 'nope'}")


class TestInventory:
    def test_std_doc_entries_are_pages(self, tmp_path: Path):
        inv = tmp_path / "objects.inv"
        inv.write_bytes(_inventory([
            "index std:doc -1 index.html Home",
            "_collections/x/README std:doc -1 _collections/x/README.html X",
            "core/actors std:doc -1 $.html Actors",
            "ray.init py:function 1 api/ray.init.html#$ -",
            "my-label std:label -1 core/actors.html#my-label Label",
        ]))
        assert load_pages(str(inv)) == {
            "/index.html", "/_collections/x/README.html", "/core/actors.html",
        }

    def test_not_an_inventory(self, tmp_path: Path):
        bad = tmp_path / "objects.inv"
        bad.write_bytes(b"hello")
        with pytest.raises(PageSourceError, match="Sphinx v2"):
            load_pages(f"inv:{bad}")

    def test_corrupt_stream(self, tmp_path: Path):
        bad = tmp_path / "objects.inv"
        bad.write_bytes(b"# Sphinx inventory version 2\n#\n#\n#\nnot zlib")
        with pytest.raises(PageSourceError, match="decompress"):
            load_pages(f"inv:{bad}")

    def test_fetches_url(self):
        response = MagicMock(content=_inventory(["index std:doc -1 index.html Home"]))
        with patch("rtd_redirects.pages.requests.get", return_value=response) as get:
            pages = load_pages("inv:https://docs.example.com/en/latest/objects.inv")
        assert pages == {"/index.html"}
        get.assert_called_once()

    def test_fetch_failure(self):
        with patch(
            "rtd_redirects.pages.requests.get",
            side_effect=requests.ConnectionError("boom"),
        ), pytest.raises(PageSourceError, match="couldn't fetch"):
            load_pages("inv:https://docs.example.com/objects.inv")

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(PageSourceError, match="couldn't read"):
            load_pages(f"inv:{tmp_path / 'objects.inv'}")


class TestSitemap:
    def test_strips_host_and_version(self, tmp_path: Path):
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://docs.example.com/en/latest/a.html</loc></url>"
            "<url><loc>https://docs.example.com/en/master/core/b.html</loc></url>"
            "<url><loc>https://docs.example.com/other.html</loc></url>"
            "</urlset>"
        )
        assert load_pages(str(sitemap)) == {"/a.html", "/core/b.html", "/other.html"}

    def test_bad_xml(self, tmp_path: Path):
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text("<urlset>")
        with pytest.raises(PageSourceError, match="parse sitemap"):
            load_pages(f"sitemap:{sitemap}")


class TestGit:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.email", "t@example.com")
        _git(tmp_path, "config", "user.name", "t")
        _git(tmp_path, "config", "commit.gpgsign", "false")
        src = tmp_path / "doc" / "source"
        (src / "core").mkdir(parents=True)
        (src / "index.md").write_text("")
        (src / "core" / "actors.rst").write_text("")
        (src / "core" / "nb.ipynb").write_text("")
        (src / "conf.py").write_text("")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-qm", "init")
        return tmp_path

    def test_sources_map_to_html(self, repo: Path):
        pages = load_pages("git:HEAD:doc/source", repo_path=repo)
        assert pages == {"/index.html", "/core/actors.html", "/core/nb.html"}

    def test_worktree_reads_disk(self, repo: Path):
        (repo / "doc" / "source" / "new.md").write_text("")
        pages = load_pages("git:WORKTREE:doc/source/", repo_path=repo)
        assert "/new.html" in pages
        assert "/new.html" not in load_pages("git:HEAD:doc/source", repo_path=repo)

    def test_bad_ref(self, repo: Path):
        with pytest.raises(PageSourceError, match="ls-tree"):
            load_pages("git:nope:doc/source", repo_path=repo)

    def test_missing_worktree_dir(self, repo: Path):
        with pytest.raises(PageSourceError, match="not a directory"):
            load_pages("git:WORKTREE:doc/missing", repo_path=repo)

    def test_malformed_spec(self):
        with pytest.raises(PageSourceError, match="git:REF:SRCDIR"):
            load_pages("git:HEAD")


class TestList:
    def test_reads_paths(self, tmp_path: Path):
        f = tmp_path / "pages.txt"
        f.write_text("# comment\n/a.html\n\n  /b.html  \n")
        assert load_pages(f"list:{f}") == {"/a.html", "/b.html"}

    def test_missing(self, tmp_path: Path):
        with pytest.raises(PageSourceError, match="couldn't read"):
            load_pages(f"list:{tmp_path / 'nope.txt'}")


def test_unknown_spec_kind():
    with pytest.raises(PageSourceError, match="can't tell"):
        load_pages("pages.txt")


def test_source_to_page():
    assert source_to_page("doc/source/core/a.md", "doc/source") == "/core/a.html"
    assert source_to_page("doc/source/core/a.md", "doc/source/") == "/core/a.html"
