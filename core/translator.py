import os
import json
from google import genai
from google.oauth2 import service_account
import google.auth
from typing import List, Dict, Any
from core.config import GOOGLE_APPLICATION_CREDENTIALS
from core.logger import get_logger

log = get_logger("translator")

# Map ISO 639-1 codes <-> full language names
LANG_MAP = {
    "hi": "hindi", "en": "english", "ta": "tamil", "te": "telugu",
    "bn": "bengali", "mr": "marathi", "gu": "gujarati", "kn": "kannada",
    "ml": "malayalam", "pa": "punjabi", "ur": "urdu", "or": "odia",
    "as": "assamese", "es": "spanish", "fr": "french", "de": "german",
    "zh": "chinese", "ja": "japanese", "ko": "korean", "pt": "portuguese",
    "ar": "arabic", "ru": "russian", "it": "italian",
}

def _normalize_lang(lang: str) -> str:
    """Normalize language to lowercase full name (e.g. 'hi' -> 'hindi', 'Hindi' -> 'hindi')."""
    lang = lang.lower().strip()
    return LANG_MAP.get(lang, lang)

class Translator:
    def __init__(self):
        """
        Initializes the Google Gemini Translator via Vertex AI.
        Uses service account JSON if available, otherwise falls back to
        Application Default Credentials (Cloud Run / GCE).
        """
        sa_path = GOOGLE_APPLICATION_CREDENTIALS
        if not os.path.isabs(sa_path):
            sa_path = os.path.join(os.getcwd(), sa_path)

        if os.path.exists(sa_path):
            credentials = service_account.Credentials.from_service_account_file(
                sa_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            log.info("Using service account: %s", sa_path)
        else:
            credentials, project = google.auth.default()
            log.info("Using ADC (Application Default Credentials)")

        self.client = genai.Client(
            vertexai=True,
            project="prachi-poc-478711",
            location="us-central1",
            credentials=credentials
        )
        self.model_id = 'gemini-2.5-flash'

    def translate_segments(self, segments: List[Dict[str, Any]], source_lang: str = "en", target_lang: str = "Hindi") -> List[Dict[str, Any]]:
        """
        Translates dialogue segments from source language to target language using Gemini.
        Also detects emotion. If source == target, returns segments unchanged.
        """
        if not segments:
            return []

        # Skip translation if source and target are the same
        src = _normalize_lang(source_lang)
        tgt = _normalize_lang(target_lang)
        if src == tgt:
            log.info("Source (%s) matches target (%s) — skipping translation", src, tgt)
            return segments

        log.info("Translating %d segments %s -> %s", len(segments), src, tgt)

        simplified_segments = []
        for seg in segments:
            simplified_segments.append({
                "id": seg.get("start"),
                "speaker": seg.get("speaker", 0),
                "text": seg.get("transcript", "")
            })

        prompt = f"""
        You are a professional dubbing translator.
        Translate these segments from {source_lang} to natural conversational {target_lang} and detect the emotion.

        Rules:
        1. Return a JSON list of objects.
        2. Preserve 'id' and 'speaker' exactly.
        3. Translate 'text' to natural conversational {target_lang}.
        4. Add an 'emotion' field: "neutral", "happy", "sad", "angry", "fearful", "surprised".
        5. Use context to determine the best translation and emotion.

        Input:
        {json.dumps(simplified_segments, indent=2)}
        """

        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt
            )
            
            # Clean up response text (remove markdown code blocks if present)
            result_text = response.text.strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            
            translated_data = json.loads(result_text)
            log.info("Translation successful: %d items", len(translated_data))
            
            # Map back to original segments structure
            # Create a map of start_time -> {text, emotion}
            translation_map = {item["id"]: {"text": item["text"], "emotion": item.get("emotion", "neutral")} for item in translated_data}
            
            final_segments = []
            for seg in segments:
                original_id = seg.get("start")
                new_seg = seg.copy()
                if original_id in translation_map:
                    new_seg["transcript"] = translation_map[original_id]["text"]
                    new_seg["emotion"] = translation_map[original_id]["emotion"]
                else:
                    print(f"[WARN] Missing translation for segment starting at {original_id}")
                final_segments.append(new_seg)
                
            return final_segments

        except Exception as e:
            log.error("Gemini translation failed: %s", e)
            print(f"[FAIL] Gemini Translation Failed: {e}")
            # Fallback: Return original segments if failure
            print("Fallback: Returning original English segments.")
            return segments

    def translate(self, text: str) -> str:
        """Single text translation (Legacy support)"""
        if not text:
            return ""
        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=f"Translate this to Hindi: {text}"
            )
            return response.text.strip()
        except:
            return text
