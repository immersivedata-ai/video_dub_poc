import os
import json
from google import genai
from google.oauth2 import service_account
import google.auth
from typing import List, Dict, Any
from core.config import GOOGLE_APPLICATION_CREDENTIALS

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
            credentials = service_account.Credentials.from_service_account_file(sa_path)
        else:
            credentials, project = google.auth.default()
            if not project:
                project = "prachi-poc-478711"

        self.client = genai.Client(
            vertexai=True,
            project="prachi-poc-478711",
            location="us-central1",
            credentials=credentials
        )
        self.model_id = 'gemini-2.5-flash'

    def translate_segments(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Translates a list of dialogue segments from English to Hindi using Gemini.
        Also detects emotion (happy, sad, angry, fearful, neutral).
        Preserves speaker context and structure.
        """
        if not segments:
            return []

        print(f"Translating {len(segments)} segments with Gemini (Context + Emotion)...")
        
        # Prepare the prompt structure
        simplified_segments = []
        for seg in segments:
            simplified_segments.append({
                "id": seg.get("start"),
                "speaker": seg.get("speaker", 0),
                "text": seg.get("transcript", "")
            })

        prompt = f"""
        You are a professional dubbing translator.
        Translate the English segments to Hindi and detect the emotion.
        
        Rules:
        1. Return a JSON list of objects.
        2. Preserve 'id' and 'speaker' exactly.
        3. Translate 'text' to natural conversational Hindi.
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
