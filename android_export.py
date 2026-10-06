#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# --- 1. DEVICE-AGNOSTIC ENVIRONMENT & PATH RESOLUTION ---
def resolve_environment():
    """
    Ensures the script runs in an environment with mutagen and finds
    tools like yt-dlp and node across Apple Silicon, Intel Macs, and user dirs.
    """
    # Prepend common Mac binary directories to PATH for subprocesses
    extra_paths = [
        str(Path(sys.executable).parent),
        "/opt/homebrew/bin",                    # Apple Silicon Homebrew
        "/usr/local/bin",                       # Intel Mac Homebrew
        str(Path.home() / ".local/bin"),        # User local bin
    ]
    current_path = os.environ.get("PATH", "")
    for p in reversed(extra_paths):
        if os.path.isdir(p) and p not in current_path.split(":"):
            current_path = f"{p}:{current_path}"
    os.environ["PATH"] = current_path

    # Check if mutagen is available in current interpreter
    try:
        import mutagen
        return
    except ImportError:
        pass

    # If mutagen is missing, try to auto-relaunch using yt-mirror-env across known Mac paths
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
            # Relaunch the script using the found conda environment
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
import unicodedata
from mutagen import File

# -------- CONFIG --------
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / ".data" if (SCRIPT_DIR / ".data").exists() else Path(".data")
DB_PATH = DATA_DIR / "metadata.db"

MUSIC_ROOT = Path.home() / "Music"
EXPORT_ROOT = Path.home() / "Downloads" / "AndroidExport"
PLAYLISTS_ONLY_ROOT = Path.home() / "Downloads" / "AndroidExport_Playlists"


def find_ytdlp_binary():
    """Locates yt-dlp across Conda, Homebrew (Apple Silicon & Intel), or PATH."""
    if "YTDLP_BIN" in os.environ and shutil.which(os.environ["YTDLP_BIN"]):
        return os.environ["YTDLP_BIN"]

    # 1. Look in the same bin/ dir as current Python
    sibling = Path(sys.executable).parent / "yt-dlp"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)

    # 2. Check system PATH
    found = shutil.which("yt-dlp")
    if found:
        return found

    # 3. Common Mac package manager locations
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
# ------------------------


def get_safe_filename(original_stem, vid):
    ascii_name = unicodedata.normalize('NFKD', original_stem).encode('ascii', 'ignore').decode('utf-8')
    safe = re.sub(r'[^a-zA-Z0-9\s\-_]', '', ascii_name).strip()
    if len(safe) < 1:
        safe = f"track_{vid}"
    return safe


def clean_playlist_name(name):
    name = unicodedata.normalize('NFC', name)
    name = re.sub(r'[\\/*?:"<>|：┃]', "_", name)
    return re.sub(r'[\s.]+$', '', name).strip()


def list_playlists(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, name, url FROM playlists ORDER BY name ASC")
    return cur.fetchall()


def get_active_playlist_vids_ordered(conn, playlist_id):
    """
    Fetches video IDs ordered by the timestamp they were added to the DB (v.added_at).
    This matches the schema defined in yt-mirror-sync.
    """
    cur = conn.cursor()
    cur.execute('''
        SELECT pv.video_id 
        FROM playlist_videos pv
        JOIN videos v ON pv.video_id = v.video_id
        WHERE pv.playlist_id = ?
        ORDER BY v.added_at ASC, pv.rowid ASC
    ''', (playlist_id,))
    return [row[0] for row in cur.fetchall()]


def fetch_live_youtube_order(url):
    """
    Fetches the live playlist order from YouTube using yt-dlp.
    Uses '--js-runtimes node' exactly like yt-mirror-sync to bypass bot blocks.
    Returns list of video IDs on success, or None on failure.
    """
    print("  Fetching live playlist order from YouTube...")
    cmd = [YTDLP_BIN, '--js-runtimes', 'node', '--flat-playlist', '-J', url]
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
            if file.startswith("._"):
                continue
                
            if file.lower().endswith((".mp3", ".m4a", ".flac")):
                path = Path(root) / file
                try:
                    audio = File(path)
                    # EasyID3['isrc'] writes to the TSRC ID3 frame
                    if audio and "TSRC" in audio:
                        isrc = str(audio["TSRC"][0])
                        isrc_map[isrc] = path
                except Exception:
                    pass

    print(f"Found {len(isrc_map)} tracks with ISRC tags.\n")
    return isrc_map


def export_playlist(conn, playlist_id, playlist_name, playlist_url, isrc_map, playlists_only=False):
    print(f"\nExporting: {playlist_name} {'(.m3u only)' if playlists_only else ''}")

    db_vids_ordered = get_active_playlist_vids_ordered(conn, playlist_id)
    if not db_vids_ordered:
        print("  No videos found in database for this playlist.")
        return

    # 1. Fetch live order from YouTube. Abort on failure to protect playlist integrity.
    live_youtube_ids = fetch_live_youtube_order(playlist_url)
    if live_youtube_ids is None:
        print(f"  [ABORT] Export cancelled for '{playlist_name}'. Internet down or invalid playlist.")
        return

    db_vids_set = set(db_vids_ordered)

    # 2. Add available tracks in current live YouTube order
    valid_video_ids = []
    seen = set()
    for vid in live_youtube_ids:
        if vid in db_vids_set and vid not in seen:
            valid_video_ids.append(vid)
            seen.add(vid)

    # 3. Preserve privated/hidden/deleted songs in their original relative positions
    orphans = [vid for vid in db_vids_ordered if vid not in seen]
    if orphans:
        for orphan_vid in orphans:
            idx_in_db = db_vids_ordered.index(orphan_vid)
            
            # Find the closest preceding track in the DB that exists in valid_video_ids
            inserted = False
            for prev_idx in range(idx_in_db - 1, -1, -1):
                prev_vid = db_vids_ordered[prev_idx]
                if prev_vid in valid_video_ids:
                    target_pos = valid_video_ids.index(prev_vid) + 1
                    valid_video_ids.insert(target_pos, orphan_vid)
                    inserted = True
                    break
            
            if not inserted:
                valid_video_ids.insert(0, orphan_vid)

        print(f"  Note: Preserved {len(orphans)} privated/hidden tracks in relative positions.")

    # 4. Resolve destination path (both modes create subfolders per playlist)
    safe_playlist_name = clean_playlist_name(playlist_name)
    base_root = PLAYLISTS_ONLY_ROOT if playlists_only else EXPORT_ROOT
    export_dir = base_root / safe_playlist_name
    export_dir.mkdir(parents=True, exist_ok=True)

    m3u_tracks = []
    matched = 0
    missing_details = []

    # 5. Process files
    for vid in valid_video_ids:
        if vid in isrc_map:
            src = isrc_map[vid]
            safe_stem = get_safe_filename(src.stem, vid)
            safe_song_name = f"{safe_stem}{src.suffix}"

            # Only copy audio files if NOT in playlist-only mode
            if not playlists_only:
                dst = export_dir / safe_song_name
                if not dst.exists():
                    shutil.copyfile(src, dst)

            display_name = unicodedata.normalize('NFC', src.stem)
            m3u_tracks.append((display_name, safe_song_name))
            matched += 1
        else:
            cur = conn.cursor()
            cur.execute("SELECT title FROM videos WHERE video_id = ?", (vid,))
            row = cur.fetchone()
            song_title = row[0] if row else f"Unknown YouTube ID ({vid})"
            missing_details.append(song_title)

    # 6. Write M3U file (utf-8-sig and CRLF for Samsung Music compatibility)
    m3u_path = export_dir / f"{safe_playlist_name}.m3u"
    with open(m3u_path, "w", encoding="utf-8-sig", newline='\r\n') as f:
        f.write("#EXTM3U\n")
        for track_name, file_name in m3u_tracks:
            f.write(f"#EXTINF:-1,{track_name}\n")
            f.write(f"{file_name}\n")

    if playlists_only:
        print(f"  Created: {safe_playlist_name}/{m3u_path.name} ({matched} tracks)")
    else:
        print(f"  Exported/Matched: {matched}")

    if missing_details:
        print(f"  Missing from Mac Hard Drive: {len(missing_details)}")
        for detail in missing_details:
            print(f"    - {detail}")
    print("  Done.")


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

    isrc_map = scan_music_library()

    try:
        while True:
            print("=" * 45)
            print("Playlists:")
            for idx, (_, name, _) in enumerate(playlists, 1):
                print(f"  [{idx}] {name}")
            print("  [A] Export ALL (Songs + .m3u)")
            print("  [P] Export ALL Playlists ONLY (.m3u only)")
            print("  [Q] Quit")
            print("=" * 45)

            choice = input("\nSelect option (e.g. 1, 4, 2, A or P): ").strip()

            if not choice:
                continue

            if choice.lower() == 'q':
                break

            if choice.lower() == 'p':
                print(f"\nExporting all .m3u files to: {PLAYLISTS_ONLY_ROOT}")
                for pid, name, url in playlists:
                    export_playlist(conn, pid, name, url, isrc_map, playlists_only=True)
            elif choice.lower() == 'a':
                for pid, name, url in playlists:
                    export_playlist(conn, pid, name, url, isrc_map, playlists_only=False)
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
                    export_playlist(conn, pid, name, url, isrc_map, playlists_only=False)

            again = input("\nDo you want to export more? [y/N]: ").strip().lower()
            if again not in ('y', 'yes'):
                break

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
    finally:
        conn.close()

    print("\nAll done. Goodbye!")


if __name__ == "__main__":
    main()
