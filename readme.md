# Overview
A macOS-only toolset to download YouTube playlists directly into Apple Music, maintain playlist order and metadata via AppleScript, and export organized tracks and `.m3u` playlists for Android/Samsung Music.


## 1. Prerequisites (macOS Only)

This tool interacts directly with macOS **Music.app** via AppleScript and uses **Conda** to isolate dependencies (`yt-dlp`, `ffmpeg`, `mutagen`, `nodejs`).

1. **Install Homebrew** (if not already installed):
   ```zsh
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

2. **Install Miniconda & Initialize Shell**:
   ```zsh
   brew install --cask miniconda
   conda init zsh
   ```
   *(If you use bash instead of zsh, run `conda init bash` instead. Restart your Terminal window after initializing).*

> **Note on Shell Configuration:**  
> Running `conda init` writes the required Conda hooks into your `~/.zshrc` (or `~/.bash_profile`). This allows the terminal to recognize the `conda` command and lets `android_export.py` seamlessly find dependencies.



## 2. File Overview

* **`run.command`**  
  Double-clickable macOS launcher. Automatically creates/activates the `yt-mirror-env` Conda environment, installs/updates dependencies (`ffmpeg`, `nodejs`, `yt-dlp`, `mutagen`), and launches `yt_mirror_sync.py`.

* **`yt_mirror_sync.py`**  
  The main sync engine. Downloads audio from YouTube, injects video IDs into ID3 tags (ISRC), copies files to Apple Music's inbox (`Automatically Add to Music`), and re-orders playlists in Apple Music via AppleScript. Maintains state inside `.data/metadata.db`.

* **`android_export.py`**  
  Export tool for Android devices (Samsung Music compatible). Scans your local Apple Music folder, matches tracks by their embedded ISRC tags, fetches the live YouTube playlist order, and exports tracks along with clean `.m3u` playlists (with UTF-8-SIG and CRLF line endings) to `~/Downloads`.


## 3. How to Run

### 3.1. Sync to Apple Music
Make `run.command` executable (one-time setup):

```zsh
chmod +x run.command
```

Double-click **`run.command`** in Finder, or run it via Terminal:
```zsh
./run.command
```
* **First run:** Automatically creates the `yt-mirror-env` Conda environment, installs all required system/Python packages, and starts the sync interface.
* **Subsequent runs:** Prompts optionally to check for updates (defaults to `N`), activates the environment, runs `yt_mirror_sync.py`, and exits cleanly.


### 3.2. Export for Android
#### 1. Export on mac
Ensure `run.command` has been run at least once so the environment exists. Then, run the exporter:

```zsh
python3 android_export.py
```
*Or via absolute path:*
```zsh
python3 "$HOME/Documents/Code_Projects/YT_Playlist/android_export.py"
```
*(The script contains built-in environment detection and will automatically route itself through the `yt-mirror-env` Python interpreter if launched outside an active Conda session).*

**Output Locations:**
* **Full Export (Audio + `.m3u`):** `~/Downloads/AndroidExport/<Playlist_Name>/`
* **Playlist Only (`.m3u`):** `~/Downloads/AndroidExport_Playlists/<Playlist_Name>/`

### 2. Transfer to Android
1. Zip the desired playlist folder(s) inside `~/Downloads/AndroidExport/`.
2. Transfer the archive to your Android device (e.g., via [KDE Connect](https://kdeconnect.kde.org/), LocalSend, or USB).
3. On your Android device, unzip and move the playlist folders directly into your device's **Music** folder (`/Internal Storage/Music/`).
4. Open Samsung Music and import to Samsung Music *(Settings > Manage Playlists > Import)* or play the synced playlists directly on your preferred player that doesn't require import.
