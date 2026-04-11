A class-based Python utility that mirrors YouTube playlists directly into a local macOS Apple Music library. 

Unlike basic downloaders, `youtube-mirror-sync` is architected for long-term library maintainability. It manages metadata, prevents duplicates, handles Music.app race conditions, and gracefully skips deleted/private videos.

> **Note:** This tool is specifically built for **macOS** as it relies on AppleScript to orchestrate the native Music.app.

---

## Key Features & What It Can Do

1. **Automatic Apple Music Integration:** Downloads YouTube playlists, converts them to high-quality MP3s, cleans up the titles, and perfectly mirrors them into native Apple Music playlists.
2. **Rename & Move Safely:** Go ahead and rename your tracks, change album artwork, or reorganize your library in Apple Music. The script tracks the actual audio data, so it won't get confused and download duplicates.
3. **Smart Dead-Link Handling:** If a video gets deleted, age-restricted, or made private on YouTube, the app remembers and skips it on future syncs so your updates stay lightning fast.
4. **Two-Way Playlist Sync:** If a video is removed from the upstream YouTube playlist, the script can automatically clean up and remove the track from your local Apple Music playlist (optional).
5. **Self-Healing:** If you ever accidentally delete the script's database, it can scan your existing MP3 library, recognize what it previously downloaded, and rebuild its memory. 
6. **Frictionless Setup:** Double-click to run. No dealing with Python environments, Conda conflicts, or "externally managed environment" errors. It just works.

### Under the Hood (For Nerds)
To achieve this level of reliability, `yt-mirror-sync` utilizes several advanced software patterns:
* **File-Hash Identification:** Uses SHA-256 hashing to track files rather than relying on rigid, fragile file paths.
* **Embedded Fingerprints:** Writes the YouTube Video ID directly into the MP3's `ISRC` ID3 tag for database reconstruction.
* **Circuit Breaker & Exponential Backoff:** Failed downloads are logged, and retry wait times double exponentially to prevent infinite hang-ups on dead links.
* **Asynchronous Import Verification:** Prevents AppleScript race conditions by actively polling the Apple Music library track count to guarantee files are imported before playlist assignments happen.
* **Absolute-Path Execution:** Bootstraps an isolated virtual environment and executes via absolute paths to completely bypass global macOS/Homebrew/Conda python restrictions.

---

## Prerequisites

You will need **Homebrew** installed on your Mac. Open your terminal and install the required system dependencies:

```bash
brew install python ffmpeg node
```
*(Note: `ffmpeg` is required to extract audio, and `node` is used by yt-dlp to bypass certain YouTube bot-protections).*

---

## Installation & Usage

1. **Clone or Download the Repository:**
   ```bash
   git clone https://github.com/YOUR-USERNAME/yt-mirror-sync.git
   cd yt-mirror-sync
   chmod +x *.command
   ```

2. **Run the App:**
   Double-click the **`run.command`** file in Finder (or run `./run.command` in terminal). 
   - *First Run:* It will automatically create an isolated Python virtual environment (`.venv`) and install `yt-dlp` and `mutagen`.
   - You will be greeted by an interactive console menu. Type `2` to **add a YouTube playlist**, then `5` to **sync**.

### macOS Permissions

Because this script automates the Music app and moves local files, macOS will prompt you for security permissions the first time it runs:

1. **Automation:** When prompted, allow Terminal to control "Music".
2. **Full Disk Access:** To seamlessly move downloaded tracks into your Apple Music Media folder, go to **System Settings > Privacy & Security > Full Disk Access** and toggle the switch ON for your terminal app (e.g., Terminal or iTerm2).

---

## Keeping Things Updated

YouTube frequently updates its backend, which regularly breaks downloaders. 

If downloads stop working, simply double-click the **`update.command`** file. It will automatically fetch the latest versions of `yt-dlp`, `pip`, and `mutagen` into your isolated environment.

---

## Data Storage

All your script data is stored cleanly inside the `.data/` folder in the project directory:
* `metadata.db`: The SQLite database tracking your playlists, hashes, and circuit-breaker statuses.
* `archives/`: `yt-dlp` download archives to prevent double-downloading.
* `staging/`: Temporary folder for processing audio before moving it to Music.

*Note: Your actual MP3 files are stored safely in your macOS Music Library directory at `~/Music/Music/Media.localized/Music/Compilations/[playlist_folder]`.*

---

## Troubleshooting

* **Script stalls while waiting for Music.app to import:** Ensure you have granted Full Disk Access (see Permissions).
* **Duplicates in Music.app:** This happens if you have iCloud "Sync Library" enabled and Apple messes up the persistent IDs. Use Option `9` in the menu (`Reset Playlist Link`) to generate a clean playlist.
* **Corrupted Library:** If your Apple Music database is beyond repair, Option `8` acts as a nuclear "Rebuild Library" tool that clears Apple Music and asks it to re-import your media from scratch.

---

## Disclaimer & License

This tool is provided for educational and personal archiving purposes only. Users are responsible for adhering to [YouTube's Terms of Service](https://www.youtube.com/static?template=terms) regarding downloading content. 

Distributed under the [MIT License](LICENSE).