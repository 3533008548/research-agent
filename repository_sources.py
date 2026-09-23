"""Inspect public GitHub repositories without cloning or executing their code.

The paper-reproduction workflow needs a clear boundary between a repository
that was merely *found* and one the user has confirmed is the paper's
implementation.  This module only calls GitHub's metadata API; it never runs
repository code, installs dependencies, or downloads datasets.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests


_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_URL = re.compile(r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", re.IGNORECASE)
_API_ROOT = "https://api.github.com"


class RepositorySourceError(ValueError):
    """The supplied source is not a supported public repository URL."""


class RepositoryLookupError(RuntimeError):
    """GitHub could not provide a repository record for this request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _headers(token: str | None = None) -> dict[str, str]:
    result = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "research-agent-paper-reproduction",
    }
    value = (token if token is not None else os.getenv("GITHUB_TOKEN", "")).strip()
    if value:
        result["Authorization"] = f"Bearer {value}"
    return result


def parse_github_repository_url(raw_url: str) -> tuple[str, str, str]:
    """Return canonical web URL and owner/repository names for a GitHub URL."""
    value = str(raw_url or "").strip()
    if value.startswith("github.com/"):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
        raise RepositorySourceError("仅支持 HTTPS GitHub 仓库地址，例如 https://github.com/owner/repo")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise RepositorySourceError("GitHub 地址必须精确指向 owner/repo，不能是文件、分支或搜索页")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not _GITHUB_REPOSITORY.fullmatch(f"{owner}/{repository}"):
        raise RepositorySourceError("GitHub 仓库地址格式无效")
    return f"https://github.com/{owner}/{repository}", owner, repository


def _get_json(url: str, *, params: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    try:
        response = requests.get(url, params=params, headers=_headers(token), timeout=8)
    except requests.RequestException as exc:
        raise RepositoryLookupError("无法连接 GitHub，请检查网络或稍后重试") from exc
    try:
        if response.status_code == 404:
            raise RepositoryLookupError("GitHub 未找到该公开仓库")
        if response.status_code in {401, 403}:
            raise RepositoryLookupError("GitHub 拒绝了请求或触发限流；可在 .env 配置 GITHUB_TOKEN 后重试")
        response.raise_for_status()
        payload = response.json()
    except ValueError as exc:
        raise RepositoryLookupError("GitHub 返回了无法解析的仓库数据") from exc
    except requests.RequestException as exc:
        raise RepositoryLookupError(f"GitHub 请求失败（HTTP {response.status_code}）") from exc
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise RepositoryLookupError("GitHub 返回的仓库数据格式异常")
    return payload


def _repository_payload(item: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    full_name = str(item.get("full_name") or "").strip()
    html_url = str(item.get("html_url") or "").strip()
    if not full_name or not html_url:
        raise RepositoryLookupError("GitHub 返回的仓库缺少名称或地址")
    return {
        "provider": "github",
        "repository_url": html_url,
        "full_name": full_name,
        "description": str(item.get("description") or "")[:500],
        "default_branch": str(item.get("default_branch") or ""),
        "is_fork": bool(item.get("fork")),
        "is_archived": bool(item.get("archived")),
        "stars": int(item.get("stargazers_count") or 0),
        "updated_at": str(item.get("updated_at") or ""),
        "observed_at": observed_at,
    }


def inspect_github_repository(raw_url: str, *, token: str | None = None) -> dict[str, Any]:
    """Fetch stable repository metadata and pin the currently selected commit."""
    canonical_url, owner, repository = parse_github_repository_url(raw_url)
    observed_at = _now()
    details = _get_json(f"{_API_ROOT}/repos/{owner}/{repository}", token=token)
    result = _repository_payload(details, observed_at=observed_at)
    result["repository_url"] = canonical_url
    branch = result["default_branch"]
    if branch:
        commit = _get_json(f"{_API_ROOT}/repos/{owner}/{repository}/commits/{branch}", token=token)
        result["commit_sha"] = str(commit.get("sha") or "")[:64]
    else:
        result["commit_sha"] = ""
    return result


def search_github_repositories(paper_title: str, *, limit: int = 5, token: str | None = None) -> list[dict[str, Any]]:
    """Return candidate repositories for a paper title, never an officialness claim."""
    title = " ".join(str(paper_title or "").split())
    if len(title) < 3:
        raise RepositorySourceError("论文标题过短，无法检索 GitHub 候选仓库")
    safe_limit = max(1, min(int(limit), 10))
    payload = _get_json(
        f"{_API_ROOT}/search/repositories",
        params={"q": f'"{title[:180]}" in:name,description', "sort": "updated", "order": "desc", "per_page": safe_limit},
        token=token,
    )
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    observed_at = _now()
    candidates = []
    for item in raw_items[:safe_limit]:
        if not isinstance(item, dict):
            continue
        try:
            candidate = _repository_payload(item, observed_at=observed_at)
        except RepositoryLookupError:
            continue
        candidate["match_status"] = "candidate"
        candidates.append(candidate)
    return candidates


def find_github_repositories_in_document(document_map: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    """Extract GitHub repository links explicitly present in local paper evidence.

    A link in a paper is stronger evidence than a title search, but still does
    not prove that the repository is an official implementation.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    safe_limit = max(1, min(int(limit), 10))
    for page_record in document_map.get("pages") or []:
        if not isinstance(page_record, dict):
            continue
        for element in page_record.get("elements") or []:
            if not isinstance(element, dict):
                continue
            text = "\n".join(str(element.get(field) or "") for field in ("text", "caption"))
            for match in _GITHUB_URL.findall(text):
                raw_url = match.rstrip(".,;:)]}")
                try:
                    repository_url, owner, repository = parse_github_repository_url(raw_url)
                except RepositorySourceError:
                    continue
                if repository_url in seen:
                    continue
                seen.add(repository_url)
                candidates.append({
                    "provider": "github",
                    "repository_url": repository_url,
                    "full_name": f"{owner}/{repository}",
                    "match_status": "paper_link",
                    "evidence_refs": [{
                        "page": element.get("page") if isinstance(element.get("page"), int) else page_record.get("page"),
                        "element_id": str(element.get("id") or ""),
                    }],
                    "observed_at": _now(),
                })
                if len(candidates) >= safe_limit:
                    return candidates
    return candidates
