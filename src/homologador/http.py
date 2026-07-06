"""Cliente HTTP async compartido: concurrencia limitada, rate-limit, retry y caché en disco.

La caché en disco es clave para la optimización: re-corridas y desarrollo no vuelven a
golpear los sitios. Se cachea por hash de la URL con TTL configurable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import time
from pathlib import Path
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config


class RateLimiter:
    """Limitador simple de requests/segundo (token-bucket aproximado por sleep)."""

    def __init__(self, per_sec: float):
        self._min_interval = 1.0 / per_sec if per_sec > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


class HttpClient:
    """Wrapper de httpx.AsyncClient con caché, semáforo de concurrencia y rate-limit."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.timeout = cfg.get("http.timeout_sec", 25)
        self.max_retries = cfg.get("http.max_retries", 4)
        self.cache_enabled = cfg.get("http.cache_enabled", True)
        self.cache_ttl = cfg.get("http.cache_ttl_sec", 86400)
        self.cache_dir: Path = cfg.path("paths.cache_dir")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(cfg.get("http.concurrency", 8))
        self._limiter = RateLimiter(cfg.get("http.rate_limit_per_sec", 6))
        self._client: Optional[httpx.AsyncClient] = None
        self.stats = {"hits": 0, "misses": 0, "errors": 0}

    async def __aenter__(self) -> "HttpClient":
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"Accept-Language": "es-PE,es;q=0.9"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    # -- caché en disco ----------------------------------------------------
    def _cache_file(self, key: str) -> Path:
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{h}.txt"

    def _read_cache(self, url: str) -> Optional[str]:
        if not self.cache_enabled:
            return None
        f = self._cache_file(url)
        if not f.exists():
            return None
        if self.cache_ttl and (time.time() - f.stat().st_mtime) > self.cache_ttl:
            return None
        return f.read_text(encoding="utf-8")

    def _write_cache(self, url: str, text: str) -> None:
        if self.cache_enabled:
            self._cache_file(url).write_text(text, encoding="utf-8")

    # -- fetch -------------------------------------------------------------
    async def get_text(
        self, url: str, headers: Optional[dict] = None, use_cache: bool = True
    ) -> Optional[str]:
        """GET que devuelve texto (o None si 404/agotó reintentos). Usa caché si aplica."""
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self.stats["hits"] += 1
                return cached

        @retry(
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.HTTPStatusError)
            ),
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, max=10),
            reraise=True,
        )
        async def _do() -> httpx.Response:
            assert self._client is not None
            await self._limiter.acquire()
            r = await self._client.get(url, headers=headers)
            # 5xx y 429 se reintentan; 404 NO (no es transitorio)
            if r.status_code >= 500 or r.status_code == 429:
                r.raise_for_status()
            return r

        async with self._sem:
            try:
                resp = await _do()
            except Exception:
                self.stats["errors"] += 1
                return None

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            self.stats["errors"] += 1
            return None

        self.stats["misses"] += 1
        text = resp.text
        if use_cache and text:  # nunca cachear respuestas vacías (evita envenenar la caché)
            self._write_cache(url, text)
        return text

    async def get_count(self, url: str, use_cache: bool = True) -> Optional[int]:
        """GET que devuelve el total del header `resources` de VTEX (formato '0-0/N')."""
        key = "count:" + url
        if use_cache:
            cached = self._read_cache(key)
            if cached is not None:
                self.stats["hits"] += 1
                return int(cached) if cached.isdigit() else None
        async with self._sem:
            try:
                await self._limiter.acquire()
                assert self._client is not None
                resp = await self._client.get(url)
            except Exception:
                self.stats["errors"] += 1
                return None
        if resp.status_code >= 400:
            self.stats["errors"] += 1
            return None
        res = resp.headers.get("resources", "")
        total = None
        if "/" in res:
            tail = res.split("/")[-1]
            if tail.isdigit():
                total = int(tail)
        self.stats["misses"] += 1
        if use_cache and total is not None:
            self._write_cache(key, str(total))
        return total

    async def post_json(
        self, url: str, payload: dict, headers: Optional[dict] = None,
        use_cache: bool = True,
    ) -> Optional[str]:
        """POST JSON con caché por (url + body). Devuelve el texto de respuesta o None."""
        body = _json.dumps(payload, sort_keys=True)
        cache_key = url + "|" + body
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                self.stats["hits"] += 1
                return cached
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        async with self._sem:
            try:
                await self._limiter.acquire()
                assert self._client is not None
                resp = await self._client.post(url, content=body, headers=hdrs)
            except Exception:
                self.stats["errors"] += 1
                return None
        if resp.status_code >= 400:
            self.stats["errors"] += 1
            return None
        self.stats["misses"] += 1
        if use_cache and resp.text:
            self._write_cache(cache_key, resp.text)
        return resp.text
