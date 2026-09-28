#!/usr/bin/env python3
"""Build NexIoT FreeSWITCH from this repository's clean, committed HEAD."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
RECIPE = "docker/nexiot/"
REPOSITORIES = {"sofia-sip": "freeswitch/sofia-sip", "spandsp": "freeswitch/spandsp"}
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    process = subprocess.run(["git", "-C", str(ROOT), *args], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, check=False, timeout=120)
    if process.returncode:
        raise ValueError("Git command failed: " + process.stderr.decode("utf-8", errors="replace").strip())
    return process.stdout


def clean_head():
    if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != ROOT.resolve():
        raise ValueError("Run this builder inside the independent FreeSWITCH Git repository")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Commit all tracked and untracked changes before building; ignored files are not archived")
    return git("rev-parse", "--verify", "HEAD^{commit}").decode().strip()


def committed(commit, path):
    return git("show", commit + ":" + path)


def read_lock(commit):
    lock = json.loads(committed(commit, RECIPE + "build.lock.json"))
    version = lock["freeswitchVersion"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("A stable FreeSWITCH version is required")
    if not re.fullmatch(re.escape(version) + r"-[A-Za-z0-9][A-Za-z0-9_.-]*", lock["imageVersion"]):
        raise ValueError("Image version must identify the upstream version and local revision")
    if not re.fullmatch(r"debian:bookworm-slim@sha256:[a-f0-9]{64}", lock["baseImage"]):
        raise ValueError("Debian base must be pinned by SHA256")
    if lock["upstreamTag"] != "v" + version or not re.fullmatch(r"[a-f0-9]{40}", lock["upstreamCommit"]):
        raise ValueError("Upstream release tag and full commit ID are required")
    sources = lock["sources"]
    if len(sources) != 2 or {item["name"] for item in sources} != set(REPOSITORIES):
        raise ValueError("Only the reviewed Sofia-SIP and SpanDSP downloads are allowed; FreeSWITCH comes from HEAD")
    for item in sources:
        if item["repository"] != REPOSITORIES[item["name"]]:
            raise ValueError("Unexpected dependency repository")
        if not re.fullmatch(r"[a-f0-9]{40}", item["commit"]) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Dependencies require full Git commit IDs and SHA256 checksums")
        expected = "https://codeload.github.com/" + item["repository"] + "/tar.gz/" + item["commit"]
        if item["url"] != expected:
            raise ValueError("Dependency URL differs from its locked repository and commit")
    tag_commit = git("rev-parse", "--verify", "refs/tags/" + lock["upstreamTag"] + "^{commit}").decode().strip()
    if tag_commit != lock["upstreamCommit"]:
        raise ValueError("Local upstream tag differs from the reviewed upstream commit")
    try:
        git("merge-base", "--is-ancestor", lock["upstreamCommit"], commit)
    except ValueError as exc:
        raise ValueError("HEAD must descend from the locked upstream release commit") from exc
    configured = re.findall(rb"^AC_INIT\(\[freeswitch\], \[([^]]+)\]", committed(commit, "configure.ac"), re.MULTILINE)
    if configured != [(version + "-release").encode()]:
        raise ValueError("Committed configure.ac version differs from the reviewed release")
    return lock


def verify_archive_data(data, item):
    if len(data) > MAX_ARCHIVE_BYTES or sha256(data) != item["sha256"]:
        raise ValueError("Source archive checksum/size mismatch: " + item["name"])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        roots = set()
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name or not name.parts:
                raise ValueError("Unsafe source archive member")
            roots.add(name.parts[0])
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("Unsupported source archive member type")
            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or "\\" in member.linkname:
                    raise ValueError("Unsafe source archive symlink")
                depth = len(name.parts) - 1
                for part in link.parts:
                    depth += -1 if part == ".." else 0 if part == "." else 1
                    if depth < 1:
                        raise ValueError("Source symlink escapes archive root")
        if len(roots) != 1:
            raise ValueError("Source archive must have one top-level directory")
    return data


def verify_archive(path, item):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular, non-symlink source archive")
    return verify_archive_data(path.read_bytes(), item)


def source_data(cache, item):
    path = cache / (item["name"] + "-" + item["commit"] + ".tar.gz")
    if path.exists() or path.is_symlink():
        return verify_archive(path, item)
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cache, prefix=".download-", delete=False) as temporary:
        download = Path(temporary.name)
        try:
            request = urllib.request.Request(item["url"], headers={"User-Agent": "NexIoT-FreeSWITCH-Build/2"})
            with urllib.request.urlopen(request, timeout=60) as response:
                total = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("Downloaded source archive exceeds size limit")
                    temporary.write(block)
            temporary.flush()
            data = verify_archive(download, item)
            download.replace(path)
            return data
        finally:
            download.unlink(missing_ok=True)


def outside_repository(path):
    resolved = path.resolve()
    if resolved == ROOT.resolve() or ROOT.resolve() in resolved.parents:
        raise ValueError("Build cache and context must be outside the FreeSWITCH repository")


def prepare(cache, context_dir=None):
    outside_repository(cache)
    if context_dir is not None:
        outside_repository(context_dir)
        if context_dir.exists() or context_dir.is_symlink():
            raise FileExistsError("Build context already exists: " + str(context_dir))
    commit = clean_head()
    lock = read_lock(commit)
    dockerfile = committed(commit, RECIPE + "Dockerfile")
    bases = re.findall(r"^FROM\s+(\S+)", dockerfile.decode(), re.MULTILINE)
    if bases != [lock["baseImage"], lock["baseImage"]]:
        raise ValueError("Both Docker stages must use the locked Debian base")
    for token, value in {"@@IMAGE_VERSION@@": lock["imageVersion"],
                         "@@FS_VERSION@@": lock["freeswitchVersion"], "@@FS_COMMIT@@": commit}.items():
        if token.encode() not in dockerfile:
            raise ValueError("Missing Dockerfile metadata token: " + token)
        dockerfile = dockerfile.replace(token.encode(), value.encode())
    # Archive a resolved commit, never the working tree or a moving branch name.
    archive_prefix = "freeswitch-" + commit + "/"
    archive = git("archive", "--format=tar.gz", "--prefix=" + archive_prefix, commit)
    fs_source = {"name": "freeswitch", "source": "local-git-archive", "commit": commit,
                 "tree": git("rev-parse", commit + "^{tree}").decode().strip(), "sha256": sha256(archive)}
    verify_archive_data(archive, fs_source)
    # Attribute overrides must not silently omit or replace the reviewed RTP source.
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        source = tar.extractfile(archive_prefix + "src/switch_rtp.c")
        if source is None or source.read() != committed(commit, "src/switch_rtp.c"):
            raise ValueError("Archived RTP source differs from the committed source")
    files = {"Dockerfile": dockerfile, "modules.conf": committed(commit, RECIPE + "modules.conf"),
             "sources/freeswitch.tar.gz": archive}
    locked_sources = [fs_source] + lock["sources"]
    source_lock = dict(lock, sources=locked_sources)
    files["sources.lock.json"] = (json.dumps(source_lock, indent=2, sort_keys=True) + "\n").encode()
    for item in lock["sources"]:
        files["sources/" + item["name"] + ".tar.gz"] = source_data(cache, item)
    files["sources/sources.sha256"] = ("\n".join(sorted(
        item["sha256"] + "  " + item["name"] + ".tar.gz" for item in locked_sources)) + "\n").encode("ascii")
    manifest = {"sourceCommit": commit, "sourceTree": fs_source["tree"], "imageBase": lock["baseImage"],
                "files": [{"path": name, "size": len(data), "sha256": sha256(data)}
                          for name, data in sorted(files.items())]}
    files["context.manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    if clean_head() != commit:
        raise ValueError("Repository HEAD changed during preparation; rerun with a clean, stable checkout")
    if context_dir is None:
        context = Path(tempfile.mkdtemp(prefix="nexiot-freeswitch-context-"))
    else:
        context = context_dir.absolute()
        context.mkdir(parents=True, exist_ok=False)
    try:
        for name, data in sorted(files.items()):
            destination = context / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
    except Exception:
        shutil.rmtree(context)
        raise
    return context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--source-cache", type=Path, default=Path.home() / ".cache/nexiot-freeswitch/sources")
    parser.add_argument("--context-dir", type=Path, help="New directory outside the repository; default: persistent temporary directory")
    parser.add_argument("--tag", help="Default: nexiot/freeswitch:<committed imageVersion>")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--debian-mirror", choices=("deb.debian.org", "mirrors.aliyun.com"), default="deb.debian.org")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 64:
        parser.error("--jobs must be between 1 and 64")
    if args.tag and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", args.tag):
        parser.error("Invalid image tag")
    try:
        context = prepare(args.source_cache, args.context_dir)
        prepared_lock = json.loads((context / "sources.lock.json").read_text())
        tag = args.tag or "nexiot/freeswitch:" + prepared_lock["imageVersion"]
        print("context=" + str(context), flush=True)
        print("source_commit=" + prepared_lock["sources"][0]["commit"], flush=True)
        if not args.prepare_only:
            subprocess.run(["docker", "build", "--platform", args.platform, "--build-arg",
                            "BUILD_JOBS=" + str(args.jobs), "--build-arg", "DEBIAN_MIRROR=" + args.debian_mirror,
                            "--tag", tag, str(context)], check=True)
            print("image=" + tag)
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError,
            subprocess.SubprocessError) as exc:
        print("build_image: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
