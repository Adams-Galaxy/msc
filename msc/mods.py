from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from pydantic import BaseModel, Field

from .config import MscConfig

MANIFEST_FILENAME = ".mscmods.json"
DEFAULT_MODS_DIR = "mods"
DEFAULT_DISABLED_DIR_SUFFIX = "-disabled"
SUPPORTED_SCHEMA_VERSION = 1


class ManifestError(RuntimeError):
    """Raised when the mods manifest cannot be loaded or used."""


class ModSourceType(str, Enum):
    LOCAL = "local"
    URL = "url"
    MODRINTH = "modrinth"
    CURSEFORGE = "curseforge"
    CUSTOM = "custom"


class ModHashes(BaseModel):
    sha256: Optional[str] = None
    sha512: Optional[str] = None
    sha1: Optional[str] = None
    md5: Optional[str] = None


class ModSource(BaseModel):
    type: str = ModSourceType.LOCAL.value
    path: Optional[str] = None
    url: Optional[str] = None
    project_id: Optional[str] = None
    version_id: Optional[str] = None
    slug: Optional[str] = None
    download_url: Optional[str] = None
    notes: Optional[str] = None


class ModEntry(BaseModel):
    id: str
    name: Optional[str] = None
    side: str = "server"
    enabled: bool = True
    filename: str
    source: ModSource = Field(default_factory=ModSource)
    version: Optional[str] = None
    mc_version: Optional[str] = None
    loader: Optional[str] = None
    installed_at: Optional[str] = None
    hashes: Optional[ModHashes] = None
    notes: Optional[str] = None


class ModManifest(BaseModel):
    schema_version: int = SUPPORTED_SCHEMA_VERSION
    loader: Optional[str] = None
    minecraft_version: Optional[str] = None
    mods_dir: str = DEFAULT_MODS_DIR
    mods: List[ModEntry] = Field(default_factory=list)

    def find(self, mod_id: str) -> ModEntry:
        for mod in self.mods:
            if mod.id == mod_id:
                return mod
        raise ManifestError(f"Mod '{mod_id}' not found in manifest")

    def add(self, entry: ModEntry) -> None:
        if any(mod.id == entry.id for mod in self.mods):
            raise ManifestError(f"Mod '{entry.id}' already exists in manifest")
        self.mods.append(entry)

    def remove(self, mod_id: str) -> None:
        before = len(self.mods)
        self.mods = [mod for mod in self.mods if mod.id != mod_id]
        if len(self.mods) == before:
            raise ManifestError(f"Mod '{mod_id}' not found in manifest")


@dataclass
class ModFile:
    filename: str
    path: Path
    location: str  # "mods" or "mods-disabled"
    sha256: Optional[str] = None


@dataclass
class ManifestEntryStatus:
    entry: ModEntry
    location: Optional[str]
    present: bool
    hash_ok: Optional[bool]

    @property
    def status(self) -> str:
        if not self.present:
            return "missing"
        if self.entry.enabled and self.location != "mods":
            return "moved"
        if (not self.entry.enabled) and self.location != "mods-disabled":
            return "moved"
        if self.hash_ok is False:
            return "hash-mismatch"
        return "ok"


@dataclass
class Inventory:
    entries: List[ManifestEntryStatus]
    extras: List[ModFile]

    @property
    def summary(self) -> Dict[str, int]:
        return {
            "total": len(self.entries),
            "ok": sum(1 for e in self.entries if e.status == "ok"),
            "missing": sum(1 for e in self.entries if e.status == "missing"),
            "moved": sum(1 for e in self.entries if e.status == "moved"),
            "hash_mismatch": sum(1 for e in self.entries if e.status == "hash-mismatch"),
            "extras": len(self.extras),
        }


@dataclass
class SourceRequest:
    cfg: MscConfig
    manifest: ModManifest
    source: str
    mods_directory: Path
    filename_override: Optional[str] = None
    suggested_mod_id: Optional[str] = None
    suggested_name: Optional[str] = None
    preferred_loader: Optional[str] = None
    preferred_mc_version: Optional[str] = None
    version_hint: Optional[str] = None
    project_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedMod:
    filename: str
    source: ModSource
    hashes: Optional[ModHashes] = None
    mod_id: Optional[str] = None
    name: Optional[str] = None
    version: Optional[str] = None
    loader: Optional[str] = None
    mc_version: Optional[str] = None
    mc_versions: Optional[List[str]] = None


class SourceResolver(Protocol):
    def resolve(self, request: SourceRequest) -> ResolvedMod:  # pragma: no cover - protocol
        ...


_SOURCE_RESOLVERS: Dict[str, SourceResolver] = {}


def register_source_resolver(source_type: ModSourceType, resolver: SourceResolver) -> None:
    _SOURCE_RESOLVERS[source_type.value] = resolver


def get_source_resolver(source_type: str) -> SourceResolver:
    try:
        return _SOURCE_RESOLVERS[source_type]
    except KeyError as exc:  # pragma: no cover - thin wrapper
        raise ManifestError(f"Source type '{source_type}' is not yet supported.") from exc


def manifest_path(cfg: MscConfig) -> Path:
    mods_dir = cfg.data_dir / DEFAULT_MODS_DIR
    return mods_dir / MANIFEST_FILENAME


def mods_dir(cfg: MscConfig, manifest: Optional[ModManifest] = None) -> Path:
    dir_name = manifest.mods_dir if manifest else DEFAULT_MODS_DIR
    return cfg.data_dir / dir_name


def disabled_dir(cfg: MscConfig, manifest: Optional[ModManifest] = None) -> Path:
    dir_name = manifest.mods_dir if manifest else DEFAULT_MODS_DIR
    return cfg.data_dir / f"{dir_name}{DEFAULT_DISABLED_DIR_SUFFIX}"


def ensure_directories(cfg: MscConfig, manifest: Optional[ModManifest] = None) -> None:
    mods_dir(cfg, manifest).mkdir(parents=True, exist_ok=True)
    disabled_dir(cfg, manifest).mkdir(parents=True, exist_ok=True)


def _normalize_version_list(value: Optional[Iterable[str] | str]) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    result = []
    for item in value:
        if item:
            result.append(str(item))
    return result


def _assert_version_compatibility(
    *,
    mod_identifier: str,
    resolved_loader: Optional[str],
    resolved_mc_versions: List[str],
    preferred_loader: Optional[str],
    preferred_mc_version: Optional[str],
) -> None:
    if preferred_loader and resolved_loader:
        if preferred_loader.lower() != resolved_loader.lower():
            raise ManifestError(
                f"{mod_identifier} targets loader '{resolved_loader}' which does not match the server loader '{preferred_loader}'."
            )

    if preferred_mc_version:
        normalized_target = preferred_mc_version.lower()
        normalized_supported = {version.lower() for version in resolved_mc_versions}
        if resolved_mc_versions and normalized_target not in normalized_supported:
            readable_supported = ", ".join(resolved_mc_versions)
            raise ManifestError(
                f"{mod_identifier} is tagged for Minecraft {readable_supported} but the server is {preferred_mc_version}."
            )


def load_manifest(cfg: MscConfig) -> ModManifest:
    path = manifest_path(cfg)
    if not path.exists():
        raise ManifestError("Mods manifest not found. Run 'msc mods init' first.")
    data = json.loads(path.read_text())
    manifest = ModManifest.model_validate(data)
    if manifest.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ManifestError(
            f"Unsupported manifest schema version {manifest.schema_version}."
        )
    return manifest


def save_manifest(cfg: MscConfig, manifest: ModManifest) -> None:
    path = manifest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2, exclude_none=True))


def init_manifest(
    cfg: MscConfig,
    *,
    force: bool = False,
    adopt_existing: bool = False,
) -> tuple[ModManifest, int]:
    path = manifest_path(cfg)
    if path.exists() and not force:
        raise ManifestError("Mods manifest already exists. Use --force to overwrite.")

    manifest = ModManifest(
        loader=cfg.server_type.lower() if cfg.server_type else None,
        minecraft_version=cfg.minecraft_version,
        mods_dir=DEFAULT_MODS_DIR,
    )

    ensure_directories(cfg, manifest)

    adopted: List[ModEntry] = []
    if adopt_existing:
        adopted = _adopt_existing_mods(cfg, manifest)

    save_manifest(cfg, manifest)
    return manifest, len(adopted)


def _adopt_existing_mods(cfg: MscConfig, manifest: ModManifest) -> List[ModEntry]:
    mods_directory = mods_dir(cfg, manifest)
    adopted: List[ModEntry] = []
    for file in _iter_mod_files(mods_directory):
        entry = ModEntry(
            id=_derive_mod_id(file.name),
            name=file.stem,
            filename=file.name,
            enabled=True,
            loader=manifest.loader,
            mc_version=manifest.minecraft_version,
            installed_at=_now_iso(),
            source=ModSource(type=ModSourceType.LOCAL.value, path=str(file)),
            hashes=ModHashes(sha256=_sha256(file)),
        )
        try:
            manifest.add(entry)
            adopted.append(entry)
        except ManifestError:
            continue
    return adopted


def _iter_mod_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return [f for f in directory.iterdir() if f.is_file() and f.suffix in {".jar", ".zip"}]


def inventory(cfg: MscConfig, manifest: ModManifest) -> Inventory:
    ensure_directories(cfg, manifest)
    files = _scan_files(cfg, manifest)

    statuses: List[ManifestEntryStatus] = []
    remaining_files = dict(files)

    for entry in manifest.mods:
        file_info = remaining_files.pop(entry.filename, None)
        present = file_info is not None
        location = file_info.location if file_info else None
        hash_ok: Optional[bool] = None
        if present and entry.hashes and entry.hashes.sha256:
            hash_ok = file_info.sha256 == entry.hashes.sha256
        statuses.append(
            ManifestEntryStatus(
                entry=entry,
                location=location,
                present=present,
                hash_ok=hash_ok,
            )
        )

    extras = list(remaining_files.values())
    return Inventory(entries=statuses, extras=extras)


def _scan_files(cfg: MscConfig, manifest: ModManifest) -> Dict[str, ModFile]:
    mods_directory = mods_dir(cfg, manifest)
    disabled_directory = disabled_dir(cfg, manifest)
    files: Dict[str, ModFile] = {}

    for file in _iter_mod_files(mods_directory):
        files[file.name] = ModFile(
            filename=file.name,
            path=file,
            location="mods",
            sha256=_sha256(file),
        )

    for file in _iter_mod_files(disabled_directory):
        files[file.name] = ModFile(
            filename=file.name,
            path=file,
            location="mods-disabled",
            sha256=_sha256(file),
        )

    return files


def add_mod(
    cfg: MscConfig,
    *,
    source: str,
    manifest: ModManifest,
    mod_id: Optional[str] = None,
    name: Optional[str] = None,
    enabled: bool = True,
    source_type: Optional[str] = None,
    manifest_only: bool = False,
    filename_override: Optional[str] = None,
    loader_hint: Optional[str] = None,
    mc_version_hint: Optional[str] = None,
    version_hint: Optional[str] = None,
    project_id: Optional[str] = None,
) -> ModEntry:
    ensure_directories(cfg, manifest)
    mods_directory = mods_dir(cfg, manifest)

    if manifest_only and not filename_override:
        raise ManifestError("--manifest-only requires --filename to be provided.")

    inferred_source_type = _infer_source_type(source) if source_type is None else source_type
    if inferred_source_type not in {t.value for t in ModSourceType}:
        raise ManifestError(f"Unsupported source type '{inferred_source_type}'.")

    normalized_source, inline_version = _normalize_source_identifier(source, inferred_source_type)
    if version_hint is None and inline_version:
        version_hint = inline_version

    default_loader = manifest.loader or _loader_from_config(cfg)
    if manifest.loader is None and default_loader:
        manifest.loader = default_loader
    default_mc_version = manifest.minecraft_version or cfg.minecraft_version
    if manifest.minecraft_version is None and cfg.minecraft_version:
        manifest.minecraft_version = cfg.minecraft_version
    preferred_loader = loader_hint or default_loader
    preferred_mc_version = mc_version_hint or default_mc_version

    if manifest_only:
        target_filename = filename_override
        hashes = None
        mod_source = ModSource(type=inferred_source_type, notes="Manifest entry only")
        resolved_name = None
        resolved_mod_id = None
        resolved_version = None
        resolved_loader = None
        resolved_mc_version = None
    else:
        resolver = get_source_resolver(inferred_source_type)
        request = SourceRequest(
            cfg=cfg,
            manifest=manifest,
            source=normalized_source,
            mods_directory=mods_directory,
            filename_override=filename_override,
            suggested_mod_id=mod_id,
            suggested_name=name,
            preferred_loader=preferred_loader,
            preferred_mc_version=preferred_mc_version,
            version_hint=version_hint,
            project_id=project_id,
        )
        resolved = resolver.resolve(request)
        resolved_versions = _normalize_version_list(resolved.mc_versions or resolved.mc_version)
        _assert_version_compatibility(
            mod_identifier=mod_id or resolved.mod_id or normalized_source,
            resolved_loader=resolved.loader,
            resolved_mc_versions=resolved_versions,
            preferred_loader=preferred_loader,
            preferred_mc_version=preferred_mc_version,
        )
        target_filename = resolved.filename
        hashes = resolved.hashes
        mod_source = resolved.source
        resolved_name = resolved.name
        resolved_mod_id = resolved.mod_id
        resolved_version = resolved.version
        resolved_loader = resolved.loader
        resolved_mc_version = resolved.mc_version

    if not target_filename:
        raise ManifestError("Unable to determine target filename for mod.")

    derived_mod_id = _derive_mod_id(target_filename)
    mod_id = mod_id or resolved_mod_id or derived_mod_id
    if any(mod.id == mod_id for mod in manifest.mods):
        raise ManifestError(f"Mod '{mod_id}' already exists in manifest.")

    entry_name = name or resolved_name or _humanize_name(mod_id)
    entry_loader = resolved_loader or preferred_loader
    entry_mc_version = resolved_mc_version or preferred_mc_version
    entry_version = resolved_version

    entry = ModEntry(
        id=mod_id,
        name=entry_name,
        filename=target_filename,
        enabled=enabled,
        loader=entry_loader,
        mc_version=entry_mc_version,
        version=entry_version,
        installed_at=_now_iso(),
        source=mod_source,
        hashes=hashes,
    )

    manifest.add(entry)
    save_manifest(cfg, manifest)
    return entry


def set_enabled(
    cfg: MscConfig,
    *,
    manifest: ModManifest,
    mod_id: str,
    enabled: bool,
    move_files: bool = True,
) -> ModEntry:
    ensure_directories(cfg, manifest)
    entry = manifest.find(mod_id)
    if entry.enabled == enabled:
        return entry

    src_dir = disabled_dir(cfg, manifest) if enabled else mods_dir(cfg, manifest)
    dst_dir = mods_dir(cfg, manifest) if enabled else disabled_dir(cfg, manifest)
    src_path = src_dir / entry.filename
    dst_path = dst_dir / entry.filename

    if move_files:
        if src_path.exists():
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
        elif enabled:
            # if enabling but file not present in disabled dir, we won't fail - manifest will show missing
            pass

    entry.enabled = enabled
    save_manifest(cfg, manifest)
    return entry


def remove_mod(
    cfg: MscConfig,
    *,
    manifest: ModManifest,
    mod_id: str,
    remove_files: bool = True,
) -> tuple[ModEntry, List[Path]]:
    """Remove a mod from the manifest and optionally delete its files."""

    ensure_directories(cfg, manifest)
    entry = manifest.find(mod_id)

    deleted_files: List[Path] = []
    if remove_files:
        candidate_paths = [
            mods_dir(cfg, manifest) / entry.filename,
            disabled_dir(cfg, manifest) / entry.filename,
        ]
        for path in candidate_paths:
            if path.exists():
                path.unlink()
                deleted_files.append(path)

    manifest.remove(mod_id)
    save_manifest(cfg, manifest)
    return entry, deleted_files


def purge_mods(
    cfg: MscConfig,
    *,
    manifest: ModManifest,
    remove_files: bool = True,
) -> tuple[int, List[Path]]:
    """Remove every mod from the manifest (optionally deleting files)."""

    ensure_directories(cfg, manifest)
    deleted: List[Path] = []
    removed_count = 0
    for entry in list(manifest.mods):
        _, removed_paths = remove_mod(cfg, manifest=manifest, mod_id=entry.id, remove_files=remove_files)
        removed_count += 1
        deleted.extend(removed_paths)
    return removed_count, deleted


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _download_file(url: str, dest: Path, headers: Optional[Dict[str, str]] = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url)
    if headers:
        for key, value in headers.items():
            if value is not None:
                request.add_header(key, value)
    try:
        with urllib.request.urlopen(request) as response, dest.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except urllib.error.URLError as exc:  # pragma: no cover - network dependent
        raise ManifestError(f"Failed to download {url}: {exc}") from exc


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Any:
    request = urllib.request.Request(url)
    if headers:
        for key, value in headers.items():
            if value is not None:
                request.add_header(key, value)
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        detail = exc.read().decode("utf-8", errors="ignore")
        message = detail or exc.reason
        raise ManifestError(f"HTTP {exc.code} error fetching {url}: {message}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network dependent
        raise ManifestError(f"Network error fetching {url}: {exc.reason}") from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - network dependent
        raise ManifestError(f"Invalid JSON payload from {url}: {exc}") from exc


def _derive_mod_id(filename: str) -> str:
    stem = Path(filename).stem
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", stem).strip("-").lower()
    return slug or "mod"


def _humanize_name(mod_id: str) -> str:
    parts = mod_id.replace("-", " ").replace("_", " ")
    return parts.title()


def _first_or_none(values: Iterable[Any]) -> Optional[Any]:
    for value in values:
        return value
    return None


def _loader_from_config(cfg: MscConfig) -> Optional[str]:
    if not cfg.server_type:
        return None
    key = cfg.server_type.strip().lower()
    mapping = {
        "fabric": "fabric",
        "quilt": "quilt",
        "forge": "forge",
        "neoforge": "neoforge",
        "paper": "paper",
        "purpur": "paper",
        "spigot": "paper",
        "vanilla": "vanilla",
    }
    return mapping.get(key, key if key else None)


def _infer_source_type(source: str) -> str:
    lowered = source.lower()
    if lowered.startswith("modrinth:") or lowered.startswith("mr:"):
        return ModSourceType.MODRINTH.value
    if lowered.startswith("curseforge:") or lowered.startswith("cf:"):
        return ModSourceType.CURSEFORGE.value
    if source.startswith("http://") or source.startswith("https://"):
        return ModSourceType.URL.value
    path = Path(source).expanduser()
    if path.exists():
        return ModSourceType.LOCAL.value
    return ModSourceType.CUSTOM.value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_source_identifier(source: str, source_type: str) -> tuple[str, Optional[str]]:
    lowered = source.lower()
    target = source
    if source_type == ModSourceType.MODRINTH.value:
        prefixes = ("modrinth:", "mr:")
    elif source_type == ModSourceType.CURSEFORGE.value:
        prefixes = ("curseforge:", "cf:")
    else:
        prefixes = tuple()

    for prefix in prefixes:
        if lowered.startswith(prefix):
            target = source.split(":", 1)[1]
            break

    inline_version: Optional[str] = None
    if source_type in {ModSourceType.MODRINTH.value, ModSourceType.CURSEFORGE.value} and "@" in target:
        parts = target.split("@", 1)
        target = parts[0]
        inline_version = parts[1]

    return target, inline_version


class LocalSourceResolver(SourceResolver):
    def resolve(self, request: SourceRequest) -> ResolvedMod:
        src_path = Path(request.source).expanduser().resolve()
        if not src_path.exists():
            raise ManifestError(f"Source file '{request.source}' does not exist.")

        filename = request.filename_override or src_path.name
        if not filename:
            raise ManifestError("Local mod filename could not be determined.")

        dest_path = request.mods_directory / filename
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if src_path.resolve() != dest_path.resolve():
            shutil.copyfile(src_path, dest_path)

        hashes = ModHashes(sha256=_sha256(dest_path))
        return ResolvedMod(
            filename=filename,
            source=ModSource(type=ModSourceType.LOCAL.value, path=str(src_path)),
            hashes=hashes,
            mod_id=request.suggested_mod_id or _derive_mod_id(filename),
            name=request.suggested_name,
        )


class UrlSourceResolver(SourceResolver):
    def resolve(self, request: SourceRequest) -> ResolvedMod:
        parsed = urlparse(request.source)
        remote_name = Path(parsed.path).name
        inferred_id = request.suggested_mod_id or _derive_mod_id(remote_name or "mod")
        filename = request.filename_override or remote_name or f"{inferred_id}.jar"
        if not filename:
            raise ManifestError("URL mod filename could not be determined.")

        dest_path = request.mods_directory / filename
        headers = {"User-Agent": request.cfg.api_user_agent}
        _download_file(request.source, dest_path, headers=headers)
        hashes = ModHashes(sha256=_sha256(dest_path))

        return ResolvedMod(
            filename=filename,
            source=ModSource(type=ModSourceType.URL.value, url=request.source),
            hashes=hashes,
            mod_id=request.suggested_mod_id or inferred_id,
            name=request.suggested_name,
        )


class ModrinthResolver(SourceResolver):
    API_BASE = "https://api.modrinth.com/v2"

    def resolve(self, request: SourceRequest) -> ResolvedMod:
        identifier = request.project_id or request.source
        headers = self._headers(request)
        project = self._fetch_project(identifier, headers)
        slug = project.get("slug") or identifier
        version = self._resolve_version(project, request, headers)
        file_data = self._select_file(version)

        filename = request.filename_override or file_data.get("filename")
        if not filename:
            raise ManifestError("Modrinth file is missing a filename.")

        download_url = file_data.get("url")
        if not download_url:
            raise ManifestError("Modrinth version does not expose a download URL.")

        dest_path = request.mods_directory / filename
        _download_file(download_url, dest_path, headers=headers)
        sha256 = _sha256(dest_path)
        hashes = ModHashes(
            sha256=sha256,
            sha512=(file_data.get("hashes") or {}).get("sha512"),
            sha1=(file_data.get("hashes") or {}).get("sha1"),
        )

        loaders = version.get("loaders") or []
        mc_versions = version.get("game_versions") or []

        return ResolvedMod(
            filename=filename,
            source=ModSource(
                type=ModSourceType.MODRINTH.value,
                url=project.get("project_url") or project.get("wiki_url"),
                project_id=project.get("id"),
                version_id=version.get("id"),
                slug=slug,
                download_url=download_url,
            ),
            hashes=hashes,
            mod_id=request.suggested_mod_id or slug,
            name=request.suggested_name or project.get("title") or slug,
            version=version.get("version_number"),
            loader=_first_or_none(loaders) or request.preferred_loader,
            mc_version=_first_or_none(mc_versions) or request.preferred_mc_version,
            mc_versions=mc_versions or None,
        )

    def _headers(self, request: SourceRequest) -> Dict[str, str]:
        return {
            "User-Agent": request.cfg.api_user_agent,
            "Accept": "application/json",
        }

    def _fetch_project(self, identifier: str, headers: Dict[str, str]) -> Dict[str, Any]:
        url = f"{self.API_BASE}/project/{identifier}"
        return _http_get_json(url, headers=headers)

    def _resolve_version(
        self,
        project: Dict[str, Any],
        request: SourceRequest,
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        hint = request.version_hint
        if hint:
            version = self._fetch_version_by_hint(hint, headers)
            if version and version.get("project_id") == project.get("id"):
                return version

        params: Dict[str, Any] = {}
        if request.preferred_loader:
            params["loaders"] = request.preferred_loader
        if request.preferred_mc_version:
            params["game_versions"] = request.preferred_mc_version

        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = f"{self.API_BASE}/project/{project.get('id')}/version{query}"
        versions = _http_get_json(url, headers=headers)
        if not versions:
            raise ManifestError("No Modrinth versions matched the requested filters.")

        if hint:
            for version in versions:
                if version.get("version_number") == hint:
                    return version

        for release_type in ("release", "beta", "alpha"):
            for version in versions:
                if version.get("version_type") == release_type:
                    return version

        return versions[0]

    def _fetch_version_by_hint(self, hint: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
        url = f"{self.API_BASE}/version/{hint}"
        try:
            return _http_get_json(url, headers=headers)
        except ManifestError:
            return None

    def _select_file(self, version: Dict[str, Any]) -> Dict[str, Any]:
        files = version.get("files") or []
        if not files:
            raise ManifestError("Modrinth version does not contain downloadable files.")
        for file in files:
            if file.get("primary"):
                return file
        return files[0]


class CurseForgeResolver(SourceResolver):
    API_BASE = "https://api.curseforge.com/v1"
    GAME_ID = 432
    MOD_CLASS_ID = 6

    def resolve(self, request: SourceRequest) -> ResolvedMod:
        if not request.cfg.curseforge_api_key:
            raise ManifestError(
                "CurseForge API key missing. Set MSC_CURSEFORGE_API_KEY or add curseforge_api_key to .msc.json."
            )

        headers = self._headers(request)
        project = self._resolve_project(request, headers)
        project_id = project.get("id")
        files = self._list_files(project_id, request, headers)
        file_data = self._select_file(files, request.version_hint)

        filename = request.filename_override or file_data.get("fileName")
        if not filename:
            raise ManifestError("CurseForge file is missing a filename.")

        download_url = file_data.get("downloadUrl")
        if not download_url:
            raise ManifestError("CurseForge file lacks a download URL.")

        dest_path = request.mods_directory / filename
        _download_file(download_url, dest_path, headers=headers)
        sha256 = _sha256(dest_path)

        hashes = ModHashes(
            sha256=sha256,
            md5=self._extract_hash(file_data, algo=2),
            sha1=self._extract_hash(file_data, algo=1),
        )
        mc_versions = file_data.get("gameVersions") or []

        return ResolvedMod(
            filename=filename,
            source=ModSource(
                type=ModSourceType.CURSEFORGE.value,
                project_id=str(project_id) if project_id is not None else None,
                version_id=str(file_data.get("id")) if file_data.get("id") is not None else None,
                slug=project.get("slug"),
                url=(project.get("links") or {}).get("websiteUrl"),
                download_url=download_url,
            ),
            hashes=hashes,
            mod_id=request.suggested_mod_id or project.get("slug") or str(project_id),
            name=request.suggested_name or project.get("name"),
            version=file_data.get("displayName") or file_data.get("fileName"),
            loader=request.preferred_loader,
            mc_version=_first_or_none(mc_versions) or request.preferred_mc_version,
            mc_versions=mc_versions or None,
        )

    def _headers(self, request: SourceRequest) -> Dict[str, str]:
        return {
            "x-api-key": request.cfg.curseforge_api_key or "",
            "User-Agent": request.cfg.api_user_agent,
            "Accept": "application/json",
        }

    def _resolve_project(self, request: SourceRequest, headers: Dict[str, str]) -> Dict[str, Any]:
        if request.project_id:
            url = f"{self.API_BASE}/mods/{request.project_id}"
            data = _http_get_json(url, headers=headers)
            return data.get("data") or {}

        params: Dict[str, Any] = {
            "gameId": self.GAME_ID,
            "classId": self.MOD_CLASS_ID,
            "searchFilter": request.source,
            "pageSize": 50,
        }
        loader_type = self._loader_type(request.preferred_loader)
        if loader_type:
            params["modLoaderType"] = loader_type
        if request.preferred_mc_version:
            params["gameVersion"] = request.preferred_mc_version

        query = f"?{urlencode(params)}"
        url = f"{self.API_BASE}/mods/search{query}"
        data = _http_get_json(url, headers=headers)
        results = data.get("data") or []
        if not results:
            raise ManifestError("No CurseForge projects matched the given search filter.")

        for result in results:
            if result.get("slug") == request.source:
                return result
        return results[0]

    def _list_files(self, project_id: Any, request: SourceRequest, headers: Dict[str, str]) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"pageSize": 50}
        loader_type = self._loader_type(request.preferred_loader)
        if loader_type:
            params["modLoaderType"] = loader_type
        if request.preferred_mc_version:
            params["gameVersion"] = request.preferred_mc_version

        query = f"?{urlencode(params)}"
        url = f"{self.API_BASE}/mods/{project_id}/files{query}"
        data = _http_get_json(url, headers=headers)
        files = data.get("data") or []
        if not files:
            raise ManifestError("No CurseForge files matched the requested filters.")
        return files

    def _select_file(self, files: List[Dict[str, Any]], version_hint: Optional[str]) -> Dict[str, Any]:
        if version_hint:
            for file in files:
                if str(file.get("id")) == version_hint or file.get("fileName") == version_hint or file.get("displayName") == version_hint:
                    return file

        for release_type in (1, 2, 3):  # 1=release,2=beta,3=alpha
            release_candidates = [f for f in files if f.get("releaseType") == release_type]
            if release_candidates:
                return release_candidates[0]

        return files[0]

    def _loader_type(self, loader: Optional[str]) -> int:
        if not loader:
            return 0
        mapping = {
            "forge": 1,
            "cauldron": 2,
            "liteloader": 3,
            "fabric": 4,
            "quilt": 5,
            "neoforge": 6,
        }
        return mapping.get(loader.lower(), 0)

    def _extract_hash(self, file_data: Dict[str, Any], algo: int) -> Optional[str]:
        for entry in file_data.get("hashes") or []:
            if entry.get("algo") == algo:
                return entry.get("value")
        return None


register_source_resolver(ModSourceType.LOCAL, LocalSourceResolver())
register_source_resolver(ModSourceType.URL, UrlSourceResolver())
register_source_resolver(ModSourceType.MODRINTH, ModrinthResolver())
register_source_resolver(ModSourceType.CURSEFORGE, CurseForgeResolver())
