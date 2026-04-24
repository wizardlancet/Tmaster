"""Server configuration loaded from environment variables.

Follows the TMASTER_* convention. A bootstrap password can be provided via
TMASTER_BOOTSTRAP_PASSWORD for the first-ever start; the user is then created
with that password if no users exist yet.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TMASTER_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = Field(default=Path("./data"))
    listen_host: str = Field(default="127.0.0.1")
    listen_port: int = Field(default=8000)

    # JWT
    jwt_secret: Optional[str] = None
    jwt_secret_file: Optional[Path] = None
    jwt_access_ttl_seconds: int = 2 * 3600
    jwt_refresh_ttl_seconds: int = 14 * 24 * 3600

    # Bootstrap (first boot)
    bootstrap_user: str = "admin"
    bootstrap_password: Optional[str] = None

    # CORS (dev); list of origins, comma-separated in env
    cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"])

    def resolve_jwt_secret(self) -> str:
        if self.jwt_secret:
            return self.jwt_secret
        if self.jwt_secret_file and self.jwt_secret_file.exists():
            return self.jwt_secret_file.read_text().strip()
        # Auto-generate on first start, persist in data_dir
        path = self.data_dir / "jwt_secret"
        if path.exists():
            return path.read_text().strip()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        path.write_text(secret)
        path.chmod(0o600)
        return secret

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tmaster.db"
