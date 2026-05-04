#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_NAME = "download"


def normalize_output(raw: str | None, fallback: str | None = None) -> Path:
    value = (raw or fallback or DEFAULT_OUTPUT_NAME).strip()
    if value.startswith("~/"):
        return (Path("/Users/moego-winches") / value[2:]).resolve()
    path = Path(value)
    if path.is_absolute():
        return path
    if value.startswith("./"):
        value = value[2:]
    return (Path.cwd() / value).resolve()


def safe_segment(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip() or fallback
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120]


def image_extension(url: str, content_type: str = "") -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    fmt = (query.get("format") or [""])[0].lower()
    if fmt in {"jpg", "jpeg", "png", "webp", "gif"}:
        return "jpg" if fmt == "jpeg" else fmt
    if "png" in content_type:
        return "png"
    if "webp" in content_type:
        return "webp"
    if "gif" in content_type:
        return "gif"
    return "jpg"


def download(url: str, target: Path) -> tuple[bool, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://x.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
        if not data:
            return False, "empty response"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return True, content_type
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def iter_posts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        posts = payload.get("posts")
        if isinstance(posts, list):
            return [item for item in posts if isinstance(item, dict)]
        return [payload]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download media_urls from autocli twitter user-posts JSON and fill local_media_paths."
    )
    parser.add_argument("--input", help="Input JSON file. Defaults to stdin.")
    parser.add_argument("--output", help="Absolute or user-home-relative output directory.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args(argv)

    raw = Path(args.input).read_text() if args.input else sys.stdin.read()
    payload = json.loads(raw)
    posts = iter_posts(payload)

    for post in posts:
        media_urls = post.get("media_urls") or []
        if not isinstance(media_urls, list):
            media_urls = []
        base_output = normalize_output(args.output, post.get("output"))
        username = safe_segment(post.get("requested_username") or post.get("author"), "unknown")
        post_id = safe_segment(post.get("id"), "unknown-post")
        images_dir = base_output / username / post_id / "images"
        local_paths: list[str] = []
        warnings = post.get("warnings")
        if not isinstance(warnings, list):
            warnings = []

        for index, url in enumerate(media_urls, start=1):
            if not isinstance(url, str) or not url:
                continue
            ext = image_extension(url)
            target = images_dir / f"img_{index:03d}.{ext}"
            ok, detail = download(url, target)
            if ok:
                ext = image_extension(url, detail)
                if target.suffix != f".{ext}":
                    renamed = target.with_suffix(f".{ext}")
                    target.rename(renamed)
                    target = renamed
                local_paths.append(str(target))
            else:
                warnings.append(f"media download failed for {url}: {detail}")

        post["output"] = str(base_output)
        post["local_media_paths"] = local_paths
        post["warnings"] = warnings

    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
