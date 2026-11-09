from __future__ import annotations

import urllib.request

import pytest

from boxd_match import net


def test_user_agent_is_honest() -> None:
    assert net.USER_AGENT.startswith("boxd-match/")
    assert "Mozilla" not in net.USER_AGENT and "@" not in net.USER_AGENT


def test_forbidden_host_refused_before_any_request() -> None:
    transport = net.UrllibTransport(net.LETTERBOXD_HOSTS, min_interval=1.0)
    with pytest.raises(net.ForbiddenHost):
        transport.get("https://example.com/film/x/")
    with pytest.raises(net.ForbiddenHost):
        transport.get("https://letterboxd.com.evil.test/film/x/")


def test_redirect_to_forbidden_host_refused() -> None:
    handler = net._CheckedRedirect(net.LETTERBOXD_HOSTS)
    req = urllib.request.Request("https://boxd.it/abc")
    with pytest.raises(net.ForbiddenHost):
        handler.redirect_request(req, None, 302, "Found", {}, "https://tracker.example/x")
    allowed = handler.redirect_request(req, None, 302, "Found", {}, "https://letterboxd.com/film/dune-2021/")
    assert allowed.full_url == "https://letterboxd.com/film/dune-2021/"


def test_throttle_waits_min_interval() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    transport = net.UrllibTransport(net.LETTERBOXD_HOSTS, 1.0, sleep=sleep, clock=lambda: now[0])
    transport._throttle()
    now[0] += 0.25
    transport._throttle()
    assert slept == [pytest.approx(0.75)]
