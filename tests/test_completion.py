# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests of the bash completion in completions/check_btrfs_sysfs."""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import check_btrfs_sysfs as cbs  # noqa: E402

COMPLETION = os.path.join(ROOT, "completions", "check_btrfs_sysfs")

MOUNTS = """\
/dev/sda2 / btrfs rw,relatime,ssd,subvolid=256,subvol=/@ 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda2 /home btrfs rw,relatime,ssd,subvolid=257,subvol=/@home 0 0
/dev/sda1 /boot/efi vfat rw,relatime 0 0
/dev/sdb1 /srv btrfs rw,relatime 0 0
"""


def complete(*words, cwd=None, mounts=os.devnull):
    """The candidates bash offers for the last of words, which may be empty."""
    words = ("check_btrfs_sysfs",) + words
    script = 'source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD=$(($# - 1)); ' \
             '_check_btrfs_sysfs; printf "%s\\n" "${COMPREPLY[@]}"'
    env = dict(os.environ, BTRFS_MOUNTS=mounts)
    result = subprocess.run(["bash", "-c", script, "bash", COMPLETION] + list(words),
                            stdout=subprocess.PIPE, universal_newlines=True, check=True,
                            cwd=cwd, env=env)
    return [line for line in result.stdout.split("\n") if line]


def help_words():
    """The option strings --help lists.

    Before Python 3.13 argparse repeats the metavar after every option string
    ("-m MOUNTPOINT, --mountpoint MOUNTPOINT"), since then only after the last
    ("-m, --mountpoint MOUNTPOINT"). An option line starts with the invocation,
    followed by the description after two or more spaces or, if the
    invocation is long, on the next line.
    """
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.suppress(SystemExit):
        cbs.build_parser().parse_args(["--help"])
    words = set()
    for line in output.getvalue().split("\n"):
        match = re.match(r"  (-\S.*?)(?:\s{2,}|$)", line)
        if match:
            words.update(re.findall(r"(?:^|, )(-{1,2}[^\s,=\[{]+)", match.group(1)))
    return words


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mounts = os.path.join(self.directory.name, "mounts")
        with open(self.mounts, "w") as f:
            f.write(MOUNTS)

    def test_syntax(self):
        subprocess.run(["bash", "-n", COMPLETION], check=True)

    def test_offers_exactly_the_options_of_help(self):
        self.assertEqual(set(complete("")), help_words())

    def test_option_prefix(self):
        self.assertEqual(complete("--meta"), ["--metadata-warning", "--metadata-critical"])

    def test_local_btrfs_mountpoints(self):
        self.assertEqual(complete("-m", "", mounts=self.mounts), ["/", "/home", "/srv"])
        self.assertEqual(complete("--mountpoint", "=", "/h", mounts=self.mounts), ["/home"])

    def test_remote_mountpoints_are_directories(self):
        os.mkdir(os.path.join(self.directory.name, "data"))
        self.assertEqual(complete("-H", "server", "-m", "da", cwd=self.directory.name,
                                  mounts=self.mounts), ["data"])

    def test_errors_state(self):
        self.assertEqual(complete("-e", ""), ["critical", "warning", "ignore"])
        self.assertEqual(complete("--errors", "=", "w"), ["warning"])

    def test_identity_file(self):
        open(os.path.join(self.directory.name, "monitor.key"), "w").close()
        self.assertEqual(complete("-i", "mon", cwd=self.directory.name), ["monitor.key"])

    def test_no_values_for_thresholds(self):
        for words in (("-u", ""), ("-w", ""), ("--data-critical", "=", ""), ("-t", ""),
                      ("-p", "")):
            self.assertEqual(complete(*words), [], words)


if __name__ == "__main__":
    unittest.main()
