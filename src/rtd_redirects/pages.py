"""Load the set of pages a docs version serves, for redirect simulation.

``force: false`` redirects fire only on a 404, so simulating a redirect change
needs to know which pages exist on each version. A page set is a frozenset of
version-relative paths such as ``/core/actors.html``.

A source spec is ``KIND:LOCATION``:

- ``html:DIR`` — a built HTML directory. Every ``*.html`` file is a page. The
  most faithful source for a local build.
- ``inv:PATH_OR_URL`` — a Sphinx ``objects.inv``. Every ``std:doc`` entry is a
  page. Includes build-generated pages, and RtD serves one per version, so
  it's the cheapest faithful source for a published version.
- ``sitemap:PATH_OR_URL`` — a ``sitemap.xml``. Each ``<loc>`` is a page, with
  the host and any ``/<lang>/<version>`` prefix stripped.
- ``git:REF:SRCDIR`` — Sphinx sources in a git tree. Each ``.md``, ``.rst``, or
  ``.ipynb`` file under ``SRCDIR`` maps to ``.html``. ``REF`` may be
  ``WORKTREE`` to read the files on disk instead. Cheap, but it misses pages
  the build generates, such as API stubs, so those read as 404s.
- ``list:PATH`` — a text file with one path per line. Blank lines and lines
  starting with ``#`` are skipped.

A spec without a kind is inferred from its shape: an existing directory is
``html``, a name ending in ``.inv`` is ``inv``, and one ending in ``.xml`` is
``sitemap``.
"""

from __future__ import annotations

import os
import re
import subprocess
import zlib
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from rtd_redirects.expand import DEFAULT_LANGUAGE_PREFIX

SOURCE_SUFFIXES = (".md", ".rst", ".ipynb")
KINDS = ("html", "inv", "sitemap", "git", "list")
_FETCH_TIMEOUT_SECONDS = 60


class PageSourceError(Exception):
    """Raised when a page-set spec is malformed or its source can't be read."""


def load_pages(
    spec: str,
    *,
    repo_path: str | Path | None = None,
    language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
) -> frozenset[str]:
    """Return the page set described by ``spec``. See the module docstring."""
    kind, location = _split_spec(spec)
    if kind == "html":
        return _from_html_dir(Path(location))
    if kind == "inv":
        return _from_inventory(_read_bytes(location))
    if kind == "sitemap":
        return _from_sitemap(_read_bytes(location), language_prefix)
    if kind == "git":
        ref, sep, srcdir = location.partition(":")
        if not sep or not ref or not srcdir:
            raise PageSourceError(f"expected git:REF:SRCDIR, got {spec!r}")
        return _from_git(ref, srcdir, repo_path)
    return _from_list(Path(location))


def spec_kind(spec: str) -> str:
    """The source kind ``spec`` names, such as ``inv`` or ``git``."""
    return _split_spec(spec)[0]


def _split_spec(spec: str) -> tuple[str, str]:
    kind, sep, location = spec.partition(":")
    if sep and kind in KINDS:
        return kind, location
    if Path(spec).is_dir():
        return "html", spec
    if spec.endswith(".inv"):
        return "inv", spec
    if spec.endswith(".xml"):
        return "sitemap", spec
    raise PageSourceError(
        f"can't tell what kind of page source {spec!r} is; prefix it with "
        f"one of {', '.join(k + ':' for k in KINDS)}"
    )


def _read_bytes(location: str) -> bytes:
    if location.startswith(("http://", "https://")):
        try:
            response = requests.get(location, timeout=_FETCH_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as e:
            raise PageSourceError(f"couldn't fetch {location}: {e}") from e
        return response.content
    try:
        return Path(location).read_bytes()
    except OSError as e:
        raise PageSourceError(f"couldn't read {location}: {e}") from e


def _from_html_dir(root: Path) -> frozenset[str]:
    if not root.is_dir():
        raise PageSourceError(f"{root} is not a directory")
    return frozenset(
        "/" + p.relative_to(root).as_posix() for p in root.rglob("*.html") if p.is_file()
    )


def _from_inventory(data: bytes) -> frozenset[str]:
    """Parse a Sphinx v2 inventory: four header lines, then a zlib stream."""
    lines = data.split(b"\n", 4)
    if len(lines) < 5 or not lines[0].startswith(b"# Sphinx inventory version 2"):
        raise PageSourceError("not a Sphinx v2 objects.inv")
    try:
        body = zlib.decompress(lines[4]).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as e:
        raise PageSourceError(f"couldn't decompress objects.inv: {e}") from e
    entry = re.compile(r"^(.+?)\s+std:doc\s+-?\d+\s+(\S+)")
    pages = set()
    for line in body.splitlines():
        m = entry.match(line)
        if not m:
            continue
        name, uri = m.groups()
        # ``$`` in the URI abbreviates the entry name.
        uri = uri.replace("$", name)
        pages.add("/" + uri.split("#", 1)[0])
    return frozenset(pages)


def _from_sitemap(data: bytes, language_prefix: str) -> frozenset[str]:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise PageSourceError(f"couldn't parse sitemap: {e}") from e
    versioned = re.compile(rf"^{re.escape(language_prefix)}/[^/]+(/.*)$")
    pages = set()
    for loc in root.iter():
        if not loc.tag.endswith("loc") or not loc.text:
            continue
        path = urlparse(loc.text.strip()).path
        m = versioned.match(path)
        pages.add(m.group(1) if m else path)
    return frozenset(pages)


def _from_git(ref: str, srcdir: str, repo_path: str | Path | None) -> frozenset[str]:
    srcdir = srcdir.rstrip("/")
    if ref == "WORKTREE":
        root = Path(repo_path or ".") / srcdir
        if not root.is_dir():
            raise PageSourceError(f"{root} is not a directory")
        files = [
            f"{srcdir}/{p.relative_to(root).as_posix()}"
            for p in root.rglob("*") if p.is_file()
        ]
    else:
        cmd = ["git"]
        if repo_path is not None:
            cmd.extend(["-C", str(repo_path)])
        cmd.extend(["ls-tree", "-r", "--name-only", ref, "--", srcdir])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise PageSourceError(
                f"git ls-tree {ref} {srcdir} failed: {result.stderr.strip()}"
            )
        files = result.stdout.split("\n")
    return frozenset(
        source_to_page(f, srcdir) for f in files
        if f.startswith(srcdir + "/") and f.endswith(SOURCE_SUFFIXES)
    )


def source_to_page(source_path: str, srcdir: str) -> str:
    """Map a Sphinx source path under ``srcdir`` to the page it builds."""
    rel = source_path[len(srcdir.rstrip("/")):]
    return os.path.splitext(rel)[0] + ".html"


def _from_list(path: Path) -> frozenset[str]:
    try:
        text = path.read_text()
    except OSError as e:
        raise PageSourceError(f"couldn't read {path}: {e}") from e
    return frozenset(
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
