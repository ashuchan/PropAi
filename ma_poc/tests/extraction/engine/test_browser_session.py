"""TDD tests for extraction.engine.browser_session.

Written before the module exists — these drive the extraction of browser
lifecycle management out of scripts/entrata.py.

Note: Tests that require an actual Playwright browser are marked with
@pytest.mark.integration and excluded from the standard test run.
"""
from __future__ import annotations

import pytest

from extraction.engine.browser_session import BrowserSession, _proxy_config


class TestProxyConfig:
    def test_none_proxy_returns_none(self):
        assert _proxy_config(None) is None

    def test_full_proxy_url_parsed(self):
        cfg = _proxy_config("http://user:pass@proxy.example.com:22225")
        assert cfg is not None
        assert cfg["server"] == "http://proxy.example.com:22225"
        assert cfg["username"] == "user"
        assert cfg["password"] == "pass"

    def test_bare_host_port_gets_http_scheme(self):
        cfg = _proxy_config("proxy.example.com:22225")
        assert cfg is not None
        assert "proxy.example.com" in cfg["server"]
        assert "22225" in cfg["server"]

    def test_malformed_proxy_returns_none(self):
        assert _proxy_config("not-a-proxy") is None

    def test_proxy_without_credentials(self):
        cfg = _proxy_config("http://proxy.example.com:8080")
        assert cfg is not None
        assert cfg["server"] == "http://proxy.example.com:8080"
        assert "username" not in cfg

    def test_url_encoded_credentials_decoded(self):
        cfg = _proxy_config("http://user%40domain:p%40ss@proxy.example.com:8080")
        assert cfg is not None
        assert cfg["username"] == "user@domain"
        assert cfg["password"] == "p@ss"


class TestBrowserSessionInit:
    def test_default_user_agent_set(self):
        session = BrowserSession()
        assert "Mozilla" in session.user_agent

    def test_custom_user_agent_accepted(self):
        session = BrowserSession(user_agent="TestBot/1.0")
        assert session.user_agent == "TestBot/1.0"

    def test_proxy_stored(self):
        session = BrowserSession(proxy="http://p:s@host:8080")
        assert session.proxy == "http://p:s@host:8080"

    def test_no_proxy_by_default(self):
        session = BrowserSession()
        assert session.proxy is None

    def test_launch_args_include_no_sandbox(self):
        session = BrowserSession()
        args = session._build_launch_args()
        assert "--no-sandbox" in args.get("args", [])

    def test_context_args_include_user_agent(self):
        ua = "CustomAgent/2.0"
        session = BrowserSession(user_agent=ua)
        ctx_args = session._build_context_args()
        assert ctx_args["user_agent"] == ua

    def test_context_args_include_viewport(self):
        session = BrowserSession()
        ctx_args = session._build_context_args()
        assert "viewport" in ctx_args
        assert ctx_args["viewport"]["width"] > 0

    def test_proxy_in_launch_args_when_set(self):
        session = BrowserSession(proxy="http://user:pass@host:1234")
        args = session._build_launch_args()
        assert "proxy" in args
        assert args["proxy"]["server"] == "http://host:1234"

    def test_no_proxy_in_launch_args_when_not_set(self):
        session = BrowserSession()
        args = session._build_launch_args()
        assert "proxy" not in args
