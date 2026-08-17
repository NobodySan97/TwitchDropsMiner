import os
import sys
import json
import urllib.request
import zipfile
import shutil
import tempfile
import asyncio
import subprocess
from tkinter import messagebox
import platform

GITHUB_REPO = "NobodySan97/TwitchDropsMiner"  # Change this to your fork
RELEASE_TAG = "dev-build"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"

async def check_for_updates(gui):
    # Only run in compiled PyInstaller mode (or for testing)
    if not getattr(sys, 'frozen', False):
        print("Updater: Not running in compiled mode, skipping update check.")
        return

    try:
        req = urllib.request.Request(API_URL, headers={'User-Agent': 'TwitchDropsMiner-Updater'})
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, urllib.request.urlopen, req)
        data = json.loads(response.read().decode())

        published_at = data.get("published_at")
        assets = data.get("assets", [])
        body = data.get("body", "")
        
        # Extract SHA from body: "- Reference commit: <SHA>"
        remote_sha = None
        for line in body.splitlines():
            if line.startswith("- Reference commit:"):
                remote_sha = line.split(":")[-1].strip()
                # we only need the short sha (7 chars) which the version has
                if len(remote_sha) >= 7:
                    remote_sha = remote_sha[:7]
                break
                
        # Compare with local version
        from version import __version__
        # __version__ looks like "1.2.3.abcdef0" or "1.2.3"
        local_sha = None
        parts = __version__.split('.')
        if len(parts) >= 3:
            local_sha = parts[-1].strip('"').strip("'")
            
        if remote_sha and local_sha and remote_sha.startswith(local_sha):
            # Already up to date!
            print("Updater: Already up to date (SHA matches).")
            return
            
        if not assets:
            return

        # Simple prompt for the user (non-blocking for asyncio)
        async def prompt_update_async():
            gui.grab_attention(sound=True)
            import tkinter as tk
            from tkinter import ttk
            
            result = False
            event = asyncio.Event()
            
            def on_yes():
                nonlocal result
                result = True
                event.set()
                
            def on_no():
                event.set()
                
            dialog = tk.Toplevel(gui._root)
            dialog.title("Update Available")
            dialog.geometry("380x150")
            dialog.resizable(False, False)
            dialog.geometry("+%d+%d" % (gui._root.winfo_x() + 50, gui._root.winfo_y() + 50))
            dialog.transient(gui._root)
            dialog.grab_set()
            dialog.protocol("WM_DELETE_WINDOW", on_no)
            
            msg = f"A new development build is available (commit: {remote_sha or 'unknown'}).\nDo you want to update now?"
            lbl = ttk.Label(dialog, text=msg, justify="center")
            lbl.pack(pady=20)
            
            btn_frame = ttk.Frame(dialog)
            btn_frame.pack(pady=10)
            
            ttk.Button(btn_frame, text="Yes", command=on_yes).pack(side="left", padx=10)
            ttk.Button(btn_frame, text="No", command=on_no).pack(side="right", padx=10)
            
            await event.wait()
            dialog.destroy()
            return result
        
        wants_update = await prompt_update_async()
        if wants_update:
            await perform_update(gui, assets)

    except Exception as e:
        print(f"Updater: Failed to check for updates: {e}")


async def perform_update(gui, assets):
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    asset_to_download = None
    if system == "windows":
        asset_name = "Twitch.Drops.Miner.Windows.zip"
    elif system == "darwin":
        asset_name = "Twitch.Drops.Miner.MacOS.zip"
    else:
        # Linux
        arch = "aarch64" if "aarch64" in machine or "arm64" in machine else "x86_64"
        asset_name = f"Twitch.Drops.Miner.Linux.PyInstaller-{arch}.zip"
        
    for asset in assets:
        if asset["name"] == asset_name:
            asset_to_download = asset["browser_download_url"]
            break
            
    if not asset_to_download:
        messagebox.showerror("Update Failed", f"Could not find an update package for your system ({system} {machine}).")
        return

    import tkinter as tk
    from tkinter import ttk
    
    prog_win = tk.Toplevel(gui._root)
    prog_win.title("Downloading Update")
    prog_win.geometry("300x120")
    prog_win.resizable(False, False)
    prog_win.geometry("+%d+%d" % (gui._root.winfo_x() + 50, gui._root.winfo_y() + 50))
    prog_win.transient(gui._root)
    prog_win.grab_set()
    
    lbl = ttk.Label(prog_win, text="Downloading update, please wait...")
    lbl.pack(pady=10)
    
    prog_bar = ttk.Progressbar(prog_win, orient="horizontal", length=250, mode="determinate")
    prog_bar.pack(pady=5)
    
    pct_lbl = ttk.Label(prog_win, text="0%")
    pct_lbl.pack()

    temp_dir = tempfile.mkdtemp(prefix="tdm_update_")
    zip_path = os.path.join(temp_dir, "update.zip")
    
    try:
        import aiohttp
        # Download
        print(f"Updater: Downloading from {asset_to_download}...")
        async with aiohttp.ClientSession() as session:
            async with session.get(asset_to_download) as response:
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                with open(zip_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(16384):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            pct = (downloaded / total_size) * 100
                            prog_bar['value'] = pct
                            pct_lbl.config(text=f"{int(pct)}%")
                            
        lbl.config(text="Extracting update...")
        await asyncio.sleep(0.1)
        
        # Extract
        print("Updater: Extracting...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        # Find executable
        extracted_folder = os.path.join(temp_dir, "Twitch Drops Miner")
        if not os.path.exists(extracted_folder):
            extracted_folder = temp_dir # fallback if zip structure changes
            
        new_exe = None
        for root, dirs, files in os.walk(extracted_folder):
            for file in files:
                if system == "windows" and file.endswith(".exe"):
                    new_exe = os.path.join(root, file)
                    break
                elif system == "linux" and "AppImage" not in file and "." not in file and os.access(os.path.join(root, file), os.X_OK):
                    # Rough heuristic for linux PyInstaller binary
                    new_exe = os.path.join(root, file)
                    break
                    
        if system == "darwin":
            # MacOS app bundle logic
            # This is complex because it's a directory. For now, simple fallback.
            messagebox.showinfo("Update", "MacOS auto-update is partially supported. Please download manually.")
            return

        if not new_exe:
            messagebox.showerror("Update Failed", "Could not locate the new executable in the downloaded update.")
            return

        current_exe = sys.executable
        
        if system == "windows":
            # Rename running executable to .old, copy new one, and start it
            old_exe = current_exe + ".old"
            if os.path.exists(old_exe):
                try:
                    os.remove(old_exe)
                except:
                    pass
            os.rename(current_exe, old_exe)
            shutil.copy2(new_exe, current_exe)
            
            # Clean PyInstaller environment variables so the new process doesn't think it's a child worker
            env = os.environ.copy()
            env.pop('_MEIPASS2', None)
            for k in list(env.keys()):
                if k.startswith('_PYI_'):
                    env.pop(k)
                    
            subprocess.Popen([current_exe], env=env, creationflags=0x00000008) # DETACHED_PROCESS
            sys.exit(0)
        else:
            # Linux: rename old, copy new, restart
            old_exe = current_exe + ".old"
            if os.path.exists(old_exe):
                os.remove(old_exe)
            os.rename(current_exe, old_exe)
            shutil.copy2(new_exe, current_exe)
            os.chmod(current_exe, 0o755)
            
            # Restart
            os.execv(current_exe, [current_exe] + sys.argv[1:])

    except Exception as e:
        messagebox.showerror("Update Error", f"An error occurred while updating:\n{e}")
    finally:
        # Cleanup
        try:
            if system != "windows": # On Windows, batch script might need these files briefly
                shutil.rmtree(temp_dir, ignore_errors=True)
        except:
            pass
