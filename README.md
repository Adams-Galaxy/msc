# Minecraft Server CLI (msc)

`msc` is a Typer-based command-line companion for running your self-hosted Minecraft server. It is designed to be installed via `pipx` and run directly inside a server directory (or from anywhere if you set a user-level default) so it can read local configuration and communicate with the running container.

## Features (initial)

- `msc status` – Summaries the Docker container's state, uptime, and metadata.
- `msc server start|stop|restart` – Proxies lifecycle operations to `docker compose`.
- `msc console run` – Sends a one-off RCON command.
- `msc console attach` – Attaches to the live docker console stream (CTRL-P CTRL-Q to detach).
- `msc quick ...` – Handy shortcuts for ops such as `op`, `whitelist`, `gamemode`, `weather`, `save-all`, and more.
- `msc logs tail` – Tails `data/logs/latest.log` with optional follow mode.
- `msc mods ...` – Initialize a manifest, inspect status, add new mods, and toggle them on/off safely.

Additional modules (mods, backups, etc.) will be layered on incrementally.

## Installation (current repo checkout)

```bash
pip install --upgrade pip
pip install -e .
```

For pipx once published:

```bash
pipx install msc
```

### pipx (local editable checkout)

While iterating locally, you can have `pipx` expose the CLI globally but still use your working tree:

```bash
pipx install --editable /home/minecraft/minecraft/msc
```

If the package is already installed, use `pipx reinstall --editable /home/minecraft/minecraft/msc` after making changes. The installed `msc` command will now reflect edits from your checkout as soon as they are saved.

## Usage

From your Minecraft server root (the same folder that contains `docker-compose.yml` and `.msc.json`):

```bash
msc status
msc server start
msc console run "say hello from msc"
msc console attach
msc quick op YourName
msc quick weather rain --duration 1200
msc quick whitelist add Friend
msc mods init --adopt-existing
msc mods add ./mods/lithium-fabric-mc1.21.1-0.12.0.jar --name "Lithium"
msc mods disable lithium
msc mods list
msc mods remove lithium --keep-file
msc mods validate
msc mods purge --yes --keep-files
msc mods repair --adopt-extras --fix-placement --apply
msc mods add modrinth:lithium --loader fabric --mc-version 1.21.1
MSC_CURSEFORGE_API_KEY=... msc mods add curseforge:sodium --source-type curseforge --loader fabric --mc-version 1.21.1
msc logs tail -f
```

### Mods manifest workflow

The mods tooling introduces a lightweight manifest stored at `data/mods/.mscmods.json`.

- `msc mods init` scaffolds the manifest and directories. Pass `--adopt-existing` to ingest currently installed `.jar`/`.zip` files.
- `msc mods status` summarizes drift between the manifest and filesystem (missing/misplaced files, hash mismatches, and untracked extras).
- `msc mods list` shows every tracked mod, its enablement flag, and hash verification results.
- `msc mods add <source>` copies in a local file or downloads from a URL, records metadata, and computes a SHA-256 hash. Use `--disable` to stage a mod without enabling it yet. Remote sources automatically filter for the correct loader + Minecraft version so you don't accidentally install the wrong build.
- **Version safety:** when using remote sources (Modrinth/CurseForge), `msc mods add` refuses to install artifacts that don’t advertise compatibility with your manifest/server Minecraft version or loader, preventing accidental mismatches.
- `msc mods remove <id>` drops the manifest entry (and deletes the jar unless you pass `--keep-file`).
- `msc mods enable|disable <id>` toggles a mod, automatically moving files between `mods/` and `mods-disabled/` (unless `--no-move` is supplied).
- `msc mods validate` audits the manifest/filesystem and exits with a non-zero status if anything is missing, misplaced, or untracked—great for CI checks.
- `msc mods purge --yes` wipes every tracked entry (add `--keep-files` to leave jars untouched) so you can rebuild a manifest from scratch.
- `msc mods repair` provides targeted fixes: `--adopt-extras` to track stray jars, `--remove-missing` to prune orphaned manifest entries, `--fix-placement` to move jars into the correct directories, and `--recompute-hashes` to refresh checksums. It runs as a dry run by default; add `--apply` to persist changes.
- Remote sources:
	- **Modrinth** – `msc mods add modrinth:<slug> --loader fabric --mc-version 1.21.1 --version <version-id>` resolves the latest compatible file, downloads it via the Modrinth API, stores hashes, and records the project/version IDs inside the manifest.
	- **CurseForge** – `MSC_CURSEFORGE_API_KEY=... msc mods add curseforge:<slug> --loader fabric --mc-version 1.21.1` searches CurseForge (gameId 432) using the official Core API and downloads the selected file. Pass `--project-id` or `--version` to target a specific mod/file ID.
- **Intelligent defaults:** when you omit `--loader`, `--mc-version`, or `--version`, MSC automatically pulls the loader and Minecraft version from `.mscmods.json` (falling back to `.msc.json`), so a bare `msc mods add modrinth:lithium` “just works” for your current server profile.

For the architectural roadmap (Modrinth/CurseForge integration, curated modpacks, etc.), see [`mods.md`](mods.md).

## Configuration

- `.msc.json` in the server root holds the persistent config (data dir, docker service, RCON details, etc.).
- `.env` (optional) can override any setting using environment variables prefixed with `MSC_` (e.g., `MSC_RCON_PASSWORD`).
- Defaults assume `data/logs/latest.log` for logs and `docker-compose.yml` defines a service named `minecraft`.
- `curseforge_api_key` (or `MSC_CURSEFORGE_API_KEY`) powers CurseForge API downloads.
- `api_user_agent` (or `MSC_API_USER_AGENT`) customizes the User-Agent header sent to Modrinth/CurseForge; defaults to `msc-cli/dev`.
- `~/.config/msc/config.json` stores user-level defaults such as `server_root`. Use `msc config set-root /path/to/server` to set it once and run `msc` from anywhere; `msc config show` displays the current values and `msc config clear-root` removes them.

## Development

```bash
python -m msc --help
```

Future enhancements: Modrinth/CurseForge fetching, curated mod packs, backups, schedulers, diagnostics, and multi-profile support.
