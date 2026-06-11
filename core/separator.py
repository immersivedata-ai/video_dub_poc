import os
import sys
import subprocess
from typing import Tuple
from core.logger import get_logger

log = get_logger("separator")

def separate_audio(audio_path: str, output_dir: str = "audio/separated") -> Tuple[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    log.info("Separating vocals from background (Demucs)")
    log.info("Input: %s", audio_path)

    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "--mp3",
        "--segment", "10",
        "-o", output_dir,
        audio_path
    ]
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("Demucs stderr: %s", result.stderr[-500:] if result.stderr else "none")

    audio_basename = os.path.splitext(os.path.basename(audio_path))[0]
    demucs_output = os.path.join(output_dir, "htdemucs", audio_basename)
    vocals_path = os.path.join(demucs_output, "vocals.mp3")
    background_path = os.path.join(demucs_output, "no_vocals.mp3")

    if os.path.exists(vocals_path) and os.path.exists(background_path):
        log.info("Vocals: %s", vocals_path)
        log.info("Background: %s", background_path)
        return vocals_path, background_path

    log.error("Separation failed. Expected: %s / %s", vocals_path, background_path)
    if os.path.exists(demucs_output):
        log.error("Found files: %s", os.listdir(demucs_output))
    raise FileNotFoundError("Demucs separation failed")
