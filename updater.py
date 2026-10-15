import os
import re
import sys
import json
import time
import shutil
import zipfile
import tempfile
import asyncio
import subprocess
import platform
import urllib.request
from tkinter import messagebox

GITHUB_REPO = "NobodySan97/TwitchDropsMiner"
RELEASE_TAG = "dev-build"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"


def _is_hex_hash(s: str | None) -> bool:
    return bool(s and re.fullmatch(r"[0-9a-fA-F]{7,40}", s))


def clean_env() -> dict[str, str]:
    """
    Environment for a process that outlives us: none of this onefile exe's PyInstaller state.

    A child started with _PYI_* / _MEIPASS2 set thinks it is part of *this* exe and loads
    python3xx.dll from our _MEI… temp folder — which is deleted the moment we exit
    ("Failed to load Python DLL" after an update).
    """
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(("_PYI_", "_MEIPASS", "TCL_LIBRARY", "TK_LIBRARY"))
    }
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        parts = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(
            p for p in parts if p and not p.lower().startswith(str(mei).lower())
        )
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # PyInstaller >= 6.9: always start as a fresh app
    return env


def apply_windows_update(
    new_exe: str | os.PathLike,
    current_exe: str | os.PathLike,
    args: list[str] | tuple[str, ...] = (),
    stage_dir: str | os.PathLike | None = None,
) -> None:
    """
    Windows in-place executable replacement.
    Renames the current running executable to a .old timestamped file (allowed by NTFS),
    copies new_exe in place, cleans staging, launches new_exe, and terminates this process.
    Leftover .old files are cleaned on next startup by cleanup_old_executables().
    """
    current_exe_str = str(current_exe)
    new_exe_str = str(new_exe)
    old_exe = current_exe_str + f".old.{int(time.time())}"

    # Rename current running executable (NTFS allows renaming open files)
    os.rename(current_exe_str, old_exe)
    try:
        shutil.copy2(new_exe_str, current_exe_str)
    except Exception as copy_err:
        if os.path.exists(old_exe) and not os.path.exists(current_exe_str):
            os.rename(old_exe, current_exe_str)
        raise copy_err

    if stage_dir and os.path.exists(stage_dir):
        shutil.rmtree(stage_dir, ignore_errors=True)

    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([current_exe_str] + list(args), env=clean_env(), creationflags=flags)
    os._exit(0)



def apply_linux_update(
    new_exe: str | os.PathLike,
    current_exe: str | os.PathLike,
    args: list[str] | tuple[str, ...] = (),
    stage_dir: str | os.PathLike | None = None,
) -> None:
    """
    Linux in-place executable replacement with clean environment.
    """
    current_exe_str = str(current_exe)
    new_exe_str = str(new_exe)
    old_exe = current_exe_str + f".old.{int(time.time())}"
    os.rename(current_exe_str, old_exe)
    try:
        shutil.copy2(new_exe_str, current_exe_str)
        os.chmod(current_exe_str, 0o755)
    except Exception as copy_err:
        if os.path.exists(old_exe) and not os.path.exists(current_exe_str):
            os.rename(old_exe, current_exe_str)
        raise copy_err

    if stage_dir and os.path.exists(stage_dir):
        shutil.rmtree(stage_dir, ignore_errors=True)

    os.execve(current_exe_str, [current_exe_str] + list(args), clean_env())


def cleanup_old_executables():
    """
    Cleans up any leftover .old.* files and temp update scripts from previous update cycles.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        exe_dir = os.path.dirname(sys.executable)
        exe_name = os.path.basename(sys.executable)
        for item in os.listdir(exe_dir):
            if item.startswith(f"{exe_name}.old.") or item.startswith(f"{exe_name}.old"):
                full_path = os.path.join(exe_dir, item)
                try:
                    os.remove(full_path)
                    print(f"Updater: Cleaned up old executable: {item}")
                except Exception:
                    pass
    except Exception:
        pass

    try:
        tmp_dir = tempfile.gettempdir()
        for item in os.listdir(tmp_dir):
            if item.startswith("tdm_stage_") or item.startswith("tdm-update-") or item.startswith("tdm_update_"):
                p = os.path.join(tmp_dir, item)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


_pending_update: dict | None = None
_declined_sha: str | None = None
_notified_sha: str | None = None
_update_in_progress: bool = False
def is_update_staged(sha: str | None = None) -> bool:
    return False


def get_staged_update() -> dict | None:
    return None


def decline_update(sha: str):
    global _declined_sha, _pending_update
    _declined_sha = sha
    _pending_update = None


async def check_for_updates(gui):
    global _notified_sha, _pending_update
    if _update_in_progress:
        return

    # Clean up any leftover .old binaries from previous update now that old process is dead
    cleanup_old_executables()

    force_test = bool(os.environ.get("TDM_FORCE_UPDATE") or "--test-update" in sys.argv)

    try:
        req = urllib.request.Request(API_URL, headers={"User-Agent": "TwitchDropsMiner-Updater"})
        loop = asyncio.get_running_loop()

        def _fetch_release() -> bytes:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()

        raw_payload = await loop.run_in_executor(None, _fetch_release)
        data = json.loads(raw_payload.decode())

        assets = data.get("assets", [])
        body = data.get("body") or ""

        # Extract SHA from body: "- Reference commit: <SHA>"
        remote_sha = None
        for line in body.splitlines():
            if line.startswith("- Reference commit:"):
                raw_sha = line.split(":", 1)[-1].strip().strip("`").strip()
                if len(raw_sha) >= 7:
                    remote_sha = raw_sha[:7].lower()
                break

        # If user previously declined this specific build during this session, skip (unless force_test)
        if not force_test and remote_sha and remote_sha == _declined_sha:
            return

        # Compare with local version (e.g. "16.dev.abcdef1")
        from version import __version__

        local_sha = None
        parts = __version__.split(".")
        if len(parts) >= 3:
            candidate = parts[-1].strip('"').strip("'")
            if _is_hex_hash(candidate):
                local_sha = candidate[:7].lower()

        # In development / git environment, try detecting current commit if __version__ is unpatched
        if local_sha is None and not getattr(sys, "frozen", False):
            try:
                git_proc = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                )
                if git_proc.returncode == 0:
                    cand = git_proc.stdout.strip().lower()
                    if _is_hex_hash(cand):
                        local_sha = cand[:7]
            except Exception:
                pass

        # If both are valid SHAs and match, already up to date
        if (
            not force_test
            and remote_sha
            and local_sha
            and _is_hex_hash(remote_sha)
            and _is_hex_hash(local_sha)
            and remote_sha == local_sha
        ):
            print(f"Updater: Already up to date (SHA: {local_sha}).")
            return

        if not assets:
            return

        system = platform.system().lower()
        if system == "darwin":
            # macOS .app bundle in-place replacement is not supported via simple executable swap
            return

        display_sha = remote_sha[:7] if (remote_sha and _is_hex_hash(remote_sha)) else "latest"
        _pending_update = {"sha": remote_sha or "latest", "assets": assets}
        gui.pending_update = _pending_update

        # If window is visible, display the non-intrusive in-app banner immediately
        if not gui.is_hidden:
            gui.set_pending_update(remote_sha or "latest", assets)


        # Deliver Windows / desktop notification (rate-limited to once per release unless force_test)
        if force_test or _notified_sha != remote_sha:
            _notified_sha = remote_sha
            msg = f"A new build is available ({display_sha}). Check window to update."
            try:
                from translate import _
                msg = _("gui", "tray", "update_available").format(sha=display_sha)
            except Exception:
                pass
            gui.tray.notify(
                msg,
                "Twitch Drops Miner",
                duration=10.0,
            )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Updater: Failed to check for updates: {e}")


async def run_update_loop(gui, interval_seconds: int = 3600):
    """
    Runs an update check immediately on startup, and then periodically
    every `interval_seconds` (default: 1 hour) while the application is active.
    """
    # Initial check on startup
    await check_for_updates(gui)

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await check_for_updates(gui)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"Updater: Error in periodic update loop: {exc}")


async def perform_update(gui, assets):
    global _update_in_progress
    if _update_in_progress:
        return
    _update_in_progress = True

    if not getattr(sys, "frozen", False):
        _update_in_progress = False
        messagebox.showinfo(
            "Twitch Drops Miner",
            "Automatic updating is only available for compiled executables (.exe).\n"
            "To update in development mode, please run 'git pull' or download the latest release from GitHub.",
            parent=gui._root,
        )
        return

    system = platform.system().lower()
    machine = platform.machine().lower()
    current_exe = sys.executable



    if system == "windows":
        asset_name = "Twitch.Drops.Miner.Windows.zip"
    elif system == "darwin":
        _update_in_progress = False
        messagebox.showinfo("Update", "macOS auto-update is not supported. Please download manually.")
        return
    else:
        arch = "aarch64" if ("aarch64" in machine or "arm64" in machine) else "x86_64"
        asset_name = f"Twitch.Drops.Miner.Linux.PyInstaller-{arch}.zip"

    asset_to_download = None
    for asset in assets:
        if asset.get("name") == asset_name:
            asset_to_download = asset.get("browser_download_url")
            break

    if not asset_to_download:
        _update_in_progress = False
        messagebox.showerror("Update Failed", f"Could not find an update package for your system ({system} {machine}).")
        return

    import tkinter as tk
    from tkinter import ttk
    import aiohttp

    prog_win = tk.Toplevel(gui._root)
    prog_win.title("Downloading Update")
    prog_win.geometry("320x130")
    prog_win.resizable(False, False)
    rx = gui._root.winfo_x()
    ry = gui._root.winfo_y()
    if rx < 0 or ry < 0:
        sw = prog_win.winfo_screenwidth()
        sh = prog_win.winfo_screenheight()
        prog_win.geometry("320x130+%d+%d" % (max(0, (sw - 320) // 2), max(0, (sh - 130) // 2)))
    else:
        prog_win.geometry("320x130+%d+%d" % (rx + 50, ry + 50))
    prog_win.transient(gui._root)
    prog_win.grab_set()

    lbl = ttk.Label(prog_win, text="Downloading update, please wait...")
    lbl.pack(pady=10)
    prog_bar = ttk.Progressbar(prog_win, orient="horizontal", length=260, mode="determinate")
    prog_bar.pack(pady=5)
    pct_lbl = ttk.Label(prog_win, text="0%")
    pct_lbl.pack()

    temp_dir = tempfile.mkdtemp(prefix="tdm_update_")
    zip_path = os.path.join(temp_dir, "update.zip")
    should_destroy_prog_win = True

    try:
        print(f"Updater: Downloading from {asset_to_download}...")
        async with aiohttp.ClientSession() as session:
            async with session.get(asset_to_download) as response:
                if response.status != 200:
                    raise RuntimeError(f"Server returned HTTP status {response.status}")
                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0
                with open(zip_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(16384):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            pct = (downloaded / total_size) * 100
                            prog_bar["value"] = pct
                            pct_lbl.config(text=f"{int(pct)}%")

        lbl.config(text="Extracting update...")
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()

        # Extract archive and preserve POSIX permissions
        def _extract_zip() -> None:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                for info in zip_ref.infolist():
                    extracted_path = zip_ref.extract(info, temp_dir)
                    mode = info.external_attr >> 16
                    if mode:
                        os.chmod(extracted_path, mode)

        await loop.run_in_executor(None, _extract_zip)

        # Find executable
        new_exe = None
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                full_path = os.path.join(root, file)
                if system == "windows" and file.lower().endswith(".exe"):
                    new_exe = full_path
                    break
                elif system == "linux" and not file.endswith((".txt", ".json", ".zip", ".md", ".png", ".ico")):
                    if "AppImage" not in file:
                        new_exe = full_path
                        os.chmod(new_exe, 0o755)
                        break
            if new_exe:
                break

        if not new_exe:
            messagebox.showerror("Update Failed", "Could not locate the new executable in the downloaded update.")
            return

        should_destroy_prog_win = False
        try:
            prog_win.destroy()
        except Exception:
            pass

        if system == "windows":
            apply_windows_update(new_exe, current_exe, sys.argv[1:], stage_dir=temp_dir)
            return
        else:
            apply_linux_update(new_exe, current_exe, sys.argv[1:], stage_dir=temp_dir)
            return

    except asyncio.CancelledError:
        pass
    except Exception as e:
        messagebox.showerror("Update Error", f"An error occurred while updating:\n{e}")
    finally:
        _update_in_progress = False
        if should_destroy_prog_win:
            try:
                prog_win.destroy()
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)
