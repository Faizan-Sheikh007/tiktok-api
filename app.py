from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import logging
from datetime import datetime
import os
import re
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CyberOrion TikTok Downloader API",
    version="5.0",
    description="Download TikTok videos without watermark - Multiple API fallbacks"
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
SSSTIK_API = "https://ssstik.io/abc?url=dl"
MUSICALDOWN_API = "https://musicaldown.com/download"
TIKMATE_API = "https://tikmate.app/download"

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
    """Download using TikWM API - Method 1 (Best for metadata)"""
    try:
        logger.info(f"🔄 [1/4] Trying TikWM API for: {url}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                TIKWM_API,
                data={
                    "url": url,
                    "hd": "1",
                    "watermark": "0"  # Request no watermark
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json"
                }
            )
            
            logger.info(f"TikWM response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get("code") == 0:
                    video_data = data.get("data", {})
                    
                    # Priority: wmplay > hdplay > play
                    video_url = None
                    url_source = None
                    has_watermark = True
                    
                    # Check for watermark-free URLs first
                    if video_data.get("wmplay"):
                        video_url = video_data.get("wmplay")
                        url_source = "wmplay"
                        has_watermark = False
                    elif video_data.get("hdplay"):
                        video_url = video_data.get("hdplay")
                        url_source = "hdplay"
                        has_watermark = True  # hdplay usually has watermark
                    elif video_data.get("play"):
                        video_url = video_data.get("play")
                        url_source = "play"
                        has_watermark = True
                    
                    if video_url and video_url.startswith('http'):
                        logger.info(f"✅ TikWM returned {url_source} URL (watermark: {has_watermark})")
                        
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
                            "has_watermark": has_watermark,
                            "url_source": url_source,
                            "api_source": "TikWM"
                        }
                
                logger.error(f"TikWM failed: {data.get('msg', 'No valid video URL')}")
                return {"success": False, "error": "TikWM: No valid URL"}
            
            return {"success": False, "error": f"TikWM status {response.status_code}"}
                
    except Exception as e:
        logger.error(f"TikWM exception: {str(e)}")
        return {"success": False, "error": f"TikWM error: {str(e)}"}

async def download_with_ssstik(url: str) -> dict:
    """Download using SSSTik API - Method 2 (Good for watermark-free)"""
    try:
        logger.info(f"🔄 [2/4] Trying SSSTik API for: {url}")
        
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # SSSTik requires specific headers
            response = await client.post(
                SSSTIK_API,
                data={
                    "id": url,
                    "locale": "en",
                    "tt": "d2F0ZXJtYXJr"  # Base64 for watermark settings
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "*/*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "https://ssstik.io",
                    "Referer": "https://ssstik.io/en"
                }
            )
            
            if response.status_code == 200:
                html = response.text
                
                # Extract download link from HTML
                # SSSTik returns HTML with download links
                patterns = [
                    r'<a[^>]+href="([^"]+)"[^>]*>\s*Without watermark',
                    r'href="([^"]+)"[^>]*download[^>]*>.*?without',
                    r'<a[^>]+class="[^"]*download[^"]*"[^>]+href="([^"]+)"'
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        download_url = match.group(1)
                        logger.info(f"✅ SSSTik Success! Watermark-free URL found")
                        
                        return {
                            "success": True,
                            "download_url": download_url,
                            "title": "TikTok Video",
                            "author": "Unknown",
                            "caption": "",
                            "thumbnail": "",
                            "has_watermark": False,
                            "api_source": "SSSTik"
                        }
                
                logger.error("SSSTik: Could not extract download URL")
                return {"success": False, "error": "SSSTik: No download URL found"}
            
            return {"success": False, "error": f"SSSTik status {response.status_code}"}
                
    except Exception as e:
        logger.error(f"SSSTik exception: {str(e)}")
        return {"success": False, "error": f"SSSTik error: {str(e)}"}

async def download_with_musicaldown(url: str) -> dict:
    """Download using MusicalDown API - Method 3 (Watermark-free specialist)"""
    try:
        logger.info(f"🔄 [3/4] Trying MusicalDown API for: {url}")
        
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Step 1: Submit URL
            response = await client.post(
                MUSICALDOWN_API,
                data={
                    "url": url
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://musicaldown.com",
                    "Referer": "https://musicaldown.com/"
                }
            )
            
            if response.status_code == 200:
                html = response.text
                
                # Extract download link
                patterns = [
                    r'href="([^"]+)"[^>]*>.*?Download\s+Server\s+01',
                    r'href="([^"]+)"[^>]*class="[^"]*download[^"]*"',
                    r'<a[^>]+href="([^"]+)"[^>]*>\s*Download'
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        download_url = match.group(1)
                        
                        # Make absolute URL if needed
                        if not download_url.startswith('http'):
                            download_url = f"https://musicaldown.com{download_url}"
                        
                        logger.info(f"✅ MusicalDown Success! No watermark")
                        
                        return {
                            "success": True,
                            "download_url": download_url,
                            "title": "TikTok Video",
                            "author": "Unknown",
                            "caption": "",
                            "thumbnail": "",
                            "has_watermark": False,
                            "api_source": "MusicalDown"
                        }
                
                return {"success": False, "error": "MusicalDown: No download URL"}
            
            return {"success": False, "error": f"MusicalDown status {response.status_code}"}
                
    except Exception as e:
        logger.error(f"MusicalDown exception: {str(e)}")
        return {"success": False, "error": f"MusicalDown error: {str(e)}"}

async def download_with_snaptik(url: str) -> dict:
    """Download using SnapTik API - Method 4 (Last resort, watermark-free)"""
    try:
        logger.info(f"🔄 [4/4] Trying SnapTik API for: {url}")
        
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.post(
                "https://snaptik.app/abc2.php",
                data={
                    "url": url,
                    "lang": "en"
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://snaptik.app",
                    "Referer": "https://snaptik.app/"
                }
            )
            
            if response.status_code == 200:
                html = response.text
                
                # Extract download link
                pattern = r'href="([^"]+)"[^>]*download[^>]*>'
                match = re.search(pattern, html, re.IGNORECASE)
                
                if match:
                    download_url = match.group(1)
                    logger.info(f"✅ SnapTik Success!")
                    
                    return {
                        "success": True,
                        "download_url": download_url,
                        "title": "TikTok Video",
                        "author": "Unknown",
                        "caption": "",
                        "thumbnail": "",
                        "has_watermark": False,
                        "api_source": "SnapTik"
                    }
                
                return {"success": False, "error": "SnapTik: No download URL"}
            
            return {"success": False, "error": f"SnapTik status {response.status_code}"}
                
    except Exception as e:
        logger.error(f"SnapTik exception: {str(e)}")
        return {"success": False, "error": f"SnapTik error: {str(e)}"}

@app.get("/")
async def root():
    """API information endpoint"""
    return {
        "status": "running",
        "service": "CyberOrion TikTok Downloader API",
        "version": "5.0",
        "method": "Multi-API Watermark-Free Download",
        "platform": "Render.com",
        "framework": "FastAPI",
        "apis": {
            "method_1": "TikWM API (metadata + download)",
            "method_2": "SSSTik API (watermark-free)",
            "method_3": "MusicalDown API (watermark-free)",
            "method_4": "SnapTik API (watermark-free)"
        },
        "features": [
            "No cookies required",
            "Watermark-free priority",
            "4-layer fallback system",
            "HD video quality",
            "Rate limiting",
            "Video metadata"
        ],
        "watermark_info": "Tries watermark-free sources first, falls back if needed",
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
        "method": "Multi-API",
        "watermark_free": True,
        "requires_cookies": False,
        "platform": "Render.com",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/download")
async def download_video(request: Request):
    """Download TikTok video using multiple APIs for watermark-free downloads"""
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
        
        # Try multiple methods in order, prioritizing watermark-free
        result = None
        
        # Method 1: TikWM (best for metadata, check if watermark-free)
        result = await download_with_tikwm(tiktok_url)
        
        # If TikWM has watermark or failed, try watermark-free specialists
        if not result.get("success") or result.get("has_watermark", True):
            if result.get("has_watermark"):
                logger.warning(f"⚠️ TikWM returned video WITH watermark, trying watermark-free APIs...")
            
            # Method 2: SSSTik (watermark-free specialist)
            result2 = await download_with_ssstik(tiktok_url)
            if result2.get("success"):
                result = result2
            else:
                # Method 3: MusicalDown (another watermark-free option)
                result3 = await download_with_musicaldown(tiktok_url)
                if result3.get("success"):
                    result = result3
                else:
                    # Method 4: SnapTik (last resort)
                    result4 = await download_with_snaptik(tiktok_url)
                    if result4.get("success"):
                        result = result4
                    # If all watermark-free failed, use TikWM result (even with watermark)
                    elif result.get("success"):
                        logger.warning("⚠️ All watermark-free APIs failed, using TikWM with watermark")
        
        if result and result.get("success"):
            logger.info(f"✅ Success via {result.get('api_source', 'Unknown')} API (watermark: {result.get('has_watermark', 'unknown')})")
            
            # Return response
            return JSONResponse(content={
                "success": True,
                "download_url": result["download_url"],
                "full_url": result["download_url"],
                "title": result.get("title", "TikTok Video"),
                "author": result.get("author", "Unknown"),
                "caption": result.get("caption", "No caption available"),
                "thumbnail": result.get("thumbnail", ""),
                "filename": f"tiktok_video.mp4",
                "message": "Video ready for download",
                "api_source": result.get("api_source", "External API"),
                "has_watermark": result.get("has_watermark", False),
                "url_source": result.get("url_source", "N/A"),
                "stats": {
                    "duration": result.get("duration", 0),
                    "plays": result.get("plays", 0),
                    "likes": result.get("likes", 0),
                    "comments": result.get("comments", 0),
                    "shares": result.get("shares", 0)
                }
            })
        else:
            error_msg = result.get("error", "All download methods failed") if result else "All download methods failed"
            logger.error(f"❌ All APIs failed: {error_msg}")
            
            return JSONResponse(
                content={
                    "success": False,
                    "error": error_msg,
                    "tried_apis": ["TikWM", "SSSTik", "MusicalDown", "SnapTik"],
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
