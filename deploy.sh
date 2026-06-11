#!/bin/bash
# Deploy to Google Cloud Run
# Usage: bash deploy.sh

set -e

PROJECT="prachi-poc-478711"
REGION="us-central1"
SERVICE="dubbing-studio"
IMAGE="gcr.io/$PROJECT/$SERVICE"

echo "=== Building Docker image ==="
gcloud builds submit --tag "$IMAGE" --project "$PROJECT"

echo "=== Deploying to Cloud Run ==="
gcloud run deploy "$SERVICE" \
    --image "$IMAGE" \
    --platform managed \
    --region "$REGION" \
    --project "$PROJECT" \
    --memory 4Gi \
    --cpu 2 \
    --timeout 3600 \
    --concurrency 3 \
    --max-instances 5 \
    --set-env-vars "DEEPGRAM_API_KEY=$DEEPGRAM_API_KEY,ELEVENLABS_API_KEY=$ELEVENLABS_API_KEY" \
    --service-account "dubbing-studio-sa@$PROJECT.iam.gserviceaccount.com" \
    --allow-unauthenticated

echo "=== Deployment complete ==="
gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format="value(status.url)"
