import os
import time
import shutil
import uuid
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from core.pipeline import process_video
from core.translator import SUPPORTED_LANGUAGES

app = FastAPI()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")

# Templates
templates = Jinja2Templates(directory="templates")

# Ensure directories exist
os.makedirs("input", exist_ok=True)
os.makedirs("output", exist_ok=True)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "languages": SUPPORTED_LANGUAGES
    })

@app.post("/process", response_class=HTMLResponse)
async def process_video_route(
    request: Request,
    source_lang: str = Form(...),
    target_lang: str = Form(...),
    speaker_count: str = Form("auto"),
    video_file: UploadFile = File(None),
    youtube_url: str = Form(None)
):
    upload_time = 0.0
    download_time = 0.0
    video_path = ""
    
    try:
        t0 = time.time()
        
        if youtube_url and youtube_url.strip():
            import yt_dlp
            print(f"Downloading YouTube URL: {youtube_url}")
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': 'input/%(title)s.%(ext)s',
                'noplaylist': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                filename = ydl.prepare_filename(info)
                video_path = filename
            download_time = time.time() - t0
            
        elif video_file and video_file.filename:
            video_path = f"input/{video_file.filename}"
            print(f"Saving uploaded file to: {video_path}")
            with open(video_path, "wb") as buffer:
                shutil.copyfileobj(video_file.file, buffer)
            upload_time = time.time() - t0
        
        else:
            return templates.TemplateResponse("index.html", {
                "request": request,
                "languages": SUPPORTED_LANGUAGES,
                "error": "Please upload a video or provide a YouTube URL."
            })

        # Process the video SYNCHRONOUSLY
        print(f"Starting processing for {video_path}...")
        
        # We pass a simple print callback since we can't update UI in real-time easily without JS
        def progress_callback(step, status="processing"):
            print(f"Progress: {step} - {status}")

        # Parse speaker count
        num_speakers = None if speaker_count == "auto" else int(speaker_count)
        print(f"Speaker count: {speaker_count} (parsed: {num_speakers})")

        result = process_video(
            video_path, 
            source_lang, 
            target_lang, 
            progress_callback=progress_callback,
            tts_provider="azure",
            speaker_count=num_speakers  # Pass to pipeline for diarization
        )
        
        # Prepare result data
        result_data = {
            "upload_time": upload_time,
            "download_time": download_time,
            "timings": result["timings"],
            "segments": result["transcription_segments"],
            "output_video": f"/output/{os.path.basename(result['output_video_path'])}",
            "source_lang": source_lang,
            "target_lang": target_lang
        }
        
        return templates.TemplateResponse("result.html", {
            "request": request,
            **result_data
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return templates.TemplateResponse("index.html", {
            "request": request,
            "languages": SUPPORTED_LANGUAGES,
            "error": f"An error occurred: {str(e)}"
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=5000, reload=True)
