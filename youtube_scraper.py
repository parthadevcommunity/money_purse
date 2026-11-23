import argparse
import concurrent.futures
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
from html import unescape
from pathlib import Path
from deep_translator import GoogleTranslator
import youtube_transcript_api
import yt_dlp

thread_local = threading.local()
DB_FILE = "pipeline_history.db"
COOKIE_FILE = "youtube_cookies.txt"
RATE_LIMITED = threading.Event()
FALLBACK_LOCK = threading.Lock()


def get_transcript_api():
    """Create a transcript client, using an optional proxy from the environment."""
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if http_proxy or https_proxy:
        from youtube_transcript_api.proxies import GenericProxyConfig

        proxy_config = GenericProxyConfig(http_url=http_proxy, https_url=https_proxy)
        return youtube_transcript_api.YouTubeTranscriptApi(proxy_config=proxy_config)
    return youtube_transcript_api.YouTubeTranscriptApi()


def fetch_transcript_with_ytdlp(video_id):
    """Fetch Telugu captions through yt-dlp, which can use the cookie file."""
    with tempfile.TemporaryDirectory(prefix="youtube_subtitles_") as temp_dir:
        options = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["te"],
            "subtitlesformat": "vtt/best",
            "outtmpl": str(Path(temp_dir) / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }
        if os.path.isfile(COOKIE_FILE):
            options["cookiefile"] = COOKIE_FILE
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if proxy:
            options["proxy"] = proxy

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        subtitle_files = list(Path(temp_dir).glob("*.vtt"))
        if not subtitle_files:
            raise RuntimeError("yt-dlp did not find a Telugu subtitle track")

        content = subtitle_files[0].read_text(encoding="utf-8", errors="replace")
        blocks = []
        for block in re.split(r"\n\s*\n", content):
            lines = block.splitlines()
            timing_index = next(
                (i for i, line in enumerate(lines) if "-->" in line), None
            )
            if timing_index is None:
                continue
            start_text = lines[timing_index].split("-->", 1)[0].strip().split()[0]
            parts = start_text.replace(",", ".").split(":")
            try:
                start = sum(float(part) * (60 ** (len(parts) - index - 1)) for index, part in enumerate(parts))
            except ValueError:
                continue
            caption = " ".join(lines[timing_index + 1 :])
            caption = unescape(re.sub(r"<[^>]+>", "", caption)).strip()
            if caption:
                blocks.append({"start": start, "text": caption})
        if not blocks:
            raise RuntimeError("Telugu subtitle file was empty or unreadable")
        return blocks


def get_db_connection():
    if not hasattr(thread_local, "connection"):
        thread_local.connection = sqlite3.connect(DB_FILE)
        thread_local.connection.execute("PRAGMA journal_mode=WAL;")
    return thread_local.connection


def initialize_system_database():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS video_history (
            video_id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            status TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    conn.commit()
    conn.close()


def get_already_processed_ids():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT video_id FROM video_history WHERE status = 'Success'"
    )
    rows = cursor.fetchall()
    conn.close()
    return {row[0] for row in rows}


def log_video_status(video_id, title, url, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO video_history (video_id, title, url, status)
        VALUES (?, ?, ?, ?)
    """,
        (video_id, title, url, status),
    )
    conn.commit()


def get_all_playlist_entries(playlist_url):
    print(f"[*] Fetching full structural layout for playlist: {playlist_url}")
    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
    }
    if os.path.exists(COOKIE_FILE):
        ydl_opts["cookiefile"] = COOKIE_FILE
        print("[+] Cookies attached to meta extraction pipeline.")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            result = ydl.extract_info(playlist_url, download=False)
            if "entries" in result:
                return [
                    {
                        "id": e.get("id"),
                        "title": e.get("title") or e.get("id"),
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    }
                    for e in result["entries"]
                    if e and e.get("id")
                ]
        except Exception as e:
            print(f"[-] Meta extraction failed: {e}")
            return None
    return []


# =====================================================================
# NO-CRASH PARALLEL TRANSCRIPT CORE
# =====================================================================
def process_single_video(video, output_folder):
    """
    Worker core: Connects to a thread-isolated database, fetches transcripts
    using explicit library paths to bypass naming bugs, and translates them.
    """
    video_id = video["id"]
    title = video["title"]
    url = video["url"]

    if RATE_LIMITED.is_set():
        return

    # Jitter Anti-Ban Defense
    time.sleep(random.uniform(3.0, 7.0))
    if RATE_LIMITED.is_set():
        return

    try:
        # STEP 1: Call via the structural library path.
        # This prevents any object attribute or naming mixups inside your terminal engine.
        try:
            transcript = get_transcript_api().fetch(video_id, languages=["te"])
            raw_blocks = transcript.to_raw_data()
        except Exception as transcript_error:
            if type(transcript_error).__name__ not in {"IpBlocked", "RequestBlocked"}:
                raise
            with FALLBACK_LOCK:
                if RATE_LIMITED.is_set():
                    return
                print(f"    [!] Transcript API IP-blocked for {video_id}; trying yt-dlp captions with cookies.")
                try:
                    raw_blocks = fetch_transcript_with_ytdlp(video_id)
                except Exception as fallback_error:
                    fallback_detail = str(fallback_error)
                    if "429" in fallback_detail or "Too Many Requests" in fallback_detail:
                        RATE_LIMITED.set()
                    raise

        combined_text = ""
        full_english_transcript = []
        translator = GoogleTranslator(source="te", target="en")

        print(f"    [*] Multi-Thread Worker: Extracting & translating [{title[:25]}...]")

        for block in raw_blocks:
            text_chunk = block["text"].strip()
            start_time = block["start"]

            if not text_chunk or (
                text_chunk.startswith("[") and text_chunk.endswith("]")
            ):
                continue

            combined_text += f" {text_chunk}"

            if len(combined_text) > 1800:
                translated_chunk = translator.translate(combined_text.strip())
                minutes = int(start_time) // 60
                seconds = int(start_time) % 60
                full_english_transcript.append(
                    f"[{minutes}:{seconds:02d}] {translated_chunk}"
                )
                combined_text = ""

        if combined_text.strip():
            translated_chunk = translator.translate(combined_text.strip())
            full_english_transcript.append(f"[End] {translated_chunk}")

        if not full_english_transcript:
            log_video_status(video_id, title, url, "Skipped - Empty Content")
            return

        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
        filename = f"{output_folder}/{video_id}_{clean_title[:30].strip()}.md"

        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# {title}\nSource: {url}\n\n## Transcript\n")
            f.write("\n".join(full_english_transcript))

        log_video_status(video_id, title, url, "Success")

    except youtube_transcript_api.TranscriptsDisabled:
        log_video_status(video_id, title, url, "Skipped - Subtitles Missing")
        print(f"[-] Subtitles completely unavailable on YouTube server for [{title[:25]}]")
    except Exception as e:
        # Keep the exception type and enough detail to distinguish missing
        # captions from YouTube blocking the request or a network failure.
        detail = " ".join(str(e).split())
        if RATE_LIMITED.is_set():
            return
        if "429" in detail or "Too Many Requests" in detail:
            RATE_LIMITED.set()
            log_video_status(video_id, title, url, "Paused - HTTP 429 rate limited")
            print(
                f"[!] YouTube returned HTTP 429 for {video_id}. Pausing the pipeline to avoid more requests. "
                "Wait for the limit to clear or configure a proxy with HTTPS_PROXY, then resume."
            )
            return
        error_msg = f"Failed [{type(e).__name__}]: {detail[:400]}"
        log_video_status(video_id, title, url, error_msg)
        print(f"[!] Transcript fetch failed for video {video_id}: {error_msg}")


def orchestrate_parallel_pipeline(
    playlist_url, output_folder="llm_knowledge_base", workers=1
):
    initialize_system_database()

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    all_videos = get_all_playlist_entries(playlist_url)
    if all_videos is None:
        print("[-] Playlist extraction failed; stopping without reporting the pipeline as up-to-date.")
        return
    processed_ids = get_already_processed_ids()

    new_videos = [v for v in all_videos if v["id"] not in processed_ids]

    print(
        f"\n[📊 Status] Total Playlist Videos: {len(all_videos)} | Finished: {len(processed_ids)} | Unprocessed: {len(new_videos)}"
    )

    if not new_videos:
        print("[🎉] System Up-To-Date. No new uploads discovered.")
        return

    print(f"[*] Initializing connection pool across {workers} pipeline workers...\n")
    RATE_LIMITED.clear()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = {
            executor.submit(process_single_video, video, output_folder): video
            for video in new_videos
        }
        concurrent.futures.wait(futures)

    if RATE_LIMITED.is_set():
        print("\n[!] Pipeline paused after HTTP 429. Wait for YouTube to clear the rate limit, then rerun.")
    else:
        print("\n[✓] Ingestion sequence finalized successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate Telugu YouTube playlist transcripts.")
    parser.add_argument("playlist_url", nargs="?", default=os.environ.get("YOUTUBE_PLAYLIST_URL"))
    parser.add_argument("--output", default="llm_knowledge_base")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if not args.playlist_url:
        parser.error("provide a playlist URL or set YOUTUBE_PLAYLIST_URL")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    orchestrate_parallel_pipeline(args.playlist_url, output_folder=args.output, workers=args.workers)
