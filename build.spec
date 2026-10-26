# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import sys
import platform
import fnmatch
from pathlib import Path
from collections import abc
from traceback import format_exc
from typing import Any, TypeAlias, TYPE_CHECKING

SELF_PATH = str(Path(".").resolve())
if SELF_PATH not in sys.path:
    sys.path.insert(0, SELF_PATH)

from constants import WORKING_DIR, DEFAULT_LANG

if TYPE_CHECKING:
    from PyInstaller.building.splash import Splash
    from PyInstaller.building.build_main import Analysis
    from PyInstaller.building.datastruct import _TOCTuple
    from PyInstaller.building.api import PYZ, EXE, COLLECT, BUNDLE


PYZTypeCOLLECT: TypeAlias = "abc.Iterable[_TOCTuple] | PYZ"
PYZTypeEXE: TypeAlias = "abc.Iterable[_TOCTuple] | PYZ | Splash"


# Simple configuration
upx: bool = False  # Use UPX compression (reduces file size, may increase AV detections)
console: bool = False  # True if you'd want to add a console window (useful for debugging)
one_dir: bool = False  # True for one-dir, False for one-file
optimize: int | None = None  # -1/None/0=none, 1=remove asserts, 2=also remove docstrings
app_name: str = "Twitch Drops Miner (by NobodySan97)"


# (source_path, dest_path, required)
to_add: list[tuple[Path, str, bool]] = [
    # icon files
    (Path("icons/pickaxe.ico"), "icons", True),
    (Path("icons/active.ico"), "icons", True),
    (Path("icons/idle.ico"), "icons", True),
    (Path("icons/error.ico"), "icons", True),
    (Path("icons/maint.ico"), "icons", True),
]
for lang_filepath in WORKING_DIR.joinpath("lang").glob("*.json"):
    if lang_filepath.stem != DEFAULT_LANG:
        to_add.append((lang_filepath, "lang", True))

# Ensure the required to-be-added data exists
datas: list[tuple[Path, str]] = []
for source_path, dest_path, required in to_add:
    if source_path.exists():
        datas.append((source_path, dest_path))
    elif required:
        raise FileNotFoundError(str(source_path))

hooksconfig: dict[str, Any] = {}
binaries: list[tuple[Path, str]] = []
hiddenimports: list[str] = [
    "PIL._tkinter_finder",
]

if sys.platform == "linux":
    # Needed files for better system tray support on Linux via pystray (AppIndicator backend).
    arch: str = platform.machine()
    candidate_library_paths: list[Path] = [
        Path(f"/usr/lib/{arch}-linux-gnu"),  # Debian/Ubuntu multiarch
        Path("/usr/lib64"),  # Fedora/RHEL
        Path("/usr/lib"),  # Arch and other single-dir distros
    ]
    for libraries_path in candidate_library_paths:
        if (libraries_path / "libayatana-appindicator3.so.1").exists():
            break
    datas.append(
        (libraries_path / "girepository-1.0/AyatanaAppIndicator3-0.1.typelib", "gi_typelibs")
    )
    binaries.append((libraries_path / "libayatana-appindicator3.so.1", "."))

    hiddenimports.extend([
        "gi.repository.Gtk",
        "gi.repository.GObject",
    ])
    hooksconfig = {
        "gi": {
            "icons": [],
            "themes": [],
            "languages": ["en_US"]
        }
    }

a = Analysis(
    ["main.py"],
    datas=datas,
    binaries=binaries,
    hooksconfig=hooksconfig,
    hiddenimports=hiddenimports,
)

# Exclude unneeded Linux libraries (supports globbing)
excluded_binaries = [
    "libicudata.so.*",
    "libicuuc.so.*",
    "librsvg-*.so.*"
]
a.binaries = [
    b for b in a.binaries
    if not any(fnmatch.fnmatch(b[0], pattern) for pattern in excluded_binaries)
]
if one_dir:
    exe_args: PYZTypeEXE = tuple()
    collect_args: PYZTypeCOLLECT = (a.datas, a.binaries)
else:
    exe_args = (a.datas, a.binaries)
    collect_args = tuple()

version_info = None
win_manifest = None

if sys.platform == "win32":
    import re
    from version import __version__
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct
    )

    _version_digits = [int(d) for d in re.findall(r"\d+", __version__)]
    while len(_version_digits) < 4:
        _version_digits.append(0)
    _file_version = tuple(_version_digits[:4])

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_file_version,
            prodvers=_file_version,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "NobodySan97"),
                        StringStruct("FileDescription", "Twitch Drops Miner"),
                        StringStruct("FileVersion", str(__version__)),
                        StringStruct("InternalName", "Twitch Drops Miner"),
                        StringStruct("LegalCopyright", "Copyright (C) NobodySan97 (fork of DevilXD)"),
                        StringStruct("OriginalFilename", "Twitch Drops Miner (by NobodySan97).exe"),
                        StringStruct("ProductName", "Twitch Drops Miner"),
                        StringStruct("ProductVersion", str(__version__)),
                    ],
                )
            ]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

    win_manifest = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity
      version="16.0.0.0"
      processorArchitecture="*"
      name="NobodySan97.TwitchDropsMiner"
      type="win32"
  />
  <description>Twitch Drops Miner</description>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <!-- Windows 10 and Windows 11 -->
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
      <!-- Windows 8.1 -->
      <supportedOS Id="{1f676c76-80e1-4239-95bb-83d0f6d0da78}"/>
      <!-- Windows 8 -->
      <supportedOS Id="{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}"/>
      <!-- Windows 7 -->
      <supportedOS Id="{35138b9a-5d96-4fbd-8e2d-a2440225f93a}"/>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2,PerMonitor</dpiAwareness>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
  <dependency>
    <dependentAssembly>
      <assemblyIdentity
          type="win32"
          name="Microsoft.Windows.Common-Controls"
          version="6.0.0.0"
          processorArchitecture="*"
          publicKeyToken="6595b64144ccf1df"
          language="*"
      />
    </dependentAssembly>
  </dependency>
</assembly>
"""

pyz = PYZ(a.pure)
try:
    exe = EXE(
        pyz,
        a.scripts,
        *exe_args,
        upx=upx,
        debug=False,
        name=app_name,
        console=console,
        optimize=optimize,
        exclude_binaries=one_dir,
        icon="icons/pickaxe.ico",
        version=version_info,
        manifest=win_manifest,
        uac_admin=False,
        uac_uiaccess=False,
    )
except PermissionError as exc:
    exc_text: str = format_exc()
    if any(t in exc_text for t in ("os.remove", "os.unlink")):
        raise PermissionError("Ensure the executable isn't running when rebuilding.") from exc
    raise
if one_dir:
    coll = COLLECT(
        exe,
        *collect_args,
        upx=upx,
        name=app_name,
    )

# macOS bundle support
if sys.platform == "darwin":
    source = coll if one_dir else exe
    app = BUNDLE(
        source,
        name=f'{app_name}.app',
        icon="icons/pickaxe.ico",
        bundle_identifier='com.twitchdrops.miner',
    )
