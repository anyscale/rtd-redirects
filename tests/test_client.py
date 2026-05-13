"""Tests for rtd_redirects.client: RtdClient, _TokenBucket, API serialization."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from rtd_redirects.client import (
    DEFAULT_BASE_URL,
    RtdAuthError,
    RtdClient,
    RtdClientError,
    RtdRateLimitError,
    _from_api,
    _to_api,
    _TokenBucket,
)
from rtd_redirects.model import Redirect


def _mock_response(
    status_code: int,
    json_data: Any = None,
    headers: dict | None = None,
    text: str = "",
) -> MagicMock:
    """Build a Mock response with the attributes RtdClient inspects."""
    m = MagicMock(spec=requests.Response)
    m.status_code = status_code
    m.headers = headers or {}
    m.json.return_value = {} if json_data is None else json_data
    m.text = text
    return m


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock(spec=requests.Session)


@pytest.fixture
def client(mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch) -> RtdClient:
    """Client with rate limiting and sleeps disabled for fast unit tests."""
    monkeypatch.setattr(_TokenBucket, "acquire", lambda self: None)
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kw: None)
    monkeypatch.setenv("RTD_API_TOKEN", "test-token")
    return RtdClient("test-project", session=mock_session)


class TestAuth:
    def test_token_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "env-token")
        client = RtdClient("any-project")
        assert client.token == "env-token"

    def test_token_kwarg_overrides_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "env-token")
        client = RtdClient("any-project", token="kwarg-token")
        assert client.token == "kwarg-token"

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("RTD_API_TOKEN", raising=False)
        with pytest.raises(RtdAuthError, match="RTD_API_TOKEN is not set"):
            RtdClient("any-project")

    def test_empty_token_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "")
        with pytest.raises(RtdAuthError):
            RtdClient("any-project")

    def test_authorization_header_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "secret-abc")
        client = RtdClient("any-project")
        assert client._session.headers["Authorization"] == "Token secret-abc"
        assert client._session.headers["Accept"] == "application/json"


class TestBaseUrl:
    def test_default_base_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "t")
        monkeypatch.delenv("RTD_BASE_URL", raising=False)
        client = RtdClient("p")
        assert client.base_url == DEFAULT_BASE_URL

    def test_kwarg_overrides_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "t")
        client = RtdClient("p", base_url="https://readthedocs.org/api/v3")
        assert client.base_url == "https://readthedocs.org/api/v3"

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "t")
        monkeypatch.setenv("RTD_BASE_URL", "https://elsewhere/api/v3")
        client = RtdClient("p")
        assert client.base_url == "https://elsewhere/api/v3"

    def test_trailing_slash_stripped(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "t")
        client = RtdClient("p", base_url="https://example.com/api/v3/")
        assert client.base_url == "https://example.com/api/v3"

    def test_url_properties_include_project_slug(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RTD_API_TOKEN", "t")
        client = RtdClient("anyscale-ray")
        assert client.redirects_url.endswith("/projects/anyscale-ray/redirects/")
        assert client.versions_url.endswith("/projects/anyscale-ray/versions/")


class TestApiSerialization:
    def test_to_api_includes_all_fields(self):
        r = Redirect(
            from_url="/a", to_url="/b", type="exact",
            http_status=302, force=True, enabled=False,
            position=5, description="note",
        )
        d = _to_api(r)
        assert d == {
            "from_url": "/a", "to_url": "/b", "type": "exact",
            "http_status": 302, "force": True, "enabled": False,
            "position": 5, "description": "note",
        }

    def test_to_api_excludes_pk(self):
        r = Redirect(from_url="/a", to_url="/b", type="exact", pk=42)
        assert "pk" not in _to_api(r)
        assert "id" not in _to_api(r)

    def test_from_api_full(self):
        r = _from_api({
            "pk": 7, "from_url": "/a", "to_url": "/b", "type": "exact",
            "http_status": 301, "force": False, "enabled": True,
            "position": 3, "description": "x",
        })
        assert r == Redirect(from_url="/a", to_url="/b", type="exact", position=3, description="x")
        assert r.pk == 7

    def test_from_api_accepts_id_alias(self):
        """Some RtD API responses key the primary key as ``id`` rather than ``pk``."""
        r = _from_api({"id": 99, "from_url": "/a", "to_url": "/b", "type": "exact"})
        assert r.pk == 99

    def test_from_api_null_description_becomes_empty(self):
        r = _from_api({"from_url": "/a", "to_url": "/b", "type": "exact", "description": None})
        assert r.description == ""

    def test_roundtrip(self):
        original = Redirect(from_url="/a", to_url="/b", type="exact", position=2)
        roundtripped = _from_api({**_to_api(original), "pk": 1})
        assert roundtripped == original


class TestListRedirects:
    def test_single_page(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(200, {
            "results": [
                {"pk": 1, "from_url": "/a", "to_url": "/b", "type": "exact"},
                {"pk": 2, "from_url": "/c", "to_url": "/d", "type": "page"},
            ],
            "next": None,
        })
        results = client.list_redirects()
        assert [r.from_url for r in results] == ["/a", "/c"]
        assert results[0].pk == 1

    def test_pagination_follows_next(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.side_effect = [
            _mock_response(200, {
                "results": [{"pk": 1, "from_url": "/a", "to_url": "/b", "type": "exact"}],
                "next": "https://readthedocs.com/api/v3/projects/test-project/redirects/?page=2",
            }),
            _mock_response(200, {
                "results": [{"pk": 2, "from_url": "/c", "to_url": "/d", "type": "exact"}],
                "next": None,
            }),
        ]
        results = client.list_redirects()
        assert len(results) == 2
        assert mock_session.request.call_count == 2
        assert mock_session.request.call_args_list[1].args[1].endswith("?page=2")

    def test_empty(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(200, {"results": [], "next": None})
        assert client.list_redirects() == []


class TestListVersions:
    def test_returns_active_slugs(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(200, {
            "results": [
                {"slug": "latest", "active": True},
                {"slug": "master", "active": True},
                {"slug": "v1.0", "active": False},
            ],
            "next": None,
        })
        assert client.list_versions() == ["latest", "master"]

    def test_only_active_false_returns_all(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(200, {
            "results": [
                {"slug": "latest", "active": True},
                {"slug": "v1.0", "active": False},
            ],
            "next": None,
        })
        assert client.list_versions(only_active=False) == ["latest", "v1.0"]


class TestMutations:
    def test_create_posts_and_returns_record(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(201, {
            "pk": 10, "from_url": "/a", "to_url": "/b", "type": "exact",
        })
        result = client.create_redirect(Redirect(from_url="/a", to_url="/b", type="exact"))
        assert result.pk == 10
        method, url = mock_session.request.call_args.args[:2]
        assert method == "POST"
        assert url == client.redirects_url
        assert mock_session.request.call_args.kwargs["json"]["from_url"] == "/a"

    def test_update_puts_to_pk_url(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(200, {
            "pk": 10, "from_url": "/a", "to_url": "/b2", "type": "exact",
        })
        result = client.update_redirect(10, Redirect(from_url="/a", to_url="/b2", type="exact"))
        assert result.to_url == "/b2"
        method, url = mock_session.request.call_args.args[:2]
        assert method == "PUT"
        assert url.endswith("/redirects/10/")

    def test_delete_calls_pk_url_and_returns_none(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(204)
        result = client.delete_redirect(10)
        assert result is None
        method, url = mock_session.request.call_args.args[:2]
        assert method == "DELETE"
        assert url.endswith("/redirects/10/")


class TestRetryAndErrors:
    def test_429_retries_with_retry_after(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.side_effect = [
            _mock_response(429, headers={"Retry-After": "1"}),
            _mock_response(200, {"results": [], "next": None}),
        ]
        assert client.list_redirects() == []
        assert mock_session.request.call_count == 2

    def test_429_exhaustion_raises(self, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(_TokenBucket, "acquire", lambda self: None)
        monkeypatch.setattr(time, "sleep", lambda *_args, **_kw: None)
        monkeypatch.setenv("RTD_API_TOKEN", "t")
        c = RtdClient("p", session=mock_session, max_retries=2)
        mock_session.request.return_value = _mock_response(429, headers={"Retry-After": "1"})
        with pytest.raises(RtdRateLimitError):
            c.list_redirects()
        assert mock_session.request.call_count == 3  # initial + 2 retries

    def test_401_raises_auth_error(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(401)
        with pytest.raises(RtdAuthError, match="check RTD_API_TOKEN"):
            client.list_redirects()

    def test_5xx_raises_client_error(self, client: RtdClient, mock_session: MagicMock):
        mock_session.request.return_value = _mock_response(500, text="internal server error")
        with pytest.raises(RtdClientError, match="500"):
            client.list_redirects()

    def test_network_error_retries_then_raises(
        self, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(_TokenBucket, "acquire", lambda self: None)
        monkeypatch.setattr(time, "sleep", lambda *_args, **_kw: None)
        monkeypatch.setenv("RTD_API_TOKEN", "t")
        c = RtdClient("p", session=mock_session, max_retries=2)
        mock_session.request.side_effect = requests.ConnectionError("boom")
        with pytest.raises(RtdClientError, match="failed"):
            c.list_redirects()
        assert mock_session.request.call_count == 3

    def test_missing_retry_after_falls_back_to_60s(
        self, client: RtdClient, mock_session: MagicMock
    ):
        mock_session.request.side_effect = [
            _mock_response(429, headers={}),
            _mock_response(200, {"results": [], "next": None}),
        ]
        assert client.list_redirects() == []


class TestTokenBucket:
    def test_initial_capacity_burst(self, monkeypatch: pytest.MonkeyPatch):
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        bucket = _TokenBucket(rpm=10)
        for _ in range(10):
            bucket.acquire()
        assert bucket._tokens < 1
        assert slept == []  # initial burst should not need to sleep

    def test_refills_over_time(self, monkeypatch: pytest.MonkeyPatch):
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        bucket = _TokenBucket(rpm=60)  # 1 token/sec
        for _ in range(60):
            bucket.acquire()
        # Fake the clock advancing 2s and discard any warmup sleeps.
        bucket._last_refill -= 2.0
        slept.clear()
        bucket.acquire()
        assert slept == []  # had >=1 token after refill

    def test_sleeps_when_depleted(self, monkeypatch: pytest.MonkeyPatch):
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        bucket = _TokenBucket(rpm=60)
        for _ in range(60):
            bucket.acquire()
        slept.clear()  # discard any warmup sleeps from imprecise timing
        bucket.acquire()
        assert len(slept) >= 1
