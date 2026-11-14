"""Run with a built writenew and the candidate Common package on PYTHONPATH."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from biznet_common.identity import validate_slug

BINARY = Path(sys.argv.pop(1)).resolve()
CONFIG = Path(sys.argv.pop(1)).read_text()
SOURCE = "psg_0000000000000000"


class Acceptance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name)
        (self.vault / "_tools").mkdir()
        self.config = self.vault / "_tools/config.yaml"
        self.config.write_text(CONFIG)
        self.folder = self.vault / "manuscript"
        self.folder.mkdir()

    @staticmethod
    def record(name, content="Passage body."):
        return {"input_record": {"slug": SOURCE, "filename_hint": name}, "content": content}

    def invoke(self, *records, ok=True, **kwargs):
        result = subprocess.run([BINARY, "--target-dir", self.vault],
                                input="\n".join(map(json.dumps, records)).encode(),
                                capture_output=True, **kwargs)
        self.assertEqual(result.returncode == 0, ok, result.stderr.decode())
        self.assertEqual(result.stdout, b"")
        return result.stderr.decode()

    def test_single_multiple_replay_and_exact_body(self):
        content = (f"---\r\nslug: {SOURCE}\r\nplan: 'old'\r\ndispatch: true\r\n"
                   "tags: [source] # preserve\r\nprovenance:\r\n  - older\r\n"
                   "---\r\n\r\nBody\r\n---\r\nMore body")
        batch = (self.record("First", content), self.record("Second"))
        self.invoke(*batch)
        first, second = [path.read_bytes() for path in sorted(self.folder.iterdir())]
        slugs = [validate_slug("passage", text.splitlines()[1].decode().removeprefix("slug: "))
                 for text in (first, second)]
        self.assertEqual(len({SOURCE, *slugs}), 3)
        self.assertIn(b"dispatch: false\r\nplan: \"\"\r\n", first)
        self.assertIn(b"tags: [source] # preserve\r\nprovenance:\r\n  - older\r\n", first)
        self.assertIn(SOURCE.encode(), first.split(b"source_slug: ", 1)[1].splitlines()[0])
        self.assertTrue(first.endswith(b"---\r\n\r\nBody\r\n---\r\nMore body"))
        self.invoke(*batch, ok=False)
        self.assertEqual([path.read_bytes() for path in sorted(self.folder.iterdir())], [first, second])

    def test_reject_config_identity_and_filename_conflicts(self):
        cases = (
            self.record("Bad", "---\nslug: different\n---\nBody"),
            self.record("Bad", "---\nslug: [\n---\nBody"),
            self.record("Bad", "---\nslug: unclosed"),
            dict(self.record("Bad"), destination_slug=SOURCE),
        )
        for record in cases:
            self.invoke(self.record("Valid"), record, ok=False)
            self.assertEqual(list(self.folder.iterdir()), [])
        (self.folder / "existing.md").write_text("Keep")
        self.invoke(self.record("EXISTING"), ok=False)
        self.assertEqual((self.folder / "existing.md").read_text(), "Keep")
        self.config.write_text(CONFIG.replace("folder: manuscript", "folder: ../outside"))
        self.assertIn("vault-relative", self.invoke(self.record("Invalid"), ok=False))
        self.config.unlink()
        self.assertIn("config.yaml", self.invoke(self.record("Missing"), ok=False))

    def test_slug_collision_retries_then_stops_at_bound(self):
        existing = self.folder / "Existing.md"
        existing.write_text(f"---\nslug: {SOURCE}\n---\nKeep")
        hooks = self.vault / "hooks"
        hooks.mkdir()
        marker = hooks / "attempted"
        (hooks / "sitecustomize.py").write_text(
            "import os, pathlib, secrets\n"
            "_original = secrets.token_bytes\n"
            f"_marker = pathlib.Path({str(marker)!r})\n"
            "def collide_once(count):\n"
            "    if os.environ.get('FORCE_COLLISION') or not _marker.exists():\n"
            "        _marker.touch()\n"
            "        return bytes(count)\n"
            "    return _original(count)\n"
            "secrets.token_bytes = collide_once\n"
        )
        env = dict(os.environ, PYTHONPATH=str(hooks) + os.pathsep + os.environ["PYTHONPATH"])
        self.invoke(self.record("Fresh"), env=env)
        self.assertTrue(marker.exists())
        self.assertEqual(existing.read_text(), f"---\nslug: {SOURCE}\n---\nKeep")
        env["FORCE_COLLISION"] = "1"
        self.assertIn("distinct passage slug", self.invoke(self.record("Blocked"), ok=False, env=env))
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["Existing.md", "Fresh.md"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux file-size limits")
    def test_write_error_rolls_back_all_staging(self):
        import resource
        import signal

        def constrain_file_size():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))

        error = self.invoke(self.record("Small"), self.record("Large", "x" * 4096),
                            ok=False, preexec_fn=constrain_file_size)
        self.assertIn("File too large", error)
        self.assertEqual(list(self.folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
