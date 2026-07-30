from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _ids(value: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    github_token: str
    github_owner: str
    github_repo: str
    github_default_branch: str
    allowed_user_ids: frozenset[int]
    max_zip_mb: int
    max_unpacked_mb: int
    max_files: int
    build_timeout_minutes: int
    telegram_max_upload_mb: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            github_token=_required("GITHUB_TOKEN"),
            github_owner=_required("GITHUB_OWNER"),
            github_repo=_required("GITHUB_REPO"),
            github_default_branch=os.getenv("GITHUB_DEFAULT_BRANCH", "main").strip(),
            allowed_user_ids=_ids(os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")),
            max_zip_mb=int(os.getenv("MAX_ZIP_MB", "100")),
            max_unpacked_mb=int(os.getenv("MAX_UNPACKED_MB", "500")),
            max_files=int(os.getenv("MAX_FILES", "1200")),
            build_timeout_minutes=int(os.getenv("BUILD_TIMEOUT_MINUTES", "35")),
            telegram_max_upload_mb=int(os.getenv("TELEGRAM_MAX_UPLOAD_MB", "100")),
        )
