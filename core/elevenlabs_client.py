import os
import time
from typing import Optional
from elevenlabs.client import ElevenLabs
from core.config import ELEVENLABS_API_KEY

class ElevenLabsClient:
    def __init__(self):
        self.api_key = ELEVENLABS_API_KEY
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not found in .env")
        
        self.client = ElevenLabs(api_key=self.api_key)
        
        # Best model for dubbing: high quality + emotion + Hindi support
        self.model_id = "eleven_multilingual_v2"
        
        # Cache for cloned voice IDs: {speaker_id: voice_id}
        self.voice_map = {}

    def generate_dub(self, text: str, output_path: str, speaker_id: int = 0) -> str:
        """
        Generates Hindi audio for the given text using the new v1.0+ SDK syntax.
        """
        if not text:
            return ""

        print(f"  [TTS] ElevenLabs | Speaker {speaker_id} | {text[:40]}...")
        
        # Default fallback voices (Public IDs from ElevenLabs library)
        default_male = "JBFqnCBsd6RMkjVDRZzb"  # George
        default_female = "HP3OkBOPWanmqpjL7XVM"  # Sarah
        
        voice_id = default_male if speaker_id % 2 == 0 else default_female
        
        # Use cloned voice if available
        if speaker_id in self.voice_map:
            voice_id = self.voice_map[speaker_id]
            
        try:
            # --- FIX STARTS HERE ---
            # Replaced self.client.generate() with self.client.text_to_speech.convert()
            audio_generator = self.client.text_to_speech.convert(
                text=text,
                voice_id=voice_id,       # Renamed from 'voice'
                model_id=self.model_id,  # Renamed from 'model'
                output_format="mp3_44100_128" # Optional: Ensures standard MP3 format
            )
            # --- FIX ENDS HERE ---
            
            # Save to file
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            with open(output_path, "wb") as f:
                for chunk in audio_generator:
                    if chunk:
                        f.write(chunk)
            
            # Rate limiting delay (Safety for Free Tier)
            time.sleep(0.5)
                
            return output_path
            
        except Exception as e:
            print(f"  [FAIL] ElevenLabs Failed: {e}")
            raise e