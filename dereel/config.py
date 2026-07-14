from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    database_path: Path = ROOT / "data" / "dereel.sqlite3"
    originals_dir: Path = ROOT / "data" / "library" / "originals"
    output_dir: Path = ROOT / "outputs"
    web_dir: Path = ROOT / "web"
    host: str = os.getenv("DEREEL_HOST", "127.0.0.1")
    port: int = int(os.getenv("DEREEL_PORT", "8765"))

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
