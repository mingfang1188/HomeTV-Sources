import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from publish import screen_language_priority


class ScreenLanguageOrderTests(unittest.TestCase):
    def test_chinese_names_and_foreign_region_annotations(self):
        for name, expected in (
            ("父母爱情", 0),
            ("CCTV6", 0),
            ("CHC家庭影院", 0),
            ("BBC Series（意大利） (1080p)", 1),
            ("Giallo（意大利）", 1),
            ("MovieSphere AU (1080p)", 1),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    screen_language_priority({"group": "电影", "name": name}), expected
                )
        self.assertEqual(
            screen_language_priority({"group": "高尔夫", "name": "GolfPass"}), 0
        )

    def test_publish_keeps_chinese_ahead_of_more_popular_foreign_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            for group, chinese, foreign in (
                ("电影", "国产电影", "Foreign Movies"),
                ("电视剧·综艺", "国产剧场", "BBC Series（意大利）"),
            ):
                for name in (foreign, chinese):
                    records.append({
                        "group": group, "name": name, "status": "GOOD",
                        "url": f"https://example.invalid/{len(records)}.m3u8",
                        "height": 1080, "speed_margin": 3, "headers": {},
                    })
            (root / "audit.json").write_text(json.dumps({"records": records}))
            (root / "popularity-order.json").write_text(json.dumps({
                "电影": ["Foreign Movies", "国产电影"],
                "电视剧·综艺": ["BBC Series（意大利）", "国产剧场"],
            }))
            subprocess.run([
                sys.executable, str(Path(__file__).with_name("publish.py")),
                "--audit", str(root / "audit.json"), "--root", str(root),
            ], check=True, capture_output=True)
            playlist = (root / "curated.m3u").read_text()
            self.assertLess(playlist.index("国产电影"), playlist.index("Foreign Movies"))
            self.assertLess(playlist.index("国产剧场"), playlist.index("BBC Series"))
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertEqual(manifest["channelCount"], 4)


if __name__ == "__main__":
    unittest.main()
