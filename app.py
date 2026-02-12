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
    version="6.0",
    description="Download TikTok videos without watermark - Direct video extraction"
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

async def get_video_id_from_url(url: str) -> str:
    """Extract or resolve video ID from any TikTok URL"""
    try:
        # If it's a short URL, resolve it first
        if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                response = await client.get(url)
                url = str(response.url)
        
        # Extract video ID
        patterns = [
            r'/video/(\d+)',
            r'/v/(\d+)',
            r'video_id=(\d+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None
    except Exception as e:
        logger.error(f"Error resolving URL: {e}")
        return None

async def download_with_tikwm_v2(url: str) -> dict:
    """TikWM API with improved parameters for watermark-free"""
    try:
        logger.info(f"🔄 [Method 1] Trying TikWM API v2: {url}")
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Try with different parameters
            response = await client.post(
                "https://www.tikwm.com/api/",
                json={
                    "url": url,
                    "hd": 1,
                    "watermark": 0
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json"
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"TikWM response: {json.dumps(data, indent=2)[:500]}")
                
                if data.get("code") == 0 and data.get("data"):
                    video_data = data["data"]
                    
                    # Check all available video URLs
                    video_urls = {
                        "wmplay": video_data.get("wmplay"),
                        "play": video_data.get("play"),
                        "hdplay": video_data.get("hdplay")
                    }
                    
                    logger.info(f"Available URLs: {json.dumps(video_urls, indent=2)}")
                    
                    # Use wmplay if available (no watermark)
                    video_url = video_urls.get("wmplay") or video_urls.get("play") or video_urls.get("hdplay")
                    
                    if video_url:
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
                            "api_source": "TikWM-v2"
                        }
        
        return {"success": False, "error": "TikWM: No valid response"}
    except Exception as e:
        logger.error(f"TikWM v2 error: {e}")
        return {"success": False, "error": str(e)}

async def download_with_tikvideo(url: str) -> dict:
    """TikVideo.app API - Known for watermark-free downloads"""
    try:
        logger.info(f"🔄 [Method 2] Trying TikVideo.app API: {url}")
        
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Step 1: Get the download page
            response = await client.get(
                f"https://tikvideo.app/api/ajaxSearch",
                params={"q": url, "lang": "en"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json"
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"TikVideo response status: {data.get('status')}")
                
                if data.get("status") == "ok":
                    html = data.get("data", "")
                    
                    # Extract the no-watermark download URL
                    patterns = [
                        r'href="([^"]+)"[^>]*>Download\s+MP4',
                        r'href="([^"]+)"[^>]*class="[^"]*download[^"]*"[^>]*>.*?Without',
                        r'<a[^>]+href="([^"]+)"[^>]*>\s*Download\s+Video'
                    ]
                    
                    for pattern in patterns:
                        match = re.search(pattern, html, re.IGNORECASE)
                        if match:
                            download_url = match.group(1)
                            logger.info(f"✅ TikVideo Success! URL: {download_url[:80]}")
                            
                            return {
                                "success": True,
                                "download_url": download_url,
                                "title": "TikTok Video",
                                "author": "Unknown",
                                "caption": "",
                                "thumbnail": "",
                                "api_source": "TikVideo"
                            }
        
        return {"success": False, "error": "TikVideo: No download URL found"}
    except Exception as e:
        logger.error(f"TikVideo error: {e}")
        return {"success": False, "error": str(e)}

async def download_with_savett(url: str) -> dict:
    """SaveTT.cc API - Watermark-free specialist"""
    try:
        logger.info(f"🔄 [Method 3] Trying SaveTT.cc API: {url}")
        
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.post(
                "https://savett.cc/api/ajaxSearch",
                data={
                    "q": url,
                    "lang": "en"
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json"
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get("status") == "ok":
                    html = data.get("data", "")
                    
                    # Find download URL
                    match = re.search(r'href="([^"]+)"[^>]*>.*?Download.*?MP4', html, re.IGNORECASE)
                    if match:
                        download_url = match.group(1)
                        logger.info(f"✅ SaveTT Success!")
                        
                        return {
                            "success": True,
                            "download_url": download_url,
                            "title": "TikTok Video",
                            "author": "Unknown",
                            "caption": "",
                            "thumbnail": "",
                            "api_source": "SaveTT"
                        }
        
        return {"success": False, "error": "SaveTT: No download URL"}
    except Exception as e:
        logger.error(f"SaveTT error: {e}")
        return {"success": False, "error": str(e)}

async def download_with_ttsave(url: str) -> dict:
    """TTSave.app API - Another watermark-free option"""
    try:
        logger.info(f"🔄 [Method 4] Trying TTSave.app API: {url}")
        
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.post(
                "https://ttsave.app/download",
                data={
                    "query": url,
                    "language_id": "1"
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "*/*"
                }
            )
            
            if response.status_code == 200:
                html = response.text
                
                # Extract download link
                match = re.search(r'href="([^"]+)"[^>]*type="no-watermark"', html, re.IGNORECASE)
                if not match:
                    match = re.search(r'href="([^"]+)"[^>]*>.*?Without\s+Watermark', html, re.IGNORECASE)
                
                if match:
                    download_url = match.group(1)
                    logger.info(f"✅ TTSave Success!")
                    
                    return {
                        "success": True,
                        "download_url": download_url,
                        "title": "TikTok Video",
                        "author": "Unknown",
                        "caption": "",
                        "thumbnail": "",
                        "api_source": "TTSave"
                    }
        
        return {"success": False, "error": "TTSave: No download URL"}
    except Exception as e:
        logger.error(f"TTSave error: {e}")
        return {"success": False, "error": str(e)}

async def download_with_snaptik_v2(url: str) -> dict:
    """SnapTik API v2 - Updated extraction"""
    try:
        logger.info(f"🔄 [Method 5] Trying SnapTik v2 API: {url}")
        
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Get the page first
            response = await client.get(
                "https://snaptik.app/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
            )
            
            # Extract token from page if needed
            token_match = re.search(r'name="token"\s+value="([^"]+)"', response.text)
            token = token_match.group(1) if token_match else ""
            
            # Submit URL
            response = await client.post(
                "https://snaptik.app/abc.php",
                data={
                    "url": url,
                    "token": token
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
                
                # Look for download link
                match = re.search(r'href="([^"]+)"[^>]*>.*?Download', html, re.IGNORECASE)
                if match:
                    download_url = match.group(1)
                    logger.info(f"✅ SnapTik v2 Success!")
                    
                    return {
                        "success": True,
                        "download_url": download_url,
                        "title": "TikTok Video",
                        "author": "Unknown",
                        "caption": "",
                        "thumbnail": "",
                        "api_source": "SnapTik-v2"
                    }
        
        return {"success": False, "error": "SnapTik: No download URL"}
    except Exception as e:
        logger.error(f"SnapTik v2 error: {e}")
        return {"success": False, "error": str(e)}

@app.get("/")
async def root():
    """API information endpoint"""
    return {
        "status": "running",
        "service": "CyberOrion TikTok Downloader API",
        "version": "6.0",
        "method": "Multi-API Watermark-Free System",
        "platform": "Render.com",
        "framework": "FastAPI",
        "apis": [
            "TikWM v2 (JSON API)",
            "TikVideo.app",
            "SaveTT.cc",
            "TTSave.app",
            "SnapTik v2"
        ],
        "features": [
            "5 different watermark-free APIs",
            "Automatic fallback",
            "No cookies required",
            "HD quality when available",
            "Rate limiting"
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
        "watermark_free": True,
        "apis_count": 5,
        "platform": "Render.com",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/download")
async def download_video(request: Request):
    """Download TikTok video - tries 5 different APIs"""
    try:
        # Rate limiting
        client_ip = request.client.host
        rate_limiter.check_rate_limit(client_ip)
        
        # Get request data
        data = await request.json()
        
        if not data or 'url' not in data:
            return JSONResponse(
                content={"success": False, "error": "No URL provided"},
                status_code=400
            )
        
        tiktok_url = data['url']
        
        # Validate TikTok URL
        valid_domains = ['tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com']
        if not any(domain in tiktok_url for domain in valid_domains):
            return JSONResponse(
                content={"success": False, "error": "Invalid TikTok URL"},
                status_code=400
            )
        
        # Check for photo posts
        if '/photo/' in tiktok_url:
            return JSONResponse(
                content={
                    "success": False, 
                    "error": "TikTok photo posts are not supported"
                },
                status_code=400
            )
        
        logger.info(f"🎬 Processing: {tiktok_url}")
        logger.info(f"📍 Client IP: {client_ip}")
        
        # Try all methods in sequence
        methods = [
            download_with_tikwm_v2,
            download_with_tikvideo,
            download_with_savett,
            download_with_ttsave,
            download_with_snaptik_v2
        ]
        
        result = None
        for method in methods:
            result = await method(tiktok_url)
            if result.get("success"):
                break
        
        if result and result.get("success"):
            logger.info(f"✅ Success via {result.get('api_source')} API")
            
            return JSONResponse(content={
                "success": True,
                "download_url": result["download_url"],
                "full_url": result["download_url"],
                "title": result.get("title", "TikTok Video"),
                "author": result.get("author", "Unknown"),
                "caption": result.get("caption", ""),
                "thumbnail": result.get("thumbnail", ""),
                "filename": "tiktok_video.mp4",
                "message": "Video ready for download (watermark-free)",
                "api_source": result.get("api_source"),
                "stats": {
                    "duration": result.get("duration", 0),
                    "plays": result.get("plays", 0),
                    "likes": result.get("likes", 0),
                    "comments": result.get("comments", 0),
                    "shares": result.get("shares", 0)
                }
            })
        else:
            logger.error("❌ All 5 APIs failed")
            return JSONResponse(
                content={
                    "success": False,
                    "error": "All download APIs failed",
                    "tried_apis": ["TikWM-v2", "TikVideo", "SaveTT", "TTSave", "SnapTik-v2"],
                    "suggestion": "Video might be private or URL invalid"
                },
                status_code=503
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error: {str(e)}", exc_info=True)
        return JSONResponse(
            content={"success": False, "error": f"Server error: {str(e)}"},
            status_code=500
        )

@app.options("/download")
async def download_options():
    """Handle CORS preflight"""
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
