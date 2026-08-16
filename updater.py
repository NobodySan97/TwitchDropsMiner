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
        if len(parts) > 3:
            local_sha = parts[3]
            
        if remote_sha and local_sha and remote_sha.startswith(local_sha):
            # Already up to date!
            print("Updater: Already up to date (SHA matches).")
            return
            
        if not assets:
            return

        # Simple prompt for the user
        def prompt_update():
            return messagebox.askyesno(
                "Update Available", 
                f"A new development build is available (commit: {remote_sha or 'unknown'}).\nDo you want to update now?"
            )
        
        wants_update = await loop.run_in_executor(None, prompt_update)
        if wants_update:
            await perform_update(assets)

    except Exception as e:
        print(f"Updater: Failed to check for updates: {e}")


async def perform_update(assets):
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

    temp_dir = tempfile.mkdtemp(prefix="tdm_update_")
    zip_path = os.path.join(temp_dir, "update.zip")
    
    try:
        # Download
        print(f"Updater: Downloading from {asset_to_download}...")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, urllib.request.urlretrieve, asset_to_download, zip_path)
        
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
            # Use batch script to replace file
            bat_path = os.path.join(tempfile.gettempdir(), "tdm_update.bat")
            with open(bat_path, "w") as f:
                f.write(f'''@echo off
timeout /t 2 /nobreak > NUL
del "{current_exe}"
copy "{new_exe}" "{current_exe}"
start "" "{current_exe}"
del "%~f0"
''')
            subprocess.Popen([bat_path], creationflags=subprocess.CREATE_NO_WINDOW)
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
