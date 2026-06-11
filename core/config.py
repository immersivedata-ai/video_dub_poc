import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "gcloud-sa.json")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
GCS_BUCKET = os.getenv("GCS_BUCKET", "")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
CLOUD_RUN_MAX_MB = 30  # Cloud Run hard request limit
