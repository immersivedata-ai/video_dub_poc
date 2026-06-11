import os
import time
from typing import Optional, Dict, Any
from elevenlabs.client import ElevenLabs
from core.config import ELEVENLABS_API_KEY
from core.logger import get_logger

log = get_logger("elevenlabs")

_credits_cache: Dict[str, Any] = {}
_credits_ts: float = 0


def get_credits() -> Dict[str, Any]:
    """Returns ElevenLabs usage info: character_limit, character_count, remaining."""
    global _credits_cache, _credits_ts
    now = time.time()
    if _credits_cache and (now - _credits_ts) < 60:
        return _credits_cache

    api_key = ELEVENLABS_API_KEY
    if not api_key or api_key == "PLACEHOLDER":
        return {"error": "No API key configured", "remaining_pct": 0}

    try:
        client = ElevenLabs(api_key=api_key)
        sub = client.user.get_subscription()
        limit = getattr(sub, "character_limit", 0) or 0
        used = getattr(sub, "character_count", 0) or 0
        remaining = max(limit - used, 0)
        pct = round((remaining / limit * 100) if limit > 0 else 0)
        status = getattr(sub, "status", "unknown")

        _credits_cache = {
            "limit": limit,
            "used": used,
            "remaining": remaining,
            "remaining_pct": pct,
            "status": status,
        }
        _credits_ts = now
        log.info("Credits: %d/%d (%d%% remaining)", remaining, limit, pct)
        return _credits_cache
    except Exception as e:
        log.error("Failed to fetch credits: %s", e)
        return {"error": str(e), "remaining_pct": 0}

class ElevenLabsClient:
    def __init__(self, gender_map: Optional[Dict[int, str]] = None):
        self.api_key = ELEVENLABS_API_KEY
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not found in .env")
        
        self.client = ElevenLabs(api_key=self.api_key)
        self.model_id = "eleven_multilingual_v2"
        self.gender_map = gender_map or {}
        self.voice_map: Dict[int, str] = {}

        # Voice pools — each unique speaker of the same gender gets a different voice
        self.voice_pool_male = [
            "JBFqnCBsd6RMkjVDRZzb",   # George
            "pNInz6obpgDQGcFmaJgB",   # Adam
            "ErXwobaYiN019PkySvjV",   # Antoni
            "TxGEqnHWrfWFTfGW9XjX",   # Josh
            "VR6AewLTigWG4xSOukaG",   # Arnold
            "yoZ06aMxZJJ28mfd3POQ",   # Sam
        ]
        self.voice_pool_female = [
            "EXAVITQu4vr4xnSDxMaL",   # Sarah
            "XB0fDUnXU5powFXDhCwa",   # Bella
            "21m00Tcm4TlvDq8ikWAM",   # Rachel
            "ThT5KcBeYPX3keUQqHPh",   # Dorothy
            "LcfcDJNUP1GQjkzn1xUU",   # Emily
            "cgSgspJ2msm6clMCkdW9",   # Grace
        ]

        # Track which pool index each gender group is at
        self._male_idx = 0
        self._female_idx = 0

    def get_voice_id(self, speaker_id: int) -> str:
        """Assigns a unique voice per speaker, respecting detected gender."""
        if speaker_id in self.voice_map:
            return self.voice_map[speaker_id]

        gender = self.gender_map.get(speaker_id)
        # Determine gender: explicit > heuristic (even=male, odd=female)
        if gender == "female":
            is_female = True
        elif gender == "male":
            is_female = False
        else:
            is_female = speaker_id % 2 != 0

        # Assign next unique voice from the appropriate pool
        if is_female:
            pool = self.voice_pool_female
            idx = self._female_idx
            self._female_idx += 1
        else:
            pool = self.voice_pool_male
            idx = self._male_idx
            self._male_idx += 1

        voice_id = pool[idx % len(pool)]
        self.voice_map[speaker_id] = voice_id
        log.info("Assigned voice %s for speaker %d (%s)", voice_id[:8], speaker_id, "female" if is_female else "male")
        return voice_id

    def generate_dub(self, text: str, output_path: str, speaker_id: int = 0) -> str:
        if not text:
            return ""

        voice_id = self.get_voice_id(speaker_id)
        log.info("TTS | speaker=%d | %s...", speaker_id, text[:50])
            
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
            log.error("ElevenLabs TTS failed: %s", e)
            raise e