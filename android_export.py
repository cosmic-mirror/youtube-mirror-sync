#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# --- 1. DEVICE-AGNOSTIC ENVIRONMENT & PATH RESOLUTION ---
def resolve_environment():
    extra_paths = [
        str(Path(sys.executable).parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local/bin"),
    ]
    current_path = os.environ.get("PATH", "")
    for p in reversed(extra_paths):
        if os.path.isdir(p) and p not in current_path.split(":"):
            current_path = f"{p}:{current_path}"
    os.environ["PATH"] = current_path

    try:
        import mutagen
        return
    except ImportError:
        pass

    candidate_envs = [
        Path.home() / "anaconda3/envs/yt-mirror-env/bin/python",
        Path.home() / "miniconda3/envs/yt-mirror-env/bin/python",
        Path.home() / "opt/anaconda3/envs/yt-mirror-env/bin/python",
        Path.home() / ".conda/envs/yt-mirror-env/bin/python",
        Path("/opt/homebrew/anaconda3/envs/yt-mirror-env/bin/python"),
        Path("/opt/homebrew/Caskroom/miniconda/base/envs/yt-mirror-env/bin/python"),
        Path("/usr/local/anaconda3/envs/yt-mirror-env/bin/python"),
        Path("/usr/local/Caskroom/miniconda/base/envs/yt-mirror-env/bin/python"),
    ]

    for env_python in candidate_envs:
        if env_python.is_file() and os.access(env_python, os.X_OK):
            os.execv(str(env_python), [str(env_python)] + sys.argv)

    print("Error: 'mutagen' library not found and 'yt-mirror-env' could not be auto-detected.", file=sys.stderr)
    print("Please activate your environment: conda activate yt-mirror-env", file=sys.stderr)
    sys.exit(1)

resolve_environment()

# --- 2. STANDARD IMPORTS ---
import shutil
import sqlite3
import re
import subprocess
import json
import datetime
import unicodedata
from mutagen import File
from mutagen.easyid3 import EasyID3

# -------- CONFIG --------
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / ".data"
DB_PATH = DATA_DIR / "metadata.db"
MANIFEST_PATH = DATA_DIR / "export_manifest.json"

MUSIC_ROOT = Path.home() / "Music"
EXPORT_ROOT = Path.home() / "Downloads" / "AndroidExport"
PLAYLISTS_ONLY_ROOT = Path.home() / "Downloads" / "AndroidExport_Playlists"


def find_ytdlp_binary():
    if "YTDLP_BIN" in os.environ and shutil.which(os.environ["YTDLP_BIN"]):
        return os.environ["YTDLP_BIN"]

    sibling = Path(sys.executable).parent / "yt-dlp"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)

    found = shutil.which("yt-dlp")
    if found:
        return found

    fallback_locs = [
        Path("/opt/homebrew/bin/yt-dlp"),
        Path("/usr/local/bin/yt-dlp"),
        Path.home() / ".local/bin/yt-dlp",
        Path.home() / "anaconda3/envs/yt-mirror-env/bin/yt-dlp",
        Path.home() / "miniconda3/envs/yt-mirror-env/bin/yt-dlp",
    ]
    for loc in fallback_locs:
        if loc.is_file() and os.access(loc, os.X_OK):
            return str(loc)

    return "yt-dlp"

YTDLP_BIN = find_ytdlp_binary()


# --- 3. PERSISTENT LEDGER (MANIFEST) ---
def load_manifest():
    default_manifest = {"playlists": {}}
    if not MANIFEST_PATH.exists():
        return default_manifest
    
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            manifest = json.load(f)
            if "playlists" not in manifest:
                manifest["playlists"] = {}
            return manifest
    except Exception as e:
        print(f"Warning: Could not read manifest ({e}). Starting fresh.")
        return default_manifest


def save_manifest(manifest):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: Could not save export manifest: {e}", file=sys.stderr)


# --- 4. UTILITIES ---
def get_safe_filename(original_stem, vid):
    clean = unicodedata.normalize('NFC', original_stem)
    safe = re.sub(r'[\\/*?:"<>|]', '_', clean)
    safe = "".join(ch for ch in safe if unicodedata.category(ch)[0] != "C")
    safe = re.sub(r'\s+', ' ', safe).strip('. ')

    if len(safe) < 1:
        safe = f"track_{vid}"

    if len(safe) > 200:
        safe = safe[:200].strip('. ')

    return safe


def clean_playlist_name(name):
    name = unicodedata.normalize('NFC', name)
    name = re.sub(r'[\\/*?:"<>|：┃]', "_", name)
    return re.sub(r'[\s.]+$', '', name).strip()


def list_playlists(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, name, url FROM playlists ORDER BY name ASC")
    return cur.fetchall()


def fetch_live_youtube_order(url):
    print("  Fetching live playlist order from YouTube...")
    cmd = [YTDLP_BIN, '--js-runtimes', 'node', '--remote-components', 'ejs:github', '--flat-playlist', '-J', url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
        return [entry.get('id') for entry in data.get('entries', []) if entry.get('id')]
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] yt-dlp failed (Exit code {e.returncode}): {e.stderr.strip()}")
        return None
    except Exception as e:
        print(f"  [ERROR] Could not fetch live order from YouTube: {e}")
        return None


def scan_music_library():
    print("Scanning music library for ISRC/video IDs...")
    isrc_map = {}

    for root, _, files in os.walk(MUSIC_ROOT):
        for file in files:
            if file.startswith("._") or not file.lower().endswith((".mp3", ".m4a", ".flac")):
                continue
                
            path = Path(root) / file
            try:
                # 1. EasyID3 (matches yt-mirror-sync)
                try:
                    audio = EasyID3(path)
                    if "isrc" in audio and audio["isrc"]:
                        isrc_map[str(audio["isrc"][0])] = path
                        continue
                except Exception:
                    pass

                # 2. Raw ID3 TSRC fallback
                audio = File(path)
                if audio and "TSRC" in audio:
                    isrc_map[str(audio["TSRC"][0])] = path
            except Exception:
                pass

    print(f"Found {len(isrc_map)} tracks with ISRC tags.\n")
    return isrc_map


# --- 5. CORE EXPORT ROUTINE ---
def export_playlist(playlist_id, playlist_name, playlist_url, isrc_map, manifest, playlists_only=False):
    str_pid = str(playlist_id)
    history = manifest["playlists"].get(str_pid, {})
    previous_tracks = history.get("tracks", {})
    is_first_init = len(previous_tracks) == 0

    mode_label = ".m3u only" if playlists_only else ("INITIAL EXPORT (Full)" if is_first_init else "INCREMENTAL (New Only)")
    print(f"\nExporting: {playlist_name} [{mode_label}]")

    # 1. Fetch live order from YouTube (Enforces Internet Connection)
    live_youtube_ids = fetch_live_youtube_order(playlist_url)
    if live_youtube_ids is None:
        print(f"\n  [ABORT] Could not connect to YouTube.")
        print(f"  Please check your internet connection to export '{playlist_name}'.")
        return

    # Deduplicate while strictly maintaining live YouTube sequence
    valid_video_ids = []
    seen = set()
    for vid in live_youtube_ids:
        if vid not in seen:
            valid_video_ids.append(vid)
            seen.add(vid)

    # 2. Target export directory
    safe_playlist_name = clean_playlist_name(playlist_name)
    base_root = PLAYLISTS_ONLY_ROOT if playlists_only else EXPORT_ROOT
    export_dir = base_root / safe_playlist_name
    export_dir.mkdir(parents=True, exist_ok=True)

    # 3. Check for deleted tracks since last export
    current_valid_set = set(valid_video_ids)
    deleted_vids = [vid for vid in previous_tracks.keys() if vid not in current_valid_set]
    if deleted_vids:
        print("\n" + "!" * 60)
        print("  ⚠️ SONGS REMOVED FROM PLAYLIST:")
        print("  Delete these files manually from your Android phone:")
        for vid in deleted_vids:
            old_filename = previous_tracks[vid]
            print(f"    - {old_filename}")
        print("!" * 60 + "\n")

    m3u_tracks = []
    matched = 0
    copied_count = 0
    skipped_count = 0
    missing_details = []
    current_exported_tracks = {}

    # 4. Process files directly from local storage matching live YouTube order
    for vid in valid_video_ids:
        if vid in isrc_map:
            src = isrc_map[vid]
            safe_stem = get_safe_filename(src.stem, vid)
            safe_song_name = f"{safe_stem}{src.suffix}"
            current_exported_tracks[vid] = safe_song_name

            if not playlists_only:
                dst = export_dir / safe_song_name
                
                # Copy only if track has not been exported in a previous session
                if vid not in previous_tracks:
                    shutil.copyfile(src, dst)
                    copied_count += 1
                else:
                    skipped_count += 1

            # Natural title for Samsung Music display label (preserves slashes/colons)
            display_name = unicodedata.normalize('NFC', src.stem)
            m3u_tracks.append((display_name, safe_song_name))
            matched += 1
        else:
            missing_details.append(f"https://youtu.be/{vid}")

    # 5. Write complete M3U playlist file (UTF-8 BOM + CRLF for Samsung Music)
    m3u_path = export_dir / f"{safe_playlist_name}.m3u"
    with open(m3u_path, "w", encoding="utf-8-sig", newline='\r\n') as f:
        f.write("#EXTM3U\n")
        for track_name, file_name in m3u_tracks:
            f.write(f"#EXTINF:-1,{track_name}\n")
            f.write(f"{file_name}\n")

    # 6. Commit to manifest ledger
    if not playlists_only:
        manifest["playlists"][str_pid] = {
            "name": playlist_name,
            "last_exported": datetime.datetime.now().isoformat(),
            "tracks": current_exported_tracks
        }
        save_manifest(manifest)

    # 7. Summary
    if playlists_only:
        print(f"  Created: {safe_playlist_name}/{m3u_path.name} ({matched} tracks)")
    else:
        print(f"  Total Tracks in Playlist: {matched}")
        print(f"  -> Newly Exported Files:  {copied_count} (Transfer to Android)")
        print(f"  -> Already on Android:    {skipped_count} (Skipped)")
        print(f"  -> Full .m3u Playlist:    {m3u_path.name} (Overwrite on Android)")

    if missing_details:
        print(f"  Missing from Mac Hard Drive: {len(missing_details)}")
        for detail in missing_details:
            print(f"    - {detail}")
    print("  Done.")


# --- 6. USER INTERFACE ---
def main():
    if not DB_PATH.exists():
        print(f"Error: Database file not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    playlists = list_playlists(conn)

    if not playlists:
        print("No playlists found in database.")
        conn.close()
        return

    manifest = load_manifest()
    isrc_map = scan_music_library()

    try:
        while True:
            print("=" * 55)
            print("Playlists:")
            for idx, (pid, name, _) in enumerate(playlists, 1):
                tracked_count = len(manifest["playlists"].get(str(pid), {}).get("tracks", {}))
                status = f"{tracked_count} synced" if tracked_count > 0 else "New"
                print(f"  [{idx}] {name} ({status})")
            
            print("\nActions:")
            print("  [A] Export ALL (Audio + .m3u)")
            print("  [P] Export ALL Playlists ONLY (.m3u only)")
            print("  [R] Reset Export History")
            print("  [Q] Quit")
            print("=" * 55)

            choice = input("\nSelect playlist(s) (e.g. 1, 3, 5) or command [A, P, R, Q]: ").strip()

            if not choice:
                continue

            if choice.lower() == 'q':
                break

            if choice.lower() == 'r':
                target = input("Enter playlist number(s) to reset (e.g. 1, 3 or 'all'): ").strip()
                if target.lower() == 'all':
                    manifest["playlists"] = {}
                    save_manifest(manifest)
                    print("\n  -> Reset export history for ALL playlists.")
                else:
                    tokens = [t.strip() for t in target.split(",") if t.strip()]
                    reset_count = 0
                    for token in tokens:
                        try:
                            idx = int(token) - 1
                            if 0 <= idx < len(playlists):
                                pid_to_reset = str(playlists[idx][0])
                                if pid_to_reset in manifest["playlists"]:
                                    del manifest["playlists"][pid_to_reset]
                                    print(f"  -> Reset history for '{playlists[idx][1]}'.")
                                    reset_count += 1
                                else:
                                    print(f"  -> '{playlists[idx][1]}' has no prior history.")
                            else:
                                print(f"  -> Number [{token}] is out of range.")
                        except ValueError:
                            print(f"  -> '{token}' is not a valid number.")
                    if reset_count > 0:
                        save_manifest(manifest)
                continue

            if choice.lower() == 'p':
                print(f"\nExporting all .m3u files to: {PLAYLISTS_ONLY_ROOT}")
                for pid, name, url in playlists:
                    export_playlist(pid, name, url, isrc_map, manifest, playlists_only=True)
            elif choice.lower() == 'a':
                for pid, name, url in playlists:
                    export_playlist(pid, name, url, isrc_map, manifest, playlists_only=False)
            else:
                tokens = [t.strip() for t in choice.split(",") if t.strip()]
                selected_indices = []

                for token in tokens:
                    try:
                        idx = int(token) - 1
                        if 0 <= idx < len(playlists):
                            if idx not in selected_indices:
                                selected_indices.append(idx)
                        else:
                            print(f"  Warning: Index [{token}] is out of range. Skipping.")
                    except ValueError:
                        print(f"  Warning: '{token}' is not a valid number. Skipping.")

                if not selected_indices:
                    print("No valid playlists chosen.")
                    continue

                for idx in selected_indices:
                    pid, name, url = playlists[idx]
                    export_playlist(pid, name, url, isrc_map, manifest, playlists_only=False)

            again = input("\nDo you want to export more? [y/N]: ").strip().lower()
            if again not in ('y', 'yes'):
                break

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
    finally:
        save_manifest(manifest)
        conn.close()

    print("\nAll done. Goodbye!")


if __name__ == "__main__":
    main()
