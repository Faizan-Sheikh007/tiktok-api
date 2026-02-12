from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import logging
from datetime import datetime
import os
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CyberOrion TikTok Downloader API",
    version="4.0",
    description="Download TikTok videos without watermark - No cookies required"
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

# API endpoints
TIKWM_API = "https://www.tikwm.com/api/"
TIKMATE_API = "https://tikmate.app/api/lookup"

class RateLimiter:
    """Simple rate limiter"""
    def __init__(self, max_requests=10, time_window=60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = {}
    
    def check_rate_limit(self, ip: str):
        import time
        now = time.time()
        
        if ip not in self.requests:
            self.requests[ip] = []
        
        # Remove old requests
        self.requests[ip] = [r for r in self.requests[ip] if now - r < self.time_window]
        
        if len(self.requests[ip]) >= self.max_requests:
            wait_time = self.time_window - (now - self.requests[ip][0])
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Too many requests",
                    "message": f"Please wait {int(wait_time)} seconds",
                    "retry_after": int(wait_time)
                }
            )
        
        self.requests[ip].append(now)

rate_limiter = RateLimiter(max_requests=10, time_window=60)

def extract_video_id(url: str) -> str:
    """Extract video ID from TikTok URL"""
    patterns = [
        r'/@[\w.-]+/video/(\d+)',
        r'/v/(\d+)',
        r'video/(\d+)',
        r'/(\d+)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

async def download_with_tikwm(url: str) -> dict:
    """Download using TikWM API (Primary method)"""
    try:
        logger.info(f"🔄 Trying TikWM API for: {url}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                TIKWM_API,
                data={
                    "url": url,
                    "hd": "1"
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json"
                }
            )
            
            logger.info(f"TikWM response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"TikWM response data: {data}")
                
                if data.get("code") == 0:
                    video_data = data.get("data", {})
                    
                    # Get video URL (try HD first, fallback to SD)
                    video_url = video_data.get("hdplay") or video_data.get("play")
                    
                    if not video_url:
                        logger.error("No video URL found in response")
                        return {"success": False, "error": "No video URL in response"}
                    
                    logger.info(f"✅ TikWM Success! Video URL: {video_url[:50]}...")
                    
                    return {
                        "success": True,
                        "download_url": video_url,
                        "title": video_data.get("title", "TikTok Video"),
                        "author": video_data.get("author", {}).get("unique_id", "Unknown"),
                        "caption": video_data.get("title", ""),
                        "thumbnail": video_data.get("cover", ""),
                        "duration": video_data.get("duration", 0),
                        "plays": video_data.get("play_count", 0),
                        "likes": video_data.get("digg_count", 0),
                        "comments": video_data.get("comment_count", 0),
                        "shares": video_data.get("share_count", 0),
                        "api_source": "TikWM"
                    }
                else:
                    error_msg = data.get("msg", "Unknown error")
                    logger.error(f"TikWM API error: {error_msg}")
                    return {"success": False, "error": f"TikWM: {error_msg}"}
            else:
                logger.error(f"TikWM status code: {response.status_code}")
                return {"success": False, "error": f"TikWM returned {response.status_code}"}
                
    except httpx.TimeoutException:
        logger.error("TikWM timeout")
        return {"success": False, "error": "TikWM API timeout"}
    except Exception as e:
        logger.error(f"TikWM exception: {str(e)}")
        return {"success": False, "error": f"TikWM error: {str(e)}"}

async def download_with_snapsave(url: str) -> dict:
    """Download using SnapSave API (Fallback method)"""
    try:
        logger.info(f"🔄 Trying SnapSave API for: {url}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            # SnapSave requires a two-step process
            response = await client.post(
                "https://snapsave.app/action.php?lang=en",
                data={
                    "url": url
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "*/*"
                }
            )
            
            if response.status_code == 200:
                # Parse HTML response to extract download URL
                html = response.text
                
                # Look for download URL in HTML
                import re
                url_pattern = r'href="([^"]+)"[^>]*>Download'
                match = re.search(url_pattern, html)
                
                if match:
                    download_url = match.group(1)
                    logger.info(f"✅ SnapSave Success!")
                    
                    return {
                        "success": True,
                        "download_url": download_url,
                        "title": "TikTok Video",
                        "author": "Unknown",
                        "caption": "",
                        "thumbnail": "",
                        "api_source": "SnapSave"
                    }
                else:
                    return {"success": False, "error": "Could not parse SnapSave response"}
            else:
                return {"success": False, "error": f"SnapSave returned {response.status_code}"}
                
    except Exception as e:
        logger.error(f"SnapSave exception: {str(e)}")
        return {"success": False, "error": f"SnapSave error: {str(e)}"}

@app.get("/")
async def root():
    """API information endpoint"""
    return {
        "status": "running",
        "service": "CyberOrion TikTok Downloader API",
        "version": "4.0",
        "method": "External API (No cookies needed)",
        "platform": "Render.com",
        "framework": "FastAPI",
        "apis": {
            "primary": "TikWM API",
            "fallback": "SnapSave API"
        },
        "features": [
            "No cookies required",
            "HD video quality",
            "Rate limiting",
            "Auto-fallback",
            "Video metadata"
        ],
        "endpoints": {
            "/download": "POST - Download TikTok video",
            "/health": "GET - Health check"
        },
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "method": "External API",
        "requires_cookies": False,
        "platform": "Render.com",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/download")
async def download_video(request: Request):
    """Download TikTok video using external APIs"""
    try:
        # Rate limiting
        client_ip = request.client.host
        rate_limiter.check_rate_limit(client_ip)
        
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
        
        # Check for photo posts
        if '/photo/' in tiktok_url:
            logger.info(f"⚠️ Photo post detected: {tiktok_url}")
            return JSONResponse(
                content={
                    "success": False, 
                    "error": "TikTok photo posts (slideshows) are not supported. Please use a video post."
                },
                status_code=400
            )
        
        logger.info(f"🎬 Processing: {tiktok_url}")
        logger.info(f"📍 Client IP: {client_ip}")
        
        # Try TikWM API first
        result = await download_with_tikwm(tiktok_url)
        
        # If TikWM fails, try SnapSave as fallback
        if not result.get("success"):
            logger.warning(f"⚠️ TikWM failed, trying SnapSave...")
            result = await download_with_snapsave(tiktok_url)
        
        if result.get("success"):
            logger.info(f"✅ Success via {result.get('api_source', 'Unknown')} API")
            
            # Return response matching your Laravel controller's expected format
            return JSONResponse(content={
                "success": True,
                "download_url": result["download_url"],
                "full_url": result["download_url"],  # Direct URL from API
                "title": result.get("title", "TikTok Video"),
                "author": result.get("author", "Unknown"),
                "caption": result.get("caption", "No caption available"),
                "thumbnail": result.get("thumbnail", ""),
                "filename": f"tiktok_video.mp4",
                "message": "Video ready for download",
                "api_source": result.get("api_source", "External API"),
                "stats": {
                    "duration": result.get("duration", 0),
                    "plays": result.get("plays", 0),
                    "likes": result.get("likes", 0),
                    "comments": result.get("comments", 0),
                    "shares": result.get("shares", 0)
                }
            })
        else:
            error_msg = result.get("error", "All download methods failed")
            logger.error(f"❌ All APIs failed: {error_msg}")
            
            return JSONResponse(
                content={
                    "success": False,
                    "error": error_msg,
                    "tried_apis": ["TikWM", "SnapSave"],
                    "suggestion": "Please verify the TikTok URL is correct and the video is public"
                },
                status_code=503
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Unexpected error: {str(e)}", exc_info=True)
        return JSONResponse(
            content={
                "success": False, 
                "error": f"Server error: {str(e)}"
            },
            status_code=500
        )

@app.options("/download")
async def download_options():
    """Handle CORS preflight for download endpoint"""
    return JSONResponse(
        content={},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"🚀 Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
