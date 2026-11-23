"""Read one YouTube transcript and save it as a text file."""

import argparse
import re
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi


def video_id_from(value):
    parsed = urlparse(value)
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/").split("/")[0]
    video_id = parse_qs(parsed.query).get("v", [None])[0]
    return video_id or value.strip()


def main():
    parser = argparse.ArgumentParser(description="Save a YouTube video's transcript as text.")
    parser.add_argument("video", help="YouTube video URL or video ID")
    parser.add_argument("--output", default="transcript.txt", help="Output text file")
    args = parser.parse_args()

    video_id = video_id_from(args.video)
    transcript_list = YouTubeTranscriptApi().list(video_id)
    available = list(transcript_list)
    if not available:
        parser.error("This video has no available transcripts")

    transcript = next(
        (item for language in ("te", "en") for item in available
         if item.language_code == language),
        available[0],
    )
    lines = []
    for item in transcript.fetch().to_raw_data():
        text = re.sub(r"\s+", " ", item["text"]).strip()
        if text:
            minutes, seconds = divmod(int(item["start"]), 60)
            lines.append(f"[{minutes}:{seconds:02d}] {text}")

    with open(args.output, "w", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    print(f"Saved {len(lines)} transcript lines ({transcript.language_code}) to {args.output}")


if __name__ == "__main__":
    main()
