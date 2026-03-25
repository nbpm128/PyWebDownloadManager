from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from schemas.url_meta import UrlMetaRequest, UrlMetaResponse, UrlMetaItem

logger = logging.getLogger(__name__)

# Timeout for all outgoing HTTP requests (seconds)
_TIMEOUT = 20.0


class UrlMetaService:
    """
    Fetches filename / file-size / checksum metadata for a URL on behalf of
    the frontend, avoiding browser CORS restrictions entirely.

    Supported sources (with rich metadata):
      • HuggingFace  — resolves blob→resolve URL, scrapes SHA-256 from blob page
      • CivitAI      — queries the /api/v1/model-versions endpoint
      • Generic      — HEAD then GET Range:bytes=0-0, reads Content-Disposition /
                        Content-Length / Content-Range headers

    All requests are made server-side so no CORS proxy is needed.
    Auth headers/cookies supplied by the caller are forwarded as-is.
    """

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def fetch_meta(self, request: UrlMetaRequest) -> UrlMetaResponse:
        url = (request.url or "").strip()
        if not url:
            return UrlMetaResponse(success=False, error="URL is required")

        merged_headers = self._build_headers(request)

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ""

            if hostname == "huggingface.co":
                item = await self._fetch_hf(url, merged_headers)
            elif "civitai.com" in hostname:
                item = await self._fetch_civitai(url, merged_headers)
            else:
                item = await self._fetch_generic(url, merged_headers)

            logger.info(
                "url_meta resolved | url=%s | file=%s | size=%s",
                url, item.file_name, item.file_size,
            )
            return UrlMetaResponse(success=True, item=item)

        except Exception as exc:
            logger.warning("url_meta failed | url=%s | error=%s", url, exc)
            # Return a fallback item so the frontend can still add the URL manually
            fallback = self._fallback_item(url)
            return UrlMetaResponse(
                success=True,
                item=fallback,
                warning=f"Metadata fetch failed ({exc}); please fill in details manually",
            )

    # ------------------------------------------------------------------
    # HuggingFace
    # ------------------------------------------------------------------

    async def _fetch_hf(self, url: str, hdrs: dict) -> UrlMetaItem:
        resolve_url = re.sub(r"/blob/", "/resolve/", url)
        blob_url    = re.sub(r"/resolve/", "/blob/", url)

        item = UrlMetaItem(
            id=url.rstrip("/").split("/")[-1],
            mirrors=[{"url": resolve_url}],
        )

        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as client:
            # 1. HEAD on resolve URL → filename + size
            try:
                r = await client.head(resolve_url, headers=hdrs)
                fname = self._filename_from_cd(r.headers.get("content-disposition"))
                if not fname:
                    fname = resolve_url.rstrip("/").split("/")[-1]
                item.id        = fname
                item.file_name = fname
                cl = r.headers.get("content-length")
                if cl and int(cl) > 1:
                    item.file_size = int(cl)
            except Exception as exc:
                logger.debug("HF HEAD failed | url=%s | %s", resolve_url, exc)

            # 2. Scrape SHA-256 from blob page
            try:
                r2 = await client.get(blob_url, headers=hdrs)
                sha = self._extract_sha256(r2.text)
                if sha:
                    item.checksum        = sha
                    item.hash_algorithm  = "sha256"
            except Exception as exc:
                logger.debug("HF blob scrape failed | url=%s | %s", blob_url, exc)

        return item

    # ------------------------------------------------------------------
    # CivitAI
    # ------------------------------------------------------------------

    async def _fetch_civitai(self, url: str, hdrs: dict) -> UrlMetaItem:
        parsed    = urlparse(url)
        qs        = parse_qs(parsed.query)
        want_type = (qs.get("type",    [""])[0]).lower()
        want_fmt  = (qs.get("format",  [""])[0]).lower()
        version_id: Optional[str] = None

        # Pattern: /api/download/models/{versionId}
        dl_match = re.search(r"/api/download/models/(\d+)", parsed.path)
        if dl_match:
            version_id = dl_match.group(1)

        # Pattern: /models/{modelId}[/slug][?modelVersionId=...]
        if not version_id:
            page_match = re.search(r"/models/(\d+)", parsed.path)
            if page_match:
                version_id = qs.get("modelVersionId", [None])[0]
                if not version_id:
                    # Fetch model to get latest version ID
                    version_id = await self._civitai_latest_version(
                        page_match.group(1), hdrs
                    )

        if not version_id:
            raise ValueError("Could not determine CivitAI version ID from URL")

        return await self._civitai_item_by_version(
            version_id, url, hdrs, want_type, want_fmt
        )

    async def _civitai_latest_version(self, model_id: str, hdrs: dict) -> str:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as client:
            r = await client.get(
                f"https://civitai.com/api/v1/models/{model_id}", headers=hdrs
            )
            r.raise_for_status()
            versions = r.json().get("modelVersions", [])
            if not versions:
                raise ValueError("No model versions found")
            return str(versions[0]["id"])

    async def _civitai_item_by_version(
        self,
        version_id: str,
        original_url: str,
        hdrs: dict,
        want_type: str,
        want_fmt: str,
    ) -> UrlMetaItem:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as client:
            r = await client.get(
                f"https://civitai.com/api/v1/model-versions/{version_id}",
                headers=hdrs,
            )
            r.raise_for_status()
            data = r.json()

        files: list[dict] = data.get("files", [])
        if not files:
            raise ValueError("No files in CivitAI API response")

        chosen = files[0]
        if want_type or want_fmt:
            for f in files:
                ft  = (f.get("type") or "").lower()
                ff  = ((f.get("metadata") or {}).get("format") or "").lower()
                type_ok = not want_type or ft == want_type or ft.replace(" ", "") == want_type
                fmt_ok  = not want_fmt  or ff == want_fmt
                if type_ok and fmt_ok:
                    chosen = f
                    break

        item = UrlMetaItem(
            id=chosen.get("name") or f"civitai_{version_id}",
            mirrors=[{"url": original_url}],
        )
        if chosen.get("name"):
            item.file_name = chosen["name"]
        if chosen.get("sizeKB"):
            item.file_size = int(chosen["sizeKB"] * 1024)
        hashes = chosen.get("hashes") or {}
        if hashes.get("SHA256"):
            item.checksum       = hashes["SHA256"].lower()
            item.hash_algorithm = "sha256"
        model = data.get("model") or {}
        if model.get("name"):
            item.description = model["name"]

        return item

    # ------------------------------------------------------------------
    # Generic (any URL)
    # ------------------------------------------------------------------

    async def _fetch_generic(self, url: str, hdrs: dict) -> UrlMetaItem:
        item = UrlMetaItem(
            id=url.rstrip("/").split("/")[-1] or "download",
            mirrors=[{"url": url}],
        )

        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as client:
            # Try HEAD first
            try:
                r = await client.head(url, headers=hdrs)
                if r.status_code in (200, 206):
                    return self._item_from_headers(item, r.headers, url)
            except Exception as exc:
                logger.debug("Generic HEAD failed | url=%s | %s", url, exc)

            # Fallback: GET with Range: bytes=0-0
            try:
                rng_hdrs = {**hdrs, "Range": "bytes=0-0"}
                r2 = await client.get(url, headers=rng_hdrs)
                if r2.status_code in (200, 206, 416):
                    return self._item_from_headers(item, r2.headers, url)
            except Exception as exc:
                logger.debug("Generic GET-range failed | url=%s | %s", url, exc)

        # Nothing worked — return URL-only item
        return item

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_headers(request: UrlMetaRequest) -> dict:
        """Merge explicit headers + cookies from the request into one dict."""
        hdrs: dict[str, str] = dict(request.headers or {})
        cookies = request.cookies or {}
        if cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return hdrs

    @staticmethod
    def _item_from_headers(
        item: UrlMetaItem,
        headers: Any,
        original_url: str,
    ) -> UrlMetaItem:
        """Populate filename + size from HTTP response headers."""
        fname = UrlMetaService._filename_from_cd(headers.get("content-disposition"))
        if not fname:
            fname = original_url.rstrip("/").split("/")[-1] or None
        if fname:
            item.id        = fname
            item.file_name = fname

        # Content-Range: bytes 0-0/TOTAL
        cr = headers.get("content-range", "")
        if cr:
            parts = cr.split("/")
            try:
                sz = int(parts[-1])
                if sz > 1:
                    item.file_size = sz
                    return item
            except ValueError:
                pass

        # Content-Length (reliable only for HEAD / full response)
        cl = headers.get("content-length")
        if cl:
            try:
                sz = int(cl)
                if sz > 1:
                    item.file_size = sz
            except ValueError:
                pass

        return item

    @staticmethod
    def _filename_from_cd(cd: Optional[str]) -> Optional[str]:
        if not cd:
            return None
        # RFC 5987: filename*=UTF-8''...
        m = re.search(r"filename\*=(?:UTF-8'')?([^\s;]+)", cd, re.I)
        if m:
            return unquote(m.group(1).strip("\"'"))
        m = re.search(r'filename=["\']?([^"\';\s]+)', cd, re.I)
        if m:
            return m.group(1).strip("\"'")
        return None

    @staticmethod
    def _extract_sha256(html: str) -> Optional[str]:
        """Try to find a 64-char hex SHA-256 in arbitrary HTML."""
        # Strategy 1: <dt>SHA256:</dt><dd>HASH</dd>
        m = re.search(
            r"<dt[^>]*>\s*SHA256:\s*</dt>\s*<dd[^>]*>\s*([0-9a-f]{64})\s*</dd>",
            html, re.I,
        )
        if m:
            return m.group(1).lower()
        # Strategy 2: bare 64-char hex near "SHA256"
        m = re.search(r"SHA256[^0-9a-f]{0,20}([0-9a-f]{64})", html, re.I)
        if m:
            return m.group(1).lower()
        return None

    @staticmethod
    def _fallback_item(url: str) -> UrlMetaItem:
        """Minimal item used when all metadata fetches fail."""
        fname = url.rstrip("/").split("/")[-1] or "download"
        return UrlMetaItem(id=fname, file_name=fname, mirrors=[{"url": url}])