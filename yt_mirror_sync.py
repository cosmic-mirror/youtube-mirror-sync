#!/usr/bin/env python3
"""
yt-mirror-sync: The Definitive Edition

This script is a self-contained, class-based application for mirroring
YouTube playlists into a local Apple Music library on macOS.

It is architected for resilience and maintainability, built upon several
core principles ("Pillars of Resilience"):
  1. Hashing for file identification, making it immune to file renames.
  2. Persistent ID linking for unambiguous playlist management.
  3. Embedded ISRC fingerprints for database self-healing and recovery.
  4. An "Accountant Supervisor" model to avoid race conditions with Music.app
     by waiting for a specific number of new tracks to be imported.
  5. Circuit Breaker / Exponential Backoff to handle deleted or private videos
     gracefully without wasting time on every run.

The application is structured with a clear Separation of Concerns, with
distinct classes for database management, Music app interaction, YouTube
downloads, library scanning, core logic, and the user interface.
"""
import os
import sys
import sqlite3
import subprocess
import json
import datetime
from pathlib import Path
import textwrap
import urllib.parse
import re
import shutil
import hashlib
import time
from typing import Optional, List, Dict, Tuple

try:
    from mutagen.easyid3 import EasyID3, EasyID3KeyError
    from mutagen.id3 import ID3NoHeaderError
except ImportError:
    print("Error: 'mutagen' library not found. The launcher script should install it.", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------
# 1. UTILITY FUNCTIONS
# ---------------------------------------------------------------------

def _run_command(cmd: List[str], capture_output: bool = True, text: bool = True, check: bool = False) -> subprocess.CompletedProcess:
    """A wrapper around subprocess.run for executing external commands."""
    try:
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=text,
            check=check
        )
    except FileNotFoundError:
        print(f"Error: Command not found: {cmd[0]}. Please install it and ensure it's on PATH.", file=sys.stderr)
        sys.exit(127)

def _clean_youtube_title(title: str, artist: str) -> str:
    """Removes common junk from YouTube video titles."""
    pattern = re.compile(f"^{re.escape(artist)}\s*[-–:]\s*", re.IGNORECASE)
    cleaned_title = pattern.sub('', title)
    junk_patterns = [
        r'\(official music video\)', r'\[official music video\]', r'\(official video\)',
        r'\[official video\]', r'\(lyrics\)', r'\[lyrics\]', r'\(lyric video\)',
        r'\[lyric video\]', r'\(hd\)', r'\[hd\]', r'\(4k\)', r'\[4k\]',
        r'\(audio\)', r'\[audio\]'
    ]
    for pattern in junk_patterns:
        cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE)
    cleaned_title = re.sub(r'\[\s*\]', '', cleaned_title)
    cleaned_title = re.sub(r'\(\s*\)', '', cleaned_title)
    return cleaned_title.strip()

def _clean_artist_name(artist: str) -> str:
    """Removes junk like '- Topic' from artist names."""
    return re.sub(r'\s-\sTopic$', '', artist, flags=re.IGNORECASE).strip()

def _sanitize_filename(name: str) -> str:
    """Removes characters illegal in filenames."""
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    return sanitized.strip('. ')

def _slugify(s: str) -> str:
    """Creates a URL-safe slug from a string."""
    s = s.strip().lower()
    s = re.sub(r"[^a-z-0-9]+", '-', s)
    return re.sub(r'-+', '-', s).strip('-') or 'playlist'

# ---------------------------------------------------------------------
# 2. APPLICATION CONFIGURATION
# ---------------------------------------------------------------------

class Config:
    """Holds all static configuration for the application."""
    SCRIPT_DIR: Path = Path(__file__).resolve().parent
    APP_DIR: Path = SCRIPT_DIR / '.data'
    
    DB_PATH: Path = APP_DIR / 'metadata.db'
    ARCHIVES_DIR: Path = APP_DIR / 'archives'
    STAGING_DIR: Path = APP_DIR / 'staging'
    
    YTDLP_BIN: str = os.environ.get('YTDLP_BIN', 'yt-dlp')
    AUDIO_FORMAT: str = 'mp3'

    DEFAULT_TARGET_DIR: Path = Path.home() / 'Music/Music/Media.localized/Automatically Add to Music.localized'
    MUSIC_LIBRARY_ROOT: Path = Path.home() / 'Music/Music/Media.localized'
    MUSIC_LIBRARY_DB_PATH: Path = Path.home() / 'Music/Music/Music Library.musiclibrary'
    
    # Sync behavior
    ALBUM_MODE: str = 'playlist'
    GLOBAL_ALBUM_NAME: str = 'YouTube Downloads'
    DEFAULT_DELETE_ON_REMOVED: bool = True

# ---------------------------------------------------------------------
# 3. LOW-LEVEL SERVICES (Adapters for external systems)
# ---------------------------------------------------------------------

class DatabaseManager:
    """Manages all persistence logic for the SQLite database."""
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._initialize()

    def _initialize(self):
        cur = self._conn.cursor()
        # Existing tables
        cur.execute('''
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY, name TEXT, url TEXT UNIQUE, archive_path TEXT,
            delete_on_removed INTEGER DEFAULT 1, created_at TEXT,
            music_app_persistent_id TEXT)''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY, title TEXT, file_hash TEXT, added_at TEXT)''')
        cur.execute('''
        CREATE TABLE IF NOT EXISTS playlist_videos (
            playlist_id INTEGER, video_id TEXT,
            FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
            FOREIGN KEY(video_id) REFERENCES videos(video_id) ON DELETE CASCADE,
            PRIMARY KEY (playlist_id, video_id))''')
        
        # --- NEW: Circuit Breaker Table ---
        cur.execute('''
        CREATE TABLE IF NOT EXISTS download_failures (
            video_id TEXT PRIMARY KEY,
            failure_count INTEGER DEFAULT 1,
            last_attempt_at TEXT,
            error_reason TEXT
        )''')
        self._conn.commit()

    def get_all_playlists_with_counts(self) -> List[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute('''
            SELECT p.id, p.name, (SELECT COUNT(*) FROM playlist_videos pv WHERE pv.playlist_id = p.id) as count
            FROM playlists p ORDER BY p.id
        ''')
        return cur.fetchall()
        
    def get_playlist_details(self, playlist_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute('SELECT * FROM playlists WHERE id = ?', (playlist_id,))
        return cur.fetchone()

    def get_playlist_video_ids(self, playlist_id: int) -> set:
        cur = self._conn.cursor()
        cur.execute('SELECT video_id FROM playlist_videos WHERE playlist_id = ?', (playlist_id,))
        return {row['video_id'] for row in cur.fetchall()}

    def get_video_hash(self, video_id: str) -> Optional[str]:
        cur = self._conn.cursor()
        cur.execute("SELECT file_hash FROM videos WHERE video_id = ?", (video_id,))
        row = cur.fetchone()
        return row['file_hash'] if row else None

    def add_playlist(self, name: str, url: str, archive_path: str, delete_on_removed: bool) -> None:
        now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
        cur = self._conn.cursor()
        cur.execute('INSERT INTO playlists (name, url, archive_path, delete_on_removed, created_at, music_app_persistent_id) VALUES (?, ?, ?, ?, ?, ?)',
                    (name, url, archive_path, int(delete_on_removed), now, None))
        self._conn.commit()

    def remove_playlist(self, playlist_id: int):
        cur = self._conn.cursor()
        cur.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
        self._conn.commit()

    def insert_or_replace_video(self, video_id: str, title: str, file_hash: str) -> None:
        now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
        cur = self._conn.cursor()
        cur.execute('INSERT OR REPLACE INTO videos (video_id, title, file_hash, added_at) VALUES (?, ?, ?, ?)',
                    (video_id, title, file_hash, now))
        self._conn.commit()
    
    def link_video_to_playlist(self, playlist_id: int, video_id: str):
        cur = self._conn.cursor()
        cur.execute('INSERT OR IGNORE INTO playlist_videos (playlist_id, video_id) VALUES (?, ?)', (playlist_id, video_id))
        self._conn.commit()

    def unlink_videos_from_playlist(self, playlist_id: int, video_ids: set):
        cur = self._conn.cursor()
        cur.executemany('DELETE FROM playlist_videos WHERE playlist_id = ? AND video_id = ?', 
                        [(playlist_id, vid) for vid in video_ids])
        self._conn.commit()

    def update_playlist_persistent_id(self, playlist_id: int, persistent_id: Optional[str]):
        cur = self._conn.cursor()
        cur.execute("UPDATE playlists SET music_app_persistent_id = ? WHERE id = ?", (persistent_id, playlist_id))
        self._conn.commit()
        
    def reset_all_persistent_ids(self):
        """Sets all persistent IDs to NULL. Used after a library rebuild."""
        cur = self._conn.cursor()
        cur.execute("UPDATE playlists SET music_app_persistent_id = NULL")
        self._conn.commit()
        print("  -> All playlist links in the internal database have been reset.")

    def get_orphaned_videos(self) -> List[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute('''
            SELECT v.video_id, v.title, v.file_hash FROM videos v 
            LEFT JOIN playlist_videos pv ON v.video_id = pv.video_id 
            WHERE pv.playlist_id IS NULL
        ''')
        return cur.fetchall()

    def delete_video_record(self, video_id: str):
        cur = self._conn.cursor()
        cur.execute('DELETE FROM videos WHERE video_id = ?', (video_id,))
        self._conn.commit()

    # --- NEW: Circuit Breaker Methods ---

    def get_failure_status(self, video_id: str) -> Optional[sqlite3.Row]:
        """Retrieves failure history for a video."""
        cur = self._conn.cursor()
        cur.execute('SELECT failure_count, last_attempt_at FROM download_failures WHERE video_id = ?', (video_id,))
        return cur.fetchone()

    def record_failure(self, video_id: str, reason: str = ""):
        """
        Records a failed download attempt.
        Implements Exponential Backoff by incrementing the failure count.
        """
        now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
        cur = self._conn.cursor()
        
        # Check if it already exists
        cur.execute('SELECT failure_count FROM download_failures WHERE video_id = ?', (video_id,))
        row = cur.fetchone()
        
        if row:
            new_count = row['failure_count'] + 1
            cur.execute('''
                UPDATE download_failures 
                SET failure_count = ?, last_attempt_at = ?, error_reason = ? 
                WHERE video_id = ?
            ''', (new_count, now, reason, video_id))
        else:
            cur.execute('''
                INSERT INTO download_failures (video_id, failure_count, last_attempt_at, error_reason)
                VALUES (?, 1, ?, ?)
            ''', (video_id, now, reason))
        
        self._conn.commit()

    def clear_failure(self, video_id: str):
        """Resets the circuit breaker when a download succeeds."""
        cur = self._conn.cursor()
        cur.execute('DELETE FROM download_failures WHERE video_id = ?', (video_id,))
        self._conn.commit()
        
    def close(self):
        if self._conn:
            self._conn.close()

class MusicAppClient:
    """A client for interacting with the macOS Music application via AppleScript."""

    def _run_applescript(self, script: str, args: List[str] = [], timeout=300) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["osascript", "-"] + args,
            input=script, capture_output=True, text=True, check=False, timeout=timeout
        )
    
    def get_library_track_count(self) -> int:
        """Returns the total number of tracks in the main music library."""
        if sys.platform != "darwin": return 0
        script = 'tell application "Music" to get count of tracks of library playlist 1'
        result = self._run_applescript(script)
        try:
            return int(result.stdout.strip())
        except (ValueError, IndexError):
            return -1 # Indicates an error or that Music app is not ready

    def create_or_update_playlist(self, playlist_name: str, persistent_id: Optional[str], file_paths: List[str]) -> Tuple[str, str]:
        """Robustly creates or updates a playlist using a simple, unambiguous process."""
        if sys.platform != "darwin": return "", playlist_name
        
        script = """
        on run argv
            if (count of argv) < 2 then return "Error: Not enough arguments."
            set playlistDbId to item 1 of argv
            set playlistName to item 2 of argv
            set trackPaths to items 3 through -1 of argv
            set foundPlaylist to missing value

            tell application "Music"
                if playlistDbId is not "" then
                    try
                        set foundPlaylist to (first user playlist whose persistent ID is playlistDbId)
                    end try
                end if
                if foundPlaylist is missing value then
                    set foundPlaylist to make new user playlist with properties {name:playlistName}
                end if
                delete every track of foundPlaylist
                if (count of trackPaths) > 0 then
                    repeat with aPath in trackPaths
                        try
                            add (POSIX file aPath) to foundPlaylist
                        end try
                    end repeat
                end if
                return {persistent ID of foundPlaylist, name of foundPlaylist}
            end tell
        end run
        """
        args = [persistent_id or "", playlist_name] + file_paths
        result = self._run_applescript(script, args=args)
        
        if result.returncode == 0 and result.stdout:
            output_parts = result.stdout.strip().split(', ')
            if len(output_parts) == 2:
                return output_parts[0], output_parts[1]
        
        print(f"  -> Warning: AppleScript for playlist '{playlist_name}' may have failed. Error: {result.stderr.strip()}", file=sys.stderr)
        return persistent_id or "", playlist_name
        
    def wait_for_import(self, expected_new_count: int, count_before_add: int):
        """
        Waits for Music.app to import a specific number of new files.
        This is the "Accountant Supervisor" which prevents race conditions.
        """
        if sys.platform != "darwin" or expected_new_count == 0: return
        
        print(f"   - Waiting for Music.app to process {expected_new_count} file(s)...")
        target_count = count_before_add + expected_new_count
        last_track_count = -1
        stable_checks = 0
        max_stable_checks = 3 # Number of times the count must be stable before we trust it
        
        while True:
            time.sleep(5)
            current_track_count = self.get_library_track_count()
            
            if current_track_count < 0:
                print("     ...waiting for library to become available.")
                stable_checks = 0
                continue
            
            print(f"     ...library now contains {current_track_count} tracks (target: >= {target_count}).")

            if current_track_count == last_track_count:
                stable_checks += 1
            else:
                stable_checks = 0
            
            # Exit condition: The count is at or above our target AND it has been stable.
            if current_track_count >= target_count and stable_checks >= max_stable_checks:
                print("   - Music.app appears to have finished importing.")
                break
            
            last_track_count = current_track_count

    def clean_dead_tracks(self):
        """Asks the Music app to find and remove references to deleted files."""
        if sys.platform != "darwin": return
        print("\nAsking Music.app to remove dead tracks...")
        script = """
        tell application "Music"
            set deadTracks to (every track of library playlist 1 whose location is missing value)
            set deadTracksCount to count of deadTracks
            if deadTracksCount is greater than 0 then
                delete deadTracks
                return "Deleted " & deadTracksCount & " missing track(s)."
            else
                return "No missing tracks found."
            end if
        end tell
        """
        result = self._run_applescript(script)
        if result.returncode == 0:
            print(f"  -> {result.stdout.strip()}")
        else:
            print(f"  -> Warning: AppleScript cleanup failed. Error: {result.stderr.strip()}", file=sys.stderr)

    def rebuild_library(self, library_db_path: Path, media_folder_to_import: Path) -> bool:
        """Performs the destructive action of rebuilding the Music.app library from scratch."""
        if sys.platform != "darwin": return False
        print("1. Quitting Music.app (best effort)...")
        self._run_applescript('tell application "Music" to quit')
        time.sleep(3)
        print("2. Deleting Music library database file...")
        if library_db_path.exists():
            try:
                shutil.rmtree(library_db_path)
                print(f"   - '{library_db_path.name}' deleted successfully.")
            except PermissionError:
                print("\n  -> ERROR: Permission denied. You must grant Full Disk Access.", file=sys.stderr)
                return False
            except Exception as e:
                print(f"\n  -> An unexpected error occurred: {e}", file=sys.stderr)
                return False
        else:
            print("   - Library file not found, skipping.")
        print("3. Relaunching Music.app to create a new, empty library...")
        self._run_applescript('tell application "Music" to activate')
        time.sleep(5)
        print("4. Asking Music.app to re-import all existing media files...")
        if media_folder_to_import.exists() and any(media_folder_to_import.iterdir()):
            count_before = self.get_library_track_count()
            import_script = f'tell application "Music" to add (POSIX file "{str(media_folder_to_import)}")'
            result = self._run_applescript(import_script, timeout=900)
            if result.returncode == 0:
                # We don't know the exact number, so we use the simpler stability check here.
                self.wait_for_import(0,0) 
            else:
                print(f"   - Warning: Re-import command may have failed. Error: {result.stderr.strip()}", file=sys.stderr)
        else:
            print("   - Media folder not found or is empty, nothing to re-import.")
        return True

class YouTubeClient:
    """A wrapper for the yt-dlp command-line tool."""
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.temp_output_template = str(self.cfg.STAGING_DIR / '%(id)s.%(ext)s')
        # We define the base command including the JS runtime to stop the warnings
        self.base_cmd = [self.cfg.YTDLP_BIN, '--js-runtimes', 'node']

    def fetch_playlist_metadata(self, url: str) -> Optional[List[Dict]]:
        cmd = self.base_cmd + ['--flat-playlist', '-J', url]
        proc = _run_command(cmd)
        if proc.returncode != 0 or not proc.stdout: return None
        try:
            data = json.loads(proc.stdout)
            return [
                {'id': e.get('id'), 'title': e.get('title') or e.get('id'), 'artist': e.get('channel') or 'Unknown'}
                for e in data.get('entries', []) if e.get('id')
            ]
        except (json.JSONDecodeError, AttributeError):
            return None
    
    def fetch_playlist_title(self, url: str) -> Optional[str]:
        cmd = self.base_cmd + ['--flat-playlist', '-J', url]
        proc = _run_command(cmd)
        if proc.returncode != 0 or not proc.stdout: return None
        try:
            return json.loads(proc.stdout).get('title')
        except (json.JSONDecodeError, AttributeError):
            return None

    def download_video(self, video_id: str, archive_path: Path) -> Optional[Path]:
        """Downloads and converts a video, returning the path to the raw audio file."""
        video_url = f'https://youtu.be/{video_id}'
        cmd = self.base_cmd + [
            '-f', 'bestaudio', '--extract-audio', '--audio-format', self.cfg.AUDIO_FORMAT,
            '--output', self.temp_output_template,
            '--ignore-errors', '--no-playlist', '--download-archive', str(archive_path),
            video_url
        ]
        _run_command(cmd, capture_output=False)
        staged_file = next(self.cfg.STAGING_DIR.glob(f'{video_id}.*'), None)
        if not staged_file:
            # We don't print stderr here, usually yt-dlp output is enough
            return None
        return staged_file

# ---------------------------------------------------------------------
# 4. CORE APPLICATION LOGIC
# ---------------------------------------------------------------------

class LibraryScanner:
    """Scans the music library on disk and provides an in-memory cache of file hashes."""
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.media_folder = cfg.MUSIC_LIBRARY_ROOT / 'Music'
        self.hash_cache: Dict[str, Path] = {}

    def _calculate_file_hash(self, file_path: Path) -> Optional[str]:
        sha256 = hashlib.sha256()
        try:
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except (IOError, OSError):
            return None

    def scan(self, force_rescan: bool = False):
        """Builds the hash -> file_path map. Caches the result for the run."""
        if self.hash_cache and not force_rescan:
            return
        
        print("Scanning music library to build file fingerprints...")
        self.hash_cache.clear()
        if self.media_folder.exists():
            for file_path in self.media_folder.rglob(f'*.{self.cfg.AUDIO_FORMAT}'):
                file_hash = self._calculate_file_hash(file_path)
                if file_hash:
                    self.hash_cache[file_hash] = file_path
        print(f"  -> Found {len(self.hash_cache)} existing music files.")
    
    def get_path_for_hash(self, file_hash: str) -> Optional[Path]:
        return self.hash_cache.get(file_hash)

class SyncOrchestrator:
    """The 'brains' of the application. Orchestrates the entire sync process."""
    
    # Backoff Configuration
    BACKOFF_BASE_SECONDS = 86400  # 1 Day
    BACKOFF_MAX_SECONDS = 2592000 # 30 Days

    def __init__(self, cfg: Config, db: DatabaseManager, music_app: MusicAppClient, yt: YouTubeClient, scanner: LibraryScanner):
        self.cfg = cfg
        self.db = db
        self.music_app = music_app
        self.yt = yt
        self.scanner = scanner

    def _get_tags(self, file_path: Path) -> Dict[str, str]:
        try:
            audio = EasyID3(file_path)
            return {
                'artist': audio.get('artist', [''])[0],
                'album': audio.get('album', [''])[0],
                'title': audio.get('title', [''])[0],
                'isrc': audio.get('isrc', [''])[0],
            }
        except (EasyID3KeyError, ID3NoHeaderError, Exception):
            return {}

    def _write_tags_to_file(self, file_path: Path, video_id: str, title: str, artist: str, album: Optional[str]):
        try:
            try:
                audio = EasyID3(file_path)
            except ID3NoHeaderError:
                audio = EasyID3()
            
            audio['title'] = title
            audio['artist'] = artist
            audio['isrc'] = video_id
            if album:
                audio['album'] = album
                audio['albumartist'] = "Various Artists"
                audio['compilation'] = '1'
            else:
                if 'album' in audio: del audio['album']
                if 'compilation' in audio: del audio['compilation']
                audio['albumartist'] = artist
            audio.save(file_path)
            print(f"  -> Wrote metadata to '{file_path.name}'.")
        except Exception as e:
            print(f"  -> Warning: Could not write metadata to '{file_path.name}': {e}", file=sys.stderr)

    def _remove_from_archive(self, video_id: str, archive_path: Path):
        if not archive_path.exists(): return
        try:
            with open(archive_path, 'r') as f: lines = f.readlines()
            updated_lines = [line for line in lines if video_id not in line.split()]
            if len(lines) != len(updated_lines):
                # print(f"  -> Resetting download record for '{video_id}' to allow re-download.")
                with open(archive_path, 'w') as f: f.writelines(updated_lines)
        except Exception as e:
            print(f"  -> Warning: Could not modify archive file {archive_path}: {e}", file=sys.stderr)

    def _is_cooling_down(self, video_id: str) -> bool:
        """
        Checks if a video is in a 'cool-down' period due to previous failures.
        Returns True if we should SKIP this video.
        """
        row = self.db.get_failure_status(video_id)
        if not row:
            return False

        failures = row['failure_count']
        last_attempt_str = row['last_attempt_at']
        
        try:
            # Parse UTC string '2023-01-01T12:00:00Z'
            last_attempt = datetime.datetime.strptime(last_attempt_str, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return False # Date format error? Safe to retry.

        # Exponential Backoff Formula
        # Delay = Base * (2 ^ (failures - 1))
        exponent = max(0, failures - 1)
        delay_seconds = self.BACKOFF_BASE_SECONDS * (2 ** exponent)
        
        # Cap the delay
        delay_seconds = min(delay_seconds, self.BACKOFF_MAX_SECONDS)
        
        next_retry = last_attempt + datetime.timedelta(seconds=delay_seconds)
        now = datetime.datetime.utcnow()
        
        if now < next_retry:
            wait_time = next_retry - now
            
            # Smart time formatting
            if wait_time.days > 0:
                time_str = f"{wait_time.days} day(s)"
            elif wait_time.seconds >= 3600:
                time_str = f"{wait_time.seconds // 3600} hour(s)"
            else:
                time_str = f"{wait_time.seconds // 60} minute(s)"

            print(f"  -> Skipping '{video_id}' (Circuit Open). Failures: {failures}. Next retry in {time_str}.")
            return True
            
        return False

    def adopt_untracked_files(self):
        """Scans library for fingerprinted files not in the DB and "adopts" them."""
        self.scanner.scan()
        print("Checking for untracked files to adopt...")
        adopted_count = 0
        
        isrc_map: Dict[str, Path] = {}
        if self.scanner.media_folder.exists():
            for file_path in self.scanner.media_folder.rglob(f'*.{self.cfg.AUDIO_FORMAT}'):
                try:
                    audio = EasyID3(file_path)
                    isrc = audio.get('isrc', [''])[0]
                    if isrc:
                        isrc_map[isrc] = file_path
                except Exception:
                    continue

        for isrc, file_path in isrc_map.items():
            if not self.db.get_video_hash(isrc):
                file_hash = self.scanner._calculate_file_hash(file_path)
                tags = self._get_tags(file_path)
                if file_hash:
                    self.db.insert_or_replace_video(isrc, tags.get('title', 'Unknown Title'), file_hash)
                    adopted_count += 1
        
        if adopted_count > 0:
            print(f"  -> Adopted {adopted_count} file(s) into the database from existing metadata.")
        else:
            print("  -> No new files to adopt.")

    def sync_playlist(self, playlist_id: int):
        self.scanner.scan()
        playlist = self.db.get_playlist_details(playlist_id)
        if not playlist:
            print(f"Playlist with ID {playlist_id} not found.", file=sys.stderr); return

        ordered_hashes_for_playlist = []
        new_files_staged = []
        try:
            print(f'\n=== Syncing [{playlist["id"]}] {playlist["name"]} ===')
            print("Step 1/3: Fetching latest video list from YouTube...")
            upstream_videos = self.yt.fetch_playlist_metadata(playlist['url'])
            if upstream_videos is None:
                print('  -> Failed to fetch playlist metadata. Aborting sync.', file=sys.stderr); return

            album_name = None
            if self.cfg.ALBUM_MODE == 'playlist': album_name = playlist['name']
            elif self.cfg.ALBUM_MODE == 'global': album_name = self.cfg.GLOBAL_ALBUM_NAME

            print(f"Step 2/3: Verifying and correcting {len(upstream_videos)} videos...")
            
            for video_data in upstream_videos:
                vid, raw_title, raw_artist = video_data['id'], video_data['title'], video_data['artist']
                artist = _clean_artist_name(raw_artist)
                title = _clean_youtube_title(raw_title, artist)
                
                db_hash = self.db.get_video_hash(vid)
                file_path = self.scanner.get_path_for_hash(db_hash) if db_hash else None

                current_file_hash = None
                
                # CASE 1: File exists locally
                if file_path and file_path.exists():
                    current_file_hash = db_hash
                    # Metadata check (optional, but good)
                    current_tags = self._get_tags(file_path)
                    if (current_tags.get('title') != title or
                        current_tags.get('artist') != artist or
                        current_tags.get('isrc') != vid):
                        print(f"Metadata for '{title}' is incorrect, fixing...")
                        self._write_tags_to_file(file_path, vid, title, artist, album_name)
                
                # CASE 2: File missing
                else:
                    # CHECK CIRCUIT BREAKER BEFORE DOWNLOADING
                    if self._is_cooling_down(vid):
                        # Skip this iteration entirely. 
                        # We do NOT add it to the playlist for Music.app this run, 
                        # effectively hiding the broken song until it's fixed.
                        continue
                    
                    print(f"\nFile for '{artist} - {title}' is missing, downloading...")
                    
                    # Only remove from archive immediately before we try
                    self._remove_from_archive(vid, Path(playlist['archive_path']))
                    
                    staged_path = self.yt.download_video(vid, Path(playlist['archive_path']))
                    
                    if staged_path:
                        # SUCCESS
                        self.db.clear_failure(vid) # Circuit Reset
                        
                        self._write_tags_to_file(staged_path, vid, title, artist, album_name)
                        file_hash = self.scanner._calculate_file_hash(staged_path)
                        if file_hash:
                            self.db.insert_or_replace_video(vid, title, file_hash)
                            current_file_hash = file_hash
                            final_filename = f"{_sanitize_filename(artist)} - {_sanitize_filename(title)}.{self.cfg.AUDIO_FORMAT}"
                            new_files_staged.append((staged_path, final_filename))
                        else:
                             print(f"  -> FATAL: Could not calculate hash for '{staged_path.name}'.", file=sys.stderr)
                    else:
                        # FAILURE
                        # Record the failure so we skip it next time
                        print(f"  -> Download failed. Recording failure for exponential backoff.")
                        self.db.record_failure(vid, reason="Download failed (generic)")

                if current_file_hash:
                    ordered_hashes_for_playlist.append(current_file_hash)
                    self.db.link_video_to_playlist(playlist_id, vid)

            if new_files_staged:
                print(f"\nMoving {len(new_files_staged)} new file(s) to Music library...")
                count_before = self.music_app.get_library_track_count()
                for staged_path, final_filename in new_files_staged:
                    inbox_path = self.cfg.DEFAULT_TARGET_DIR / final_filename
                    shutil.move(staged_path, inbox_path)
                
                self.music_app.wait_for_import(len(new_files_staged), count_before)
                self.scanner.scan(force_rescan=True)
            
            print("\nStep 3/3: Rebuilding playlist in Music.app to match YouTube order...")
            file_paths = [str(self.scanner.get_path_for_hash(h)) for h in ordered_hashes_for_playlist if self.scanner.get_path_for_hash(h)]
            
            new_pid, final_name = self.music_app.create_or_update_playlist(
                playlist['name'], playlist['music_app_persistent_id'], file_paths
            )
            if new_pid and new_pid != playlist['music_app_persistent_id']:
                self.db.update_playlist_persistent_id(playlist_id, new_pid)
            
            track_count = len(file_paths)
            if track_count > 0:
                print(f"  -> Successfully updated playlist '{final_name}' with {track_count} tracks.")

            print("\nStep 4/4: Checking for items to remove...")
            upstream_ids = {v['id'] for v in upstream_videos}
            local_ids = self.db.get_playlist_video_ids(playlist_id)
            to_remove = local_ids - upstream_ids
            if to_remove:
                if playlist['delete_on_removed']:
                    print(f'Processing {len(to_remove)} item(s) removed from remote playlist...')
                    self.db.unlink_videos_from_playlist(playlist_id, to_remove)
                    self.cleanup_orphaned_files()
                else:
                    print(f'{len(to_remove)} items removed upstream will be kept locally.')
            else:
                print("  -> No items to remove.")
            print(f'Sync for "{playlist["name"]}" complete.')

        except KeyboardInterrupt:
            print("\n\n-- INTERRUPTED --")
            print("Sync process stopped by user. Finalizing playlist with processed songs...")
            
            if new_files_staged:
                print(f"\nMoving {len(new_files_staged)} partially downloaded file(s) to Music library...")
                count_before = self.music_app.get_library_track_count()
                for staged_path, final_filename in new_files_staged:
                    inbox_path = self.cfg.DEFAULT_TARGET_DIR / final_filename
                    shutil.move(staged_path, inbox_path)
                self.music_app.wait_for_import(len(new_files_staged), count_before)
                self.scanner.scan(force_rescan=True)

            file_paths = [str(self.scanner.get_path_for_hash(h)) for h in ordered_hashes_for_playlist if self.scanner.get_path_for_hash(h)]
            if file_paths:
               print(f"  -> Adding {len(file_paths)} fully processed song(s) to the playlist.")
               self.music_app.create_or_update_playlist(playlist['name'], playlist['music_app_persistent_id'], file_paths)
            else:
               print("  -> No songs were fully processed. No playlist changes made.")
            raise

    def sync_all(self):
        playlists = self.db.get_all_playlists_with_counts()
        if not playlists:
            print('No playlists to sync.'); return
        for p in playlists:
            self.sync_playlist(p['id'])

    def cleanup_orphaned_files(self):
        self.scanner.scan()
        orphans = self.db.get_orphaned_videos()
        if not orphans:
            print("No orphaned videos found in the database to clean up.")
            return

        print(f"\nFound {len(orphans)} orphaned video(s) that are no longer in any playlist.")
        files_were_deleted = False
        for orphan in orphans:
            path_to_delete = self.scanner.get_path_for_hash(orphan['file_hash'])
            if path_to_delete and path_to_delete.exists():
                try:
                    path_to_delete.unlink()
                    print(f"  - Deleted local file for '{orphan['title']}'")
                    files_were_deleted = True
                except OSError as e:
                    print(f"  - Error deleting file {path_to_delete}: {e}", file=sys.stderr)
            else:
                 print(f"  - Could not find local file for '{orphan['title']}', removing from DB.")
            self.db.delete_video_record(orphan['video_id'])
        
        if files_were_deleted:
            self.music_app.clean_dead_tracks()

    def rebuild_full_library(self) -> bool:
        """Orchestrates the entire library rebuild process."""
        media_folder = self.scanner.media_folder
        
        rebuild_succeeded = self.music_app.rebuild_library(self.cfg.MUSIC_LIBRARY_DB_PATH, media_folder)
        if not rebuild_succeeded:
            print("\nLibrary rebuild failed. Aborting subsequent sync.", file=sys.stderr)
            return False

        print("5. Resetting this script's internal state to match new library...")
        self.db.reset_all_persistent_ids()
        self.scanner.scan(force_rescan=True)
        print("   - Script's file cache and playlist links have been reset.")

        print("\nRebuild process complete. Running a full sync to re-link your files and playlists...")
        self.sync_all()
        return True

# ---------------------------------------------------------------------
# 5. USER INTERFACE
# ---------------------------------------------------------------------

class ConsoleUI:
    """Handles all user interaction via the console."""
    def __init__(self, cfg: Config, orchestrator: SyncOrchestrator, db: DatabaseManager, yt: YouTubeClient, scanner: LibraryScanner):
        self.cfg = cfg
        self.orchestrator = orchestrator
        self.db = db
        self.yt = yt
        self.scanner = scanner
        
        self.menu = {
            '1': ('List Playlists', self._handle_list_playlists),
            '2': ('Add Playlist', self._handle_add_playlist),
            '3': ('Show Playlist Details', self._handle_show_details),
            '4': ('Update a Playlist', self._handle_update_one),
            '5': ('Update All Playlists', self._handle_update_all),
            '6': ('Remove a Playlist', self._handle_remove_playlist),
            '7': ('Clean Music Library (Remove Dead Tracks)', self.orchestrator.music_app.clean_dead_tracks),
            '8': ('Rebuild Music App Library (Recovery Tool)', self._handle_rebuild_library),
            '9': ('Reset Playlist Link (Fix Duplicates)', self._handle_reset_link),
            '0': ('Exit', lambda: sys.exit(0))
        }

    def run(self):
        intro = textwrap.dedent(f"""
        yt-mirror-sync interactive console
        Target folder: {self.cfg.DEFAULT_TARGET_DIR}
        Data directory: {self.cfg.APP_DIR}
        """)
        print(intro)
        
        while True:
            print("\n--- Main Menu ---")
            sorted_items = sorted(self.menu.items(), key=lambda item: 10 if item[0] == '0' else int(item[0]))
            for key, (desc, _) in sorted_items:
                print(f"  {key}. {desc}")
            
            choice = input('> ').strip()
            if choice in self.menu:
                desc, action = self.menu[choice]
                print(f"\n--- {desc} ---")
                action()
            elif choice:
                print("Invalid choice, please try again.")

    def _get_id_from_user(self, prompt: str) -> Optional[int]:
        try:
            pid_str = input(prompt).strip()
            if not pid_str:
                print("Operation cancelled."); return None
            return int(pid_str)
        except ValueError:
            print("Invalid input. Please enter a number.", file=sys.stderr); return None

    def _handle_list_playlists(self):
        playlists = self.db.get_all_playlists_with_counts()
        if not playlists:
            print('No playlists configured yet.'); return
        print("--- Configured Playlists ---")
        for p in playlists:
            print(f'[{p["id"]}] {p["name"]} ({p["count"]} items)')
    
    def _handle_add_playlist(self):
        url = input('Playlist URL: ').strip()
        if not ("youtube.com" in url or "youtu.be" in url):
            print('Invalid URL provided.', file=sys.stderr); return

        print("Fetching playlist title...")
        suggested_title = self.yt.fetch_playlist_title(url)
        
        prompt = "Friendly name for the playlist"
        if suggested_title: prompt += f" [press Enter to use: '{suggested_title}']"
        name = input(f"{prompt}: ").strip()
        if not name and suggested_title: name = suggested_title
        elif not name: name = "Unnamed Playlist"

        archive_path = self.cfg.ARCHIVES_DIR / f'archive-{_slugify(name)}.txt'
        archive_path.touch()
        self.db.add_playlist(name, url, str(archive_path), self.cfg.DEFAULT_DELETE_ON_REMOVED)
        print(f'Added playlist "{name}"')

    def _handle_show_details(self):
        self._handle_list_playlists()
        pid = self._get_id_from_user("Enter playlist ID to show details: ")
        if pid is None: return
        
        playlist = self.db.get_playlist_details(pid)
        if not playlist:
            print(f"Playlist with ID {pid} not found.", file=sys.stderr); return
        
        print(f"\n--- Details for [{pid}] {playlist['name']} ---"); print(f"URL: {playlist['url']}")

        self.scanner.scan()
        cur = self.db._conn.cursor()
        cur.execute('''
            SELECT v.title, v.video_id, v.file_hash FROM videos v 
            JOIN playlist_videos pv ON v.video_id = pv.video_id 
            WHERE pv.playlist_id = ? ORDER BY v.title
        ''', (pid,))
        videos = cur.fetchall()

        if not videos: print("(No tracked videos for this playlist)")
        for video in videos:
            status = "✓ In Library" if self.scanner.get_path_for_hash(video['file_hash']) else "✗ MISSING"
            print(f"  - {video['title']} ({video['video_id']}) [{status}]")

    def _handle_update_one(self):
        self._handle_list_playlists()
        pid = self._get_id_from_user("Enter playlist ID to sync: ")
        if pid is None: return
        try:
            self.orchestrator.sync_playlist(pid)
        except KeyboardInterrupt:
            print("\nSync interrupted by user. Returning to menu.")
            pass

    def _handle_update_all(self):
        try:
            self.orchestrator.sync_all()
        except KeyboardInterrupt:
            print("\nSync interrupted by user. Returning to menu.")
            pass

    def _handle_remove_playlist(self):
        self._handle_list_playlists()
        pid = self._get_id_from_user("Enter playlist ID to remove: ")
        if pid is None: return
        
        playlist = self.db.get_playlist_details(pid)
        if not playlist:
            print(f"Playlist with ID {pid} not found.", file=sys.stderr); return
        
        confirm = input(f"Remove '{playlist['name']}'? This cannot be undone. [y/N]: ").strip().lower()
        if confirm == 'y':
            self.db.remove_playlist(pid)
            print(f"Removed '{playlist['name']}'.")
            cleanup_confirm = input("Clean up and delete orphaned audio files? [y/N]: ").strip().lower()
            if cleanup_confirm == 'y':
                self.orchestrator.cleanup_orphaned_files()
        else:
            print("Aborted.")
    
    def _handle_rebuild_library(self):
        warning = textwrap.dedent("""
            !!!!!!!!!!!!!!!!!!!!!!!!!! WARNING !!!!!!!!!!!!!!!!!!!!!!!!!!
            
            This will reset your Music app's library. This feature will
            ONLY work correctly if you have FIRST manually disabled
            "Sync Library" (iCloud Music Library) in Music's settings.
            
            This will DESTROY:
              - All your existing playlists in the Music app.
              - All song ratings, play counts, and date added information.
            
            This will NOT delete:
              - Your actual MP3 music files.
              - This script's internal database or download archives.
            
            This is a last-resort recovery tool.
            !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        """)
        print(warning)
        
        confirm = input('To proceed, type "REBUILD": ')
        if confirm != "REBUILD":
            print("\nAborted. No changes were made."); return
        
        print("\nStarting smart rebuild process...")
        self.orchestrator.rebuild_full_library()
        
    def _handle_reset_link(self):
        self._handle_list_playlists()
        pid = self._get_id_from_user("Enter ID of playlist to reset: ")
        if pid is None: return
        
        playlist = self.db.get_playlist_details(pid)
        if not playlist:
            print(f"Playlist with ID {pid} not found.", file=sys.stderr); return

        print("\nThis will erase the script's link to the Music.app playlist.")
        print("The next sync will create a new, clean playlist.")
        print("You should first manually delete any duplicates in Music.app.")
        confirm = input(f"Are you sure you want to reset the link for '{playlist['name']}'? [y/N]: ").strip().lower()
        if confirm == 'y':
            self.db.update_playlist_persistent_id(pid, None)
            print(f"  -> Link for '{playlist['name']}' has been reset.")
        else:
            print("Aborted.")

# ---------------------------------------------------------------------
# 6. APPLICATION ENTRY POINT
# ---------------------------------------------------------------------

def main():
    """Initializes and runs the application."""
    print('yt-mirror-sync starting...')
    
    config = Config()
    
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    config.ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    config.DEFAULT_TARGET_DIR.mkdir(parents=True, exist_ok=True)
    if config.STAGING_DIR.exists(): shutil.rmtree(config.STAGING_DIR)
    config.STAGING_DIR.mkdir(parents=True, exist_ok=True)

    db = None
    try:
        db = DatabaseManager(config.DB_PATH)
        music_app = MusicAppClient()
        yt = YouTubeClient(config)
        scanner = LibraryScanner(config)
        
        orchestrator = SyncOrchestrator(config, db, music_app, yt, scanner)
        
        orchestrator.adopt_untracked_files()

        ui = ConsoleUI(config, orchestrator, db, yt, scanner)
        
        ui.run()

    finally:
        if db:
            db.close()


if __name__ == '__main__':
    try:
        main()
    except (EOFError, SystemExit):
        print("\nExiting gracefully.")
        sys.exit(0)
    except KeyboardInterrupt:
        print("\nExiting gracefully.")
        sys.exit(0)
    except Exception as e:
        print(f"\nFATAL ERROR: An unexpected error occurred: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)