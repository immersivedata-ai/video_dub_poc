import os
import time
from typing import Optional
import azure.cognitiveservices.speech as speechsdk
from dotenv import load_dotenv

# Load env variables
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))


class AzureTTSClient:
    """
    Azure Speech Service TTS Client for high-quality voice synthesis.
    Uses Azure Speech Gallery voices for natural-sounding speech.
    """

    def __init__(self):
        self.speech_key = os.getenv("AZURE_SPEECH_KEY")
        self.speech_region = os.getenv("AZURE_SPEECH_REGION", "australiaeast")
        
        if not self.speech_key:
            raise RuntimeError("AZURE_SPEECH_KEY not found in .env")
        
        # Initialize speech config
        self.speech_config = speechsdk.SpeechConfig(
            subscription=self.speech_key,
            region=self.speech_region
        )
        
        # Set default audio format (high quality)
        self.speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3
        )
        
        # Cache for speaker voice assignments
        self.voice_map = {}

    def get_best_voice_for_language(self, lang_code: str, gender: str = "male") -> str:
        """
        Returns an optimized Azure voice name for the given language code.
        Uses Azure Neural Voices (Gallery voices) for best quality.
        
        Args:
            lang_code: Language code (e.g., 'hi', 'en', 'ta')
            gender: 'male' or 'female' (default: male for more natural dubbing)
            
        Returns:
            Azure voice name string
        """
        # Azure Neural Voice mappings for Indian languages (2025/2026 Gallery Voices)
        # Using the BEST premium voices that sound most natural and human-like
        # Reference: https://learn.microsoft.com/azure/ai-services/speech-service/language-support
        voice_map = {
            # Hindi - Super Realistic voices (GA Feb 2025)
            # Arjun & Aarti are the most natural, conversational, empathetic voices
            "hi": {
                "female": "hi-IN-AartiNeural",     # Expressive, natural female
                "male": "hi-IN-ArjunNeural"         # Super-realistic male (BEST)
            },
            # English (India) - Premium expressive voices
            "en": {
                "female": "en-IN-AashiNeural",      # Warm, natural female
                "male": "en-IN-AaravNeural"         # Natural conversational male
            },
            # Tamil - Best available neural voices
            "ta": {
                "female": "ta-IN-PallaviNeural",    # Clear female voice
                "male": "ta-IN-ValluvarNeural"      # Natural male voice
            },
            # Telugu - Best available neural voices  
            "te": {
                "female": "te-IN-ShrutiNeural",     # Expressive female
                "male": "te-IN-MohanNeural"         # Natural male
            },
            # Kannada - Best available neural voices
            "kn": {
                "female": "kn-IN-SapnaNeural",      # Natural female
                "male": "kn-IN-GaganNeural"         # Clear male voice
            },
            # Malayalam - Best available neural voices
            "ml": {
                "female": "ml-IN-SobhanaNeural",    # Natural female
                "male": "ml-IN-MidhunNeural"        # Clear male voice
            },
            # Bengali - Best available neural voices
            "bn": {
                "female": "bn-IN-TanishaaNeural",   # Expressive female
                "male": "bn-IN-BashkarNeural"       # Natural male
            },
            # Marathi - Best available neural voices
            "mr": {
                "female": "mr-IN-AarohiNeural",     # Natural female
                "male": "mr-IN-ManoharNeural"       # Clear male voice
            },
            # Gujarati - Best available neural voices
            "gu": {
                "female": "gu-IN-DhwaniNeural",     # Natural female
                "male": "gu-IN-NiranjanNeural"      # Clear male voice
            },
            # Punjabi - Best available neural voices
            "pa": {
                "female": "pa-IN-GurpreetNeural",   # Natural female
                "male": "pa-IN-AmitNeural"          # Clear male voice
            },
            # Odia - Best available neural voices
            "or": {
                "female": "or-IN-SubhasiniNeural",  # Natural female
                "male": "or-IN-SukantNeural"        # Clear male voice
            },
            # Assamese - Best available neural voices
            "as": {
                "female": "as-IN-YashicaNeural",    # Natural female
                "male": "as-IN-PriyomNeural"        # Clear male voice
            },
            # Urdu (India) - For Hindi-Urdu content
            "ur": {
                "female": "ur-IN-GulNeural",        # Natural female
                "male": "ur-IN-SalmanNeural"        # Clear male voice
            },
        }
        
        lang_voices = voice_map.get(lang_code, voice_map.get("hi"))
        return lang_voices.get(gender, lang_voices.get("female"))

    def generate_dub(
        self, 
        text: str, 
        output_path: str, 
        speaker_id: int = 0, 
        language: str = "hi",
        style: Optional[str] = None,
        speaking_rate: float = 1.0,
        pitch: str = "0%"
    ) -> str:
        """
        Generates audio for the given text using Azure Speech Service.
        
        Args:
            text: Text to synthesize
            output_path: Path to save the output audio file
            speaker_id: Speaker identifier (used for gender selection)
            language: Target language code
            style: Optional speaking style (e.g., 'cheerful', 'sad', 'angry')
            speaking_rate: Speech rate multiplier (0.5 to 2.0)
            pitch: Pitch adjustment (e.g., '-10%', '+5%')
            
        Returns:
            Path to the generated audio file
        """
        if not text:
            return ""

        print(f"  🎙️  Azure TTS | Speaker {speaker_id} | Lang: {language} | {text[:30]}...")
        
        # Default to male voice for dubbing (more common in videos)
        # Use female voice only for speaker_id 1, 3, 5... (odd numbers)
        gender = "female" if speaker_id % 2 == 1 else "male"
        
        # Get the best voice for this language
        voice_name = self.get_best_voice_for_language(language, gender)
        
        # Override with specific speaker mapping if available
        if speaker_id in self.voice_map:
            voice_name = self.voice_map[speaker_id]

        # Build SSML for advanced control
        ssml = self._build_ssml(
            text=text,
            voice_name=voice_name,
            style=style,
            speaking_rate=speaking_rate,
            pitch=pitch
        )

        try:
            # Configure audio output to file
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            audio_config = speechsdk.audio.AudioOutputConfig(filename=output_path)
            
            # Create synthesizer
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self.speech_config,
                audio_config=audio_config
            )
            
            # Synthesize speech
            result = synthesizer.speak_ssml_async(ssml).get()
            
            # Check result
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                print(f"  ✅ Azure TTS | Saved to {output_path}")
                # Small delay to avoid rate limiting
                time.sleep(0.1)
                return output_path
                
            elif result.reason == speechsdk.ResultReason.Canceled:
                cancellation_details = result.cancellation_details
                error_msg = f"Speech synthesis canceled: {cancellation_details.reason}"
                if cancellation_details.reason == speechsdk.CancellationReason.Error:
                    error_msg += f" | Error: {cancellation_details.error_details}"
                print(f"  ❌ {error_msg}")
                raise RuntimeError(error_msg)
                
        except Exception as e:
            print(f"  ❌ Azure TTS Failed: {e}")
            raise e

    def _build_ssml(
        self, 
        text: str, 
        voice_name: str,
        style: Optional[str] = None,
        speaking_rate: float = 1.0,
        pitch: str = "0%"
    ) -> str:
        """
        Builds SSML markup for advanced speech synthesis control.
        
        Args:
            text: Text to synthesize
            voice_name: Azure voice name
            style: Optional speaking style
            speaking_rate: Speech rate multiplier
            pitch: Pitch adjustment
            
        Returns:
            SSML string
        """
        # Escape special XML characters
        escaped_text = (
            text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;")
        )
        
        # Build prosody element
        prosody_attrs = f'rate="{speaking_rate}" pitch="{pitch}"'
        
        # Build the SSML
        if style:
            # Use express-as for emotional styles
            ssml = f'''<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis"
                xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="en-US">
                <voice name="{voice_name}">
                    <mstts:express-as style="{style}">
                        <prosody {prosody_attrs}>
                            {escaped_text}
                        </prosody>
                    </mstts:express-as>
                </voice>
            </speak>'''
        else:
            ssml = f'''<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
                <voice name="{voice_name}">
                    <prosody {prosody_attrs}>
                        {escaped_text}
                    </prosody>
                </voice>
            </speak>'''
        
        return ssml

    def set_speaker_voice(self, speaker_id: int, voice_name: str):
        """
        Assigns a specific Azure voice to a speaker ID.
        
        Args:
            speaker_id: Speaker identifier
            voice_name: Azure voice name to use for this speaker
        """
        self.voice_map[speaker_id] = voice_name
        print(f"  📢 Assigned voice '{voice_name}' to speaker {speaker_id}")

    def list_available_voices(self, locale: str = None) -> list:
        """
        Lists available voices from Azure Speech Gallery.
        
        Args:
            locale: Optional locale filter (e.g., 'hi-IN', 'en-IN')
            
        Returns:
            List of available voice information
        """
        try:
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self.speech_config,
                audio_config=None  # No audio output needed
            )
            
            result = synthesizer.get_voices_async(locale=locale or "").get()
            
            if result.reason == speechsdk.ResultReason.VoicesListRetrieved:
                voices = []
                for voice in result.voices:
                    voices.append({
                        "name": voice.short_name,
                        "locale": voice.locale,
                        "gender": str(voice.gender),
                        "local_name": voice.local_name,
                        "styles": voice.style_list if hasattr(voice, 'style_list') else []
                    })
                return voices
            else:
                print(f"  ⚠️ Failed to retrieve voices: {result.reason}")
                return []
                
        except Exception as e:
            print(f"  ❌ Error listing voices: {e}")
            return []
