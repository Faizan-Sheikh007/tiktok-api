import asyncio
import base64
import hashlib
import hmac
import ipaddress
import secrets
import socket
import logging
import tempfile
from pathlib import Path
import os
import re
import time
from collections import defaultdict, deque
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
from starlette.background import BackgroundTask

try:
    import yt_dlp
    from yt_dlp import YoutubeDL
except Exception:  # pragma: no cover
    yt_dlp = None
    YoutubeDL = None

try:
    import curl_cffi  # noqa: F401
    CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover
    CURL_CFFI_AVAILABLE = False

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("cybervid")

APP_VERSION = "6.3"
TIKWM_API = os.getenv("TIKWM_API_URL", "https://tikwm.com/api/").strip()
TDOWN_API = os.getenv("TDOWN_API_URL", "https://tdownv4.sl-bjs.workers.dev/").strip()
GODOWNLOADER_API = os.getenv(
    "GODOWNLOADER_API_URL",
    "https://godownloader.com/api/tiktok-no-watermark-free",
).strip()
ENABLE_TDOWN = os.getenv("ENABLE_TDOWN", "true").lower() == "true"
ENABLE_GODOWNLOADER = os.getenv("ENABLE_GODOWNLOADER", "false").lower() == "true"
ENABLE_YTDLP = os.getenv("ENABLE_YTDLP", "true").lower() == "true"
API_SHARED_SECRET = os.getenv("API_SHARED_SECRET", "").strip()
DEBUG_PROVIDER_ERRORS = os.getenv("DEBUG_PROVIDER_ERRORS", "false").lower() == "true"
MEDIA_PROXY_TTL = int(os.getenv("MEDIA_PROXY_TTL", "900"))
MEDIA_PROXY_SECRET = os.getenv("MEDIA_PROXY_SECRET", API_SHARED_SECRET).strip()
if not MEDIA_PROXY_SECRET:
    MEDIA_PROXY_SECRET = secrets.token_urlsafe(32)
    logger.warning("MEDIA_PROXY_SECRET/API_SHARED_SECRET is not configured; media links will stop working after a service restart.")

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



def _b64url_encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def _sign_media_token(token: str, expires: int) -> str:
    message = f"{token}.{expires}".encode("utf-8")
    return hmac.new(MEDIA_PROXY_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_media_proxy_url(request: Request, source_tiktok_url: str) -> str:
    # Sign the ORIGINAL TikTok post URL, not the provider's temporary CDN URL.
    # TikTok webapp-prime CDN links can be session/IP bound and often return 403
    # when replayed from another client. /media re-resolves and downloads the post
    # in one yt-dlp session instead.
    expires = int(time.time()) + max(60, MEDIA_PROXY_TTL)
    token = _b64url_encode(source_tiktok_url)
    signature = _sign_media_token(token, expires)
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/media?token={token}&expires={expires}&sig={signature}"


def proxify_result(result: dict[str, Any], request: Request, source_tiktok_url: str) -> dict[str, Any]:
    output = dict(result)
    proxy_url = build_media_proxy_url(request, source_tiktok_url)
    output["download_url"] = proxy_url
    output["full_url"] = proxy_url
    output["proxied"] = True
    output["proxy_mode"] = "yt-dlp-session"
    return output


async def validate_public_media_target(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Invalid media target")

    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise HTTPException(status_code=403, detail="Blocked media target")

    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HTTPException(status_code=502, detail="Could not resolve media host") from exc

    for address in addresses:
        ip_text = address[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(status_code=403, detail="Blocked media target")


async def open_media_stream(url: str, range_header: str | None):
    await validate_public_media_target(url)

    referers = [
        "https://www.tiktok.com/",
        "https://tikwm.com/",
        None,
    ]
    last_status = 502

    for referer in referers:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=12.0, read=45.0, write=45.0),
            follow_redirects=True,
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        if range_header:
            headers["Range"] = range_header

        try:
            upstream_request = client.build_request("GET", url, headers=headers)
            upstream = await client.send(upstream_request, stream=True, follow_redirects=True)
            last_status = upstream.status_code

            if upstream.status_code in {200, 206}:
                return client, upstream

            await upstream.aclose()
            await client.aclose()

            if upstream.status_code not in {401, 403, 429}:
                break
        except Exception:
            await client.aclose()
            raise

    raise HTTPException(status_code=502, detail=f"Media CDN rejected the download request (upstream HTTP {last_status})")

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


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_result(
    *,
    media_url: str,
    title: str = "TikTok Video",
    author: str = "Unknown",
    caption: str = "",
    thumbnail: str = "",
    duration: Any = 0,
    plays: Any = 0,
    likes: Any = 0,
    comments: Any = 0,
    shares: Any = 0,
    source: str,
) -> dict[str, Any]:
    if not media_url or not media_url.startswith(("http://", "https://")):
        raise RuntimeError(f"{source} returned an invalid media URL")
    return {
        "success": True,
        "download_url": media_url,
        "full_url": media_url,
        "title": title or "TikTok Video",
        "author": author or "Unknown",
        "caption": caption or title or "",
        "thumbnail": thumbnail or "",
        "duration": safe_int(duration),
        "plays": safe_int(plays),
        "likes": safe_int(likes),
        "comments": safe_int(comments),
        "shares": safe_int(shares),
        "filename": "tiktok_video.mp4",
        "api_source": source,
    }


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

    return normalize_result(
        media_url=media_url,
        title=video.get("title") or "TikTok Video",
        author=author,
        caption=video.get("title") or "",
        thumbnail=video.get("cover") or video.get("origin_cover") or "",
        duration=video.get("duration"),
        plays=video.get("play_count"),
        likes=video.get("digg_count"),
        comments=video.get("comment_count"),
        shares=video.get("share_count"),
        source="TikWM",
    )


async def download_with_tikwm(url: str) -> dict[str, Any]:
    # Use tikwm.com (not www) because current maintained wrappers target this host.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tikwm.com/",
    }
    timeout = httpx.Timeout(12.0, connect=7.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = await client.get(TIKWM_API, params={"url": url, "hd": "1"})
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        try:
            return parse_tikwm(response.json())
        except ValueError as exc:
            raise RuntimeError("non-JSON response") from exc


def _find_url(obj: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        for value in obj.values():
            found = _find_url(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_url(value, keys)
            if found:
                return found
    return None


def _find_text(obj: Any, keys: tuple[str, ...], default: str = "") -> str:
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _find_text(value, keys, "")
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_text(value, keys, "")
            if found:
                return found
    return default


async def download_with_tdown(url: str) -> dict[str, Any]:
    if not ENABLE_TDOWN:
        raise RuntimeError("disabled")
    timeout = httpx.Timeout(15.0, connect=7.0)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = await client.get(TDOWN_API, params={"down": url})
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("non-JSON response") from exc

    media_url = _find_url(data, ("download_url", "downloadUrl", "nowm", "no_watermark", "play", "video_url", "video"))
    if not media_url:
        raise RuntimeError(str(data.get("error") or data.get("message") or "no download URL"))

    author_obj = data.get("author") if isinstance(data, dict) else None
    if isinstance(author_obj, dict):
        author = author_obj.get("username") or author_obj.get("unique_id") or author_obj.get("nickname") or "Unknown"
    else:
        author = _find_text(data, ("username", "unique_id", "author"), "Unknown")

    return normalize_result(
        media_url=media_url,
        title=_find_text(data, ("title", "caption", "description"), "TikTok Video"),
        author=author,
        caption=_find_text(data, ("caption", "description", "title"), ""),
        thumbnail=_find_url(data, ("thumbnail", "cover", "image", "avatar")) or "",
        duration=(author_obj or {}).get("duration", 0) if isinstance(author_obj, dict) else 0,
        plays=(author_obj or {}).get("view_count", 0) if isinstance(author_obj, dict) else 0,
        likes=(author_obj or {}).get("like_count", 0) if isinstance(author_obj, dict) else 0,
        source="TDOWN",
    )


async def download_with_godownloader(url: str) -> dict[str, Any]:
    if not ENABLE_GODOWNLOADER:
        raise RuntimeError("disabled")
    timeout = httpx.Timeout(15.0, connect=7.0)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = await client.get(GODOWNLOADER_API, params={"url": url, "key": "godownloader.com"})
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("non-JSON response") from exc

    media_url = _find_url(data, ("download_url", "downloadUrl", "video", "video_url", "nowm", "play"))
    if not media_url:
        raise RuntimeError(str(data.get("error") or data.get("message") or "no download URL"))

    return normalize_result(
        media_url=media_url,
        title=_find_text(data, ("title", "caption", "description"), "TikTok Video"),
        author=_find_text(data, ("author", "username", "unique_id"), "Unknown"),
        caption=_find_text(data, ("caption", "description", "title"), ""),
        thumbnail=_find_url(data, ("thumbnail", "cover", "image")) or "",
        source="GoDownloader",
    )


def _yt_dlp_extract(url: str) -> dict[str, Any]:
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed")

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 25,
        "retries": 1,
        "extractor_retries": 1,
        "format": "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
        },
    }

    # Current yt-dlp can use curl_cffi browser impersonation automatically when
    # available. Keep the option explicit for generic HTTP requests as well.
    if CURL_CFFI_AVAILABLE:
        options["extractor_args"] = {"generic": {"impersonate": ["chrome"]}}

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

    title = info.get("title") or info.get("description") or "TikTok Video"
    return normalize_result(
        media_url=media_url,
        title=title,
        author=info.get("uploader_id") or info.get("uploader") or info.get("creator") or "Unknown",
        caption=info.get("description") or title,
        thumbnail=info.get("thumbnail") or "",
        duration=info.get("duration"),
        plays=info.get("view_count"),
        likes=info.get("like_count"),
        comments=info.get("comment_count"),
        shares=info.get("repost_count"),
        source="yt-dlp",
    )


async def download_with_ytdlp(url: str) -> dict[str, Any]:
    if not ENABLE_YTDLP:
        raise RuntimeError("disabled")
    return await asyncio.wait_for(asyncio.to_thread(_yt_dlp_extract, url), timeout=35)


async def try_provider(name: str, provider: Callable[[str], Awaitable[dict[str, Any]]], url: str):
    started = time.monotonic()
    try:
        result = await provider(url)
        return name, result, None, round((time.monotonic() - started) * 1000)
    except Exception as exc:
        return name, None, str(exc), round((time.monotonic() - started) * 1000)


async def race_primary_providers(url: str):
    providers: list[tuple[str, Callable[[str], Awaitable[dict[str, Any]]]]] = [("TikWM", download_with_tikwm)]
    if ENABLE_TDOWN:
        providers.append(("TDOWN", download_with_tdown))

    tasks = [asyncio.create_task(try_provider(name, provider, url)) for name, provider in providers]
    failures: list[dict[str, Any]] = []
    try:
        for future in asyncio.as_completed(tasks):
            name, result, error, elapsed_ms = await future
            if result:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                logger.info("Provider %s succeeded in %sms", name, elapsed_ms)
                return result, failures
            failures.append({"provider": name, "error": error, "elapsed_ms": elapsed_ms})
            logger.warning("Provider %s failed in %sms: %s", name, elapsed_ms, error)
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
    return None, failures


@app.get("/")
async def root():
    providers = ["TikWM"]
    if ENABLE_TDOWN:
        providers.append("TDOWN")
    if ENABLE_YTDLP:
        providers.append("yt-dlp")
    if ENABLE_GODOWNLOADER:
        providers.append("GoDownloader")
    return {
        "status": "running",
        "service": "CyberVid TikTok Downloader API",
        "version": APP_VERSION,
        "providers": providers,
    }


def _download_tiktok_to_temp(source_url: str) -> str:
    """Resolve and download a TikTok post inside one yt-dlp session.

    This intentionally avoids replaying provider-returned webapp-prime URLs,
    which can be tied to the resolver's session/IP and return HTTP 403 elsewhere.
    """
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed")

    fd, base_path = tempfile.mkstemp(prefix="cybervid_", suffix=".mp4")
    os.close(fd)
    try:
        os.unlink(base_path)
    except FileNotFoundError:
        pass

    outtmpl = base_path.rsplit(".mp4", 1)[0] + ".%(ext)s"
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "format": "best[ext=mp4]/best",
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "overwrites": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            "Referer": source_url,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    # If you later provide a Netscape cookies file on Render, yt-dlp can use it
    # without any code changes.
    cookies_file = os.getenv("TIKTOK_COOKIES_FILE", "").strip()
    if cookies_file:
        options["cookiefile"] = cookies_file

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(source_url, download=True)
        if not info:
            raise RuntimeError("yt-dlp returned no video information")
        if info.get("entries"):
            info = next((entry for entry in info["entries"] if entry), None)
            if not info:
                raise RuntimeError("yt-dlp returned an empty result")

        requested = info.get("requested_downloads") or []
        candidates: list[str] = []
        for item in requested:
            if isinstance(item, dict):
                fp = item.get("filepath")
                if fp:
                    candidates.append(fp)

        filename = info.get("_filename")
        if filename:
            candidates.append(filename)

        try:
            candidates.append(ydl.prepare_filename(info))
        except Exception:
            pass

    # yt-dlp may normalize the extension; locate the actual completed file.
    stem = Path(base_path).with_suffix("")
    candidates.extend(str(p) for p in stem.parent.glob(stem.name + ".*"))

    for candidate in candidates:
        p = Path(candidate)
        if p.is_file() and p.stat().st_size > 0 and not p.name.endswith(".part"):
            return str(p)

    raise RuntimeError("yt-dlp finished but no media file was created")


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "CyberVid media resolver",
        "version": APP_VERSION,
        "tikwm_api": TIKWM_API,
        "tdown_enabled": ENABLE_TDOWN,
        "yt_dlp_enabled": ENABLE_YTDLP,
        "yt_dlp_available": YoutubeDL is not None,
        "yt_dlp_version": getattr(getattr(yt_dlp, "version", None), "__version__", None) if yt_dlp else None,
        "curl_cffi_available": CURL_CFFI_AVAILABLE,
        "godownloader_enabled": ENABLE_GODOWNLOADER,
    }


@app.get("/media")
async def media_proxy(
    token: str,
    expires: int,
    sig: str,
):
    if expires < int(time.time()):
        raise HTTPException(status_code=410, detail="This download link has expired. Please generate it again.")

    expected = _sign_media_token(token, expires)
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail="Invalid download signature")

    try:
        source_url = _b64url_decode(token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid download token") from exc

    # The signed token must contain a TikTok post URL. This also prevents /media
    # from becoming an open proxy for arbitrary hosts.
    source_url = validate_tiktok_url(source_url)

    if not ENABLE_YTDLP or YoutubeDL is None:
        raise HTTPException(status_code=503, detail="Server-side downloader is unavailable")

    try:
        file_path = await asyncio.wait_for(
            asyncio.to_thread(_download_tiktok_to_temp, source_url),
            timeout=90,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="TikTok download timed out. Please try again.") from exc
    except Exception as exc:
        logger.warning("Server-side TikTok download failed: %s", exc)
        detail = "TikTok rejected the server-side download. Please try again with a fresh link."
        if DEBUG_PROVIDER_ERRORS:
            detail = f"{detail} ({exc})"
        raise HTTPException(status_code=502, detail=detail) from exc

    path = Path(file_path)
    suffix = path.suffix.lower() or ".mp4"
    media_type = "video/mp4" if suffix == ".mp4" else "application/octet-stream"

    def cleanup() -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Could not remove temporary media file %s", path)

    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=f"tiktok_video{suffix}",
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(cleanup),
    )


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

    errors: list[dict[str, Any]] = []

    # Race the two lightweight HTTP providers so a blocked provider does not
    # make users wait before the healthy provider is tried.
    result, primary_errors = await race_primary_providers(url)
    errors.extend(primary_errors)
    if result:
        return JSONResponse(proxify_result(result, request, url))

    # yt-dlp is heavier, so only invoke it after the fast providers fail.
    if ENABLE_YTDLP:
        name, result, error, elapsed_ms = await try_provider("yt-dlp", download_with_ytdlp, url)
        if result:
            logger.info("Provider %s succeeded in %sms", name, elapsed_ms)
            return JSONResponse(proxify_result(result, request, url))
        errors.append({"provider": name, "error": error, "elapsed_ms": elapsed_ms})
        logger.warning("Provider %s failed in %sms: %s", name, elapsed_ms, error)

    # The public GoDownloader endpoint has a very small free quota, so it is
    # opt-in rather than consuming the quota on every request.
    if ENABLE_GODOWNLOADER:
        name, result, error, elapsed_ms = await try_provider("GoDownloader", download_with_godownloader, url)
        if result:
            logger.info("Provider %s succeeded in %sms", name, elapsed_ms)
            return JSONResponse(proxify_result(result, request, url))
        errors.append({"provider": name, "error": error, "elapsed_ms": elapsed_ms})
        logger.warning("Provider %s failed in %sms: %s", name, elapsed_ms, error)

    response: dict[str, Any] = {
        "success": False,
        "error": "No download provider could resolve this public TikTok video right now.",
        "code": "ALL_PROVIDERS_FAILED",
        "version": APP_VERSION,
    }
    if DEBUG_PROVIDER_ERRORS:
        response["provider_errors"] = errors

    return JSONResponse(response, status_code=502)
