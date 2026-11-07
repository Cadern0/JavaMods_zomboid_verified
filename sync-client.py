#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sincronizador de mods Java do Project Zomboid.

Uso basico:
    python sync-client.py
    python sync-client.py --check
    python sync-client.py --dry-run
    python sync-client.py --uninstall

Opcoes uteis:
    --game-dir PASTA       Pasta onde esta ProjectZomboid64.json
    --user-mods PASTA      Pasta ~/Zomboid/mods (ou equivalente)
    --manifest-url URL     Manifesto alternativo

Variaveis de ambiente:
    PZ_GAME_DIR, PZ_USER_MODS, PZ_MANIFEST_URL, PZ_SYNC_STATE

O script usa apenas a biblioteca padrao do Python 3.8+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# O publicador substitui este marcador pela URL do repositorio configurado.
DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/Cadern0/JavaMods_zomboid_verified/main/manifest.json"
FALLBACK_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Cadern0/JavaMods_zomboid_verified/"
    "main/manifest.json"
)

STATE_VERSION = 1
USER_AGENT = "PZSM-Sync/1.0 (+https://github.com/Cadern0/JavaMods_zomboid_verified)"
GAME_PROCESS_NAMES = {
    "projectzomboid",
    "projectzomboid64",
    "projectzomboid.exe",
    "projectzomboid64.exe",
}


class SyncError(RuntimeError):
    """Erro esperado do sincronizador."""


# Arquivos que o manifesto pode gravar na pasta do JOGO. Sem esta lista, um
# manifesto adulterado podia sobrescrever qualquer arquivo da instalação.
GAME_DIR_ALLOWED_SUFFIXES = (".jar", ".dll", ".so", ".dylib")


def _require_https(url: str, what: str) -> None:
    if not url.lower().startswith("https://"):
        raise SyncError(f"{what} precisa usar HTTPS: {url}")


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Recusa redirecionamentos para HTTP (downgrade)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not str(newurl).lower().startswith("https://"):
            raise urllib.error.URLError(f"redirecionamento inseguro recusado: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirect())


def log(message: str, level: str = "info") -> None:
    prefixes = {
        "info": "[i]",
        "ok": "[OK]",
        "warn": "[!]",
        "error": "[ERRO]",
    }
    print(f"[{time.strftime('%H:%M:%S')}] {prefixes.get(level, '[i]')} {message}", flush=True)


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    result: List[Path] = []
    seen = set()
    for path in paths:
        try:
            key = os.path.normcase(os.path.realpath(str(path)))
        except (OSError, RuntimeError):
            key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _steamapps_dir(root: Path) -> Path:
    root = root.expanduser()
    if root.name.lower() == "steamapps":
        return root
    return root / "steamapps"


def _vdf_path(value: str) -> Path:
    # O VDF escapa barras invertidas como \\.
    return Path(value.replace("\\\\", "\\"))


def _parse_library_folders(root: Path) -> List[Path]:
    steamapps = _steamapps_dir(root)
    candidates = [
        steamapps / "libraryfolders.vdf",
        root / "libraryfolders.vdf",
        root / "config" / "libraryfolders.vdf",
    ]
    libraries: List[Path] = []
    for vdf in _dedupe_paths(candidates):
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Formato novo: "path" "D:\\SteamLibrary"
        for match in re.finditer(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
            libraries.append(_vdf_path(match.group(1)))
        # Formato antigo: "1" "D:\\SteamLibrary"
        for match in re.finditer(r'^\s*"\d+"\s+"([^"]+)"', text, re.MULTILINE):
            libraries.append(_vdf_path(match.group(1)))
    return _dedupe_paths(libraries)


def _steam_roots() -> List[Path]:
    roots: List[Path] = []
    env = os.environ.get("STEAM_PATH")
    if env:
        roots.append(Path(env))

    home = Path.home()
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            entries = (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            )
            for hive, subkey, value_name in entries:
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                        if value:
                            roots.append(Path(value))
                except OSError:
                    pass
        except ImportError:
            pass

        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env_name)
            if base:
                roots.append(Path(base) / "Steam")
    elif sys.platform == "darwin":
        roots.extend(
            [
                home / "Library" / "Application Support" / "Steam",
                home / "Library" / "Application Support" / "Steam" / "steamapps",
            ]
        )
    else:
        roots.extend(
            [
                home / ".steam" / "steam",
                home / ".steam" / "root",
                home / ".local" / "share" / "Steam",
                home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
            ]
        )

    return _dedupe_paths(roots)


def find_game_dir(explicit: Optional[str] = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            raise SyncError(f"Pasta do jogo nao encontrada: {path}")
        return path.resolve()

    env = os.environ.get("PZ_GAME_DIR")
    if env:
        return find_game_dir(env)

    candidates: List[Path] = []
    for root in _steam_roots():
        steamapps = _steamapps_dir(root)
        candidates.append(steamapps / "common" / "ProjectZomboid")
        for library in _parse_library_folders(root):
            candidates.append(_steamapps_dir(library) / "common" / "ProjectZomboid")

    if os.name == "nt":
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(
                    Path(base) / "Steam" / "steamapps" / "common" / "ProjectZomboid"
                )
    elif sys.platform == "darwin":
        candidates.append(
            Path.home()
            / "Library"
            / "Application Support"
            / "Steam"
            / "steamapps"
            / "common"
            / "ProjectZomboid"
        )
    else:
        candidates.extend(
            [
                Path("/usr/local/games/ProjectZomboid"),
                Path("/opt/ProjectZomboid"),
            ]
        )

    unique = _dedupe_paths(candidates)
    for candidate in unique:
        if candidate.is_dir() and (candidate / "ProjectZomboid64.json").is_file():
            return candidate.resolve()
    for candidate in unique:
        if candidate.is_dir():
            return candidate.resolve()

    raise SyncError(
        "Nao encontrei o Project Zomboid. Use --game-dir para informar a pasta "
        "que contem ProjectZomboid64.json."
    )


def find_user_mods(explicit: Optional[str] = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists() and not path.is_dir():
            raise SyncError(f"Pasta de mods invalida: {path}")
        return path.resolve()

    env = os.environ.get("PZ_USER_MODS")
    if env:
        return find_user_mods(env)

    candidates = [Path.home() / "Zomboid" / "mods"]

    # Fallback para quem joga pelo Proton/Flatpak.
    for root in _steam_roots():
        users = (
            _steamapps_dir(root)
            / "compatdata"
            / "108600"
            / "pfx"
            / "drive_c"
            / "users"
        )
        if not users.is_dir():
            continue
        try:
            for user in users.iterdir():
                candidates.append(user / "Zomboid" / "mods")
        except OSError:
            pass

    for candidate in _dedupe_paths(candidates):
        if candidate.is_dir():
            return candidate.resolve()

    return (Path.home() / "Zomboid" / "mods").resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.realpath(str(path)))
        normalized_root = os.path.normcase(os.path.realpath(str(root)))
        return os.path.commonpath([normalized_path, normalized_root]) == normalized_root
    except (ValueError, OSError, RuntimeError):
        return False


def _validate_target(path: Path, allowed_roots: Tuple[Path, ...]) -> None:
    if not any(_is_within(path, root) for root in allowed_roots):
        raise SyncError(f"Destino fora das pastas permitidas: {path}")
    for root in allowed_roots:
        try:
            if os.path.normcase(os.path.realpath(str(path))) == os.path.normcase(
                os.path.realpath(str(root))
            ):
                raise SyncError(f"Destino invalido (e uma pasta raiz): {path}")
        except (OSError, RuntimeError):
            pass
    if path.exists() and path.is_dir():
        raise SyncError(f"Destino invalido (e um diretorio): {path}")


def _expand_target(raw: str, game_dir: Path, user_mods: Path) -> Path:
    value = str(raw).replace("\\", "/")
    pairs = (
        ("%GAME_DIR%", game_dir),
        ("%USER_MODS%", user_mods),
    )
    for prefix, root in pairs:
        if value.upper() == prefix:
            return root
        if value.upper().startswith(prefix + "/"):
            relative = value[len(prefix) + 1 :]
            return root / relative
    raise SyncError(f"Caminho de destino nao reconhecido no manifesto: {raw}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_copy(source: Path, destination: Path) -> None:
    _atomic_write_bytes(destination, source.read_bytes())


def _check_game_running() -> Optional[str]:
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
            for line in completed.stdout.splitlines():
                name = line.split(",")[0].strip().strip('"').lower()
                if name in GAME_PROCESS_NAMES:
                    return name
        else:
            completed = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
            for line in completed.stdout.splitlines():
                name = Path(line.strip()).name.lower()
                if name in GAME_PROCESS_NAMES:
                    return name
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Nao foi possivel verificar se o jogo esta aberto: {exc}", "warn")
    return None


def _load_manifest(url: str) -> Dict[str, Any]:
    _require_https(url, "URL do manifesto")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
    )
    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            with _OPENER.open(request, timeout=60) as response:
                raw = response.read()
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise SyncError("O manifesto nao e um objeto JSON valido.")
            return data
        except (urllib.error.URLError, UnicodeError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
            if attempt < 2:
                log(
                    f"Falha ao consultar o manifesto (tentativa {attempt + 1}/3): {exc}",
                    "warn",
                )
                time.sleep(1.5 * (attempt + 1))
    raise SyncError(f"Nao foi possivel baixar o manifesto: {last_error}")


def _iter_manifest_files(manifest: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    for category_name in ("engine_core", "java_mods"):
        category = manifest.get(category_name) or {}
        if not isinstance(category, dict):
            raise SyncError(f"Secao invalida no manifesto: {category_name}")
        for mod_id, mod_data in category.items():
            if not isinstance(mod_data, dict):
                continue
            files = mod_data.get("files")
            if files is None:
                continue
            if not isinstance(files, list):
                raise SyncError(f"Lista de arquivos invalida em {category_name}/{mod_id}")
            for entry in files:
                if not isinstance(entry, dict):
                    raise SyncError(f"Entrada de arquivo invalida em {category_name}/{mod_id}")
                yield str(mod_id), entry


def _valid_sha(raw: Any, name: str) -> str:
    sha = str(raw or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise SyncError(f"Hash SHA256 invalido para {name}: {raw!r}")
    return sha


def _download_file(url: str, destination: Path, expected_sha: str, name: str) -> None:
    _require_https(url, f"URL de download de {name}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")

    for attempt in range(3):
        try:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with _OPENER.open(request, timeout=120) as response, temporary.open("wb") as handle:
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                next_report = 25
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = min(100, downloaded * 100 // total)
                        if sys.stdout.isatty():
                            sys.stdout.write(f"\r  {name}: {percent:3d}%")
                            sys.stdout.flush()
                        elif percent >= next_report:
                            log(f"  {name}: {percent}%")
                            next_report = ((percent // 25) + 1) * 25
                if sys.stdout.isatty():
                    sys.stdout.write("\n")
                    sys.stdout.flush()

            actual_sha = _sha256_file(temporary)
            if actual_sha != expected_sha:
                raise SyncError(
                    f"Hash de {name} nao confere "
                    f"(esperado {expected_sha[:12]}..., obtido {actual_sha[:12]}...)"
                )
            os.replace(str(temporary), str(destination))
            return
        except (urllib.error.URLError, SyncError, OSError) as exc:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            if isinstance(exc, SyncError) or attempt >= 2:
                raise SyncError(f"Falha no download de {name}: {exc}") from exc
            log(
                f"Falha no download de {name} (tentativa {attempt + 1}/3): {exc}",
                "warn",
            )
            time.sleep(1.5 * (attempt + 1))

    raise SyncError(f"Falha no download de {name}.")


ZB_JAR = "ZombieBuddy.jar"
ZB_DLL = "zbNative.dll"


def _is_windows() -> bool:
    return os.name == "nt"


def _client_is_windows(game_dir: Path) -> bool:
    """True se o cliente que roda a JVM e o build Windows (nativo ou via Proton).

    O SO do host nao decide isso: no Linux o jogo pode ser o build Windows
    executado pelo Proton (só existe ProjectZomboid64.exe na pasta).
    """
    if _is_windows():
        return True
    # Instalacao Linux rodando o build Windows no Proton: so existe o .exe
    return (game_dir / "ProjectZomboid64.exe").is_file() and not (
        game_dir / "ProjectZomboid64"
    ).is_file()


def _agent_arg(game_dir: Path) -> str:
    """Argumento da JVM que ativa o ZombieBuddy (ver doc/Installation.md do projeto).

    No build Windows o JRE do jogo nao consegue carregar instrument.dll sozinho;
    o zbNative.dll resolve isso e carrega o ZombieBuddy.jar como agente.
    """
    if _client_is_windows(game_dir):
        return "-agentlib:zbNative"
    return "-javaagent:ZombieBuddy.jar"


def _is_zb_arg(value: str) -> bool:
    low = str(value).strip().lower()
    return low.startswith("-agentlib:zbnative") or (
        low.startswith("-javaagent:") and "zombiebuddy" in low
    )


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _find_key(data: Dict[str, Any], name: str) -> Optional[str]:
    """Chave existente com o mesmo nome ignorando maiusculas (o jogo usa 'classpath')."""
    if name in data:
        return name
    for key in data:
        if key.lower() == name.lower():
            return key
    return None


def _read_game_json(json_path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"Nao foi possivel ler {json_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SyncError(f"Conteudo invalido em {json_path}: esperado um objeto JSON.")
    return data


def _write_game_json(json_path: Path, data: Dict[str, Any]) -> None:
    _atomic_write_text(json_path, json.dumps(data, indent="\t", ensure_ascii=False) + "\n")


def _clean_zb_legacy(data: Dict[str, Any]) -> bool:
    """Desfaz o metodo antigo (ZombieBuddy no classpath), que nunca ativava o agente.

    Versoes antigas deste script procuravam 'classPath', nao achavam a chave
    real 'classpath' e criavam uma segunda chave so com o ZombieBuddy.
    """
    changed = False
    keys = [key for key in list(data) if key.lower() == "classpath"]
    if not keys:
        return False
    main = "classpath" if "classpath" in data else keys[0]
    main_items = _as_str_list(data[main])
    for key in keys:
        if key == main:
            continue
        for item in _as_str_list(data[key]):
            if "zombiebuddy" not in item.lower() and item not in main_items:
                main_items.append(item)
        del data[key]
        changed = True
    filtered = [item for item in main_items if "zombiebuddy" not in item.replace("\\", "/").lower()]
    if filtered != _as_str_list(data[main]) or changed:
        data[main] = filtered
        changed = True
    return changed


def _activate_zombiebuddy(game_dir: Path, dry_run: bool) -> bool:
    """Configura o ProjectZomboid64.json para carregar o ZombieBuddy como agente."""
    json_path = game_dir / "ProjectZomboid64.json"
    if not json_path.is_file():
        log(f"ProjectZomboid64.json nao encontrado em {game_dir}; ativacao ignorada.", "warn")
        return False
    if not (game_dir / ZB_JAR).is_file():
        log(f"{ZB_JAR} nao esta na pasta do jogo; ativacao ignorada.", "warn")
        return False

    data = _read_game_json(json_path)
    changed = _clean_zb_legacy(data)
    ready = True
    if _client_is_windows(game_dir) and not (game_dir / ZB_DLL).is_file():
        log(
            f"{ZB_DLL} ausente: no build Windows ele e obrigatorio para ativar o ZombieBuddy. "
            "Peca ao administrador do servidor para republicar incluindo o zbNative.dll.",
            "error",
        )
        ready = False

    key = _find_key(data, "vmArgs") or "vmArgs"
    vm_args = _as_str_list(data.get(key))
    wanted = _agent_arg(game_dir)
    new_args = [arg for arg in vm_args if not _is_zb_arg(arg)]
    if ready:
        # Primeiro argumento: o agente precisa carregar antes do jogo.
        new_args.insert(0, wanted)
    if new_args != vm_args:
        data[key] = new_args
        changed = True

    if not changed:
        log(f"ZombieBuddy ja configurado ({wanted}).", "ok")
        return ready

    if dry_run:
        log(f"Simulacao: atualizaria {json_path.name} ({wanted if ready else 'limpeza do metodo antigo'}).")
        return ready

    backup = json_path.with_name(json_path.name + ".bak")
    if not backup.is_file():
        shutil.copy2(json_path, backup)
        log(f"Backup criado: {backup}")
    _write_game_json(json_path, data)
    if ready:
        log(f"ZombieBuddy ativado no {json_path.name} ({wanted}).", "ok")
    else:
        log(f"{json_path.name} limpo, mas o ZombieBuddy NAO foi ativado.", "warn")
    return ready


def _deactivate_zombiebuddy(game_dir: Path, dry_run: bool) -> None:
    json_path = game_dir / "ProjectZomboid64.json"
    if not json_path.is_file():
        return
    data = _read_game_json(json_path)
    changed = _clean_zb_legacy(data)
    key = _find_key(data, "vmArgs")
    if key:
        vm_args = _as_str_list(data.get(key))
        filtered = [arg for arg in vm_args if not _is_zb_arg(arg)]
        if filtered != vm_args:
            data[key] = filtered
            changed = True
    if not changed:
        log(f"{json_path.name} ja esta sem o ZombieBuddy.", "ok")
        return
    if dry_run:
        log(f"Simulacao: removeria o ZombieBuddy de {json_path.name}.")
        return
    _write_game_json(json_path, data)
    log(f"ZombieBuddy removido de {json_path.name}.", "ok")


def _state_path() -> Path:
    env = os.environ.get("PZ_SYNC_STATE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".pzsm" / "sync-state.json"


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log(f"Estado local ilegivel ({path}): {exc}", "warn")
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    _atomic_write_text(path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def _fallback_managed_files(game_dir: Path) -> List[Path]:
    return [
        game_dir / "java" / "ZombieBuddy.jar",
        game_dir / "zbNative.dll",
        game_dir / "ZombieBuddy.jar",
    ]


def _resolve_manifest_url(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value.strip()
    env = os.environ.get("PZ_MANIFEST_URL")
    if env:
        return env.strip()
    if "{{" not in DEFAULT_MANIFEST_URL:
        return DEFAULT_MANIFEST_URL.strip()
    return FALLBACK_MANIFEST_URL


def _sync(args: argparse.Namespace, manifest_url: str) -> int:
    game_dir = find_game_dir(args.game_dir)
    user_mods = find_user_mods(args.user_mods)
    log(f"Jogo: {game_dir}")
    log(f"Mods do usuario: {user_mods}")
    log(f"Manifesto: {manifest_url}")

    if not (args.check or args.dry_run):
        running = _check_game_running()
        if running:
            raise SyncError(
                f"O Project Zomboid esta em execucao ({running}). "
                "Feche o jogo antes de instalar ou atualizar."
            )

    manifest = _load_manifest(manifest_url)
    entries = list(_iter_manifest_files(manifest))
    if not entries:
        log("O manifesto nao contem arquivos para sincronizar.", "warn")
        return 0

    exit_problem = False
    targets: List[Path] = []
    seen_targets: Dict[str, str] = {}
    updates = 0
    unchanged = 0
    missing = 0
    zb_jar_target: Optional[Path] = None
    zb_seen = False

    for mod_id, entry in entries:
        name = str(entry.get("name") or "arquivo")
        raw_target = entry.get("target_path")
        if not raw_target:
            raise SyncError(f"{mod_id}/{name}: target_path ausente no manifesto.")

        target = _expand_target(str(raw_target), game_dir, user_mods)
        _validate_target(target, (game_dir, user_mods))
        if (
            _is_within(target, game_dir)
            and not _is_within(target, user_mods)
            and target.suffix.lower() not in GAME_DIR_ALLOWED_SUFFIXES
        ):
            raise SyncError(
                f"{mod_id}/{name}: o manifesto so pode gravar "
                f"{', '.join(GAME_DIR_ALLOWED_SUFFIXES)} na pasta do jogo: {target}"
            )
        sha = _valid_sha(entry.get("sha256"), name)

        target_key = os.path.normcase(os.path.realpath(str(target)))
        if target_key in seen_targets:
            if seen_targets[target_key] != sha:
                raise SyncError(f"Conflito de hashes para o mesmo destino: {target}")
            continue
        seen_targets[target_key] = sha
        targets.append(target)

        if name.lower() == ZB_JAR.lower() and _is_within(target, game_dir):
            zb_jar_target = target
            zb_seen = True

        if target.is_file():
            current_sha = _sha256_file(target)
            if current_sha == sha:
                log(f"[{mod_id}] {name}: ja esta atualizado.", "ok")
                unchanged += 1
                continue
            log(f"[{mod_id}] {name}: desatualizado.")
        else:
            log(f"[{mod_id}] {name}: ausente.")
            missing += 1

        if args.check or args.dry_run:
            updates += 1
            continue

        url = str(entry.get("url") or "").strip()
        if not url:
            raise SyncError(f"{mod_id}/{name}: URL de download ausente no manifesto.")

        log(f"[{mod_id}] Baixando {name}...")
        _download_file(url, target, sha, name)
        log(f"[{mod_id}] {name}: instalado/atualizado.", "ok")
        updates += 1

    if zb_seen and not args.check:
        root_jar = game_dir / ZB_JAR
        # Manifestos antigos publicavam o JAR em java/; o agente precisa dele
        # na raiz da pasta do jogo (ao lado do zbNative.dll).
        if zb_jar_target is not None and os.path.normcase(str(zb_jar_target)) != os.path.normcase(str(root_jar)):
            log(f"Manifesto antigo: copiando {ZB_JAR} para a raiz da pasta do jogo.", "warn")
            if not args.dry_run and zb_jar_target.is_file():
                _atomic_copy(zb_jar_target, root_jar)
                targets.append(root_jar)
        if not _activate_zombiebuddy(game_dir, args.dry_run) and not args.dry_run:
            exit_problem = True

    if not (args.check or args.dry_run):
        state = _load_state()
        files = set(str(item) for item in (state.get("files") or []))
        files.update(str(item) for item in targets)
        state.update(
            {
                "version": STATE_VERSION,
                "manifest_url": manifest_url,
                "game_dir": str(game_dir),
                "user_mods": str(user_mods),
                "backup": str(game_dir / "ProjectZomboid64.json.bak"),
                "files": sorted(files),
                "updated_at": time.time(),
            }
        )
        _save_state(state)
        log(f"Estado local salvo em {_state_path()}.", "ok")

    if args.check:
        if updates:
            log(f"{updates} arquivo(s) precisam ser atualizados.", "warn")
            return 1
        log("Tudo atualizado. Nenhum download necessario.", "ok")
        return 0

    if args.dry_run:
        log(f"Simulacao concluida: {updates} arquivo(s) seriam atualizados.", "ok")
        return 0

    log(
        f"Sincronizacao concluida: {updates} atualizado(s), "
        f"{unchanged} ja atualizado(s), {missing} ausente(s).",
        "ok",
    )
    if exit_problem:
        log("Arquivos sincronizados, mas o ZombieBuddy nao pode ser ativado (veja o erro acima).", "error")
        return 1
    log("Abra o Project Zomboid e entre no servidor.", "ok")
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    game_dir = find_game_dir(args.game_dir)
    user_mods = find_user_mods(args.user_mods)
    log(f"Jogo: {game_dir}")
    log(f"Mods do usuario: {user_mods}")

    if not args.dry_run:
        running = _check_game_running()
        if running:
            raise SyncError(
                f"O Project Zomboid esta em execucao ({running}). "
                "Feche o jogo antes de desinstalar."
            )

    state = _load_state()
    json_path = game_dir / "ProjectZomboid64.json"
    backup = json_path.with_name(json_path.name + ".bak")

    state_backup = state.get("backup")
    if state_backup:
        candidate = Path(str(state_backup))
        if _is_within(candidate, game_dir) and candidate.is_file():
            backup = candidate

    # Remocao cirurgica: restaurar o .bak inteiro desfaria atualizacoes do
    # jogo feitas depois da instalacao. O .bak continua disponivel.
    _deactivate_zombiebuddy(game_dir, args.dry_run)
    if backup.is_file():
        log(f"Backup original mantido em {backup}.")

    files = state.get("files")
    if not isinstance(files, list) or not files:
        log("Estado local nao encontrado; removendo arquivos padrao do ZombieBuddy.", "warn")
        files = [str(path) for path in _fallback_managed_files(game_dir)]

    removed = 0
    failed = 0
    for raw in files:
        path = Path(str(raw))
        if not (_is_within(path, game_dir) or _is_within(path, user_mods)):
            log(f"Ignorando caminho fora das pastas permitidas: {path}", "warn")
            failed += 1
            continue
        if path.is_file():
            log(f"Removendo {path}")
            if not args.dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    log(f"Falha ao remover {path}: {exc}", "error")
                    failed += 1
                    continue
            removed += 1
        elif path.exists():
            log(f"Ignorando diretorio: {path}", "warn")

    if not args.dry_run and failed == 0:
        state_path = _state_path()
        if state_path.is_file():
            try:
                state_path.unlink()
                log(f"Estado local removido: {state_path}", "ok")
            except OSError as exc:
                log(f"Nao foi possivel remover o estado local: {exc}", "warn")

    log(
        f"Desinstalacao concluida: {removed} arquivo(s) removido(s), "
        f"{failed} falha(s).",
        "ok" if failed == 0 else "warn",
    )
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Instala/atualiza os mods Java homologados do Project Zomboid.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python sync-client.py\n"
            "  python sync-client.py --check\n"
            "  python sync-client.py --dry-run --verbose\n"
            "  python sync-client.py --uninstall\n"
        ),
    )
    parser.add_argument("--game-dir", help="Pasta do Project Zomboid (contem ProjectZomboid64.json).")
    parser.add_argument("--user-mods", help="Pasta de mods do usuario (ex.: ~/Zomboid/mods).")
    parser.add_argument("--manifest-url", help="URL alternativa do manifest.json.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Somente verifica; nao baixa nem altera arquivos.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o que seria feito, sem baixar nem alterar arquivos.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove o ZombieBuddy do ProjectZomboid64.json e apaga os arquivos gerenciados.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Mostra informacoes adicionais de ambiente.",
    )
    parser.add_argument("--version", action="version", version="sync-client 1.0")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.uninstall and args.check:
        parser.error("--check e --uninstall nao podem ser usados juntos.")

    if args.verbose:
        log(f"Sistema: {platform.platform()}")
        log(f"Python: {sys.version.split()[0]}")
        log(f"Executavel: {sys.executable}")
        if os.name == "nt":
            log(f"APPDATA: {os.environ.get('APPDATA', '(nao definido)')}")

    try:
        if args.uninstall:
            return _uninstall(args)
        return _sync(args, _resolve_manifest_url(args.manifest_url))
    except KeyboardInterrupt:
        log("Cancelado pelo usuario.", "warn")
        return 130
    except SyncError as exc:
        log(str(exc), "error")
        return 1
    except Exception as exc:  # pragma: no cover - protecao para o usuario final
        if os.environ.get("PZ_SYNC_DEBUG"):
            raise
        log(f"Erro inesperado: {exc}", "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())