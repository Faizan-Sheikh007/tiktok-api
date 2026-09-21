import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl

try:
    import yt_dlp
    from yt_dlp import YoutubeDL
except Exception:
    yt_dlp = None
    YoutubeDL = None

try:
    import curl_cffi  # noqa: F401
    CURL_CFFI_AVAILABLE = True
except Exception:
    CURL_CFFI_AVAILABLE = False


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("cybervid")


APP_VERSION = "6.1"

TIKWM_API = os.getenv(
    "TIKWM_API_URL",
    "https://tikwm.com/api/",
).strip()

TDOWN_API = os.getenv(
    "TDOWN_API_URL",
    "https://tdownv4.sl-bjs.workers.dev/",
).strip()

GODOWNLOADER_API = os.getenv(
    "GODOWNLOADER_API_URL",
    "https://godownloader.com/api/tiktok-no-watermark-free",
).strip()


ENABLE_TDOWN = os.getenv(
    "ENABLE_TDOWN",
    "true",
).lower() == "true"

ENABLE_GODOWNLOADER = os.getenv(
    "ENABLE_GODOWNLOADER",
    "false",
).lower() == "true"

ENABLE_YTDLP = os.getenv(
    "ENABLE_YTDLP",
    "true",
).lower() == "true"

API_SHARED_SECRET = os.getenv(
    "API_SHARED_SECRET",
    "",
).strip()

DEBUG_PROVIDER_ERRORS = os.getenv(
    "DEBUG_PROVIDER_ERRORS",
    "false",
).lower() == "true"


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
    allow_headers=[
        "Content-Type",
        "Accept",
        "X-API-Key",
        "X-Client-IP",
        "X-CyberVid-Request",
    ],
)


class DownloadRequest(BaseModel):
    url: HttpUrl


class SlidingWindowLimiter:

    def __init__(
        self,
        limit: int = 30,
        window_seconds: int = 60,
    ):
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:

        now = time.monotonic()

        bucket = self.hits[key]

        while bucket and now - bucket[0] >= self.window:
            bucket.popleft()

        if len(bucket) >= self.limit:

            retry_after = max(
                1,
                int(
                    self.window
                    - (now - bucket[0])
                ),
            )

            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Too many requests",
                    "retry_after": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                },
            )

        bucket.append(now)


limiter = SlidingWindowLimiter(
    limit=int(
        os.getenv(
            "RATE_LIMIT_PER_MINUTE",
            "30",
        )
    ),
    window_seconds=60,
)


def validate_tiktok_url(value: str) -> str:

    parsed = urlparse(value)

    host = (
        parsed.hostname or ""
    ).lower()

    if parsed.scheme not in {
        "http",
        "https",
    }:
        raise HTTPException(
            status_code=400,
            detail="Only http/https URLs are supported",
        )

    if (
        host not in ALLOWED_HOSTS
        and not host.endswith(".tiktok.com")
    ):
        raise HTTPException(
            status_code=400,
            detail="Please provide a valid TikTok URL",
        )

    if "/photo/" in parsed.path:
        raise HTTPException(
            status_code=400,
            detail="TikTok photo posts are not currently supported",
        )

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

    if (
        not media_url
        or not media_url.startswith(
            (
                "http://",
                "https://",
            )
        )
    ):
        raise RuntimeError(
            f"{source} returned an invalid media URL"
        )

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


def parse_tikwm(
    data: dict[str, Any],
) -> dict[str, Any]:

    if data.get("code") not in (
        0,
        "0",
    ):
        raise RuntimeError(
            str(
                data.get("msg")
                or data.get("message")
                or "TikWM rejected the request"
            )
        )

    video = data.get("data") or {}

    media_url = (
        video.get("hdplay")
        or video.get("play")
        or video.get("wmplay")
    )

    if not media_url:
        raise RuntimeError(
            "TikWM returned no playable video URL"
        )

    author_data = (
        video.get("author")
        or {}
    )

    if isinstance(
        author_data,
        dict,
    ):
        author = (
            author_data.get("unique_id")
            or author_data.get("nickname")
            or "Unknown"
        )
    else:
        author = str(
            author_data
            or "Unknown"
        )

    return normalize_result(
        media_url=media_url,
        title=(
            video.get("title")
            or "TikTok Video"
        ),
        author=author,
        caption=(
            video.get("title")
            or ""
        ),
        thumbnail=(
            video.get("cover")
            or video.get("origin_cover")
            or ""
        ),
        duration=video.get(
            "duration"
        ),
        plays=video.get(
            "play_count"
        ),
        likes=video.get(
            "digg_count"
        ),
        comments=video.get(
            "comment_count"
        ),
        shares=video.get(
            "share_count"
        ),
        source="TikWM",
    )


async def download_with_tikwm(
    url: str,
) -> dict[str, Any]:

    headers = {
        "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36",
        "Accept":
            "application/json, text/plain, */*",
        "Accept-Language":
            "en-US,en;q=0.9",
        "Referer":
            "https://tikwm.com/",
    }

    timeout = httpx.Timeout(
        12.0,
        connect=7.0,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:

        response = await client.get(
            TIKWM_API,
            params={
                "url": url,
                "hd": "1",
            },
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"HTTP {response.status_code}"
            )

        try:
            return parse_tikwm(
                response.json()
            )

        except ValueError as exc:
            raise RuntimeError(
                "non-JSON response"
            ) from exc


def _find_url(
    obj: Any,
    keys: tuple[str, ...],
) -> str | None:

    if isinstance(
        obj,
        dict,
    ):

        for key in keys:

            value = obj.get(
                key
            )

            if (
                isinstance(
                    value,
                    str,
                )
                and value.startswith(
                    (
                        "http://",
                        "https://",
                    )
                )
            ):
                return value

        for value in obj.values():

            found = _find_url(
                value,
                keys,
            )

            if found:
                return found

    elif isinstance(
        obj,
        list,
    ):

        for value in obj:

            found = _find_url(
                value,
                keys,
            )

            if found:
                return found

    return None


def _find_text(
    obj: Any,
    keys: tuple[str, ...],
    default: str = "",
) -> str:

    if isinstance(
        obj,
        dict,
    ):

        for key in keys:

            value = obj.get(
                key
            )

            if (
                isinstance(
                    value,
                    str,
                )
                and value.strip()
            ):
                return value.strip()

        for value in obj.values():

            found = _find_text(
                value,
                keys,
                "",
            )

            if found:
                return found

    elif isinstance(
        obj,
        list,
    ):

        for value in obj:

            found = _find_text(
                value,
                keys,
                "",
            )

            if found:
                return found

    return default


async def download_with_tdown(
    url: str,
) -> dict[str, Any]:

    if not ENABLE_TDOWN:
        raise RuntimeError(
            "disabled"
        )

    timeout = httpx.Timeout(
        15.0,
        connect=7.0,
    )

    headers = {
        "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/153.0.0.0 Safari/537.36",
        "Accept":
            "application/json, text/plain, */*",
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:

        response = await client.get(
            TDOWN_API,
            params={
                "down": url,
            },
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"HTTP {response.status_code}"
            )

        try:
            data = response.json()

        except ValueError as exc:
            raise RuntimeError(
                "non-JSON response"
            ) from exc

    media_url = _find_url(
        data,
        (
            "download_url",
            "downloadUrl",
            "nowm",
            "no_watermark",
            "play",
            "video_url",
            "video",
        ),
    )

    if not media_url:

        raise RuntimeError(
            str(
                data.get("error")
                or data.get("message")
                or "no download URL"
            )
        )

    author_obj = (
        data.get("author")
        if isinstance(
            data,
            dict,
        )
        else None
    )

    if isinstance(
        author_obj,
        dict,
    ):
        author = (
            author_obj.get("username")
            or author_obj.get("unique_id")
            or author_obj.get("nickname")
            or "Unknown"
        )

    else:
        author = _find_text(
            data,
            (
                "username",
                "unique_id",
                "author",
            ),
            "Unknown",
        )

    return normalize_result(
        media_url=media_url,
        title=_find_text(
            data,
            (
                "title",
                "caption",
                "description",
            ),
            "TikTok Video",
        ),
        author=author,
        caption=_find_text(
            data,
            (
                "caption",
                "description",
                "title",
            ),
            "",
        ),
        thumbnail=(
            _find_url(
                data,
                (
                    "thumbnail",
                    "cover",
                    "image",
                    "avatar",
                ),
            )
            or ""
        ),
        duration=(
            (author_obj or {}).get(
                "duration",
                0,
            )
            if isinstance(
                author_obj,
                dict,
            )
            else 0
        ),
        plays=(
            (author_obj or {}).get(
                "view_count",
                0,
            )
            if isinstance(
                author_obj,
                dict,
            )
            else 0
        ),
        likes=(
            (author_obj or {}).get(
                "like_count",
                0,
            )
            if isinstance(
                author_obj,
                dict,
            )
            else 0
        ),
        source="TDOWN",
    )


async def download_with_godownloader(
    url: str,
) -> dict[str, Any]:

    if not ENABLE_GODOWNLOADER:
        raise RuntimeError(
            "disabled"
        )

    timeout = httpx.Timeout(
        15.0,
        connect=7.0,
    )

    headers = {
        "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/153.0.0.0 Safari/537.36",
        "Accept":
            "application/json, text/plain, */*",
    }

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:

        response = await client.get(
            GODOWNLOADER_API,
            params={
                "url": url,
                "key": "godownloader.com",
            },
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"HTTP {response.status_code}"
            )

        try:
            data = response.json()

        except ValueError as exc:
            raise RuntimeError(
                "non-JSON response"
            ) from exc

    media_url = _find_url(
        data,
        (
            "download_url",
            "downloadUrl",
            "video",
            "video_url",
            "nowm",
            "play",
        ),
    )

    if not media_url:

        raise RuntimeError(
            str(
                data.get("error")
                or data.get("message")
                or "no download URL"
            )
        )

    return normalize_result(
        media_url=media_url,
        title=_find_text(
            data,
            (
                "title",
                "caption",
                "description",
            ),
            "TikTok Video",
        ),
        author=_find_text(
            data,
            (
                "author",
                "username",
                "unique_id",
            ),
            "Unknown",
        ),
        caption=_find_text(
            data,
            (
                "caption",
                "description",
                "title",
            ),
            "",
        ),
        thumbnail=(
            _find_url(
                data,
                (
                    "thumbnail",
                    "cover",
                    "image",
                ),
            )
            or ""
        ),
        source="GoDownloader",
    )


def _yt_dlp_extract(
    url: str,
) -> dict[str, Any]:

    if YoutubeDL is None:
        raise RuntimeError(
            "yt-dlp is not installed"
        )

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 25,
        "retries": 1,
        "extractor_retries": 1,
        "format":
            "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/153.0.0.0 Safari/537.36"
        },
    }

    if CURL_CFFI_AVAILABLE:

        options[
            "extractor_args"
        ] = {
            "generic": {
                "impersonate": [
                    "chrome"
                ]
            }
        }

    cookies_file = os.getenv(
        "TIKTOK_COOKIES_FILE",
        "",
    ).strip()

    if cookies_file:
        options[
            "cookiefile"
        ] = cookies_file

    with YoutubeDL(
        options
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=False,
        )

    if not info:
        raise RuntimeError(
            "yt-dlp returned no video information"
        )

    if info.get(
        "entries"
    ):

        info = next(
            (
                entry
                for entry in info[
                    "entries"
                ]
                if entry
            ),
            None,
        )

        if not info:
            raise RuntimeError(
                "yt-dlp returned an empty result"
            )

    media_url = info.get(
        "url"
    )

    if not media_url:

        candidates = [
            f
            for f in (
                info.get(
                    "formats"
                )
                or []
            )
            if (
                f.get("url")
                and f.get(
                    "vcodec"
                ) != "none"
                and str(
                    f.get(
                        "protocol",
                        "",
                    )
                ).startswith(
                    "http"
                )
            )
        ]

        candidates.sort(
            key=lambda f: (
                f.get(
                    "height"
                )
                or 0,
                f.get(
                    "tbr"
                )
                or 0,
            ),
            reverse=True,
        )

        if candidates:
            media_url = (
                candidates[
                    0
                ]["url"]
            )

    if not media_url:
        raise RuntimeError(
            "yt-dlp found the video but no direct media URL"
        )

    title = (
        info.get("title")
        or info.get(
            "description"
        )
        or "TikTok Video"
    )

    return normalize_result(
        media_url=media_url,
        title=title,
        author=(
            info.get(
                "uploader_id"
            )
            or info.get(
                "uploader"
            )
            or info.get(
                "creator"
            )
            or "Unknown"
        ),
        caption=(
            info.get(
                "description"
            )
            or title
        ),
        thumbnail=(
            info.get(
                "thumbnail"
            )
            or ""
        ),
        duration=info.get(
            "duration"
        ),
        plays=info.get(
            "view_count"
        ),
        likes=info.get(
            "like_count"
        ),
        comments=info.get(
            "comment_count"
        ),
        shares=info.get(
            "repost_count"
        ),
        source="yt-dlp",
    )


async def download_with_ytdlp(
    url: str,
) -> dict[str, Any]:

    if not ENABLE_YTDLP:
        raise RuntimeError(
            "disabled"
        )

    return await asyncio.wait_for(
        asyncio.to_thread(
            _yt_dlp_extract,
            url,
        ),
        timeout=35,
    )


async def try_provider(
    name: str,
    provider: Callable[
        [
            str
        ],
        Awaitable[
            dict[
                str,
                Any,
            ]
        ],
    ],
    url: str,
):

    started = time.monotonic()

    try:

        result = await provider(
            url
        )

        return (
            name,
            result,
            None,
            round(
                (
                    time.monotonic()
                    - started
                )
                * 1000
            ),
        )

    except Exception as exc:

        return (
            name,
            None,
            str(exc),
            round(
                (
                    time.monotonic()
                    - started
                )
                * 1000
            ),
        )


async def race_primary_providers(
    url: str,
):

    providers: list[
        tuple[
            str,
            Callable[
                [
                    str
                ],
                Awaitable[
                    dict[
                        str,
                        Any,
                    ]
                ],
            ],
        ]
    ] = [
        (
            "TikWM",
            download_with_tikwm,
        )
    ]

    if ENABLE_TDOWN:

        providers.append(
            (
                "TDOWN",
                download_with_tdown,
            )
        )

    tasks = [
        asyncio.create_task(
            try_provider(
                name,
                provider,
                url,
            )
        )
        for (
            name,
            provider,
        ) in providers
    ]

    failures: list[
        dict[
            str,
            Any,
        ]
    ] = []

    try:

        for future in asyncio.as_completed(
            tasks
        ):

            (
                name,
                result,
                error,
                elapsed_ms,
            ) = await future

            if result:

                for task in tasks:

                    if not task.done():
                        task.cancel()

                logger.info(
                    "Provider %s succeeded in %sms",
                    name,
                    elapsed_ms,
                )

                return (
                    result,
                    failures,
                )

            failures.append(
                {
                    "provider": name,
                    "error": error,
                    "elapsed_ms":
                        elapsed_ms,
                }
            )

            logger.warning(
                "Provider %s failed in %sms: %s",
                name,
                elapsed_ms,
                error,
            )

    finally:

        await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

    return (
        None,
        failures,
    )


@app.get("/")
async def root():

    providers = [
        "TikWM"
    ]

    if ENABLE_TDOWN:
        providers.append(
            "TDOWN"
        )

    if ENABLE_YTDLP:
        providers.append(
            "yt-dlp"
        )

    if ENABLE_GODOWNLOADER:
        providers.append(
            "GoDownloader"
        )

    return {
        "status":
            "running",
        "service":
            "CyberVid TikTok Downloader API",
        "version":
            APP_VERSION,
        "providers":
            providers,
    }


@app.get("/health")
async def health():

    return {
        "status":
            "healthy",
        "service":
            "CyberVid media resolver",
        "version":
            APP_VERSION,
        "tikwm_api":
            TIKWM_API,
        "tdown_enabled":
            ENABLE_TDOWN,
        "yt_dlp_enabled":
            ENABLE_YTDLP,
        "yt_dlp_available":
            YoutubeDL is not None,
        "yt_dlp_version":
            getattr(
                getattr(
                    yt_dlp,
                    "version",
                    None,
                ),
                "__version__",
                None,
            )
            if yt_dlp
            else None,
        "curl_cffi_available":
            CURL_CFFI_AVAILABLE,
        "godownloader_enabled":
            ENABLE_GODOWNLOADER,
    }


@app.post("/download")
async def download_video(
    payload: DownloadRequest,
    request: Request,
    x_client_ip: str | None = Header(
        default=None,
        alias="X-Client-IP",
    ),
    x_api_key: str | None = Header(
        default=None,
        alias="X-API-Key",
    ),
):

    if (
        API_SHARED_SECRET
        and x_api_key
        != API_SHARED_SECRET
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

    client_ip = (
        x_client_ip
        or (
            request.client.host
            if request.client
            else "unknown"
        )
    ).split(",")[0].strip()

    limiter.check(
        client_ip
    )

    url = validate_tiktok_url(
        str(
            payload.url
        )
    )

    errors: list[
        dict[
            str,
            Any,
        ]
    ] = []

    result, primary_errors = (
        await race_primary_providers(
            url
        )
    )

    errors.extend(
        primary_errors
    )

    if result:
        return JSONResponse(
            result
        )

    if ENABLE_YTDLP:

        (
            name,
            result,
            error,
            elapsed_ms,
        ) = await try_provider(
            "yt-dlp",
            download_with_ytdlp,
            url,
        )

        if result:

            logger.info(
                "Provider %s succeeded in %sms",
                name,
                elapsed_ms,
            )

            return JSONResponse(
                result
            )

        errors.append(
            {
                "provider":
                    name,
                "error":
                    error,
                "elapsed_ms":
                    elapsed_ms,
            }
        )

        logger.warning(
            "Provider %s failed in %sms: %s",
            name,
            elapsed_ms,
            error,
        )

    if ENABLE_GODOWNLOADER:

        (
            name,
            result,
            error,
            elapsed_ms,
        ) = await try_provider(
            "GoDownloader",
            download_with_godownloader,
            url,
        )

        if result:

            logger.info(
                "Provider %s succeeded in %sms",
                name,
                elapsed_ms,
            )

            return JSONResponse(
                result
            )

        errors.append(
            {
                "provider":
                    name,
                "error":
                    error,
                "elapsed_ms":
                    elapsed_ms,
            }
        )

        logger.warning(
            "Provider %s failed in %sms: %s",
            name,
            elapsed_ms,
            error,
        )

    response: dict[
        str,
        Any,
    ] = {
        "success":
            False,
        "error":
            "No download provider could resolve this public TikTok video right now.",
        "code":
            "ALL_PROVIDERS_FAILED",
        "version":
            APP_VERSION,
    }

    if DEBUG_PROVIDER_ERRORS:
        response[
            "provider_errors"
        ] = errors

    return JSONResponse(
        response,
        status_code=502,
    )
