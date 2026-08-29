#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


GROUP_ORDER = [
    "央视·卫视",
    "地方频道",
    "港澳台",
    "高尔夫",
    "足球",
    "篮球",
    "综合体育",
    "纪录片",
    "电影",
    "电视剧·综艺",
    "少儿动漫",
    "国际新闻",
    "国际综合",
    "日本综合",
]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args()


def station_key(record):
    name = record["name"].lower().strip()
    name = re.sub(r"\([^)]*(?:1080|720|576|4k|uhd|hd)[^)]*\)", "", name)
    name = re.sub(r"\[[^]]+\]", "", name).strip()
    name = re.sub(
        r"\b(?:us|uk|de|es|br|au|france|finland|italy|spain|mountain|pacific|alaska)\b$",
        "",
        name,
    )
    name = name.replace(" and ", " & ")
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff]+", "", name)
    return record["group"], normalized


def stream_key(record):
    payload = record["url"] + "\n" + json.dumps(
        record.get("headers", {}),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def score(record):
    return (
        record.get("height") or 0,
        record.get("width") or 0,
        record.get("speed_margin") or 0,
        record.get("throughput_bps") or 0,
        record["url"].startswith("https://"),
        -(record.get("manifest_seconds") or 99),
    )


def quote(value):
    return str(value).replace('"', "'")


def m3u_text(records):
    lines = ["#EXTM3U"]
    for record in records:
        stable_id = hashlib.sha1(
            f'{record["group"]}|{station_key(record)[1]}'.encode("utf-8")
        ).hexdigest()[:16]
        attrs = [f'tvg-id="hometv.{stable_id}"']
        attrs.append(f'tvg-name="{quote(record["name"])}"')
        if record.get("logo"):
            attrs.append(f'tvg-logo="{quote(record["logo"])}"')
        attrs.append(f'group-title="{quote(record["group"])}"')
        for name, value in record.get("headers", {}).items():
            if name.lower() == "user-agent":
                attrs.append(f'http-user-agent="{quote(value)}"')
            elif name.lower() in {"referer", "referrer"}:
                attrs.append(f'http-referrer="{quote(value)}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{record["name"]}')
        lines.append(record["url"])
    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    popularity_path = args.root / "popularity-order.json"
    popularity = (
        json.loads(popularity_path.read_text(encoding="utf-8"))
        if popularity_path.exists()
        else {}
    )
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    state_path = args.root / "health-state.json"
    previous_state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {}
    )
    state = {}
    successful = defaultdict(list)
    for record in audit["records"]:
        key = stream_key(record)
        old = previous_state.get(key, {})
        failures = 0 if record["status"] == "GOOD" else int(old.get("failures", 0)) + 1
        state[key] = {
            "name": record["name"],
            "group": record["group"],
            "url": record["url"],
            "status": record["status"],
            "failures": failures,
            "checkedAt": now.isoformat(),
        }
        if record["status"] == "GOOD":
            successful[station_key(record)].append(record)

    selected = []
    for station in successful:
        choices = successful.get(station, [])
        if choices:
            choices.sort(key=score, reverse=True)
            selected.append(choices[0])

    selected.sort(
        key=lambda record: (
            GROUP_ORDER.index(record["group"]) if record["group"] in GROUP_ORDER else 999,
            (
                popularity.get(record["group"], []).index(record["name"])
                if record["name"] in popularity.get(record["group"], [])
                else 9999
            ),
            -(record.get("height") or 0),
            -(record.get("speed_margin") or 0),
            record["name"].lower(),
        )
    )
    text = m3u_text(selected)
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (args.root / "curated.m3u").write_text(text, encoding="utf-8")
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    groups = Counter(record["group"] for record in selected)
    manifest = {
        "version": now.strftime("%Y.%m.%d.%H%M"),
        "updatedAt": now.isoformat(),
        "channelCount": len(selected),
        "sha256": sha256,
        "playlistUrl": "https://mingfang1188.github.io/HomeTV-Sources/curated.m3u",
        "groups": {group: groups[group] for group in GROUP_ORDER if groups[group]},
    }
    (args.root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    status_counts = Counter(record["status"] for record in audit["records"])
    report = {
        "generatedAt": now.isoformat(),
        "testedCandidates": len(audit["records"]),
        "publishedChannels": len(selected),
        "statusCounts": dict(status_counts),
        "groups": manifest["groups"],
        "criteria": {
            "minimumLiveSpeedMargin": 1.5,
            "requiredCompletePasses": 2,
            "anyFailedPassIsRejected": True,
        },
    }
    (args.root / "health-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
