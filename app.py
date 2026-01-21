from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import yt_dlp
import os
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# Create downloads directory if it doesn't exist
download_dir = os.path.join(os.getcwd(), 'downloads')
os.makedirs(download_dir, exist_ok=True)

# Mount static files for serving downloaded videos
app.mount("/downloads", StaticFiles(directory=download_dir), name="downloads")

def extract_tiktok_id(url):
    """Extract TikTok video ID from URL"""
    patterns = [
        r'tiktok\.com/@[\w\.-]+/video/(\d+)',
        r'tiktok\.com/v/(\d+)',
        r'vm\.tiktok\.com/([\w\d]+)',
        r'vt\.tiktok\.com/([\w\d]+)'
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

@app.post("/download")
async def download(request: Request):
    try:
        data = await request.json()
        url = data.get("url")

        if not url:
            return JSONResponse(
                content={"error": "No URL provided"},
                status_code=400
            )

        # Validate TikTok URL
        if 'tiktok.com' not in url and 'vm.tiktok.com' not in url and 'vt.tiktok.com' not in url:
            return JSONResponse(
                content={"error": "Invalid TikTok URL"},
                status_code=400
            )

        # Configure yt-dlp options for TikTok without watermark
        ydl_opts = {
            'format': 'best',
            'outtmpl': os.path.join(download_dir, '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            # TikTok specific options to get video without watermark
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract video info
                info = ydl.extract_info(url, download=True)

                # Get video details
                video_id = info.get('id', 'video')
                ext = info.get('ext', 'mp4')
                title = info.get('title', 'TikTok Video')
                author = info.get('uploader', 'Unknown')
                thumbnail = info.get('thumbnail', '')
                # Get caption with fallback options
                caption = info.get('description') or info.get('alt_title') or info.get('title') or "No caption available"

                # Construct file path
                filename = f"{video_id}.{ext}"
                file_path = os.path.join(download_dir, filename)

                # Verify file exists
                if not os.path.exists(file_path):
                    return JSONResponse(
                        content={"error": "Video download failed - file not found"},
                        status_code=500
                    )

                # Return video info and download URL
                return JSONResponse(content={
                    "success": True,
                    "video": f"/downloads/{filename}",
                    "title": title,
                    "author": author,
                    "thumbnail": thumbnail,
                    "filename": filename,
                    "caption": caption
                })

        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            if "Private video" in error_msg:
                return JSONResponse(
                    content={"error": "This video is private and cannot be downloaded"},
                    status_code=400
                )
            elif "Video unavailable" in error_msg:
                return JSONResponse(
                    content={"error": "Video not found or unavailable"},
                    status_code=404
                )
            else:
                return JSONResponse(
                    content={"error": f"Download failed: {error_msg}"},
                    status_code=400
                )

    except Exception as e:
        return JSONResponse(
            content={"error": f"Internal server error: {str(e)}"},
            status_code=500
        )

@app.get("/health")
async def health():
    return {"status": "ok", "downloads_dir": download_dir}
