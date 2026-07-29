from __future__ import annotations

import stat
import zipfile
from pathlib import Path, PurePosixPath


class UnsafeArchive(ValueError):
    pass


def _safe_name(raw: str) -> PurePosixPath:
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise UnsafeArchive(f"Unsafe archive path: {raw}")
    if ":" in path.parts[0]:
        raise UnsafeArchive(f"Unsafe archive path: {raw}")
    return path


def extract_project(
    archive: Path,
    destination: Path,
    *,
    max_unpacked_bytes: int,
    max_files: int,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0

    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        files = [item for item in members if not item.is_dir()]
        if len(files) > max_files:
            raise UnsafeArchive(f"Archive contains more than {max_files} files")

        for item in members:
            relative = _safe_name(item.filename)
            unix_mode = item.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise UnsafeArchive("Symbolic links are not accepted")
            if item.file_size > 90 * 1024 * 1024:
                raise UnsafeArchive("A single extracted file exceeds 90 MB")

            total += item.file_size
            if total > max_unpacked_bytes:
                raise UnsafeArchive("Archive expands beyond the configured limit")

            target = destination.joinpath(*relative.parts)
            resolved = target.resolve()
            if destination.resolve() not in resolved.parents and resolved != destination.resolve():
                raise UnsafeArchive("Archive path escapes the temporary directory")

            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as incoming, target.open("wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    outgoing.write(chunk)
            extracted.append(target)

    return extracted


def locate_project_root(root: Path) -> Path:
    candidates = {path.parent for path in root.rglob("project.yml")}
    candidates.update(path.parent for path in root.rglob("project.yaml"))
    if not candidates:
        raise UnsafeArchive("No project.yml/project.yaml was found")
    if len(candidates) != 1:
        raise UnsafeArchive("The ZIP must contain exactly one XcodeGen project")
    return candidates.pop()
