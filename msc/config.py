from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_FILENAME = ".msc.json"
DEFAULT_ENV_FILENAME = ".env"
DEFAULT_DATA_DIR = Path("data")
DEFAULT_LOG_FILE = Path("data/logs/latest.log")
DEFAULT_DOCKER_SERVICE = "minecraft"

USER_CONFIG_DIR = Path.home() / ".config" / "msc"
USER_CONFIG_FILENAME = "config.json"
USER_CONFIG_PATH = USER_CONFIG_DIR / USER_CONFIG_FILENAME


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded."""


class RconConfig(BaseModel):
    enabled: bool = True
    host: str = Field(default="127.0.0.1", description="RCON host")
    port: int = Field(default=25575, description="RCON port")
    password: str = Field(default="rconpw", description="RCON password")


class FileConfig(BaseModel):
    name: str = "default-server"
    server_type: str = "FABRIC"
    minecraft_version: str = "1.21.1"
    data_dir: Optional[Path] = None
    log_file: Optional[Path] = None
    docker_service: Optional[str] = None
    rcon: Optional[RconConfig] = None
    api_user_agent: Optional[str] = None
    curseforge_api_key: Optional[str] = None


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MSC_", extra="ignore")

    server_root: Optional[Path] = None
    data_dir: Optional[Path] = None
    log_file: Optional[Path] = None
    docker_service: Optional[str] = None
    rcon_enabled: Optional[bool] = None
    rcon_host: Optional[str] = None
    rcon_port: Optional[int] = None
    rcon_password: Optional[str] = None
    api_user_agent: Optional[str] = None
    curseforge_api_key: Optional[str] = None


class MscConfig(BaseModel):
    server_root: Path
    data_dir: Path
    log_file: Path
    docker_service: str
    rcon: RconConfig
    name: str
    server_type: str
    minecraft_version: str
    api_user_agent: str
    curseforge_api_key: Optional[str] = None


class UserConfig(BaseModel):
    server_root: Optional[Path] = None


def _coerce_path(base: Path, value: Path | str) -> Path:
    path = value if isinstance(value, Path) else Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def load_user_config() -> UserConfig:
    if not USER_CONFIG_PATH.exists():
        return UserConfig()
    try:
        data = json.loads(USER_CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:  # pragma: no cover - config errors are user-facing
        raise ConfigError(f"Invalid JSON in {USER_CONFIG_PATH}: {exc}") from exc
    return UserConfig(**data)


def save_user_config(cfg: UserConfig) -> Path:
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(cfg.model_dump_json(indent=2))
    return USER_CONFIG_PATH


def _resolve_initial_root(root: Path | None, user_cfg: UserConfig) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()

    cwd = Path.cwd().resolve()
    if (cwd / DEFAULT_CONFIG_FILENAME).exists():
        return cwd

    if user_cfg.server_root is not None:
        return Path(user_cfg.server_root).expanduser().resolve()

    return cwd


def _load_file_config(path: Path) -> FileConfig:
    if not path.exists():
        raise ConfigError(
            f"Could not find {path.name}. Run msc from your server root or pass --root."
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:  # pragma: no cover - config errors are user-facing
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    return FileConfig(**data)


def load_config(root: Path | None = None) -> MscConfig:
    """Load configuration from env + .msc.json."""

    user_cfg = load_user_config()
    server_root = _resolve_initial_root(root, user_cfg)
    env_file = server_root / DEFAULT_ENV_FILENAME
    env_settings = EnvSettings(
        _env_file=env_file if env_file.exists() else None,
    )

    if env_settings.server_root:
        server_root = _coerce_path(server_root, env_settings.server_root)

    file_config_path = server_root / DEFAULT_CONFIG_FILENAME
    file_cfg = _load_file_config(file_config_path)

    data_dir = env_settings.data_dir or file_cfg.data_dir or DEFAULT_DATA_DIR
    log_file = env_settings.log_file or file_cfg.log_file or DEFAULT_LOG_FILE
    docker_service = env_settings.docker_service or file_cfg.docker_service or DEFAULT_DOCKER_SERVICE

    rcon_cfg = file_cfg.rcon or RconConfig()
    if env_settings.rcon_enabled is not None:
        rcon_cfg.enabled = env_settings.rcon_enabled
    if env_settings.rcon_host is not None:
        rcon_cfg.host = env_settings.rcon_host
    if env_settings.rcon_port is not None:
        rcon_cfg.port = env_settings.rcon_port
    if env_settings.rcon_password is not None:
        rcon_cfg.password = env_settings.rcon_password

    api_user_agent = env_settings.api_user_agent or file_cfg.api_user_agent or "msc-cli/dev"
    curseforge_api_key = env_settings.curseforge_api_key or file_cfg.curseforge_api_key

    return MscConfig(
        server_root=server_root,
        data_dir=_coerce_path(server_root, data_dir),
        log_file=_coerce_path(server_root, log_file),
        docker_service=docker_service,
        rcon=rcon_cfg,
        name=file_cfg.name,
        server_type=file_cfg.server_type,
        minecraft_version=file_cfg.minecraft_version,
        api_user_agent=api_user_agent,
        curseforge_api_key=curseforge_api_key,
    )
