import os
import sys
import subprocess
from typing import Tuple

def separate_audio(audio_path: str, output_dir: str = "audio/separated") -> Tuple[str, str]:
    """
    Separates audio into vocals and background using Demucs.
    
    Returns:
        Tuple of (vocals_path, background_path)
    """
    os.makedirs(output_dir, exist_ok=True)
    
    print("=" * 50)
    print("STEP 2: Separating vocals from background (Demucs)")
    print("=" * 50)
    print(f"Input: {audio_path}")
    print("(This may take several minutes...)")
    
    # Run Demucs with MP3 output (bypasses torchaudio WAV issue on Python 3.13)
    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "--mp3",  # Save as MP3 to bypass torchcodec
        "-o", output_dir,
        audio_path
    ]
    
    subprocess.run(cmd)
    
    # Get output paths (now .mp3)
    audio_basename = os.path.splitext(os.path.basename(audio_path))[0]
    demucs_output = os.path.join(output_dir, "htdemucs", audio_basename)
    
    vocals_path = os.path.join(demucs_output, "vocals.mp3")
    background_path = os.path.join(demucs_output, "no_vocals.mp3")
    
    # Verify files exist
    if os.path.exists(vocals_path) and os.path.exists(background_path):
        print(f"[OK] Vocals extracted: {vocals_path}")
        print(f"[OK] Background extracted: {background_path}")
        return vocals_path, background_path
    else:
        print(f"[FAIL] Separation failed. Files not found.")
        print(f"   Expected vocals: {vocals_path}")
        print(f"   Expected background: {background_path}")
        # List what was actually created
        if os.path.exists(demucs_output):
            print(f"   Found files: {os.listdir(demucs_output)}")
        raise FileNotFoundError("Demucs separation failed")
