import subprocess
import os
from core.logger import get_logger

log = get_logger("audioextractor")

def extract_audio(video_path: str, output_audio_path: str):
    log.info("Extracting audio from %s", video_path)
    os.makedirs(os.path.dirname(output_audio_path), exist_ok=True)

    command = ["ffmpeg", "-y", "-i", video_path, output_audio_path]
    result = subprocess.run(command, capture_output=True, text=True)

    if not os.path.exists(output_audio_path):
        log.error("FFmpeg failed: %s", result.stderr.strip())
        raise RuntimeError("Audio extraction failed")

    log.info("Audio extracted: %s", output_audio_path)
    return output_audio_path
