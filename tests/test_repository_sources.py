from __future__ import annotations

import unittest
from unittest.mock import patch

from repository_sources import (
    RepositorySourceError,
    find_github_repositories_in_document,
    inspect_github_repository,
    parse_github_repository_url,
    search_github_repositories,
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        return None


class RepositorySourceTests(unittest.TestCase):
    def test_parse_only_accepts_root_github_repository_url(self) -> None:
        self.assertEqual(
            parse_github_repository_url("github.com/example/project.git"),
            ("https://github.com/example/project", "example", "project"),
        )
        with self.assertRaises(RepositorySourceError):
            parse_github_repository_url("https://github.com/example/project/tree/main")

    @patch("repository_sources.requests.get")
    def test_inspection_pins_default_branch_commit_without_cloning(self, get) -> None:
        get.side_effect = [
            _Response({
                "full_name": "example/project", "html_url": "https://github.com/example/project",
                "description": "Reference implementation", "default_branch": "main",
                "fork": False, "archived": False, "stargazers_count": 7, "updated_at": "2026-09-01T00:00:00Z",
            }),
            _Response({"sha": "a" * 40}),
        ]

        repository = inspect_github_repository("https://github.com/example/project")

        self.assertEqual(repository["commit_sha"], "a" * 40)
        self.assertEqual(repository["repository_url"], "https://github.com/example/project")
        self.assertEqual(get.call_count, 2)
        self.assertIn("/commits/main", get.call_args_list[1].args[0])

    @patch("repository_sources.requests.get")
    def test_search_returns_candidates_not_officialness_claims(self, get) -> None:
        get.return_value = _Response({"items": [{
            "full_name": "sample/implementation", "html_url": "https://github.com/sample/implementation",
            "description": "Maybe related", "default_branch": "main", "fork": False,
            "archived": False, "stargazers_count": 3, "updated_at": "2026-09-01T00:00:00Z",
        }]})

        candidates = search_github_repositories("A paper title")

        self.assertEqual(candidates[0]["match_status"], "candidate")
        self.assertNotIn("official", candidates[0])

    def test_document_link_is_preferred_as_a_candidate_not_an_official_claim(self) -> None:
        candidates = find_github_repositories_in_document({"pages": [{"page": 4, "elements": [{
            "id": "S4", "page": 4,
            "text": "Code is available at https://github.com/example/paper-code.",
        }]}]})

        self.assertEqual(candidates[0]["repository_url"], "https://github.com/example/paper-code")
        self.assertEqual(candidates[0]["match_status"], "paper_link")
        self.assertEqual(candidates[0]["evidence_refs"][0]["page"], 4)


if __name__ == "__main__":
    unittest.main()
