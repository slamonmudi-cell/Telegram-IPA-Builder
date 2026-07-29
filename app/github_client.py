from __future__ import annotations

import asyncio
import base64
import io
import time
import zipfile
from pathlib import Path

import httpx


class GitHubError(RuntimeError):
    pass


class GitHubBuilder:
    api = "https://api.github.com"

    def __init__(self, token: str, owner: str, repo: str, default_branch: str):
        self.owner = owner
        self.repo = repo
        self.default_branch = default_branch
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=20),
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "telegram-ipa-builder-bot",
            },
        )

    async def __aenter__(self) -> "GitHubBuilder":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.client.aclose()

    @property
    def repo_api(self) -> str:
        return f"{self.api}/repos/{self.owner}/{self.repo}"

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        response = await self.client.request(method, f"{self.repo_api}{path}", **kwargs)
        if response.status_code >= 400:
            detail = response.text[:1000]
            raise GitHubError(f"GitHub API {response.status_code}: {detail}")
        return response

    async def create_build_branch(self, job_id: str, project_root: Path) -> str:
        branch = f"build/{job_id}"
        ref = await self._request("GET", f"/git/ref/heads/{self.default_branch}")
        base_commit = ref.json()["object"]["sha"]
        commit = await self._request("GET", f"/git/commits/{base_commit}")
        base_tree = commit.json()["tree"]["sha"]

        entries: list[dict[str, str]] = []
        for path in sorted(item for item in project_root.rglob("*") if item.is_file()):
            relative = path.relative_to(project_root).as_posix()
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            blob = await self._request(
                "POST",
                "/git/blobs",
                json={"content": data, "encoding": "base64"},
            )
            entries.append(
                {
                    "path": f"jobs/{job_id}/project/{relative}",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob.json()["sha"],
                }
            )

        tree = await self._request(
            "POST",
            "/git/trees",
            json={"base_tree": base_tree, "tree": entries},
        )
        new_commit = await self._request(
            "POST",
            "/git/commits",
            json={
                "message": f"Temporary IPA build {job_id}",
                "tree": tree.json()["sha"],
                "parents": [base_commit],
            },
        )
        await self._request(
            "POST",
            "/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": new_commit.json()["sha"]},
        )
        return branch

    async def dispatch(self, branch: str, job_id: str) -> float:
        started_at = time.time()
        await self._request(
            "POST",
            "/actions/workflows/ipa-builder.yml/dispatches",
            json={
                "ref": branch,
                "inputs": {
                    "job_id": job_id,
                    "project_path": f"jobs/{job_id}/project",
                },
            },
        )
        return started_at

    async def wait_for_run(
        self,
        branch: str,
        started_at: float,
        *,
        timeout_seconds: int,
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        run: dict | None = None
        while time.monotonic() < deadline:
            response = await self._request(
                "GET",
                "/actions/workflows/ipa-builder.yml/runs",
                params={"branch": branch, "event": "workflow_dispatch", "per_page": 10},
            )
            candidates = response.json().get("workflow_runs", [])
            if candidates:
                run = candidates[0]
                if run["status"] == "completed":
                    return run
            await asyncio.sleep(8 if run is None else 15)
        raise GitHubError("The GitHub Actions build timed out")

    async def download_artifact(self, run_id: int, destination: Path) -> list[Path]:
        response = await self._request("GET", f"/actions/runs/{run_id}/artifacts")
        artifacts = response.json().get("artifacts", [])
        artifact = next((item for item in artifacts if item["name"] == "ipa-output"), None)
        if artifact is None:
            raise GitHubError("The build finished without the ipa-output artifact")

        archive = await self._request(
            "GET",
            f"/actions/artifacts/{artifact['id']}/zip",
        )
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            zipped.extractall(destination)
        return [path for path in destination.rglob("*") if path.is_file()]

    async def delete_branch(self, branch: str) -> None:
        try:
            await self._request("DELETE", f"/git/refs/heads/{branch}")
        except GitHubError:
            pass
