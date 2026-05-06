import binascii
import logging
import shutil
from pathlib import Path

import vdf

from steam_config_patcher.formats.binary_keyvalues import patch_binary_keyvalues
from steam_config_patcher.formats.keyvalues import patch_keyvalues
from steam_config_patcher.types import ArtworkConfig, ConfigPatch, PatcherConfig, UserConfig

logger = logging.getLogger(__name__)


def get_grid_id(exe: str, appname: str) -> int:
    """Compute the Steam grid artwork ID for a non-Steam shortcut.

    Steam uses crc32(exe + appname) | 0x80000000 as the ID prefix
    for artwork filenames in userdata/<userid>/config/grid/.
    This is distinct from the shortcut appid stored in shortcuts.vdf.
    """
    unique_id = exe + appname
    return binascii.crc32(unique_id.encode()) | 0x80000000


def patch_grid_artwork(
    steam_dir: Path,
    user_id: int,
    exe: str,
    appname: str,
    artwork: ArtworkConfig,
) -> None:
    """Copy artwork files into Steam's grid directory for a non-Steam shortcut."""
    grid_id = get_grid_id(exe, appname)
    grid_dir = steam_dir / "userdata" / str(user_id) / "config" / "grid"
    grid_dir.mkdir(parents=True, exist_ok=True)

    # mapping: artwork field -> (filename suffix, description)
    artwork_files: list[tuple[str | None, str, str]] = [
        (artwork.cover,  f"{grid_id}p",      "cover"),
        (artwork.banner, f"{grid_id}",        "banner"),
        (artwork.hero,   f"{grid_id}_hero",   "hero"),
        (artwork.logo,   f"{grid_id}_logo",   "logo"),
    ]

    for source_str, stem, kind in artwork_files:
        if source_str is None:
            continue

        source = Path(source_str)
        if not source.is_file():
            logger.warning("Artwork %s not found: %s", kind, source)
            continue

        dest = grid_dir / (stem + source.suffix)

        # skip if already identical (same resolved path, e.g. nix store symlinks)
        if dest.exists() and dest.resolve() == source.resolve():
            continue

        try:
            shutil.copy2(source, dest)
            logger.info("Installed %s artwork: %s -> %s", kind, source, dest)
        except OSError as e:
            logger.error("Failed to install %s artwork: %s", kind, e)


def generate_config_vdf_patch(cfg: PatcherConfig) -> ConfigPatch:
    return ConfigPatch(
        file_path=cfg.steam_dir.joinpath("config", "config.vdf"),
        file_format="keyvalues",
        data={
            "InstallConfigStore": {
                "Software": {
                    "Valve": {
                        "Steam": {
                            "CompatToolMapping": {
                                str(app_id): {
                                    "config": "",
                                    "name": compat_tool.name,
                                    "priority": str(compat_tool.priority),
                                }
                                for app_id, compat_tool in cfg.compat_tool_mapping.items()
                            }
                        }
                    }
                }
            }
        },
        close_steam=cfg.close_steam,
    )


def generate_localconfig_vdf_patch(
    cfg: PatcherConfig, user_id: int, user_config: UserConfig
) -> ConfigPatch:
    return ConfigPatch(
        file_path=cfg.steam_dir.joinpath(
            "userdata", str(user_id), "config", "localconfig.vdf"
        ),
        file_format="keyvalues",
        data={
            "UserLocalConfigStore": {
                "Software": {
                    "Valve": {
                        "Steam": {
                            "Apps": {
                                str(app_id): {"LaunchOptions": launch_options}
                                for app_id, launch_options in user_config.launch_options.items()
                            }
                        }
                    }
                }
            }
        },
        close_steam=cfg.close_steam,
    )


def generate_shortcuts_vdf_patch(
    cfg: PatcherConfig, user_id: int, user_config: UserConfig
) -> ConfigPatch:
    file_path = cfg.steam_dir.joinpath(
        "userdata", str(user_id), "config", "shortcuts.vdf"
    )

    # hacky way to skip patching, non existant file_path will be skipped in patching stage
    if not file_path.is_file():
        return ConfigPatch(
            file_path=file_path,
            file_format="binary-keyvalues",
            data={},
            close_steam=cfg.close_steam,
        )

    with file_path.open(mode="rb") as read_file:
        kv = vdf.binary_load(read_file)

    # hacky way to see which index we should use based on app id and existing shortcuts
    shortcuts = kv.get("shortcuts") or {}
    index_mapping: dict[int, int] = {}
    max_index = max([int(k) for k in shortcuts.keys()])
    index_offset = 1

    for app_id in user_config.non_steam_apps.keys():
        # set index by matching app id
        for shortcut_index, shortcut in shortcuts.items():
            if shortcut.get("appid") == app_id:
                index_mapping[app_id] = shortcut_index
                break

        # if no matching app id, use next available index
        if app_id not in index_mapping:
            index_mapping[app_id] = max_index + index_offset
            index_offset += 1

    return ConfigPatch(
        file_path=file_path,
        file_format="binary-keyvalues",
        data={
            "shortcuts": {
                str(index_mapping[app_id]): {
                    "appid": app_id,
                    "AppName": app.name,
                    "Exe": app.target,
                    "StartDir": app.start_in,
                    "icon": app.icon,
                    "LaunchOptions": app.launch_options,
                    "IsHidden": 1 if app.is_hidden else 0,
                    "AllowDesktopConfig": 1 if app.allow_desktop_config else 0,
                    "OpenVR": 1 if app.in_vr_library else 0,
                    "tags": {},
                }
                for app_id, app in user_config.non_steam_apps.items()
            }
        },
        close_steam=cfg.close_steam,
    )


def patch_config_files(cfg: PatcherConfig):
    config_patches = [
        generate_config_vdf_patch(cfg),
        *[
            generate_localconfig_vdf_patch(cfg, user_id, user)
            for user_id, user in cfg.users.items()
        ],
        *[
            generate_shortcuts_vdf_patch(cfg, user_id, user)
            for user_id, user in cfg.users.items()
        ],
    ]

    for config_patch in config_patches:
        match config_patch.file_format:
            case "keyvalues":
                patch_keyvalues(config_patch)
            case "binary-keyvalues":
                patch_binary_keyvalues(config_patch)

    # copy grid artwork for non-steam apps (runs after shortcuts are patched)
    for user_id, user in cfg.users.items():
        for app in user.non_steam_apps.values():
            patch_grid_artwork(
                steam_dir=cfg.steam_dir,
                user_id=user_id,
                exe=app.target,
                appname=app.name,
                artwork=app.artwork,
            )

