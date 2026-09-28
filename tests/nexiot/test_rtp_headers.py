#!/usr/bin/env python3
"""Compile and test the actual normalization helper from patched FreeSWITCH source.

This verifies synthetic packet handling with ASan/UBSan, not a live SIP session
or SRTP interoperability. No user media, recording, or network access is used.
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="patched src/switch_rtp.c")
    parser.add_argument("--no-sanitize", action="store_true", help="disable ASan/UBSan")
    parser.add_argument("--sanitizers", choices=("address,undefined", "address", "undefined"),
                        default="address,undefined", help="enabled compiler sanitizers")
    args = parser.parse_args()
    source = args.source.read_text()
    signature = "static switch_bool_t rtp_normalize_read_packet("
    if source.count(signature) != 1:
        raise SystemExit("Expected one patched RTP normalizer in the supplied source")
    start = source.index(signature)
    body = source.index("{", start)
    depth = 1
    end = body + 1
    while depth and end < len(source):
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    if depth:
        raise SystemExit("Unclosed RTP normalizer in supplied source")
    helper = source[start:end]
    prelude = """#include <stddef.h>
#include <string.h>
typedef size_t switch_size_t;
typedef int switch_bool_t;
#define SWITCH_TRUE 1
#define SWITCH_FALSE 0
#define rtp_header_len 12
"""
    with tempfile.TemporaryDirectory(prefix="nexiot-rtp-headers-") as directory:
        path = Path(directory)
        (path / "rtp_normalizer_under_test.h").write_text(prelude + helper + "\n")
        command = shlex.split(os.environ.get("CC", "cc")) + [
            "-std=c99", "-Wall", "-Wextra", "-Werror", "-O1", "-g",
            "-fno-omit-frame-pointer", "-I", str(path),
            str(Path(__file__).with_name("rtp_headers_test.c")), "-o", str(path / "test"),
        ]
        if not args.no_sanitize:
            command += ["-fsanitize=" + args.sanitizers]
            if sys.platform.startswith("linux") and "address" in args.sanitizers.split(","):
                # PIE + ASLR can collide with GCC ASan's shadow mapping on Linux
                # (even an empty main crashes intermittently). Only the isolated
                # test executable uses a fixed address; engine build flags stay intact.
                command += ["-fno-pie", "-no-pie"]
        subprocess.run(command, check=True)
        subprocess.run([str(path / "test")], check=True)


if __name__ == "__main__":
    main()
