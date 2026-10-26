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


def cleanup_old_executables():
    """
    Cleans up any leftover .old.* files from previous update cycles.
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


_pending_update: dict | None = None
_declined_sha: str | None = None
_notified_sha: str | None = None
_update_in_progress: bool = False


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
            title = "Update Available"
            try:
                from translate import _
                msg = _("gui", "tray", "update_available").format(sha=display_sha)
                title = _("gui", "tray", "update_title")
            except Exception:
                pass
            gui.tray.notify(
                msg,
                title,
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
    from utils import resource_path, set_root_icon
    from translate import _

    prog_win = tk.Toplevel(gui._root)
    set_root_icon(prog_win, resource_path("icons/pickaxe.ico"))
    prog_win.title(_("gui", "tray", "update_title"))
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
    prog_win.protocol("WM_DELETE_WINDOW", lambda: None)

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
        download_success = False
        try:
            session_kwargs = {"max_line_size": 131072, "max_field_size": 131072}
            try:
                session = aiohttp.ClientSession(**session_kwargs)
            except TypeError:
                session = aiohttp.ClientSession()
            async with session:
                async with session.get(asset_to_download) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Server returned HTTP status {response.status}")
                    total_size = int(response.headers.get("content-length", 0))
                    downloaded = 0
                    last_pct = -1
                    with open(zip_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(32768):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                pct = int((downloaded / total_size) * 100)
                                if pct != last_pct:
                                    last_pct = pct
                                    prog_bar["value"] = pct
                                    pct_lbl.config(text=f"{pct}%")
            download_success = True
        except Exception as aio_err:
            print(f"Updater: aiohttp download failed ({aio_err}), falling back to urllib...")

        # Robust fallback using standard urllib.request if aiohttp fails
        if not download_success:
            def _urllib_download():
                req = urllib.request.Request(
                    asset_to_download,
                    headers={"User-Agent": "TwitchDropsMiner-Updater"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    last_pct = -1
                    with open(zip_path, "wb") as f:
                        while True:
                            chunk = resp.read(32768)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                pct = int((downloaded / total_size) * 100)
                                if pct != last_pct:
                                    last_pct = pct
                                    gui._root.after(0, lambda p=pct: _update_prog(p))

            def _update_prog(p):
                prog_bar["value"] = p
                pct_lbl.config(text=f"{p}%")

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _urllib_download)

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

        current_exe = sys.executable

        if system == "windows":
            old_exe = current_exe + f".old.{int(time.time())}"
            # Rename current running exe
            os.rename(current_exe, old_exe)
            try:
                shutil.copy2(new_exe, current_exe)
            except Exception as copy_err:
                # Rollback if copy fails
                if os.path.exists(current_exe):
                    try:
                        os.remove(current_exe)
                    except Exception:
                        pass
                if os.path.exists(old_exe) and not os.path.exists(current_exe):
                    os.rename(old_exe, current_exe)
                raise copy_err

            # PyInstaller clean environment
            env = os.environ.copy()
            env.pop("_MEIPASS2", None)
            env.pop("_MEIPASS", None)
            for k in list(env.keys()):
                if k.startswith("_PYI_"):
                    env.pop(k)

            # Cleanup temp_dir before spawning new process
            shutil.rmtree(temp_dir, ignore_errors=True)

            # Spawn detached process with original command line arguments
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([current_exe] + sys.argv[1:], env=env, creationflags=flags)

            # Cleanly stop system tray icon before exit to avoid ghost tray icon
            if hasattr(gui, "tray"):
                try:
                    gui.tray.stop()
                except Exception:
                    pass

            # Immediately terminate parent process to release lock file
            os._exit(0)

        else:
            # Linux: rename old, copy new, chmod, and execv
            old_exe = current_exe + f".old.{int(time.time())}"
            os.rename(current_exe, old_exe)
            try:
                shutil.copy2(new_exe, current_exe)
                os.chmod(current_exe, 0o755)
            except Exception as copy_err:
                if os.path.exists(old_exe) and not os.path.exists(current_exe):
                    os.rename(old_exe, current_exe)
                raise copy_err

            shutil.rmtree(temp_dir, ignore_errors=True)
            # In-place process replacement on Linux
            os.execv(current_exe, [current_exe] + sys.argv[1:])

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
