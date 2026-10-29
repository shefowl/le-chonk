"""Path and platform resolution, in one place.

Where the game is installed varies per machine. Every module used to carry a
hardcoded path, which meant the project did not run on anyone else's computer.
Resolution order:

  1. the HOI4_PATH environment variable (explicit, for CI and portable installs)
  2. the well-known Steam locations for this operating system
  3. extra Steam library drives listed in libraryfolders.vdf

Nothing above this module needs to know which operating system it is on.
"""
import os
import platform
import re

WINDOWS = platform.system() == "Windows"
EXE_NAME = "hoi4.exe" if WINDOWS else "hoi4"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Caches and reverse-engineering dumps
CACHE_DIR = os.environ.get("CLAUSEWITZ_CACHE", HERE)
DUMP_DIR = os.environ.get("CLAUSEWITZ_DUMPS", os.path.join(ROOT, "research", "re-dumps"))

_STEAM_DIRS = [
    # Windows
    r"C:\Program Files (x86)\Steam\steamapps\common\Hearts of Iron IV",
    r"C:\Program Files\Steam\steamapps\common\Hearts of Iron IV",
    r"D:\SteamLibrary\steamapps\common\Hearts of Iron IV",
    # Linux
    os.path.expanduser("~/.steam/steam/steamapps/common/Hearts of Iron IV"),
    os.path.expanduser("~/.local/share/Steam/steamapps/common/Hearts of Iron IV"),
    os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.local/share/Steam/"
                       "steamapps/common/Hearts of Iron IV"),
    # WSL / mounted Windows drive
    "/mnt/c/Program Files (x86)/Steam/steamapps/common/Hearts of Iron IV",
    "/mnt/C/Program Files (x86)/Steam/steamapps/common/Hearts of Iron IV",
]


def _extra_steam_libraries():
    """Extra Steam library drives (libraryfolders.vdf)."""
    roots = [
        os.path.expanduser("~/.steam/steam/steamapps/libraryfolders.vdf"),
        os.path.expanduser("~/.local/share/Steam/steamapps/libraryfolders.vdf"),
        r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf",
    ]
    out = []
    for vdf in roots:
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    out.append(os.path.join(m.group(1).replace("\\\\", os.sep),
                                            "steamapps", "common", "Hearts of Iron IV"))
        except OSError:
            continue
    return out


def find_game_dir():
    """The directory the game is installed in, or None."""
    env = os.environ.get("HOI4_PATH")
    if env and os.path.isdir(env):
        return env
    for d in _STEAM_DIRS + _extra_steam_libraries():
        if os.path.isdir(d):
            return d
    return None


def game_dir(required=True):
    d = find_game_dir()
    if d is None and required:
        raise RuntimeError(
            "Could not find a Hearts of Iron IV installation. Point HOI4_PATH at "
            "the game directory, e.g.\n"
            "  export HOI4_PATH=\"$HOME/.steam/steam/steamapps/common/Hearts of Iron IV\"")
    return d


def binary_path(required=True):
    """The game executable, used for static analysis."""
    d = game_dir(required)
    if d is None:
        return None
    for name in (EXE_NAME, "hoi4", "hoi4.exe"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    if required:
        raise RuntimeError("found the game directory but no executable in it: %s" % d)
    return None


def cache(name):
    return os.path.join(CACHE_DIR, name)


def disk_build_id(path=None):
    """GNU build-id of the hoi4 binary on disk (its NT_GNU_BUILD_ID note), or None."""
    path = path or binary_path(required=False)
    try:
        with open(path, "rb") as f:
            head = f.read(1 << 16)
    except (OSError, TypeError):
        return None
    tag = b"\x04\x00\x00\x00\x14\x00\x00\x00\x03\x00\x00\x00GNU\x00"
    i = head.find(tag)
    return head[i + len(tag):i + len(tag) + 20].hex() if i >= 0 else None


def dump(name):
    """A research cache path. Caches are per build (27.10: a game update left the 1.19.3
    function and RTTI tables in place for 1.19.4), so they live under the build-id."""
    bid = disk_build_id()
    d = os.path.join(DUMP_DIR, bid[:12]) if bid else DUMP_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)
