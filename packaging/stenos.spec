# PyInstaller specification building a single file stenos executable.
#
# Run from the repository root:
#     pyinstaller packaging/stenos.spec --noconfirm
#
# The executable bundles libopus, without which voice receive cannot decode
# audio, and the faster-whisper backend, without which a recording cannot be
# transcribed. Model weights are not bundled; they are downloaded on first use
# and cached, so the executable stays the same size for every model.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
ENTRY = ROOT / "src" / "stenos" / "__main__.py"

#: Where each platform keeps libopus. py-cord bundles a binary for Windows
#: only, so the other platforms take it from the system at build time.
OPUS_SEARCH = {
    "darwin": [
        "/opt/homebrew/lib/libopus.0.dylib",
        "/opt/homebrew/lib/libopus.dylib",
        "/usr/local/lib/libopus.0.dylib",
        "/usr/local/lib/libopus.dylib",
    ],
    "linux": [
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib64/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/lib/x86_64-linux-gnu/libopus.so.0",
    ],
}


def opus_binaries():
    """Locate libopus so it can be placed at the root of the bundle."""
    if sys.platform.startswith("win"):
        # py-cord ships the Windows binary; collect it under discord/bin so the
        # library's own loader finds it unchanged.
        import discord

        bindir = Path(discord.__file__).parent / "bin"
        return [(str(path), "discord/bin") for path in bindir.glob("*.dll")]

    key = "darwin" if sys.platform == "darwin" else "linux"
    for candidate in OPUS_SEARCH[key]:
        if Path(candidate).exists():
            return [(candidate, ".")]

    raise SystemExit(
        "libopus was not found, so the executable would be unable to decode "
        "received audio. Install it with brew install opus on macOS or "
        "libopus0 on Linux, then build again."
    )


binaries = opus_binaries()
binaries += collect_dynamic_libs("davey")
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("onnxruntime")
binaries += collect_dynamic_libs("av")

hiddenimports = ["davey"]
hiddenimports += collect_submodules("faster_whisper")
hiddenimports += collect_submodules("ctranslate2")

analysis = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "pytest",
        "mypy",
        "ruff",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="stenos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
