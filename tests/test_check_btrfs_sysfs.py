# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for check_btrfs_sysfs using canned sysfs data."""

import argparse
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import check_btrfs_sysfs as cb  # noqa: E402

GiB = 1024 ** 3
MiB = 1024 ** 2


def sysfs(device_gib=30, data_total=20 * GiB, data_used=15 * GiB, meta_total=512 * MiB,
          meta_used=200 * MiB, reserve=50 * MiB, sys_total=32 * MiB, meta_profile="dup",
          errors=None, missing="0", devices=1):
    """Collector output lines of one file system."""
    meta_factor = 2 if meta_profile in ("dup", "raid1") else 1
    lines = [
        f"allocation/data/total_bytes:{data_total}",
        f"allocation/data/bytes_used:{int(data_used)}",
        f"allocation/data/disk_total:{data_total}",
        f"allocation/data/single/total_bytes:{data_total}",
        f"allocation/metadata/total_bytes:{meta_total}",
        f"allocation/metadata/bytes_used:{meta_used}",
        f"allocation/metadata/disk_total:{meta_total * meta_factor}",
        f"allocation/metadata/{meta_profile}/total_bytes:{meta_total}",
        f"allocation/system/total_bytes:{sys_total}",
        f"allocation/system/disk_total:{sys_total * meta_factor}",
        f"allocation/global_rsv_size:{reserve}",
        "allocation/data/size_classes:small 9",
    ]
    for devid in range(1, devices + 1):
        counters = {"write_errs": 0, "read_errs": 0, "flush_errs": 0, "corruption_errs": 0,
                    "generation_errs": 0}
        counters.update((errors or {}).get(str(devid), {}))
        lines += [f"devinfo/{devid}/error_stats:{name} {count}"
                  for name, count in counters.items()]
        lines.append(f"devinfo/{devid}/missing:{missing if devid == 1 else 0}")
        lines.append(f"devices/vd{chr(96 + devid)}/size:{device_gib * GiB // devices // 512}")
    return lines


def output(*filesystems):
    """Complete collector output; a string item is a marker instead of data."""
    result = []
    for number, fs in enumerate(filesystems):
        result.append(f"@@mount {number}@@")
        result += [f"@@{fs}@@"] if isinstance(fs, str) else fs
    return "\n".join(result) + "\n"


def run(argv, collected):
    with mock.patch.object(cb, "collect", return_value=collected), \
            mock.patch("builtins.print") as printed:
        state = cb.main(argv)
    return state, printed.call_args[0][0]


class ThresholdTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(cb.SizeThreshold("2G").bytes(0), 2 * GiB)
        self.assertEqual(cb.SizeThreshold("2").bytes(0), 2 * GiB)
        self.assertEqual(cb.SizeThreshold("512M").bytes(0), 512 * MiB)
        self.assertEqual(cb.SizeThreshold("1.5GiB").bytes(0), 1.5 * GiB)
        self.assertEqual(cb.SizeThreshold("10%").bytes(50 * GiB), 5 * GiB)

    def test_invalid(self):
        for spec in ("", "abc", "-1G", "5X"):
            with self.assertRaises(argparse.ArgumentTypeError):
                cb.SizeThreshold(spec)
        with self.assertRaises(argparse.ArgumentTypeError):
            cb.percent("101")


class FilesystemTest(unittest.TestCase):
    def test_space(self):
        fs = cb.Filesystem(sysfs())
        # 30 GiB - 20 GiB data - 2 * 512 MiB metadata - 2 * 32 MiB system
        self.assertEqual(fs.unallocated, 30 * GiB - 20 * GiB - 1024 * MiB - 64 * MiB)
        self.assertEqual(fs.factor("metadata"), 2.0)
        used, usable, pct = fs.space("metadata", fs.global_reserve)
        self.assertEqual(used, 250 * MiB)
        self.assertEqual(usable, 512 * MiB + fs.unallocated / 2)
        self.assertAlmostEqual(pct, 100.0 * used / usable)
        self.assertEqual(fs.profile("metadata"), "dup")

    def test_factor_without_chunks_uses_profile(self):
        lines = [l for l in sysfs(meta_profile="raid1c3") if not l.startswith(
            ("allocation/metadata/total_bytes", "allocation/metadata/disk_total"))]
        self.assertEqual(cb.Filesystem(lines).factor("metadata"), 3.0)

    def test_raid5_factor(self):
        lines = [l for l in sysfs(devices=4, meta_profile="raid5") if not l.startswith(
            ("allocation/metadata/total_bytes", "allocation/metadata/disk_total"))]
        self.assertAlmostEqual(cb.Filesystem(lines).factor("metadata"), 4 / 3)

    def test_errors_and_missing(self):
        fs = cb.Filesystem(sysfs(devices=2, errors={"2": {"corruption_errs": 3}}, missing="1"))
        self.assertEqual(fs.errors(), {"2": {"corruption_errs": 3}})
        self.assertEqual(fs.missing(), ["1"])
        self.assertEqual(fs.devices, 2)

    def test_incomplete(self):
        with self.assertRaises(cb.CheckError):
            cb.Filesystem(["allocation/data/total_bytes:1"])


class CheckTest(unittest.TestCase):
    def test_ok(self):
        state, text = run([], output(sysfs()))
        self.assertEqual(state, cb.OK)
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("BTRFS OK - /: unallocated 8.9 GiB, metadata"), first)
        self.assertIn("/_unallocated=", first)
        self.assertIn("/_metadata_pct=", first)
        self.assertIn("/_errors=0c", first)
        self.assertNotIn("balance", text)

    def test_fully_allocated(self):
        fs = sysfs(device_gib=21, data_total=20 * GiB - 64 * MiB, data_used=15 * GiB)
        state, text = run([], output(fs))
        self.assertEqual(state, cb.CRITICAL)
        self.assertIn("unallocated 0 B (< 1.0 GiB)", text)
        self.assertIn("btrfs balance start -dusage=50 /", text)

    def test_unallocated_warning(self):
        fs = sysfs(device_gib=23, data_total=20 * GiB, data_used=19.5 * GiB)
        state, text = run([], output(fs))
        self.assertEqual(state, cb.WARNING)
        self.assertIn("(< 2.0 GiB)", text)
        self.assertNotIn("balance", text)  # less than 1 GiB to reclaim

    def test_unallocated_percent(self):
        state, _ = run(["-u", "40%", "-U", "35%"], output(sysfs()))
        self.assertEqual(state, cb.CRITICAL)

    def test_metadata_full(self):
        fs = sysfs(device_gib=21, data_total=20 * GiB - 64 * MiB, meta_used=470 * MiB)
        state, text = run(["-U", "0", "-u", "0"], output(fs))
        self.assertEqual(state, cb.CRITICAL)
        self.assertIn("metadata 102% used", text)

    def test_metadata_warning(self):
        fs = sysfs(device_gib=21, data_total=20 * GiB - 64 * MiB, meta_used=350 * MiB)
        state, text = run(["-U", "0", "-u", "0"], output(fs))
        self.assertEqual(state, cb.WARNING)
        self.assertIn("metadata 78% used (112.0 MiB left)", text)

    def test_metadata_with_unallocated_space_is_fine(self):
        state, _ = run([], output(sysfs(meta_used=500 * MiB)))
        self.assertEqual(state, cb.OK)

    def test_data_thresholds(self):
        self.assertEqual(run([], output(sysfs(data_used=19 * GiB)))[0], cb.OK)
        state, text = run(["-d", "60", "-D", "80"], output(sysfs(data_used=19 * GiB)))
        self.assertEqual(state, cb.WARNING)
        self.assertIn("data 66% used", text)
        self.assertIn("/_data_pct=65.66%;60;80;0;100", text)

    def test_device_errors(self):
        fs = sysfs(errors={"1": {"write_errs": 1}})
        state, text = run([], output(fs))
        self.assertEqual(state, cb.CRITICAL)
        self.assertIn("device 1 errors: write_errs 1", text)
        self.assertIn("/_errors=1c", text)
        self.assertEqual(run(["-e", "warning"], output(fs))[0], cb.WARNING)
        self.assertEqual(run(["-e", "ignore"], output(fs))[0], cb.OK)

    def test_missing_device(self):
        state, text = run([], output(sysfs(missing="1")))
        self.assertEqual(state, cb.CRITICAL)
        self.assertIn("device 1 missing", text)

    def test_multiple_mount_points(self):
        state, text = run(["-m", "/", "-m", "/srv"],
                          output(sysfs(), sysfs(errors={"1": {"read_errs": 2}})))
        self.assertEqual(state, cb.CRITICAL)
        first = text.splitlines()[0]
        self.assertIn("/: unallocated", first)
        self.assertIn("/srv: device 1 errors: read_errs 2", first)
        self.assertIn("/srv_unallocated=", first)
        self.assertIn("\n/srv:\n", text)

    def test_not_btrfs(self):
        state, text = run(["-m", "/", "-m", "/home"], output(sysfs(), "notbtrfs ext4"))
        self.assertEqual(state, cb.UNKNOWN)
        self.assertIn("/home is not btrfs (ext4)", text)

    def test_findmnt_error(self):
        state, text = run(["-m", "/nope"], output("error findmnt: /nope: not found"))
        self.assertEqual(state, cb.UNKNOWN)
        self.assertIn("/nope: findmnt: /nope: not found", text)

    def test_wrong_block_count(self):
        state, text = run(["-m", "/", "-m", "/srv"], output(sysfs()))
        self.assertEqual(state, cb.UNKNOWN)
        self.assertIn("expected data of 2 mount points, got 1", text)


class CollectTest(unittest.TestCase):
    def args(self, *argv):
        args = cb.build_parser().parse_args(list(argv))
        args.mountpoint = args.mountpoint or ["/"]
        return args

    def test_ssh_command(self):
        proc = mock.Mock(returncode=0, stdout="@@mount 0@@\n", stderr="")
        with mock.patch.object(cb.subprocess, "run", return_value=proc) as srun:
            cb.collect(self.args("-H", "host.example", "-l", "monitor", "-p", "2222",
                                 "-m", "/", "-m", "/my data", "-o", "Foo=bar"))
        cmd = srun.call_args[0][0]
        self.assertEqual(cmd[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"])
        self.assertEqual(cmd[-3:-1], ["monitor", "host.example"])
        self.assertIn("2222", cmd)
        self.assertIn("Foo=bar", cmd)
        self.assertTrue(cmd[-1].startswith("sh -c "))
        self.assertIn("set -- '\\''/'\\'' '\\''/my data'\\''", cmd[-1])

    def test_local_command(self):
        proc = mock.Mock(returncode=0, stdout="@@mount 0@@\n", stderr="")
        with mock.patch.object(cb.subprocess, "run", return_value=proc) as srun:
            cb.collect(self.args("-m", "/srv"))
        cmd = srun.call_args[0][0]
        self.assertEqual(cmd[:2], ["sh", "-c"])
        self.assertEqual(cmd[-2:], ["sh", "/srv"])

    def test_ssh_failure(self):
        proc = mock.Mock(returncode=255, stdout="", stderr="Permission denied (publickey).\n")
        with mock.patch.object(cb.subprocess, "run", return_value=proc), \
                mock.patch("builtins.print") as printed:
            state = cb.main(["-H", "host.example"])
        self.assertEqual(state, cb.UNKNOWN)
        self.assertIn("ssh failed: Permission denied", printed.call_args[0][0])

    def test_timeout(self):
        err = cb.subprocess.TimeoutExpired("ssh", 10)
        with mock.patch.object(cb.subprocess, "run", side_effect=err), \
                mock.patch("builtins.print") as printed:
            state = cb.main(["-H", "host.example"])
        self.assertEqual(state, cb.UNKNOWN)
        self.assertIn("timeout after 10s", printed.call_args[0][0])

    def test_collector_on_non_btrfs_path(self):
        """Runs the real collector; the test directory is not btrfs on most systems."""
        directory = os.path.dirname(os.path.abspath(__file__))
        fstype = subprocess.run(["findmnt", "-rno", "FSTYPE", "--target", directory],
                                stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        if not fstype or fstype == "btrfs":
            self.skipTest("needs findmnt and a non-btrfs test directory")
        out = cb.collect(self.args("-m", directory))
        self.assertEqual(out.splitlines(), ["@@mount 0@@", f"@@notbtrfs {fstype}@@"])


if __name__ == "__main__":
    unittest.main()
