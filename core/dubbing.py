import os
import subprocess
import shutil
from typing import List, Dict, Any
from core.elevenlabs_client import ElevenLabsClient
from core.azure_tts_client import AzureTTSClient

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
    language: str = "hi",
    temp_dir: str = "temp_tts",
    cleanup_temp: bool = True,
    tts_provider: str = "azure"  # Options: 'elevenlabs' or 'azure'
) -> str:
    """
    Generates Hindi TTS using ElevenLabs and mixes with background.
    Processes segments SEQUENTIALLY to respect API concurrency limits.
    
    Args:
        cleanup_temp: If True, deletes temp_dir after mixing to save storage.
    """
    print("=" * 50)
    print(f"STEP 6: Generating TTS ({tts_provider.upper()}) and Mixing")
    print("=" * 50)
    
    if not segments:
        print("No segments to dub.")
        return background_audio_path

    os.makedirs(temp_dir, exist_ok=True)
    
    # Initialize TTS Client based on provider
    try:
        if tts_provider.lower() == "azure":
            tts_client = AzureTTSClient()
        else:
            tts_client = ElevenLabsClient()
    except Exception as e:
        print(f"❌ Failed to init {tts_provider} TTS: {e}")
        return background_audio_path
    
    tts_audio_files = []
    print(f"Processing {len(segments)} segments sequentially...")
    
    for i, seg in enumerate(segments):
        start_time = seg.get("start", 0)
        original_text = seg.get("transcript", "")
        # Use simple mapping for speaker ID if available, else 0
        speaker_id = seg.get("speaker", 0) 
        target_duration = seg.get("end", 0.0) - seg.get("start", 0.0)
        
        # Skip empty segments
        if not original_text.strip():
            continue
            
        temp_file = os.path.join(temp_dir, f"segment_{i}_{start_time}.mp3")
        
        try:
            # Generate TTS (blocking/sequential)
            # Pass language and speaker_id
            audio_path = tts_client.generate_dub(
                text=original_text, 
                output_path=temp_file, 
                speaker_id=speaker_id,
                language=language
            )
            
            if not os.path.exists(temp_file):
                continue
            
            # Duration Sync check
            current_duration = get_audio_duration(temp_file)
            final_segment_path = temp_file
            
            # Speed up if TTS is longer than original slot
            if current_duration > target_duration * 1.05 and target_duration > 0.5:
                speed_factor = min(current_duration / target_duration, 1.3)
                
                speed_filename = temp_file.replace(".mp3", "_fast.mp3")
                subprocess.run([
                    "ffmpeg", "-y", "-i", temp_file,
                    "-filter:a", f"atempo={speed_factor}",
                    "-vn", speed_filename
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if os.path.exists(speed_filename):
                    final_segment_path = speed_filename
        
            tts_audio_files.append({"path": final_segment_path, "start": start_time})
        
        except Exception as e:
            print(f"  ❌ Segment {i} failed: {e}")

    if not tts_audio_files:
        print("No TTS generated.")
        return background_audio_path

    # Build FFmpeg mix command
    cmd = ["ffmpeg", "-y", "-i", background_audio_path]
    for tts in tts_audio_files:
        cmd.extend(["-i", tts["path"]])

    filter_complex = []
    dialogue_inputs = []
    
    filter_complex.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo[bg]")

    for i, tts in enumerate(tts_audio_files):
        delay_ms = int(tts["start"] * 1000)
        chain = f"[{i+1}:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay={delay_ms}|{delay_ms}[tts{i}]"
        filter_complex.append(chain)
        dialogue_inputs.append(f"[tts{i}]")

    mix_str = "".join(dialogue_inputs)
    # CRITICAL: normalize=0 prevents amix from dividing volume by number of inputs
    # Without this, each segment's volume = 1/N where N = total segments, causing very low audio
    # that increases as segments finish playing
    filter_complex.append(f"{mix_str}amix=inputs={len(tts_audio_files)}:dropout_transition=0:normalize=0[dialogue]")
    filter_complex.append("[bg]volume=0.4[bg_quiet]")  # Background slightly lower
    filter_complex.append("[dialogue]volume=2.5[dialogue_loud]")  # Dialogue boost (normalized now, so less needed)
    filter_complex.append("[bg_quiet][dialogue_loud]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]")

    full_filter = ";".join(filter_complex)
    
    cmd.extend([
        "-filter_complex", full_filter,
        "-map", "[out]",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path
    ])

    print("Mixing audio...")
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    print(f"✅ Dubbed audio saved: {output_path}")
    
    # Cleanup temp TTS files to save storage
    if cleanup_temp and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
            print(f"🧹 Cleaned up temp files: {temp_dir}")
        except Exception as e:
            print(f"⚠️ Could not cleanup temp dir: {e}")
    
    return output_path

