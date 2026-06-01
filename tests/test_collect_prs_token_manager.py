from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import collect_prs_wo_image  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._body

    def close(self) -> None:
        pass


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def get(self, *args, **kwargs) -> FakeResponse:
        return self.response


def test_search_success_does_not_replace_core_quota(monkeypatch) -> None:
    token = "ghp_test"
    manager = collect_prs_wo_image.TokenManager(
        tokens=[token],
        initial_rate_limits={token: {"remaining": 4872, "reset_time": 0}},
    )
    client = collect_prs_wo_image.GitHubClient(manager)

    client.sessions[token] = FakeSession(
        FakeResponse(
            200,
            {
                "X-RateLimit-Resource": "search",
                "X-RateLimit-Remaining": "29",
                "X-RateLimit-Reset": "1234567890",
            },
            {"items": []},
        )
    )
    monkeypatch.setattr(collect_prs_wo_image, "API_REQUEST_DELAY", 0)

    client.make_request(f"{collect_prs_wo_image.GITHUB_API_URL}/search/repositories")

    assert manager.token_status[token]["remaining"] == 4872
    assert manager.get_available_token() == token


def test_core_success_still_updates_core_quota(monkeypatch) -> None:
    token = "ghp_test"
    manager = collect_prs_wo_image.TokenManager(
        tokens=[token],
        initial_rate_limits={token: {"remaining": 4872, "reset_time": 0}},
    )
    client = collect_prs_wo_image.GitHubClient(manager)

    client.sessions[token] = FakeSession(
        FakeResponse(
            200,
            {
                "X-RateLimit-Resource": "core",
                "X-RateLimit-Remaining": "4860",
                "X-RateLimit-Reset": "1234567890",
            },
            {"ok": True},
        )
    )
    monkeypatch.setattr(collect_prs_wo_image, "API_REQUEST_DELAY", 0)

    client.make_request(f"{collect_prs_wo_image.GITHUB_API_URL}/repos/octocat/Hello-World")

    assert manager.token_status[token]["remaining"] == 4860
