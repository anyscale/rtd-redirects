"""Request-time model of how Read the Docs resolves a URL through redirects.

``validate.py`` reasons about rules in closed form: which rules can shadow or
chain into which. It can't say where a given URL actually lands, because that
depends on which pages exist on each version, and ``force: false`` rules only
fire on a 404. This module answers that question for one URL on one version,
given a ``RedirectSet`` and the set of pages each version serves.

Semantics modeled, per the README:

- Rules are tried in ``position`` order; the first match wins.
- ``force: false`` (the default) fires only when the requested page 404s.
  ``force: true`` fires even when the page exists.
- ``page`` rules match the path after ``/<lang>/<version>``. ``exact`` rules
  match the full ``/<lang>/<version>/...`` path.
- A single suffix ``*`` in ``from``, with ``:splat`` in ``to`` substituting the
  matched remainder.
- Fragments never reach the server, so a ``from`` containing ``#`` never
  matches, and a ``#`` in ``to`` doesn't change which page is served.
- A ``to`` under ``/<lang>/<version>/`` switches to that version's page set. A
  path-only ``to`` stays on the current version.
- Each redirect is a client-side 3xx, so resolution repeats until no rule
  fires. Revisiting a URL, or exceeding ``max_hops``, is a loop.

Disabled rules and the URL-style types (``clean_url_to_html`` /
``html_to_clean_url``) are ignored: the first never fires, and the second
describes a project-wide URL scheme rather than a per-URL rule.

Kept separate from ``simulate.py`` so other checks, such as a removed-URL
coverage guard, can share one resolver.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import Literal

from rtd_redirects.expand import DEFAULT_LANGUAGE_PREFIX, is_external
from rtd_redirects.model import URL_STYLE_TYPES, Redirect, RedirectSet

Status = Literal["ok", "404", "loop", "external", "unmodeled"]

DEFAULT_MAX_HOPS = 10


@dataclass(frozen=True)
class Resolution:
    """Where one request ends up.

    ``status`` is one of:

    - ``ok``: a page that exists on ``version``.
    - ``404``: no page, and no rule fired.
    - ``loop``: the redirects revisit a URL or exceed the hop limit.
    - ``external``: a redirect left the site; ``path`` holds the full URL.
    - ``unmodeled``: a redirect switched to a version with no page set, so
      the final outcome is unknown. ``version`` names that version.

    ``hops`` counts the redirects followed. ``rules`` lists the rules that
    fired, in order.
    """

    version: str
    path: str
    hops: int
    status: Status
    rules: tuple[Redirect, ...] = ()

    @property
    def resolved(self) -> bool:
        """True when a reader reaches content: a real page or an external target."""
        return self.status in ("ok", "external")

    @property
    def landing(self) -> tuple[str, str]:
        """``(version, path)`` of the final URL, for comparing two resolutions."""
        if self.status == "external":
            return ("", self.path)
        return (self.version, self.path)


class Resolver:
    """Resolve URLs through one redirect set against per-version page sets.

    ``pages`` maps each version slug to the version-relative page paths it
    serves, such as ``/core/actors.html``. A path ending in ``/`` also exists
    when its ``index.html`` does.
    """

    def __init__(
        self,
        rules: RedirectSet,
        pages: Mapping[str, Set[str]],
        *,
        language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
        max_hops: int = DEFAULT_MAX_HOPS,
    ) -> None:
        self._rules = [
            r for r in rules if r.enabled and r.type not in URL_STYLE_TYPES
        ]
        self._pages = pages
        self._language_prefix = language_prefix
        self._max_hops = max_hops
        self._versioned = re.compile(rf"^{re.escape(language_prefix)}/([^/#?]+)(/[^#]*)?")

    def exists(self, version: str, path: str) -> bool:
        pages = self._pages[version]
        if path in pages:
            return True
        return path.endswith("/") and f"{path}index.html" in pages

    def resolve(self, version: str, path: str) -> Resolution:
        """Follow redirects from ``/<lang>/<version><path>`` until none fires."""
        if version not in self._pages:
            raise KeyError(f"no page set for version {version!r}")
        seen = {(version, path)}
        fired: list[Redirect] = []
        while True:
            exists = self.exists(version, path)
            hit = self._first_match(version, path, exists)
            if hit is None:
                return Resolution(
                    version, path, len(fired), "ok" if exists else "404", tuple(fired),
                )
            rule, splat = hit
            fired.append(rule)
            target = rule.to_url.replace(":splat", splat)
            if is_external(target):
                return Resolution(version, target, len(fired), "external", tuple(fired))
            target = target.split("#", 1)[0]
            m = self._versioned.match(target)
            if m:
                version, path = m.group(1), m.group(2) or "/"
                if version not in self._pages:
                    return Resolution(version, path, len(fired), "unmodeled", tuple(fired))
            else:
                path = target
            if (version, path) in seen or len(fired) > self._max_hops:
                return Resolution(version, path, len(fired), "loop", tuple(fired))
            seen.add((version, path))

    def _first_match(
        self, version: str, path: str, exists: bool,
    ) -> tuple[Redirect, str] | None:
        full = f"{self._language_prefix}/{version}{path}"
        for rule in self._rules:
            if exists and not rule.force:
                continue
            splat = match(rule, path, full)
            if splat is not None:
                return rule, splat
        return None


def match(rule: Redirect, path: str, full: str) -> str | None:
    """Return the ``:splat`` remainder if ``rule`` matches, else ``None``.

    A non-wildcard match returns ``""``. ``path`` is version-relative and is
    what ``page`` rules see; ``full`` includes ``/<lang>/<version>`` and is
    what ``exact`` rules see.
    """
    frm = rule.from_url
    if "#" in frm:
        return None
    target = full if rule.type == "exact" else path
    if frm.endswith("*"):
        prefix = frm[:-1]
        return target[len(prefix):] if target.startswith(prefix) else None
    return "" if target == frm else None
