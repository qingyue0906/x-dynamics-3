from __future__ import annotations

import asyncio

import pytest

from xdyn.config import load_config
from xdyn.errors import XTransactionError
from xdyn.transaction import TransactionProvider


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        value = self.responses.get(url, (404, ""))
        if isinstance(value, Exception):
            raise value
        status, text = value
        return FakeResponse(status, text)

    async def close(self):
        pass


PAGE = """<html><head>
<meta name="twitter-site-verification" content="abc">
</head><body><svg id="loading-x-anim-0"></svg></body></html>"""


class FakeTransaction:
    def generate_transaction_id(self, method, path):
        return "tid-" + method + "-" + path


def test_public_embed_is_used_before_authenticated_pages(tmp_path, monkeypatch):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    auth = FakeClient({})
    public = FakeClient({"https://x.com/i/embed": (200, PAGE)})
    auth.calls.clear()
    provider = TransactionProvider(config, auth)

    async def get_public():
        return public

    monkeypatch.setattr(provider, "_public_client", get_public)
    monkeypatch.setattr(provider, "_extract_signing", lambda soup, html, client: _signing())
    monkeypatch.setattr(provider, "_build_transaction", lambda soup, js: FakeTransaction())

    async def run():
        await provider.ensure()
        return await provider.generate("GET", "/i/api/graphql/x/UserByScreenName")

    result = asyncio.run(run())
    assert result.startswith("tid-GET-")
    assert public.calls == ["https://x.com/i/embed"]
    assert auth.calls == []
    assert "transaction=ok" in provider.last_diagnostics[-1]


async def _signing():
    return "fake-signing-js"


def test_transaction_failure_reports_each_source(tmp_path, monkeypatch):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    auth = FakeClient({
        "https://x.com/tesla": (401, ""),
        "https://x.com/home": (401, ""),
    })
    public = FakeClient({"https://x.com/i/embed": (401, "")})
    provider = TransactionProvider(config, auth)

    async def get_public():
        return public

    monkeypatch.setattr(provider, "_public_client", get_public)
    with pytest.raises(XTransactionError) as excinfo:
        asyncio.run(provider.ensure())
    message = str(excinfo.value)
    assert "i/embed(public)" in message
    assert "tesla(auth)" in message
    assert "home(auth)" in message
    assert "HTTP 401" in message
