from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import os
import uuid
import logging
from datetime import datetime
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CyberOrion TikTok Downloader API",
    version="2.0",
    description="Download TikTok videos without watermark"
)

# ✅ IMPROVED CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cybervid.online",
        "http://cybervid.online",
        "https://www.cybervid.online",
        "http://www.cybervid.online",
        "http://localhost:3000",
        "http://localhost:5500",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5500",
        "http://127.0.0.1:8000"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE", "PUT"],
    allow_headers=["*"],
    expose_headers=["*"]  # ✅ Important for file downloads
)

# Create downloads directory - Use /tmp on Render
DOWNLOAD_DIR = '/tmp/downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logger.info(f"✅ Download directory: {DOWNLOAD_DIR}")

def cleanup_old_files():
    """Delete files older than 30 minutes"""
    try:
        current_time = time.time()
        if not os.path.exists(DOWNLOAD_DIR):
            return
            
        for filename in os.listdir(DOWNLOAD_DIR):
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.isfile(filepath):
                if current_time - os.path.getmtime(filepath) > 1800:  # 30 minutes
                    os.remove(filepath)
                    logger.info(f"🗑️ Cleaned up: {filename}")
    except Exception as e:
        logger.error(f"❌ Cleanup error: {str(e)}")

@app.get("/")
async def root():
    """API information endpoint"""
    return {
        "status": "running",
        "service": "CyberOrion TikTok Downloader API",
        "platform": "Render.com",
        "version": "2.0",
        "framework": "FastAPI",
        "endpoints": {
            "/download": "POST - Download TikTok video",
            "/health": "GET - Health check",
            "/files/{filename}": "GET - Serve downloaded file"
        },
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    try:
        file_count = len([f for f in os.listdir(DOWNLOAD_DIR) if os.path.isfile(os.path.join(DOWNLOAD_DIR, f))])
        
        return {
            "status": "healthy",
            "platform": "Render.com",
            "framework": "FastAPI",
            "download_dir": DOWNLOAD_DIR,
            "files_cached": file_count,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return JSONResponse(
            content={"status": "error", "error": str(e)},
            status_code=500
        )

@app.post("/download")
async def download_video(request: Request):
    """Download TikTok video without watermark"""
    
    try:
        # Clean up old files
        cleanup_old_files()
        
        # Get request data
        data = await request.json()
        
        if not data or 'url' not in data:
            logger.warning("⚠️ No URL provided")
            return JSONResponse(
                content={"success": False, "error": "No URL provided"},
                status_code=400
            )
        
        tiktok_url = data['url']
        
        # Validate TikTok URL
        valid_domains = ['tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com']
        if not any(domain in tiktok_url for domain in valid_domains):
            logger.warning(f"⚠️ Invalid URL: {tiktok_url}")
            return JSONResponse(
                content={"success": False, "error": "Invalid TikTok URL"},
                status_code=400
            )
        
        logger.info(f"🎬 Processing: {tiktok_url}")
        
        # Generate unique filename
        unique_id = str(uuid.uuid4())[:8]
        output_filename = f"tiktok_{unique_id}.mp4"
        output_path = os.path.join(DOWNLOAD_DIR, output_filename)
        
        # yt-dlp configuration
        ydl_opts = {
            'format': 'best',
            'outtmpl': output_path,
            'quiet': False,
            'no_warnings': False,
            'extract_flat': False,
            
            # Bypass restrictions
            'nocheckcertificate': True,
            'geo_bypass': True,
            'geo_bypass_country': 'US',
            'proxy': None,
            
            # Headers
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
                'Sec-Fetch-Mode': 'navigate',
            },
            
            # Retry settings
            'retries': 5,
            'fragment_retries': 5,
            'skip_unavailable_fragments': True,
            'socket_timeout': 30,
        }
        
        # Download video
        logger.info("📥 Starting download...")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(tiktok_url, download=True)
            
            # Extract metadata
            title = info.get('title', 'TikTok Video')
            author = info.get('uploader', 'Unknown')
            description = info.get('description', 'No caption available')
            thumbnail = info.get('thumbnail', '')
            
            # Get actual filename
            actual_filename = ydl.prepare_filename(info)
            
            logger.info(f"✅ Downloaded successfully!")
        
        # Verify file exists
        if not os.path.exists(output_path):
            if os.path.exists(actual_filename):
                output_path = actual_filename
                output_filename = os.path.basename(actual_filename)
            else:
                logger.error(f"❌ File not found")
                return JSONResponse(
                    content={"success": False, "error": "Video file not found"},
                    status_code=500
                )
        
        # Get base URL
        base_url = os.environ.get('RENDER_EXTERNAL_URL', str(request.base_url).rstrip('/'))
        
        # Generate URLs
        file_url = f"/files/{output_filename}"
        full_url = f"{base_url}{file_url}"
        
        logger.info(f"🎉 Success! URL: {full_url}")
        
        return JSONResponse(content={
            "success": True,
            "video": file_url,
            "full_url": full_url,
            "title": title,
            "author": author,
            "caption": description,
            "thumbnail": thumbnail,
            "filename": output_filename,
            "message": "Video downloaded successfully"
        })
        
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"❌ yt-dlp error: {error_msg}")
        
        if "Private video" in error_msg:
            return JSONResponse(
                content={"success": False, "error": "Private video"},
                status_code=403
            )
        elif "Video unavailable" in error_msg or "404" in error_msg:
            return JSONResponse(
                content={"success": False, "error": "Video not found"},
                status_code=404
            )
        elif "403" in error_msg or "Forbidden" in error_msg:
            return JSONResponse(
                content={"success": False, "error": "Access forbidden. Try again later."},
                status_code=403
            )
        else:
            return JSONResponse(
                content={"success": False, "error": f"Download failed: {error_msg}"},
                status_code=500
            )
            
    except Exception as e:
        logger.error(f"❌ Error: {str(e)}")
        return JSONResponse(
            content={"success": False, "error": f"Server error: {str(e)}"},
            status_code=500
        )

# ✅ FIXED: File serving endpoint with explicit CORS headers
@app.get("/files/{filename}")
async def serve_file(filename: str):
    """Serve downloaded video file"""
    try:
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        
        if not os.path.exists(filepath):
            logger.warning(f"⚠️ File not found: {filename}")
            return JSONResponse(
                content={"success": False, "error": "File not found"},
                status_code=404
            )
        
        logger.info(f"📤 Serving: {filename}")
        
        # ✅ Add explicit CORS headers to FileResponse
        return FileResponse(
            filepath,
            media_type="video/mp4",
            filename=filename,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "*"
            }
        )
        
    except Exception as e:
        logger.error(f"❌ Serve error: {str(e)}")
        return JSONResponse(
            content={"success": False, "error": "Failed to serve file"},
            status_code=500
        )

# ✅ NEW: Handle preflight OPTIONS requests for file endpoint
@app.options("/files/{filename}")
async def serve_file_options(filename: str):
    """Handle CORS preflight for file serving"""
    return JSONResponse(
        content={},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )

# For local development
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
