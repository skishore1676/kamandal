"""
Module 1: Live Idea Scraper — daily trade idea capture from tastytrade content.

Usage:
    python -m vol_crush.idea_scraper --mode live
    python -m vol_crush.idea_scraper --mode transcript --file path/to/file.txt
"""

from vol_crush.idea_scraper.scraper import (
    extract_ideas_from_transcript,
    capture_from_audio_file,
    capture_from_transcript_file,
    record_audio,
)

__all__ = [
    "extract_ideas_from_transcript",
    "capture_from_audio_file",
    "capture_from_transcript_file",
    "record_audio",
]

