"""Sandboxed filesystem operations for a single workspace."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PathDenied(PermissionError):
    pass


@dataclass
class FsSandbox:
    """All paths are resolved and must be inside `root` (after following symlinks)."""

    root: Path

    def _resolve(self, user_path: str | os.PathLike[str]) -> Path:
        p = Path(user_path)
        if not p.is_absolute():
            p = self.root / p
        try:
            resolved = p.resolve(strict=False)
        except (OSError, RuntimeError) as e:
            raise PathDenied(f"cannot resolve {user_path}: {e}") from e
        root_resolved = self.root.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as e:
            raise PathDenied(f"path escapes sandbox: {user_path}") from e
        return resolved

    def list_dir(self, user_path: str) -> list[dict[str, Any]]:
        p = self._resolve(user_path)
        if not p.is_dir():
            raise NotADirectoryError(str(p))
        out: list[dict[str, Any]] = []
        for name in sorted(os.listdir(p)):
            child = p / name
            try:
                st = child.stat()
            except FileNotFoundError:
                continue
            out.append(
                {
                    "name": name,
                    "type": _type_of(st.st_mode),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
        return out

    def stat(self, user_path: str) -> dict[str, Any]:
        p = self._resolve(user_path)
        st = p.stat()
        return {
            "path": str(p),
            "type": _type_of(st.st_mode),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "mode": st.st_mode,
        }

    def read(self, user_path: str, *, max_bytes: int | None = None) -> bytes:
        p = self._resolve(user_path)
        data = p.read_bytes()
        if max_bytes is not None and len(data) > max_bytes:
            data = data[:max_bytes]
        return data

    def write(
        self,
        user_path: str,
        data: bytes,
        *,
        expected_mtime: int | None = None,
        mode: int | None = None,
    ) -> int:
        p = self._resolve(user_path)
        if expected_mtime is not None and p.exists():
            if int(p.stat().st_mtime) != int(expected_mtime):
                raise FileExistsError(
                    f"mtime conflict: expected {expected_mtime}, got {int(p.stat().st_mtime)}"
                )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mode is not None:
            os.chmod(p, mode)
        return int(p.stat().st_mtime)

    def mkdir(self, user_path: str, *, parents: bool = True) -> None:
        p = self._resolve(user_path)
        p.mkdir(parents=parents, exist_ok=True)

    def delete(self, user_path: str, *, recursive: bool = False) -> None:
        p = self._resolve(user_path)
        if p.is_dir():
            if recursive:
                import shutil
                shutil.rmtree(p)
            else:
                p.rmdir()
        else:
            p.unlink()

    def rename(self, src: str, dst: str) -> None:
        s = self._resolve(src)
        d = self._resolve(dst)
        s.rename(d)


def _type_of(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"
