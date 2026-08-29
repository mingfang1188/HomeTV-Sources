#!/usr/bin/env python3

import argparse
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def key(record):
    return record["url"], json.dumps(record.get("headers", {}), sort_keys=True)


def main():
    args = parse_args()
    first_payload = json.loads(args.first.read_text(encoding="utf-8"))
    second_payload = json.loads(args.second.read_text(encoding="utf-8"))
    first = {key(record): record for record in first_payload["records"]}
    second = {key(record): record for record in second_payload["records"]}
    records = []
    for stream_key in first.keys() | second.keys():
        first_record = first.get(stream_key)
        second_record = second.get(stream_key)
        record = dict(second_record or first_record)
        if (
            first_record is not None
            and second_record is not None
            and first_record["status"] == "GOOD"
            and second_record["status"] == "GOOD"
        ):
            record["status"] = "GOOD"
            record["reason"] = ""
        else:
            record["status"] = "BAD"
            record["reason"] = "did not pass both required test rounds"
        records.append(record)
    output = {
        "channel_records": len(records),
        "unique_streams": len(records),
        "status_counts": {
            "GOOD": sum(record["status"] == "GOOD" for record in records),
            "BAD": sum(record["status"] != "GOOD" for record in records),
        },
        "records": records,
    }
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output["status_counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
