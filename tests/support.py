from __future__ import annotations

import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTempDir:
    """Временный каталог внутри проекта.

    Не используем tempfile: в некоторых песочницах системный TEMP доступен
    только на чтение. Каталог создаётся с обычными правами и удаляется в
    cleanup.
    """

    def __init__(self, prefix: str = ".testtmp-") -> None:
        self.path = ROOT / f"{prefix}{uuid.uuid4().hex}"
        self.path.mkdir()

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *args: object) -> None:
        self.cleanup()
