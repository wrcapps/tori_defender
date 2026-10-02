import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import datastore


def put(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class DatastoreTest(unittest.TestCase):
    def test_migrate_never_overwrites(self):
        with tempfile.TemporaryDirectory() as t:
            fb, pr = Path(t, "fb"), Path(t, "pr")
            put(fb, "s/inbox/d/w/frame_1.jpg", "new")
            put(fb, "s/dataset/same.txt", "x")
            put(pr, "s/dataset/same.txt", "x")
            put(fb, "s/dataset/log.jsonl", '{"b":2}\n')
            put(pr, "s/dataset/log.jsonl", '{"a":1}')          # no trailing newline
            put(fb, "s/dataset/verdicts.json", '{"new":1}')
            put(pr, "s/dataset/verdicts.json", '{"old":1}')
            st = datastore.migrate(fb, pr, log=lambda *_: None)
            self.assertEqual((st["moved"], st["identical"], st["appended"], len(st["conflicts"])), (1, 1, 1, 1))
            self.assertEqual((pr / "s/inbox/d/w/frame_1.jpg").read_text(), "new")
            self.assertEqual((pr / "s/dataset/log.jsonl").read_text(), '{"a":1}\n{"b":2}\n')
            self.assertEqual((pr / "s/dataset/verdicts.json").read_text(), '{"old":1}')
            self.assertEqual(Path(st["conflicts"][0]).read_text(), '{"new":1}')
            self.assertEqual(datastore.pending(fb), [])

    def test_choose_root(self):
        with tempfile.TemporaryDirectory() as t:
            fb, pr = Path(t, "fb"), Path(t, "pr")
            root, src = datastore.choose_root(pr, fb, log=lambda *_: None)     # primary missing
            self.assertEqual(root, fb)
            self.assertTrue(src.startswith("fallback"))
            put(fb, "s/a.txt", "1")
            pr.mkdir()
            root, src = datastore.choose_root(pr, fb, log=lambda *_: None)     # primary back
            self.assertEqual((root, src), (pr, "settings"))
            self.assertEqual((pr / "s/a.txt").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
