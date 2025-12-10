# MSC Mod Management Design

This document outlines the planned architecture for mod management in the Minecraft Server CLI (MSC). It focuses on design and behavior only; implementation will come later.

## Goals

- **Single source of truth** for mods via a manifest file.
- **Reproducible setups** so a new server can be brought to the same mod set from GitHub.
- **Safe operations** that avoid corrupting worlds or leaving mods half-updated.
- **Pluggable sources** (local files, URLs; later Modrinth/CurseForge).
- **Docker-friendly**: mods live under `/data/mods` and are managed from the host.

Assumptions:

- Server type: initially Fabric, but the design should be loader-agnostic.
- Data dir: `/data`, with host path `./data` (from the server root).
- Mods dir: `/data/mods` (host: `./data/mods`).
- MSC runs from the server root and reads `data_dir` from `.msc.json`.

---

## Core Artifacts & Config

### `.mscmods.json` Manifest

This is the **source of truth** for what mods a server should have.

- Default location: `data/mods/.mscmods.json` relative to the server root.
- May be overridden via `.msc.json` if needed later.

Example schema (v1):

```jsonc
{
  "schema_version": 1,
  "loader": "fabric",
  "minecraft_version": "1.21.1",
  "mods_dir": "mods",
  "mods": [
    {
      "id": "lithium",                 // canonical ID (slug or logical name)
      "name": "Lithium",               // human-friendly name
      "side": "server",                // server/client/both
      "enabled": true,
      "filename": "lithium-fabric-mc1.21.1-0.12.0.jar",
      "source": {
        "type": "modrinth",            // local | url | modrinth | curseforge | custom
        "project_id": "AANobbMI",
        "version_id": "abcd-1234",
        "slug": "lithium",
        "download_url": null
      },
      "version": "0.12.0",
      "mc_version": "1.21.1",
      "loader": "fabric",
      "installed_at": "2025-12-10T12:00:00Z",
      "hashes": {
        "sha256": "..."
      },
      "notes": "Performance optimization"
    }
  ]
}
```

Key points:

- `schema_version` enables future migrations.
- `mods_dir` allows changing the mods directory structure later.
- `source` abstracts where the mod came from; initially it may just be `{ "type": "local", "path": "..." }`.

### Filesystem vs Manifest

We distinguish between:

- **Manifest**: desired mods as recorded in `.mscmods.json`.
- **Filesystem**: actual `.jar`/`.zip` files in `data/mods` (and possibly `data/mods-disabled`).

Three useful sets:

- `manifest_only` – mods listed in manifest but missing on disk.
- `filesystem_only` – mods on disk but not in manifest (manual additions or leftovers).
- `in_sync` – manifest entry matches a file on disk (filename and optionally hash).

These drive commands like `msc mods status` and `msc mods sync`.

### Per-server vs Shared Library

- `.mscmods.json` is **per server**, stored under that server's `data` directory.
- A future extension might introduce a shared `~/.config/msc/mod-cache` for downloaded artifacts, referenced from each manifest via the `source` block.

---

## Conceptual Domains

The mod management logic is divided into four domains:

1. **Manifest domain**
   - Read/write `.mscmods.json`.
   - Validate schema and handle version migrations.
   - Provide CRUD on manifest entries (add/update/remove/enable/disable) without touching the filesystem.

2. **Filesystem domain**
   - Inspect `mods/` and (optionally) `mods-disabled/` on disk.
   - Enable/disable mods by moving or renaming files.
   - Handle conflicts and backups when overwriting files.

3. **Source domain**
   - Represent and resolve mod sources:
     - `local`: a file on the host.
     - `url`: a direct download URL.
     - `modrinth` / `curseforge`: later integrations.
     - `custom`: fallback type for advanced use.
   - Provide operations like:
     - Resolve to a concrete download URL + metadata.
     - Download a file to the mods directory.

4. **Lifecycle domain**
   - Enforce safe timing for operations (preferably when server is stopped).
   - Tie into `msc server` commands for stopping/restarting after changes.
   - Potentially add locking to avoid concurrent mod operations.

### Mod Identity

Each mod has two important identifiers:

- `id` – a stable canonical identifier (e.g., `lithium`, `fabric-api`).
- `filename` – the actual jar/zip on disk, which may change between versions.

When adding a mod:

- For **local files**:
  - `id` may default to a normalized version of the filename (minus obvious version suffixes).
  - `source.type = "local"` with the original path remembered.
- For **URLs**:
  - `source.type = "url"` and we store the URL and hash.
- For **Modrinth/CurseForge** (later):
  - `source.type = "modrinth"` or `"curseforge"` with project/version identifiers.

---

## CLI Surface: `msc mods ...`

The mod management commands live under `msc mods` to avoid bloating root.

Planned subcommands:

- `msc mods init` – create a new `.mscmods.json` for the current server.
- `msc mods list` – list mods from manifest + filesystem.
- `msc mods status` – show a high-level drift summary.
- `msc mods add` – add a mod to the manifest (and optionally download/copy to disk).
- `msc mods remove` – remove a mod from manifest and/or disk.
- `msc mods enable` / `msc mods disable` – toggle enabled state in manifest (and optionally on disk).
- `msc mods sync` – reconcile disk with manifest.
- `msc mods info` – display detailed info about a particular mod.
- `msc mods update` – check for updates and optionally apply (future).

### `msc mods list`

- Purpose: show all mods known to MSC and their current state.
- Output columns:
  - ID, name
  - Version
  - Enabled/disabled
  - Source type
  - On-disk status: `ok` / `missing` / `extra`
- Options:
  - `--enabled-only`
  - `--disabled-only`
  - `--extra` (filesystem-only)
  - `--json` (machine-readable view)

Implementation concept:

- Load manifest.
- Scan filesystem for `.jar`/`.zip` files in `mods/` (and `mods-disabled/` if used).
- Join the data to assign status flags per mod.

### `msc mods status`

- Purpose: quick overview of whether mods are in a healthy state.
- Example view:

  - Total manifest mods: N
  - Installed (in-sync): M
  - Missing from disk: X
  - Extra on disk: Y
  - (Later) Compatibility warnings: counts of loader/MC version mismatches.

### `msc mods add`

- Purpose: register a mod in the manifest and optionally place its file into the mods directory.

Inputs:

- `IDENTIFIER` (positional):
  - Local path (e.g., `~/Downloads/some-mod.jar`).
  - URL (e.g., `https://.../mod.jar`).
  - Later: `modrinth:<slug>` or just `<slug>` with `--source modrinth`.

Options:

- `--source {auto,local,url,modrinth,curseforge}` (default: auto-detect by scheme/path).
- `--id ID` – override the canonical ID.
- `--no-download` – just add to manifest using metadata, do not change files.
- `--enable/--disable` – initial enabled state.
- `--no-restart` – don't restart server automatically if changes occur.

Behavior sketch:

1. Resolve the source type.
2. If downloading/copying:
   - Ensure a unique target filename in `mods/`.
   - Write file and compute hash.
3. Add/update manifest entry with correct `filename`, `hashes`, and `source`.
4. Optionally suggest or trigger a server restart.

### `msc mods remove`

- Purpose: remove a mod in whole or in part.

Options:

- `msc mods remove ID`
- `--manifest-only` – forget it in manifest but leave files.
- `--file-only` – delete/move file but keep manifest entry (e.g., marking disabled).
- `--force` – confirm jar deletion if necessary.

### `msc mods enable` / `msc mods disable`

Two levels of effect:

1. **Manifest level**: flip `enabled` boolean.
2. **Filesystem level**: move or rename files.

Planned on-disk approach (for clarity):

- Enabled mods: `data/mods/<file>.jar`
- Disabled mods: `data/mods-disabled/<file>.jar`

Commands and behaviors:

- `msc mods disable ID [--no-move]`
  - Default: flip `enabled=false` *and* move file into `mods-disabled/`.
  - `--no-move`: only flip manifest flag.
- `msc mods enable ID`
  - Flip `enabled=true` and move file back to `mods/`.

### `msc mods sync`

- Purpose: reconcile actual files with what the manifest says.

For each manifest entry:

- If `enabled=true` but file isn't in `mods/`:
  - Option: download/copy if `source` permits.
  - Otherwise: warn as `missing`.
- If `enabled=false` but file is in `mods/`:
  - Move file to `mods-disabled/`.

For `filesystem_only` files (on disk but not in manifest):

- Options:
  - `--adopt-extra` – add them to the manifest as `source.local`.
  - `--delete-extra` – delete them from disk.
  - Default: report them as `extra` and take no action.

---

## Server Lifecycle Integration

Mod changes should generally be made while the server is **stopped**, or at least with caution.

Planned policy:

- For **destructive** or structural operations (`add`, `remove`, `sync` with deletes):
  - If server is running, MSC will usually **refuse** and print a clear message:
    - "Server is running; stop it first or use --force if you know what you're doing."
  - Allow an override flag (e.g., `--force` or `--allow-while-running`) with loud warnings.

- For **manifest-only** edits (no files changed):
  - Always allowed, with a note that changes apply on next restart.

- For **optional restarts**:
  - Certain commands might accept `--restart` to call `msc server restart` when mods change.

Since the server is running in a Docker container using `docker compose`, all file operations are performed on the host bind mount. The container sees updated files immediately once it's restarted.

---

## Safety & Validation

### Hashes & Integrity

The manifest can optionally store a checksum:

- `hashes.sha256` per mod.

Checks:

- When scanning disk, if a file's hash doesn't match the manifest, MSC can mark it as `modified`.
- This helps detect manual file replacements or corrupted downloads.

### Compatibility Hooks (Future)

Plan for, but don't yet implement:

- Loader compatibility: `mod.loader` vs `.msc.json`'s `server_type` (FABRIC, FORGE, PAPER, ...).
- Minecraft version compatibility: `mod.mc_version` vs `.msc.json`'s `minecraft_version`.
- A simple compatibility database or ruleset for known conflicts.

Exposure:

- `msc mods status` could show warning counts.
- `msc mods verify` could provide a detailed compatibility report.

---

## Docker Integration

The current Docker setup (using `itzg/minecraft-server`) mounts:

- Host server root: `/home/.../server`.
- Host `./data` -> container `/data`.

For mods:

- Host `./data/mods` <-> container `/data/mods`.

MSC modifies **host-side** files only:

- Download/copy/move jars into `./data/mods` (and `./data/mods-disabled`).
- Read/write manifest at `./data/mods/.mscmods.json`.
- Use existing `msc server` commands to stop/start/restart the Docker container when necessary.

No `docker exec` is required for mod file management, which keeps things simple and robust.

---

## Implementation Phases

To keep scope manageable, implementation will proceed in phases.

### Phase 1 (MVP)

- Manifest schema and loader for `.mscmods.json` (Fabric-first, but generic fields).
- Commands:
  - `msc mods init`
  - `msc mods list`
  - `msc mods status`
  - `msc mods add` (local file + basic URL support)
  - `msc mods enable` / `msc mods disable` (manifest + simple disk behavior)
- Conservative behavior:
  - Refuse to modify mods while server is running, unless forced.

### Phase 2

- `msc mods sync` to reconcile disk and manifest.
- Hash calculation and verification.
- `msc mods info` for detailed views of single mods.

### Phase 3

- `source` plugins for Modrinth/CurseForge.
- `msc mods update --check` / `--apply` for version upgrades.
- `msc mods verify` for compatibility and conflict checks.

---

## Modrinth & CurseForge Integration Plan

To extend the **source domain** beyond local files/URLs, MSC will add first-party resolvers for Modrinth and CurseForge. This section captures the APIs, required metadata, and CLI behavior so implementation can proceed methodically.

### Modrinth

- **API base:** `https://api.modrinth.com/v2` (no auth required, ~300 req/min soft limit).
- **Key endpoints:**
  - `GET /project/{slug|id}` → metadata, supported loaders/MC versions.
  - `GET /project/{slug}/version?game_versions=1.21.1&loaders=fabric` → list filtered versions.
  - `GET /version/{version_id}` → exact version info, including downloadable files.
- **Artifacts:** every version contains `files` entries with filename, primary flag, size, `is_server`, `hashes.sha512`/`sha1`, and a direct `url` (no headers needed).
- **Manifest mapping:** store `project_id`, `version_id`, `slug`, chosen `loader`, `mc_version`, upstream `version_number`, and `hashes.sha512`. `ModSource.type` becomes `"modrinth"` with additional fields for project/version IDs.
- **CLI UX:**
  - `msc mods add lithium --source-type modrinth [--loader fabric] [--mc-version 1.21.1]` resolves the latest compatible version, downloads the jar, and records metadata.
  - Future `msc mods search --source modrinth <query>` lists projects for discovery.
  - `msc mods update lithium --source modrinth --latest` will re-resolve and switch manifest entry.
- **Implementation sketch:**
  - Introduce `mods/sources/modrinth.py` with helpers `fetch_project(identifier)`, `select_version(project, loader, mc_version)`, `resolve_version(version_id)`.
  - Return a `ResolvedMod` dataclass capturing manifest info + download URL.
  - Reuse existing `_download_file` for the actual transfer.

### CurseForge (CF Core API)

- **API base:** `https://api.curseforge.com/v1` (requires API key via `x-api-key`). Request limit ~300/sec; be conservative.
- **Configuration:** add `curseforge_api_key` in `.msc.json` plus `MSC_CURSEFORGE_API_KEY` env override. Commands must fail fast with a helpful error if no key is present.
- **Key endpoints:**
  - `GET /mods/search?gameId=432&classId=6&searchFilter=<slug>&gameVersion=1.21.1&modLoaderType=4` to discover mods (432=Minecraft, class 6=Mods, loader enum values defined by CF).
  - `GET /mods/{modId}` for project metadata.
  - `GET /mods/{modId}/files` with filters by `gameVersion`/`modLoaderType` to list versions.
  - `GET /mods/{modId}/files/{fileId}` for download URL + hashes (MD5 only).
- **Manifest mapping:** store numeric `project_id` (mod id), `version_id` (file id), `slug`, loader + MC version, official file display name, and computed `hashes.sha256` (MSC should hash the downloaded jar since CF provides only MD5). `ModSource.type` becomes `"curseforge"` with `project_id`/`version_id` populated.
- **CLI UX:**
  - `msc mods add --source-type curseforge --project-id 238222 --mc-version 1.21.1 --loader fabric` resolves latest compatible file.
  - Allow text-based search: `msc mods add sodium --source curseforge` → auto-search and prompt if multiple matches.
  - `msc mods update sodium --source curseforge --latest` parallels Modrinth flow.
- **Implementation sketch:**
  - Create `mods/sources/curseforge.py` handling auth headers, search, version selection, and download resolution.
  - Add polite rate limiting/backoff (e.g., 5 req/sec cap) and descriptive error messages for HTTP 4xx/5xx responses.

### Shared Resolver Architecture

- Define a `SourceResolver` protocol (or simple class with `resolve(request: SourceRequest) -> ResolvedMod`). `SourceRequest` includes `identifier`, `loader`, `mc_version`, preferred release channel, and manifest defaults.
- Implement concrete resolvers for `local`, `url`, `modrinth`, and `curseforge`. The existing logic in `add_mod` becomes a thin wrapper that delegates to these resolvers.
- `ResolvedMod` should expose:
  - `filename`
  - `download_url` (optional for local sources)
  - `hashes` (if provided remotely; otherwise computed after download)
  - `source_metadata` (fields to persist in `ModSource`)
- Add a resolver registry keyed by `ModSourceType`. `add_mod` will look up the resolver based on CLI `--source-type` or manifest `entry.source.type` when rehydrating.
- Intelligent defaults: when loader or Minecraft version hints are omitted, MSC first looks in `.mscmods.json` and, if blank, derives them from `.msc.json`’s `server_type`/`minecraft_version`, persisting those defaults back into the manifest for future runs.

### Safety & User Experience Enhancements

- Maintain the current rule of refusing destructive operations while the server is running unless `--force` is used.
- Provide actionable tips when API prerequisites aren’t met (e.g., “Set MSC_CURSEFORGE_API_KEY before using CurseForge sources.”).
- Consider caching remote metadata under `~/.cache/msc/mods` to avoid repeated HTTP calls, especially for `status`/`update` flows. This is optional for the first iteration.
- Log (and possibly store) the upstream hash format (`sha512`, `sha1`, `md5`) for debugging; always compute and persist SHA-256 locally for consistent verification.

This design aims to keep mod management declarative, safe, and reproducible while fitting naturally into your existing MSC server and Docker setup.
