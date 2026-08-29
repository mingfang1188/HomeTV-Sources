#!/usr/bin/env python3

import argparse
import asyncio
import json
import re
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp


ATTRIBUTE_PATTERN = re.compile(r'([A-Za-z0-9-]+)=(?:"([^"]*)"|([^\s,]+))')
GOLF_NAMES = {
    "30A Golf Kingdom (720p)",
    "CCTV-Golf & Tennis (1080p)",
    "Golf Channel",
    "Golf Channel Latin America (1080p)",
    "GolfPass (1080p)",
    "PGA Tour (1080p)",
}
INTERNATIONAL_NAMES = {
    "Arirang TV (1080p)",
    "Bloomberg TV Asia (720p)",
    "CNA (1080p) [Geo-blocked]",
    "DW English (1080p)",
    "NBC News NOW (1080p)",
    "NHK World-Japan (1080p)",
    "Reuters (1080p)",
    "TRT World (1080p) [Not 24/7]",
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
    "HomeTV-Audit/1.0"
)
POLICY_PATH = Path(__file__).resolve().parents[1] / "quality-policy.json"
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8")) if POLICY_PATH.exists() else {}
MIN_SPEED_MARGIN = float(POLICY.get("minimumSpeedMargin", 1.5))
MAX_MANIFEST_SECONDS = float(POLICY.get("maximumManifestSeconds", 8))
MAX_DIRECT_STARTUP_SECONDS = float(POLICY.get("maximumDirectStartupSeconds", 8))
INTEREST_GROUPS = set(POLICY.get("interestGroups", []))
MIN_INTEREST_HEIGHT = int(POLICY.get("minimumResolution", {}).get("interestChannels", 720))
MIN_GENERAL_HEIGHT = int(POLICY.get("minimumResolution", {}).get("generalChannels", 480))
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_SEGMENT_BYTES = 24 * 1024 * 1024


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry-from", type=Path)
    parser.add_argument("--channels-json", type=Path)
    parser.add_argument("--m3u", type=Path)
    return parser.parse_args()


def parse_attributes(info):
    attributes = {}
    for match in ATTRIBUTE_PATTERN.finditer(info):
        attributes[match.group(1).lower()] = (
            match.group(2) or match.group(3) or ""
        ).strip()
    return attributes


def display_name(info, attributes):
    in_quotes = False
    escaped = False
    for index, char in enumerate(info):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            value = info[index + 1 :].strip()
            if value:
                return value
    return attributes.get("tvg-name") or "未命名频道"


def parse_m3u(path, source_name, included_names=None):
    channels = []
    pending = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.lower().startswith("#extinf:"):
            pending = line
            continue
        if not pending or not line or line.startswith("#"):
            continue
        if not line.lower().startswith(("http://", "https://", "rtmp://")):
            pending = None
            continue
        attributes = parse_attributes(pending)
        name = display_name(pending, attributes)
        if included_names is None or name in included_names:
            headers = {}
            user_agent = attributes.get("http-user-agent")
            referer = attributes.get("http-referer") or attributes.get("http-referrer")
            if user_agent:
                headers["User-Agent"] = user_agent
            if referer:
                headers["Referer"] = referer
            channels.append(
                {
                    "source": source_name,
                    "name": name,
                    "group": attributes.get("group-title") or "其他",
                    "tvg_id": attributes.get("tvg-id") or "",
                    "logo": attributes.get("tvg-logo") or "",
                    "url": line,
                    "headers": headers,
                }
            )
        pending = None
    return channels


def inventory(input_dir):
    channels = []
    channels += parse_m3u(input_dir / "ipv4_curated.m3u", "IPv4 综合")
    channels += parse_m3u(
        input_dir / "iptv_org_index.m3u",
        "高尔夫公共频道",
        GOLF_NAMES,
    )
    channels += parse_m3u(
        input_dir / "iptv_org_index.m3u",
        "国际高清公共频道",
        INTERNATIONAL_NAMES,
    )
    channels += parse_m3u(input_dir / "japan.m3u", "日本频道")
    return channels


def highest_variant(text, base_url):
    lines = [line.strip() for line in text.splitlines()]
    candidates = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        uri = next(
            (
                candidate
                for candidate in lines[index + 1 :]
                if candidate and not candidate.startswith("#")
            ),
            None,
        )
        if not uri:
            continue
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
        bandwidth = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line, re.I)
        width = int(resolution.group(1)) if resolution else 0
        height = int(resolution.group(2)) if resolution else 0
        bitrate = int(bandwidth.group(1)) if bandwidth else 0
        candidates.append((height, width, bitrate, urljoin(base_url, uri)))
    return max(candidates, default=None)


def media_segment(text, base_url):
    lines = [line.strip() for line in text.splitlines()]
    segments = []
    duration = None
    init_uri = None
    for line in lines:
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_uri = urljoin(base_url, match.group(1))
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = None
        elif line and not line.startswith("#"):
            segments.append((duration, urljoin(base_url, line)))
            duration = None
    if not segments:
        return None
    segment_duration, segment_url = segments[-1]
    return segment_duration, segment_url, init_uri


async def read_limited(response, limit):
    chunks = []
    total = 0
    truncated = False
    async for chunk in response.content.iter_chunked(64 * 1024):
        if total + len(chunk) > limit:
            chunks.append(chunk[: limit - total])
            total = limit
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


async def fetch(session, url, headers, limit, timeout_total):
    request_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Encoding": "identity",
        **headers,
    }
    started = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_total, connect=6, sock_read=12)
        response = await session.get(
            url,
            headers=request_headers,
            allow_redirects=True,
            timeout=timeout,
        )
        try:
            body, truncated = await read_limited(response, limit)
            elapsed = time.monotonic() - started
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "content_length": response.headers.get("Content-Length"),
                "url": str(response.url),
                "body": body,
                "bytes": len(body),
                "elapsed": elapsed,
                "truncated": truncated,
                "error": "",
            }
        finally:
            if response.content.at_eof():
                response.release()
            else:
                response.close()
    except Exception as error:
        return {
            "ok": False,
            "status": 0,
            "content_type": "",
            "content_length": None,
            "url": url,
            "body": b"",
            "bytes": 0,
            "elapsed": time.monotonic() - started,
            "truncated": False,
            "error": f"{type(error).__name__}: {error}",
        }


async def ffprobe_file(data):
    if not data:
        return None
    with tempfile.NamedTemporaryFile(suffix=".media") as handle:
        handle.write(data)
        handle.flush()
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            handle.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=7)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return None
    try:
        streams = json.loads(stdout).get("streams") or []
        return streams[0] if streams else None
    except Exception:
        return None


async def ffprobe_url(url, headers):
    header_text = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    args = [
        "ffprobe",
        "-v",
        "error",
        "-rw_timeout",
        "12000000",
        "-analyzeduration",
        "2500000",
        "-probesize",
        "2500000",
    ]
    if header_text:
        args += ["-headers", header_text]
    args += [
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        url,
    ]
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=16)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return None, time.monotonic() - started, "ffprobe timeout"
    try:
        streams = json.loads(stdout).get("streams") or []
        stream = streams[0] if streams else None
    except Exception:
        stream = None
    return stream, time.monotonic() - started, "" if stream else "no video stream"


async def sample_rtmp(url):
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-rw_timeout",
        "12000000",
        "-i",
        url,
        "-t",
        "8",
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.communicate(), timeout=16)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return False, time.monotonic() - started, "RTMP sustained-read timeout"
    elapsed = time.monotonic() - started
    return process.returncode == 0, elapsed, "" if process.returncode == 0 else "RTMP sustained-read failed"


def result_base(url):
    return {
        "url": url,
        "status": "BAD",
        "reason": "",
        "http_status": 0,
        "content_type": "",
        "effective_url": url,
        "manifest_seconds": None,
        "segment_seconds": None,
        "segment_bytes": None,
        "throughput_bps": None,
        "segment_duration": None,
        "speed_margin": None,
        "width": None,
        "height": None,
        "codec": None,
        "fps": None,
        "master_width": None,
        "master_height": None,
        "master_bandwidth": None,
    }


def set_stream_info(result, stream):
    if not stream:
        return
    result["width"] = stream.get("width")
    result["height"] = stream.get("height")
    result["codec"] = stream.get("codec_name")
    result["fps"] = stream.get("r_frame_rate")


def classify_hls(result, segment_fetch, duration, stream):
    if not segment_fetch["ok"]:
        result["status"] = "BAD"
        result["reason"] = (
            f"segment HTTP {segment_fetch['status']}"
            if segment_fetch["status"]
            else segment_fetch["error"] or "segment request failed"
        )
        return
    if not stream:
        result["status"] = "BAD"
        result["reason"] = "segment is not decodable video"
        return
    if segment_fetch["truncated"]:
        result["status"] = "BORDERLINE"
        result["reason"] = "segment exceeded audit size limit"
        return
    margin = None
    if duration and segment_fetch["elapsed"] > 0:
        margin = duration / segment_fetch["elapsed"]
        result["speed_margin"] = round(margin, 3)
    if (result.get("manifest_seconds") or 0) > MAX_MANIFEST_SECONDS:
        result["status"] = "SLOW"
        result["reason"] = "playlist startup exceeded policy limit"
    elif segment_fetch["elapsed"] > 15:
        result["status"] = "SLOW"
        result["reason"] = "segment download exceeded 15 seconds"
    elif margin is not None and margin < 1.15:
        result["status"] = "SLOW"
        result["reason"] = "download speed is below live bitrate"
    elif margin is not None and margin < MIN_SPEED_MARGIN:
        result["status"] = "BORDERLINE"
        result["reason"] = "download speed has little live-play margin"
    else:
        result["status"] = "GOOD"
        result["reason"] = ""


async def audit_http(session, channel, host_semaphore):
    url = channel["url"]
    headers = channel["headers"]
    result = result_base(url)
    async with host_semaphore:
        first = await fetch(session, url, headers, MAX_TEXT_BYTES, 18)
        result["http_status"] = first["status"]
        result["content_type"] = first["content_type"]
        result["effective_url"] = first["url"]
        result["manifest_seconds"] = round(first["elapsed"], 3)
        if not first["ok"]:
            result["reason"] = (
                f"HTTP {first['status']}"
                if first["status"]
                else first["error"] or "request failed"
            )
            return result

        text = first["body"].decode("utf-8", errors="replace").lstrip("\ufeff\r\n ")
        if not text.startswith("#EXTM3U"):
            stream, elapsed, error = await ffprobe_url(first["url"], headers)
            set_stream_info(result, stream)
            result["segment_seconds"] = round(elapsed, 3)
            result["segment_bytes"] = first["bytes"]
            if first["elapsed"] > 0:
                result["throughput_bps"] = round(first["bytes"] * 8 / first["elapsed"])
            height = (stream or {}).get("height") or 0
            required_bps = (
                5_000_000 if height >= 2000 else
                2_000_000 if height >= 1000 else
                1_000_000 if height >= 700 else
                500_000
            )
            if (
                stream
                and elapsed <= MAX_DIRECT_STARTUP_SECONDS
                and result["throughput_bps"] >= required_bps
            ):
                result["status"] = "GOOD"
            elif stream and elapsed > MAX_DIRECT_STARTUP_SECONDS:
                result["status"] = "SLOW"
                result["reason"] = "direct stream startup exceeded 12 seconds"
            elif stream:
                result["status"] = "SLOW"
                result["reason"] = "direct stream sustained throughput is too low"
            else:
                result["status"] = "BAD"
                result["reason"] = error or "response is not M3U or video"
            return result

        variant = highest_variant(text, first["url"])
        if variant:
            height, width, bandwidth, variant_url = variant
            result["master_width"] = width or None
            result["master_height"] = height or None
            result["master_bandwidth"] = bandwidth or None
            media = await fetch(session, variant_url, headers, MAX_TEXT_BYTES, 18)
            if not media["ok"]:
                result["status"] = "BAD"
                result["reason"] = (
                    f"variant HTTP {media['status']}"
                    if media["status"]
                    else media["error"] or "variant request failed"
                )
                return result
            media_text = media["body"].decode("utf-8", errors="replace")
            media_url = media["url"]
            result["manifest_seconds"] = round(first["elapsed"] + media["elapsed"], 3)
        else:
            media_text = text
            media_url = first["url"]

        if re.search(
            r"#EXT-X-KEY:.*(?:METHOD=SAMPLE-AES|KEYFORMAT=|widevine|fairplay|playready)",
            media_text,
            re.IGNORECASE,
        ):
            result["status"] = "BAD"
            result["reason"] = "DRM-protected HLS is not eligible"
            return result

        segment = media_segment(media_text, media_url)
        if not segment:
            result["status"] = "BAD"
            result["reason"] = "HLS playlist has no media segment"
            return result
        duration, segment_url, init_url = segment
        result["segment_duration"] = duration
        init_data = b""
        if init_url:
            init_fetch = await fetch(session, init_url, headers, 4 * 1024 * 1024, 12)
            if init_fetch["ok"]:
                init_data = init_fetch["body"]
        segment_fetch = await fetch(
            session,
            segment_url,
            headers,
            MAX_SEGMENT_BYTES,
            24,
        )
        result["segment_seconds"] = round(segment_fetch["elapsed"], 3)
        result["segment_bytes"] = segment_fetch["bytes"]
        if segment_fetch["elapsed"] > 0:
            result["throughput_bps"] = round(
                segment_fetch["bytes"] * 8 / segment_fetch["elapsed"]
            )
        stream = await ffprobe_file(init_data + segment_fetch["body"])
        set_stream_info(result, stream)
        classify_hls(result, segment_fetch, duration, stream)
        return result


async def audit_rtmp(channel):
    result = result_base(channel["url"])
    stream, elapsed, error = await ffprobe_url(channel["url"], channel["headers"])
    set_stream_info(result, stream)
    result["segment_seconds"] = round(elapsed, 3)
    if stream and elapsed <= MAX_DIRECT_STARTUP_SECONDS:
        sustained, sample_elapsed, sample_error = await sample_rtmp(channel["url"])
        result["segment_seconds"] = round(sample_elapsed, 3)
        if sustained:
            result["status"] = "GOOD"
        else:
            result["status"] = "SLOW"
            result["reason"] = sample_error
    elif stream:
        result["status"] = "SLOW"
        result["reason"] = "RTMP startup exceeded 12 seconds"
    else:
        result["status"] = "BAD"
        result["reason"] = error or "RTMP probe failed"
    return result


def apply_resolution_policy(result, channel):
    if result["status"] != "GOOD":
        return result
    minimum = MIN_INTEREST_HEIGHT if channel.get("group") in INTEREST_GROUPS else MIN_GENERAL_HEIGHT
    height = result.get("height") or 0
    if height < minimum:
        result["status"] = "BAD"
        result["reason"] = f"actual resolution {height}p is below required {minimum}p"
    return result


async def main_async(args):
    if args.channels_json:
        channels = json.loads(args.channels_json.read_text(encoding="utf-8"))
    elif args.m3u:
        channels = parse_m3u(args.m3u, "HomeTV-Sources")
    elif args.retry_from:
        previous = json.loads(args.retry_from.read_text(encoding="utf-8"))
        channels = [
            {
                key: record[key]
                for key in ("source", "name", "group", "tvg_id", "logo", "url", "headers")
            }
            for record in previous["records"]
            if record["status"] in {"BAD", "SLOW", "BORDERLINE"}
        ]
    else:
        if not args.input_dir:
            raise SystemExit("--input-dir, --channels-json or --m3u is required")
        channels = inventory(args.input_dir)
    if args.limit:
        channels = channels[: args.limit]
    unique = {}
    for channel in channels:
        key = (channel["url"], tuple(sorted(channel["headers"].items())))
        unique.setdefault(key, channel)

    host_counts = Counter(
        urlparse(channel["url"]).hostname or urlparse(channel["url"]).scheme
        for channel in unique.values()
    )
    host_semaphores = {
        host: asyncio.Semaphore(1 if args.retry_from or count >= 16 else 2)
        for host, count in host_counts.items()
    }
    overall = asyncio.Semaphore(args.concurrency)
    connector = aiohttp.TCPConnector(
        limit=args.concurrency,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    results = {}
    completed = 0
    lock = asyncio.Lock()

    async with aiohttp.ClientSession(connector=connector) as session:
        async def run(key, channel):
            nonlocal completed
            async with overall:
                parsed = urlparse(channel["url"])
                if parsed.scheme.lower() == "rtmp":
                    result = await audit_rtmp(channel)
                else:
                    host = parsed.hostname or parsed.scheme
                    result = await audit_http(session, channel, host_semaphores[host])
                result = apply_resolution_policy(result, channel)
            results[key] = result
            async with lock:
                completed += 1
                if completed % 25 == 0 or completed == len(unique):
                    counts = Counter(item["status"] for item in results.values())
                    print(
                        f"progress={completed}/{len(unique)} "
                        + " ".join(f"{name}={value}" for name, value in sorted(counts.items())),
                        flush=True,
                    )

        await asyncio.gather(
            *(run(key, channel) for key, channel in unique.items())
        )

    records = []
    for channel in channels:
        key = (channel["url"], tuple(sorted(channel["headers"].items())))
        records.append({**channel, **results[key]})

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "channel_records": len(channels),
        "unique_streams": len(unique),
        "status_counts": dict(Counter(record["status"] for record in records)),
        "records": records,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_path = args.output.with_suffix(".tsv")
    with summary_path.open("w", encoding="utf-8") as handle:
        columns = [
            "source",
            "group",
            "name",
            "status",
            "reason",
            "http_status",
            "manifest_seconds",
            "segment_seconds",
            "throughput_bps",
            "speed_margin",
            "width",
            "height",
            "codec",
            "url",
        ]
        handle.write("\t".join(columns) + "\n")
        for record in records:
            handle.write(
                "\t".join(str(record.get(column) or "") for column in columns) + "\n"
            )
    print(json.dumps(payload["status_counts"], ensure_ascii=False), flush=True)
    print(f"output={args.output}", flush=True)


def main():
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
