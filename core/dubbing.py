import os
import subprocess
from typing import List, Dict, Any
from core.elevenlabs_client import ElevenLabsClient
from core.logger import get_logger

log = get_logger("dubbing")

def get_audio_duration(file_path: str) -> float:
    """Returns the duration of an audio file in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, IndexError):
        return 0.0

def generate_dubbed_audio(
    background_audio_path: str,
    segments: List[Dict[str, Any]],
    output_path: str,
    temp_dir: str = "temp_tts",
    gender_map: Dict[int, str] = None
) -> str:
    """
    Generates Hindi TTS using ElevenLabs and mixes with background.
    Processes segments SEQUENTIALLY to respect API concurrency limits.
    """
    log.info("Generating dubbed audio: %d segments", len(segments))
    
    if not segments:
        log.warning("No segments to dub")
        return background_audio_path

    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        el_client = ElevenLabsClient(gender_map=gender_map or {})
    except Exception as e:
        log.error("Failed to init ElevenLabs: %s", e)
        return background_audio_path
    
    tts_files = []
    log.info("Processing %d segments sequentially", len(segments))
    
    for i, segment in enumerate(segments):
        text = segment.get("transcript", "").strip()
        speaker_id = segment.get("speaker", 0)
        target_duration = segment.get("end", 0.0) - segment.get("start", 0.0)
        start_time = segment.get("start", 0.0)
        
        if not text:
            continue
            
        tts_filename = os.path.join(temp_dir, f"segment_{i}.mp3")
        
        try:
            # Generate TTS (blocking/sequential)
            el_client.generate_dub(text, tts_filename, speaker_id=speaker_id)
            
            if not os.path.exists(tts_filename):
                continue
            
            # Duration Sync check
            current_duration = get_audio_duration(tts_filename)
            final_segment_path = tts_filename
            
            # Speed up if TTS is longer than original slot
            if current_duration > target_duration * 1.05 and target_duration > 0.5:
                speed_factor = min(current_duration / target_duration, 1.3)
                
                speed_filename = tts_filename.replace(".mp3", "_fast.mp3")
                subprocess.run([
                    "ffmpeg", "-y", "-i", tts_filename,
                    "-filter:a", f"atempo={speed_factor}",
                    "-vn", speed_filename
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if os.path.exists(speed_filename):
                    final_segment_path = speed_filename

            tts_files.append({"path": final_segment_path, "start": start_time})

        except Exception as e:
            log.error("Segment %d failed: %s", i, e)

    if not tts_files:
        log.warning("No TTS generated")
        return background_audio_path

    # Build FFmpeg mix command
    cmd = ["ffmpeg", "-y", "-i", background_audio_path]
    for tts in tts_files:
        cmd.extend(["-i", tts["path"]])

    filter_complex = []
    dialogue_inputs = []
    
    filter_complex.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo[bg]")

    for i, tts in enumerate(tts_files):
        delay_ms = int(tts["start"] * 1000)
        chain = f"[{i+1}:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay={delay_ms}|{delay_ms}[tts{i}]"
        filter_complex.append(chain)
        dialogue_inputs.append(f"[tts{i}]")

    mix_str = "".join(dialogue_inputs)
    filter_complex.append(f"{mix_str}amix=inputs={len(tts_files)}:dropout_transition=0[dialogue]")
    filter_complex.append("[bg]volume=0.5[bg_quiet]")  # Reduced from 0.6
    filter_complex.append("[dialogue]volume=3.0[dialogue_loud]")  # Increased from 2.0
    filter_complex.append("[bg_quiet][dialogue_loud]amix=inputs=2:duration=first:dropout_transition=0[out]")

    full_filter = ";".join(filter_complex)
    
    cmd.extend([
        "-filter_complex", full_filter,
        "-map", "[out]",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path
    ])

    log.info("Mixing audio...")
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    log.info("Dubbed audio saved: %s", output_path)
    return output_path
