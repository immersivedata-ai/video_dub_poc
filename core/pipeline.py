import time
import os
import shutil
from typing import Dict, Any, List, Optional

# Core modules (assuming these exist from previous Context)
from core.audioextractor import extract_audio
from core.separator import separate_audio
from core.transcribe import transcribe_audio
from core.translator import Translator, SUPPORTED_LANGUAGES
from core.dubbing import generate_dubbed_audio

def process_video(
    video_path: str, 
    source_lang: str, 
    target_lang: str,
    progress_callback=None,
    tts_provider: str = "azure",
    speaker_count: Optional[int] = None  # None = auto-detect, or specify 1-7
) -> Dict[str, Any]:
    """
    Orchestrates the video dubbing process with timing.
    
    Args:
        speaker_count: Number of speakers for diarization. None for auto-detect.
    """
    def report_progress(step_name, status="processing"):
        if progress_callback:
            progress_callback(step_name, status)

    timings = {}
    
    # Generate paths
    video_basename = os.path.splitext(os.path.basename(video_path))[0]
    
    # Ensure directories exist
    os.makedirs("audio", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    original_audio = f"audio/{video_basename}_original.wav"
    dubbed_audio = f"audio/{video_basename}_dubbed_{target_lang}.aac"
    output_video = f"output/{video_basename}_{target_lang}.mp4"
    
    start_total = time.time()
    
    # STEP 1: Extract Audio
    report_progress("Extracting Audio")
    print(f"--- Step 1: Extracting Audio ---")
    t0 = time.time()
    extract_audio(video_path, original_audio)
    timings["extract_audio"] = time.time() - t0
    
    # STEP 2: Separate Audio
    report_progress("Separating Voice")
    print(f"--- Step 2: Separating Audio ---")
    t0 = time.time()
    vocals_path, background_path = separate_audio(original_audio)
    timings["separation"] = time.time() - t0
    
    # STEP 3: Transcribe
    report_progress("Transcribing")
    print(f"--- Step 3: Transcribing (Speakers: {speaker_count or 'auto'}) ---")
    t0 = time.time()
    utterances = transcribe_audio(
        vocals_path, 
        source_language=source_lang,
        speaker_count=speaker_count
    )
    timings["transcribe"] = time.time() - t0
    
    # Prepare transcription segments for return
    # (Segments are translated in next step)

    # STEP 4: Translate
    report_progress("Translating")
    print(f"--- Step 4: Translating ---")
    t0 = time.time()
    translator = Translator(target_language=target_lang)
    translated_segments = translator.translate_segments(utterances)
    timings["translate"] = time.time() - t0
    
    # STEP 5: Synthesize & Mix
    report_progress("Generating Voice")
    print(f"--- Step 5: Synthesizing & Mixing ---")
    t0 = time.time()
    generate_dubbed_audio(background_path, translated_segments, dubbed_audio, language=target_lang, tts_provider=tts_provider)
    timings["synthesize"] = time.time() - t0
    
    # STEP 6: Merge Video
    report_progress("Finalizing Video")
    print(f"--- Step 6: Merging Video ---")
    t0 = time.time()
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", dubbed_audio,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "copy",
        "-shortest",
        output_video
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    timings["merge_video"] = time.time() - t0
    
    timings["total_dubbing"] = time.time() - start_total
    report_progress("Completed", status="done")
    
    # --- CLEANUP ---
    print("--- Cleanup: Removing intermediate files ---")
    try:
        if os.path.exists(original_audio):
            os.remove(original_audio)
        if os.path.exists(dubbed_audio):
            os.remove(dubbed_audio)
        
        # Cleanup Demucs output: SKIPPED to allow caching for faster re-runs
        # demucs_video_dir = os.path.dirname(vocals_path)
        # if os.path.exists(demucs_video_dir):
        #    shutil.rmtree(demucs_video_dir)
            
    except Exception as cleanup_error:
        print(f"Warning: Cleanup failed: {cleanup_error}")

    return {
        "output_video_path": output_video,
        "transcription_segments": translated_segments, 
        "timings": timings
    }
