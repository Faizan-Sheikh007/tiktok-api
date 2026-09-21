import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

try:
    from yt_dlp import YoutubeDL
except Exception:  # pragma: no cover
    YoutubeDL = None

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("cybervid")

APP_VERSION = "5.0"
TIKWM_API = "https://www.tikwm.com/api/"
API_SHARED_SECRET = os.getenv("API_SHARED_SECRET", "").strip()
DEBUG_PROVIDER_ERRORS = os.getenv("DEBUG_PROVIDER_ERRORS", "false").lower() == "true"

ALLOWED_ORIGINS = [
    "https://cybervid.online",
    "https://www.cybervid.online",
    "http://localhost:3000",
    "http://localhost:5500",
    "http://localhost:8000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5500",
    "http://127.0.0.1:8000",
]
ALLOWED_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}

app = FastAPI(
    title="CyberVid TikTok Downloader API",
    version=APP_VERSION,
    description="Server-side media resolver used by cybervid.online",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "X-API-Key", "X-Client-IP", "X-CyberVid-Request"],
)


class DownloadRequest(BaseModel):
    url: HttpUrl


class SlidingWindowLimiter:
    def __init__(self, limit: int = 30, window_seconds: int = 60):
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self.hits[key]
        while bucket and now - bucket[0] >= self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry_after = max(1, int(self.window - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail={"error": "Too many requests", "retry_after": retry_after},
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


limiter = SlidingWindowLimiter(
    limit=int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
    window_seconds=60,
)


def validate_tiktok_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Only http/https URLs are supported")
    if host not in ALLOWED_HOSTS and not host.endswith(".tiktok.com"):
        raise HTTPException(status_code=400, detail="Please provide a valid TikTok URL")
    if "/photo/" in parsed.path:
        raise HTTPException(status_code=400, detail="TikTok photo posts are not currently supported")
    return value


def parse_tikwm(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("code") not in (0, "0"):
        raise RuntimeError(str(data.get("msg") or data.get("message") or "TikWM rejected the request"))

    video = data.get("data") or {}
    media_url = video.get("hdplay") or video.get("play") or video.get("wmplay")
    if not media_url:
        raise RuntimeError("TikWM returned no playable video URL")

    author_data = video.get("author") or {}
    if isinstance(author_data, dict):
        author = author_data.get("unique_id") or author_data.get("nickname") or "Unknown"
    else:
        author = str(author_data or "Unknown")

    return {
        "success": True,
        "download_url": media_url,
        "full_url": media_url,
        "title": video.get("title") or "TikTok Video",
        "author": author,
        "caption": video.get("title") or "",
        "thumbnail": video.get("cover") or video.get("origin_cover") or "",
        "duration": video.get("duration") or 0,
        "plays": video.get("play_count") or 0,
        "likes": video.get("digg_count") or 0,
        "comments": video.get("comment_count") or 0,
        "shares": video.get("share_count") or 0,
        "filename": "tiktok_video.mp4",
        "api_source": "TikWM",
    }


async def download_with_tikwm(url: str) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.tikwm.com",
        "Referer": "https://www.tikwm.com/",
    }
    timeout = httpx.Timeout(30.0, connect=12.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        # TikWM's public interface is primarily GET-based. The older CyberVid
        # deployment only POSTed here, so try GET first and keep POST as a compatibility fallback.
        last_error = "TikWM request failed"
        for method in ("GET", "POST"):
            try:
                if method == "GET":
                    response = await client.get(TIKWM_API, params={"url": url, "hd": "1"})
                else:
                    response = await client.post(TIKWM_API, data={"url": url, "hd": "1"})

                if response.status_code != 200:
                    last_error = f"TikWM returned HTTP {response.status_code}"
                    continue

                try:
                    payload = response.json()
                except ValueError:
                    last_error = "TikWM returned a non-JSON response"
                    continue

                return parse_tikwm(payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"TikWM network error: {type(exc).__name__}"
            except Exception as exc:
                last_error = str(exc)

    raise RuntimeError(last_error)


def _yt_dlp_extract(url: str) -> dict[str, Any]:
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed")

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 30,
        "retries": 1,
        "format": "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
        },
    }

    cookies_file = os.getenv("TIKTOK_COOKIES_FILE", "").strip()
    if cookies_file:
        options["cookiefile"] = cookies_file

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("yt-dlp returned no video information")
    if info.get("entries"):
        info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise RuntimeError("yt-dlp returned an empty result")

    media_url = info.get("url")
    if not media_url:
        candidates = [
            f for f in (info.get("formats") or [])
            if f.get("url") and f.get("vcodec") != "none" and str(f.get("protocol", "")).startswith("http")
        ]
        candidates.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
        if candidates:
            media_url = candidates[0]["url"]

    if not media_url:
        raise RuntimeError("yt-dlp found the video but no direct media URL")

    thumbnail = info.get("thumbnail") or ""
    author = info.get("uploader_id") or info.get("uploader") or info.get("creator") or "Unknown"
    title = info.get("title") or info.get("description") or "TikTok Video"

    return {
        "success": True,
        "download_url": media_url,
        "full_url": media_url,
        "title": title,
        "author": author,
        "caption": info.get("description") or title,
        "thumbnail": thumbnail,
        "duration": info.get("duration") or 0,
        "plays": info.get("view_count") or 0,
        "likes": info.get("like_count") or 0,
        "comments": info.get("comment_count") or 0,
        "shares": info.get("repost_count") or 0,
        "filename": "tiktok_video.mp4",
        "api_source": "yt-dlp",
    }


async def download_with_ytdlp(url: str) -> dict[str, Any]:
    return await asyncio.wait_for(asyncio.to_thread(_yt_dlp_extract, url), timeout=45)


@app.get("/")
async def root():
    return {
        "status": "running",
        "service": "CyberVid TikTok Downloader API",
        "version": APP_VERSION,
        "providers": ["TikWM GET/POST", "yt-dlp fallback"],
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "CyberVid media resolver",
        "version": APP_VERSION,
        "yt_dlp_available": YoutubeDL is not None,
    }


@app.post("/download")
async def download_video(
    payload: DownloadRequest,
    request: Request,
    x_client_ip: str | None = Header(default=None, alias="X-Client-IP"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    if API_SHARED_SECRET and x_api_key != API_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    client_ip = (x_client_ip or (request.client.host if request.client else "unknown")).split(",")[0].strip()
    limiter.check(client_ip)

    url = validate_tiktok_url(str(payload.url))
    errors: list[str] = []

    providers = (
        ("TikWM", download_with_tikwm),
        ("yt-dlp", download_with_ytdlp),
    )

    for name, provider in providers:
        try:
            logger.info("Trying provider %s", name)
            result = await provider(url)
            logger.info("Provider %s succeeded", name)
            return JSONResponse(result)
        except Exception as exc:
            message = f"{name}: {exc}"
            errors.append(message)
            logger.warning("Provider failed: %s", message)

    response = {
        "success": False,
        "error": "The TikTok provider is temporarily unavailable or rejected this video. Please verify the video is public and try again shortly.",
        "tried_apis": [name for name, _ in providers],
    }
    if DEBUG_PROVIDER_ERRORS:
        response["provider_errors"] = errors

    return JSONResponse(response, status_code=502)
