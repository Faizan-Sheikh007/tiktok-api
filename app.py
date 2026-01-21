from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import os
import uuid
import logging
from datetime import datetime
import time
from pathlib import Path
import asyncio

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CyberOrion TikTok Downloader API",
    version="3.0",
    description="Download TikTok videos without watermark - Enhanced with Cookie Support"
)

# CORS Configuration
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
    expose_headers=["*"]
)

# Directories
DOWNLOAD_DIR = '/tmp/downloads'
COOKIES_FILE = 'cookies.txt'  # Place cookies.txt in root directory

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
logger.info(f"✅ Download directory: {DOWNLOAD_DIR}")

# Check for cookies file
if os.path.exists(COOKIES_FILE):
    logger.info(f"✅ Cookies file found: {COOKIES_FILE}")
else:
    logger.warning(f"⚠️ Cookies file not found: {COOKIES_FILE}")
    logger.warning("TikTok downloads may fail without cookies!")

def cleanup_old_files():
    """Delete files older than 30 minutes"""
    try:
        current_time = time.time()
        if not os.path.exists(DOWNLOAD_DIR):
            return
            
        for filename in os.listdir(DOWNLOAD_DIR):
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.isfile(filepath):
                if current_time - os.path.getmtime(filepath) > 1800:
                    os.remove(filepath)
                    logger.info(f"🗑️ Cleaned up: {filename}")
    except Exception as e:
        logger.error(f"❌ Cleanup error: {str(e)}")

class RateLimiter:
    """Simple rate limiter to avoid TikTok blocks"""
    def __init__(self, max_requests=5, time_window=60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = []
    
    async def check_rate_limit(self):
        now = time.time()
        # Remove old requests
        self.requests = [r for r in self.requests if now - r < self.time_window]
        
        if len(self.requests) >= self.max_requests:
            wait_time = self.time_window - (now - self.requests[0])
            logger.warning(f"⚠️ Rate limit hit. Waiting {wait_time:.1f}s")
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Too many requests",
                    "message": f"Please wait {int(wait_time)} seconds before trying again",
                    "retry_after": int(wait_time)
                }
            )
        
        self.requests.append(now)

rate_limiter = RateLimiter(max_requests=5, time_window=60)

@app.get("/")
async def root():
    """API information endpoint"""
    cookies_status = "✅ Available" if os.path.exists(COOKIES_FILE) else "❌ Missing"
    
    return {
        "status": "running",
        "service": "CyberOrion TikTok Downloader API",
        "version": "3.0",
        "platform": "Render.com",
        "cookies": cookies_status,
        "framework": "FastAPI",
        "features": [
            "Cookie-based authentication",
            "Rate limiting",
            "Auto cleanup",
            "Enhanced error handling"
        ],
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
        cookies_exists = os.path.exists(COOKIES_FILE)
        
        return {
            "status": "healthy" if cookies_exists else "degraded",
            "cookies_available": cookies_exists,
            "platform": "Render.com",
            "framework": "FastAPI",
            "download_dir": DOWNLOAD_DIR,
            "files_cached": file_count,
            "rate_limiter": {
                "max_requests": rate_limiter.max_requests,
                "time_window": rate_limiter.time_window,
                "current_requests": len(rate_limiter.requests)
            },
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
        # Check rate limit
        await rate_limiter.check_rate_limit()
        
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
        
        # Check if it's a photo/slideshow post
        if '/photo/' in tiktok_url:
            logger.info(f"⚠️ Photo post detected: {tiktok_url}")
            return JSONResponse(
                content={
                    "success": False, 
                    "error": "TikTok photo posts (slideshows) are not supported. Please try a video post instead."
                },
                status_code=400
            )
        
        logger.info(f"🎬 Processing: {tiktok_url}")
        
        # Generate unique filename
        unique_id = str(uuid.uuid4())[:8]
        output_filename = f"tiktok_{unique_id}.mp4"
        output_path = os.path.join(DOWNLOAD_DIR, output_filename)
        
        # Enhanced yt-dlp configuration with cookies
        ydl_opts = {
            'format': 'best',
            'outtmpl': output_path,
            'quiet': False,
            'no_warnings': False,
            'verbose': True,
            
            # CRITICAL: Use cookies if available
            'cookiefile': COOKIES_FILE if os.path.exists(COOKIES_FILE) else None,
            
            # Enhanced headers to mimic real browser
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Referer': 'https://www.tiktok.com/',
                'Origin': 'https://www.tiktok.com',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'same-origin',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Ch-Ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
            },
            
            # TikTok specific extractor arguments
            'extractor_args': {
                'tiktok': {
                    'api_hostname': 'api16-normal-c-useast1a.tiktokv.com',
                    'webpage_download_max_retries': 5,
                }
            },
            
            # Network settings
            'retries': 10,
            'fragment_retries': 10,
            'skip_unavailable_fragments': True,
            'socket_timeout': 60,
            'nocheckcertificate': True,
            'geo_bypass': True,
            'geo_bypass_country': 'US',
        }
        
        # Download video
        logger.info("📥 Starting download...")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(tiktok_url, download=True)
                
                # Extract metadata
                title = info.get('title', 'TikTok Video')
                author = info.get('uploader', info.get('creator', 'Unknown'))
                description = info.get('description', 'No caption available')
                thumbnail = info.get('thumbnail', '')
                
                # Get actual filename
                actual_filename = ydl.prepare_filename(info)
                
                logger.info(f"✅ Downloaded successfully!")
        
        except yt_dlp.utils.ExtractorError as e:
            error_msg = str(e)
            logger.error(f"❌ Extractor error: {error_msg}")
            
            # Provide specific error messages
            if "Unable to extract webpage video data" in error_msg:
                if not os.path.exists(COOKIES_FILE):
                    return JSONResponse(
                        content={
                            "success": False,
                            "error": "TikTok extraction failed - Cookies required",
                            "message": "This error occurs because TikTok blocks automated requests. The server needs a cookies.txt file to work.",
                            "solutions": [
                                "Server administrator needs to add cookies.txt file",
                                "Cookies should be exported from a logged-in TikTok session",
                                "Cookies expire every few hours and need regular updates"
                            ]
                        },
                        status_code=503
                    )
                else:
                    return JSONResponse(
                        content={
                            "success": False,
                            "error": "TikTok extraction failed - Cookies may be expired",
                            "message": "The cookies file exists but may be outdated. TikTok cookies expire frequently.",
                            "solutions": [
                                "Update cookies.txt with fresh cookies from browser",
                                "Ensure you're logged into TikTok when exporting cookies",
                                "Try again in a few minutes (TikTok may be rate limiting)"
                            ]
                        },
                        status_code=503
                    )
            elif "Private video" in error_msg:
                return JSONResponse(
                    content={"success": False, "error": "This video is private"},
                    status_code=403
                )
            elif "Video unavailable" in error_msg or "404" in error_msg:
                return JSONResponse(
                    content={"success": False, "error": "Video not found or has been deleted"},
                    status_code=404
                )
            else:
                return JSONResponse(
                    content={"success": False, "error": f"Download failed: {error_msg}"},
                    status_code=500
                )
        
        # Verify file exists
        if not os.path.exists(output_path):
            if os.path.exists(actual_filename):
                output_path = actual_filename
                output_filename = os.path.basename(actual_filename)
            else:
                logger.error(f"❌ File not found: {output_path}")
                return JSONResponse(
                    content={"success": False, "error": "Video file not found after download"},
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
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error: {str(e)}")
        return JSONResponse(
            content={"success": False, "error": f"Server error: {str(e)}"},
            status_code=500
        )

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

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
