import os
import json
import time
from google import genai
from typing import List, Dict, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

# Supported languages for dubbing (both source → target)
SUPPORTED_LANGUAGES = {
    "en": "English",
    "hi": "Hindi",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "mr": "Marathi",
    "bn": "Bengali",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "or": "Odia",
    "as": "Assamese",
}

class Translator:
    def __init__(self, target_language: str = "hi"):
        """
        Initializes the Google Gemini Translator using Vertex AI with GCP credentials.

        Uses environment variables:
            - GOOGLE_APPLICATION_CREDENTIALS: Path to service account JSON key
            - GCP_PROJECT_ID: Google Cloud project ID
            - GCP_REGION: Region for Vertex AI (e.g., us-central1)
            - GEMINI_MODEL: Model name (e.g., gemini-2.5-flash)

        Args:
            target_language: Language code (hi, ta, te, kn, ml, etc.)
                             Defaults to Hindi for backward compatibility.
        """
        # Get GCP configuration from environment
        gcp_project = os.getenv("GCP_PROJECT_ID")
        # Use GEMINI_REGION if set, otherwise fallback to us-central1 (Required for Vertex AI models)
        # CRITICAL: Do NOT set this to 'us' (multi-region) as it causes 404 errors for prediction endpoints.
        gcp_region = os.getenv("GEMINI_REGION", "us-central1")
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        # Validate required environment variables
        if not gcp_project:
            raise RuntimeError("GCP_PROJECT_ID not found. Please add it to your .env file.")
        if not credentials_path:
            raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS not found. Please add it to your .env file.")
        if not os.path.exists(credentials_path):
            raise RuntimeError(f"Service account key file not found at: {credentials_path}")

        if target_language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Unsupported language: {target_language}. Supported: {list(SUPPORTED_LANGUAGES.keys())}")

        self.target_language = target_language
        self.language_name = SUPPORTED_LANGUAGES[target_language]

        # Initialize Vertex AI client with GCP credentials
        # The GOOGLE_APPLICATION_CREDENTIALS env var is automatically used by the client
        self.client = genai.Client(
            vertexai=True,
            project=gcp_project,
            location=gcp_region
        )
        self.model_name = gemini_model

        print(f"🌐 Translator initialized for: {self.language_name} ({target_language})")
        print(f"   Using Vertex AI: Project={gcp_project}, Region={gcp_region}, Model={gemini_model}")

    def translate_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Translates dialogue segments from English to target language using Gemini.
        Passes full timestamp, duration, and speaker context to help LLM:
        - Pick appropriate words that fit within the time window
        - Understand conversation flow and speaker context
        - Detect emotion for TTS voice modulation
        """
        if not segments:
            return []

        print(f"Translating {len(segments)} segments to {self.language_name} with Gemini...")
        print(f"  → Passing timestamps and speaker info for context")

        # Prepare detailed segment info with duration for duration‑aware translation
        detailed_segments = []
        for seg in segments:
            start = seg.get("start", 0.0)
            end = seg.get("end", 0.0)
            duration = end - start
            # Estimate max words: ~2.5 words/second is typical for Indian languages
            max_words = max(3, int(duration * 2.5))

            detailed_segments.append({
                "id": start,
                "english_dialogue": seg.get("transcript", ""),
                "speaker": seg.get("speaker", 0),
                "timestamp": f"{round(start, 2)}s - {round(end, 2)}s",
                "duration_sec": round(duration, 2),
                "max_words_allowed": max_words,
            })

        translated_segments_map = {}
        
        # Batch processing: Process in small chunks of 5 to avoid API overload and empty responses
        BATCH_SIZE = 5
        
        for i in range(0, len(detailed_segments), BATCH_SIZE):
            batch = detailed_segments[i : i + BATCH_SIZE]
            print(f"  Processing batch {i//BATCH_SIZE + 1} ({len(batch)} segments)...")
            
            prompt = f"""You are a professional dubbing translator for video/film content.
Translate the English dialogues to {self.language_name} and detect emotion.

CRITICAL RULES FOR DUBBING:
1. **DURATION CONSTRAINT**: Each segment has 'duration_sec' and 'max_words_allowed'.
   - Your translation MUST fit within the 'max_words_allowed' limit.
   - This prevents dialogue overlap in the final video.
   - Prefer shorter synonyms and natural contractions.
   - If needed, summarize while preserving core meaning.

2. **DIALOGUE CONTEXT**:
   - 'english_dialogue' shows what was spoken at that timestamp.
   - 'timestamp' shows when the dialogue occurs (e.g., "2.5s - 5.0s").
   - Use this context to understand conversation flow and pacing.

3. **SPEAKER CONTEXT**:
   - 'speaker' identifies different speakers (0, 1, 2, etc.)
   - Maintain consistent voice/style for each speaker throughout.
   - Use appropriate formality based on speaker relationships.

4. **OUTPUT FORMAT**:
   - Return a JSON list of objects.
   - Preserve 'id' and 'speaker' exactly as given.
   - Add 'text' field with your {self.language_name} translation.
   - Add 'emotion' field: "neutral", "happy", "sad", "angry", "fearful", "surprised".

5. **QUALITY**:
   - Use natural, conversational {self.language_name} (not formal/bookish).
   - Preserve the tone, intent, and emotion of the original dialogue.
   - Make it sound like native {self.language_name} speakers would say it.

Input Segments (with English dialogue and timing):
{json.dumps(batch, indent=2, ensure_ascii=False)}

Return ONLY the JSON array, no other text."""

            # Retry logic for 503 Service Unavailable / Overload
            MAX_RETRIES = 3
            success = False
            
            for attempt in range(MAX_RETRIES):
                try:
                    response = self.client.models.generate_content(
                        model=self.model_name,
                        contents=prompt,
                        config={
                            "temperature": 0.3,
                            "max_output_tokens": 8000,
                            "response_schema": self._output_schema(),
                            "response_mime_type": "application/json",
                        },
                    )

                    batch_data = response.parsed
                    if batch_data:
                        for item in batch_data:
                            # Handle both object attributes (dot notation) and dictionary keys
                            if isinstance(item, dict):
                                item_id = item.get("id")
                                item_text = item.get("text")
                                item_emotion = item.get("emotion", "neutral")
                            else:
                                item_id = item.id
                                item_text = item.text
                                item_emotion = getattr(item, "emotion", "neutral")

                            translated_segments_map[item_id] = {
                                "text": item_text,
                                "emotion": item_emotion
                            }
                        success = True
                        break # Success, exit retry loop
                    else:
                        print(f"  ⚠️ Warning: Batch {i//BATCH_SIZE + 1} returned None (empty). Raw Response: {response.text}")
                        # This can happen on transient model errors, so we SHOULD retry
                        # Fall through to exception-like retry logic
                        time.sleep(2)

                except Exception as e:
                    print(f"  ⚠️ Batch {i//BATCH_SIZE + 1} attempt {attempt+1}/{MAX_RETRIES} failed: {e}")
                
                # Retry logic for both Exception and None result
                if not success:
                    if attempt < MAX_RETRIES - 1:
                        wait_time = (2 ** attempt) * 5 # 5s, 10s, 20s
                        print(f"     Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        print(f"  ❌ Batch {i//BATCH_SIZE + 1} permanently failed.")

            if success:
                time.sleep(2) # Increased delay between batches for safety

        # Map back to original segments structure using the accumulated map
        final_segments = []
        for seg in segments:
            original_id = seg.get("start")
            new_seg = seg.copy()
            if original_id in translated_segments_map:
                new_seg["original_transcript"] = new_seg.get("transcript", "")
                new_seg["transcript"] = translated_segments_map[original_id]["text"]
                new_seg["emotion"] = translated_segments_map[original_id]["emotion"]
                print(f"  ✅ [{original_id:.1f}s] Speaker {seg.get('speaker', 0)}: {new_seg['transcript'][:40]}...")
            else:
                print(f"  ⚠️ Missing translation for segment at {original_id}s")
            final_segments.append(new_seg)

        print(f"✅ Translation complete: {len(final_segments)} segments in {self.language_name}")
        return final_segments


    def translate(self, text: str) -> str:
        """Single text translation (Legacy support)"""
        if not text:
            return ""
        try:
            # FIX: Nest the response_schema inside 'config'
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=f"Translate this to {self.language_name} (natural, conversational): {text}",
                config={
                    "temperature": 0.7, 
                    "max_output_tokens": 500,
                    "response_mime_type": "application/json",
                    "response_schema": self._output_schema(single=True)
                }
            )
            # Use .parsed for clean output if using schema, otherwise .text
            return response.parsed.text if hasattr(response, 'parsed') else response.text.strip()
        except Exception as e:
            print(f"Single translation failed: {e}")
            return text
    def _output_schema(self, single: bool = False) -> dict:
        """Return JSON schema for Gemini controlled generation.
        If *single* is True, the schema describes a single object with `text`
        and optional `emotion`. Otherwise it describes an array of objects
        each containing `id`, `text`, and optional `emotion`.
        """
        if single:
            return {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "emotion": {"type": "string"},
                },
                "required": ["text"],
            }
        else:
            return {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "number"},
                        "text": {"type": "string"},
                        "emotion": {"type": "string"},
                    },
                    "required": ["id", "text"],
                },
            }

