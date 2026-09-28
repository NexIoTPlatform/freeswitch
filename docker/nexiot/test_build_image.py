"""Ensure images use committed branch source and reject ambiguous worktrees."""
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import build_image as build


class CommittedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "freeswitch"
        self.recipe = self.repo / "docker/nexiot"
        self.recipe.mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Build Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "src").mkdir()
        (self.repo / "src/switch_rtp.c").write_text("upstream RTP source\n")
        (self.repo / "configure.ac").write_text("AC_INIT([freeswitch], [1.11.3-release], bugs@freeswitch.org)\n")
        self.git("add", ".")
        self.git("commit", "-qm", "upstream release")
        upstream = self.git("rev-parse", "HEAD").strip()
        self.git("tag", "v1.11.3")
        self.git("checkout", "-qb", "codex/nexiot-freeswitch")
        self.lock = {"freeswitchVersion": "1.11.3", "imageVersion": "1.11.3-nexiot.2",
                     "upstreamTag": "v1.11.3", "upstreamCommit": upstream,
                     "baseImage": "debian:bookworm-slim@sha256:" + "a" * 64, "sources": []}
        for name, repository in build.REPOSITORIES.items():
            self.lock["sources"].append({"name": name, "repository": repository, "commit": "1" * 40,
                "sha256": "2" * 64, "url": "https://codeload.github.com/" + repository + "/tar.gz/" + "1" * 40})
        self.save_lock()
        (self.repo / ".gitignore").write_text("runtime/\n")
        (self.recipe / "Dockerfile").write_text("FROM " + self.lock["baseImage"] + " AS builder\nFROM " + self.lock["baseImage"]
            + '\nLABEL version="@@IMAGE_VERSION@@" revision="@@FS_COMMIT@@"\nRUN echo @@FS_VERSION@@\n')
        (self.recipe / "modules.conf").write_text("endpoints/mod_sofia\n")
        self.fix = b"branch RTP source: rtp_normalize_read_packet\n"
        (self.repo / "src/switch_rtp.c").write_bytes(self.fix)
        self.git("add", ".")
        self.git("commit", "-qm", "NexIoT RTP fix and image recipe")
        self.commit = self.git("rev-parse", "HEAD").strip()
        root_patch = patch.object(build, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.context = self.directory / "context"
        self.cache = self.directory / "cache"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.PIPE)

    def save_lock(self):
        (self.recipe / "build.lock.json").write_text(json.dumps(self.lock))

    def test_archive_contains_branch_fix_and_excludes_ignored_runtime(self):
        (self.repo / "runtime").mkdir()
        (self.repo / "runtime/private.json").write_text("DO-NOT-PACK-THIS")
        with patch.object(build, "source_data", side_effect=lambda _cache, item: item["name"].encode()) as dependencies:
            context = build.prepare(self.cache, self.context)
        self.assertEqual([call.args[1]["name"] for call in dependencies.call_args_list], ["sofia-sip", "spandsp"])
        archive_bytes = (context / "sources/freeswitch.tar.gz").read_bytes()
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            self.assertEqual(archive.extractfile("freeswitch-" + self.commit + "/src/switch_rtp.c").read(), self.fix)
            self.assertFalse(any("/runtime/" in name or "/.git/" in name for name in archive.getnames()))
        generated = json.loads((context / "sources.lock.json").read_text())
        fs_source = generated["sources"][0]
        self.assertEqual(fs_source["commit"], self.commit)
        self.assertEqual(fs_source["sha256"], build.sha256(archive_bytes))
        self.assertNotIn("url", fs_source)
        self.assertIn(self.commit, (context / "Dockerfile").read_text())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_tracked_and_untracked_edits_are_rejected_before_any_download(self):
        for filename, content in (("src/switch_rtp.c", b"uncommitted fix"), ("forgotten.txt", b"untracked input")):
            target = self.repo / filename
            previous = target.read_bytes() if target.exists() else None
            target.write_bytes(content)
            try:
                with self.subTest(filename=filename), patch.object(build, "source_data") as dependencies:
                    with self.assertRaisesRegex(ValueError, "Commit all tracked and untracked"):
                        build.prepare(self.cache, self.context)
                    dependencies.assert_not_called()
                    self.assertFalse(self.context.exists())
            finally:
                if previous is None:
                    target.unlink()
                else:
                    target.write_bytes(previous)

    def test_source_download_cannot_be_switched_back_to_official_freeswitch(self):
        self.lock["sources"].append(dict(self.lock["sources"][0], name="freeswitch", repository="signalwire/freeswitch"))
        self.save_lock()
        self.git("add", ".")
        self.git("commit", "-qm", "invalid source lock")
        with patch.object(build, "source_data") as dependencies:
            with self.assertRaisesRegex(ValueError, "FreeSWITCH comes from HEAD"):
                build.prepare(self.cache, self.context)
        dependencies.assert_not_called()

    def test_wrong_upstream_tag_and_wrong_version_are_rejected(self):
        self.git("tag", "-f", "v1.11.3", self.commit)
        with self.assertRaisesRegex(ValueError, "tag differs"):
            build.prepare(self.cache, self.context)
        self.git("tag", "-f", "v1.11.3", self.lock["upstreamCommit"])
        (self.repo / "configure.ac").write_text("AC_INIT([freeswitch], [1.12.0-release], bugs@freeswitch.org)\n")
        self.git("add", ".")
        self.git("commit", "-qm", "unreviewed version change")
        with self.assertRaisesRegex(ValueError, "configure.ac version differs"):
            build.prepare(self.cache, self.context)

    def test_unrelated_history_is_rejected_even_with_matching_release_tag(self):
        self.git("checkout", "--orphan", "unrelated")
        self.git("commit", "-qm", "unrelated source with matching lock")
        with self.assertRaisesRegex(ValueError, "must descend"):
            build.prepare(self.cache, self.context)


if __name__ == "__main__":
    unittest.main()
