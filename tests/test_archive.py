from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app.archive import UnsafeArchive, extract_project, locate_project_root


class ArchiveTests(unittest.TestCase):
    def _zip(self, root: Path, entries: dict[str, bytes]) -> Path:
        archive = root / "input.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name, data in entries.items():
                output.writestr(name, data)
        return archive

    def test_extracts_valid_xcodegen_project(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = self._zip(
                root,
                {
                    "Lucky/project.yml": b"name: Lucky",
                    "Lucky/App.swift": b"import SwiftUI",
                },
            )
            output = root / "out"
            files = extract_project(
                archive,
                output,
                max_unpacked_bytes=1024,
                max_files=10,
            )
            self.assertEqual(len(files), 2)
            self.assertEqual(locate_project_root(output), output / "Lucky")

    def test_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = self._zip(root, {"../escape.txt": b"no"})
            with self.assertRaises(UnsafeArchive):
                extract_project(
                    archive,
                    root / "out",
                    max_unpacked_bytes=1024,
                    max_files=10,
                )

    def test_rejects_expanded_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = self._zip(root, {"large.bin": b"x" * 100})
            with self.assertRaises(UnsafeArchive):
                extract_project(
                    archive,
                    root / "out",
                    max_unpacked_bytes=50,
                    max_files=10,
                )


if __name__ == "__main__":
    unittest.main()
