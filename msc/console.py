from __future__ import annotations

from mcrcon import MCRcon

from .config import MscConfig


class ConsoleError(RuntimeError):
    pass


def send_command(cfg: MscConfig, command: str) -> str:
    if not cfg.rcon.enabled:
        raise ConsoleError("RCON is disabled in this configuration")

    try:
        with MCRcon(cfg.rcon.host, cfg.rcon.password, port=cfg.rcon.port) as mcr:
            response = mcr.command(command)
    except ConnectionError as exc:  # pragma: no cover - depends on runtime environment
        raise ConsoleError(f"Unable to reach RCON at {cfg.rcon.host}:{cfg.rcon.port}") from exc
    return response.strip()
