#!/usr/bin/env python3
# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Nagios/Naemon/Icinga plugin for btrfs space and device errors, without root.

Reads /sys/fs/btrfs/<UUID>/ (world readable) instead of using btrfs ioctls,
which need CAP_SYS_ADMIN. Checks the unallocated space, the space left for
metadata, optionally the space left for data, and the device error counters.

Without -H the local machine is checked, with -H the plugin reads sysfs of a
remote host over SSH, so nothing has to be installed there.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys

__version__ = "0.3"

OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
STATE_NAMES = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}

UNITS = {"": 1024 ** 3, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}

# bytes on disk per byte stored, used when a block group type has no chunks yet
PROFILE_FACTORS = {"single": 1.0, "dup": 2.0, "raid0": 1.0, "raid1": 2.0, "raid1c3": 3.0,
                   "raid1c4": 4.0, "raid10": 2.0}

# Prints the sysfs files of each mount point as "relative/path:line". Mount
# points are passed as positional parameters; each block starts with a
# "@@mount N@@" line (N counts from 0).
COLLECTOR = r"""
n=0
for p in "$@"; do
    echo "@@mount $n@@"
    n=$((n + 1))
    info=$(findmnt -rno FSTYPE,UUID --target "$p" 2>&1) || {
        echo "@@error $info@@"; continue; }
    fstype=${info%% *} uuid=${info#* }
    if [ "$fstype" != btrfs ]; then
        echo "@@notbtrfs $fstype@@"; continue
    fi
    (
        cd "/sys/fs/btrfs/$uuid" 2>/dev/null || { echo "@@nosysfs@@"; exit; }
        grep -rs '' allocation devinfo/*/error_stats devinfo/*/missing
        for s in devices/*/size; do
            [ -r "$s" ] && echo "$s:$(cat "$s")"
        done
    )
done
"""


class CheckError(Exception):
    """Raised when the state cannot be determined (exits UNKNOWN)."""


def worst(*states: int) -> int:
    """Combine states; CRITICAL beats WARNING beats UNKNOWN beats OK."""
    order = [OK, UNKNOWN, WARNING, CRITICAL]
    return max(states, key=order.index, default=OK)


class SizeThreshold:
    """Lower limit for free space: a size like "2G", "512M" or a percentage."""

    def __init__(self, spec: str):
        self.spec = spec
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([BKMGT%]?)i?B?\s*", spec, re.I)
        if not match:
            raise argparse.ArgumentTypeError(f"invalid size '{spec}', use e.g. 2G, 500M or 5%")
        self.value = float(match.group(1))
        self.unit = match.group(2).upper()

    def bytes(self, size: int) -> float:
        if self.unit == "%":
            return size * self.value / 100
        return self.value * UNITS[self.unit]

    def __str__(self) -> str:
        return self.spec


def percent(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid percentage '{value}'")
    if not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("percentage must be between 0 and 100")
    return result


def fmt_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return str(num)


def perf_label(name: str) -> str:
    name = name.replace("'", "_").replace("=", "_")
    return f"'{name}'" if re.search(r"\s", name) else name


def fmt_num(value: float) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def perf(label: str, value, uom="", warning="", critical="", minimum="", maximum="") -> str:
    fields = [fmt_num(value) + uom] + [fmt_num(v) if v != "" else "" for v in
                                       (warning, critical, minimum, maximum)]
    return f"{perf_label(label)}=" + ";".join(fields).rstrip(";")


# --------------------------------------------------------------------------
# Collecting and parsing
# --------------------------------------------------------------------------


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def collect(args) -> str:
    """Run the collector locally or over SSH and return its output."""
    if args.hostname:
        script = "set -- " + " ".join(shell_quote(m) for m in args.mountpoint) + "\n" + COLLECTOR
        cmd = [args.ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(args.timeout, 30)}"]
        if args.port:
            cmd += ["-p", str(args.port)]
        if args.identity:
            cmd += ["-i", args.identity]
        for option in args.ssh_option or []:
            cmd += ["-o", option]
        if args.user:
            cmd += ["-l", args.user]
        cmd += [args.hostname, "sh -c " + shell_quote(script)]
    else:
        cmd = ["sh", "-c", COLLECTOR, "sh"] + args.mountpoint
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=args.timeout,
                              universal_newlines=True)
    except subprocess.TimeoutExpired:
        raise CheckError(f"timeout after {args.timeout}s")
    except OSError as err:
        raise CheckError(f"could not run {cmd[0]}: {err.strerror}")
    if args.hostname and proc.returncode == 255:
        lines = proc.stderr.strip().splitlines()
        raise CheckError("ssh failed: " + (lines[-1] if lines else "unknown error"))
    if "@@mount 0@@" not in proc.stdout:
        lines = proc.stderr.strip().splitlines()
        raise CheckError("collecting the sysfs data failed"
                         + (f": {lines[-1]}" if lines else ""))
    return proc.stdout


def split_mounts(output: str, count: int) -> list[tuple[str, list[str]]]:
    """Split the collector output into (marker, lines) per mount point."""
    blocks: list[tuple[str, list[str]]] = []
    for line in output.splitlines():
        match = re.fullmatch(r"@@(.*)@@", line.strip())
        if match and match.group(1).startswith("mount "):
            blocks.append(("", []))
        elif match and blocks:
            blocks[-1] = (match.group(1), blocks[-1][1])
        elif blocks:
            blocks[-1][1].append(line)
    if len(blocks) != count:
        raise CheckError(f"expected data of {count} mount points, got {len(blocks)}")
    return blocks


class Filesystem:
    """Space and device state of one btrfs file system from sysfs."""

    def __init__(self, lines: list[str]):
        files: dict[str, list[str]] = {}
        for line in lines:
            path, sep, value = line.partition(":")
            if sep:
                files.setdefault(path, []).append(value.strip())
        self.files = files
        self.device_size = sum(int(v[0]) * 512 for p, v in files.items()
                               if re.fullmatch(r"devices/[^/]+/size", p))
        if not self.device_size or "allocation/data/total_bytes" not in files:
            raise CheckError("incomplete sysfs data")
        self.devices = len([p for p in files if re.fullmatch(r"devices/[^/]+/size", p)])
        self.allocated = sum(self.int(f"allocation/{t}/disk_total")
                             for t in ("data", "metadata", "system"))
        self.unallocated = max(self.device_size - self.allocated, 0)
        self.global_reserve = self.int("allocation/global_rsv_size")

    def int(self, path: str) -> int:
        try:
            return int(self.files[path][0])
        except (KeyError, IndexError, ValueError):
            return 0

    def profile(self, kind: str) -> str:
        names = {p.split("/")[2] for p in self.files
                 if p.startswith(f"allocation/{kind}/") and p.count("/") == 3}
        return ",".join(sorted(names)) or "?"

    def factor(self, kind: str) -> float:
        """Bytes on disk per byte stored for a block group type."""
        total = self.int(f"allocation/{kind}/total_bytes")
        if total:
            return self.int(f"allocation/{kind}/disk_total") / total
        profile = self.profile(kind)
        if profile in ("raid5", "raid6") and self.devices > (1 if profile == "raid5" else 2):
            parity = 1 if profile == "raid5" else 2
            return self.devices / (self.devices - parity)
        return PROFILE_FACTORS.get(profile, 1.0)

    def space(self, kind: str, reserve: int = 0) -> tuple[int, float, float]:
        """Used bytes, usable bytes and usage in percent of a block group type.

        Usable is the size of the allocated chunks plus the unallocated space
        converted with the profile factor, i.e. what the type could still grow
        to. The reserve (the global reserve for metadata) counts as used.
        """
        used = self.int(f"allocation/{kind}/bytes_used") + reserve
        usable = self.int(f"allocation/{kind}/total_bytes") + self.unallocated / self.factor(kind)
        return used, usable, (100.0 * used / usable if usable else 100.0)

    def errors(self) -> dict[str, dict[str, int]]:
        """Non-zero error counters per device id."""
        result: dict[str, dict[str, int]] = {}
        for path, values in self.files.items():
            match = re.fullmatch(r"devinfo/(\d+)/error_stats", path)
            if not match:
                continue
            for value in values:
                name, _, count = value.partition(" ")
                if count.strip().isdigit() and int(count) > 0:
                    result.setdefault(match.group(1), {})[name] = int(count)
        return result

    def missing(self) -> list[str]:
        return sorted(re.fullmatch(r"devinfo/(\d+)/missing", p).group(1)
                      for p, v in self.files.items()
                      if re.fullmatch(r"devinfo/(\d+)/missing", p) and v and v[0] == "1")


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def balance_hint(mount: str, reclaimable: int, fill: float) -> list[str]:
    """Lines suggesting a balance of the data chunks, which are fill percent full on average.

    sysfs has no usage per chunk, only the average. -dusage=N relocates the
    chunks that are at most N percent full, and at least one chunk is not
    fuller than the average, so N = the average rounded up relocates at least
    that one, if the other chunks have room for its data.
    """
    if fill <= 50:
        return [f"  about {fmt_bytes(reclaimable)} of the data chunks are unused, "
                f"'btrfs balance start -dusage=50 {mount}' returns most of it"]
    usage = min(100, math.ceil(fill / 5) * 5)
    return [f"  about {fmt_bytes(reclaimable)} of the data chunks are unused, but they are "
            f"{fill:.0f}% full on average, so -dusage=50 may relocate nothing",
            f"  'btrfs balance start -dusage={usage} {mount}' relocates at least one chunk; "
            f"if it fails with 'No space left on device', delete data first"]


def check_filesystem(mount: str, fs: Filesystem, args) -> tuple[int, list[str], str,
                                                                  list[str], list[str]]:
    """State, problems, OK summary, detail lines and performance data."""
    state = OK
    problems: list[str] = []
    details: list[str] = []
    perfdata: list[str] = []

    unalloc_w = args.unallocated_warning.bytes(fs.device_size)
    unalloc_c = args.unallocated_critical.bytes(fs.device_size)
    if fs.unallocated < unalloc_c:
        unalloc_state = CRITICAL
    elif fs.unallocated < unalloc_w:
        unalloc_state = WARNING
    else:
        unalloc_state = OK
    state = worst(state, unalloc_state)
    text = f"unallocated {fmt_bytes(fs.unallocated)}"
    if unalloc_state != OK:
        limit = unalloc_c if unalloc_state == CRITICAL else unalloc_w
        problems.append(f"{text} (< {fmt_bytes(limit)})")
    details.append(f"[{STATE_NAMES[unalloc_state]}] {text} of {fmt_bytes(fs.device_size)} "
                   f"({fs.devices} device{'s' if fs.devices != 1 else ''})")
    perfdata.append(perf(f"{mount}_unallocated", fs.unallocated, "B", round(unalloc_w),
                         round(unalloc_c), 0, fs.device_size))

    meta_used, meta_usable, meta_pct = fs.space("metadata", fs.global_reserve)
    meta_state = OK
    if meta_pct >= args.metadata_critical:
        meta_state = CRITICAL
    elif meta_pct >= args.metadata_warning:
        meta_state = WARNING
    state = worst(state, meta_state)
    text = f"metadata {meta_pct:.0f}% used"
    if meta_state != OK:
        problems.append(f"{text} ({fmt_bytes(meta_usable - meta_used)} left)")
    details.append(f"[{STATE_NAMES[meta_state]}] {text}: {fmt_bytes(meta_used)} of "
                   f"{fmt_bytes(meta_usable)} incl. {fmt_bytes(fs.global_reserve)} global "
                   f"reserve ({fs.profile('metadata')})")
    perfdata.append(perf(f"{mount}_metadata_pct", round(meta_pct, 2), "%",
                         args.metadata_warning, args.metadata_critical, 0, 100))
    perfdata.append(perf(f"{mount}_metadata_used", meta_used, "B", "", "", 0, round(meta_usable)))

    data_used, data_usable, data_pct = fs.space("data")
    data_state = OK
    if args.data_critical is not None and data_pct >= args.data_critical:
        data_state = CRITICAL
    elif args.data_warning is not None and data_pct >= args.data_warning:
        data_state = WARNING
    state = worst(state, data_state)
    text = f"data {data_pct:.0f}% used"
    if data_state != OK:
        problems.append(f"{text} ({fmt_bytes(data_usable - data_used)} left)")
    details.append(f"[{STATE_NAMES[data_state]}] {text}: {fmt_bytes(data_used)} of "
                   f"{fmt_bytes(data_usable)} ({fs.profile('data')})")
    perfdata.append(perf(f"{mount}_data_pct", round(data_pct, 2), "%",
                         "" if args.data_warning is None else args.data_warning,
                         "" if args.data_critical is None else args.data_critical, 0, 100))
    perfdata.append(perf(f"{mount}_data_used", data_used, "B", "", "", 0, round(data_usable)))

    # space in partly used data chunks that a balance would return
    data_total = fs.int("allocation/data/total_bytes")
    data_bytes_used = fs.int("allocation/data/bytes_used")
    reclaimable = int((data_total - data_bytes_used) * fs.factor("data"))
    if unalloc_state != OK and reclaimable >= 1024 ** 3:
        details.extend(balance_hint(mount, reclaimable, 100 * data_bytes_used / data_total))

    errors = fs.errors()
    total_errors = sum(sum(c.values()) for c in errors.values())
    missing = fs.missing()
    if args.errors != "ignore":
        error_state = CRITICAL if args.errors == "critical" else WARNING
        for devid in missing:
            state = worst(state, CRITICAL)
            problems.append(f"device {devid} missing")
            details.append(f"[CRITICAL] device {devid} missing")
        for devid, counters in sorted(errors.items()):
            text = ", ".join(f"{name} {count}" for name, count in sorted(counters.items()))
            state = worst(state, error_state)
            problems.append(f"device {devid} errors: {text}")
            details.append(f"[{STATE_NAMES[error_state]}] device {devid} errors: {text} "
                           f"(reset with 'btrfs device stats -z {mount}')")
    perfdata.append(perf(f"{mount}_errors", total_errors, "c"))
    ok_summary = f"unallocated {fmt_bytes(fs.unallocated)}, metadata {meta_pct:.0f}% used"
    return state, problems, ok_summary, details, perfdata


def run(args) -> tuple[int, str]:
    output = collect(args)
    state = OK
    summary: list[str] = []
    details: list[str] = []
    perfdata: list[str] = []
    for mount, (marker, lines) in zip(args.mountpoint,
                                      split_mounts(output, len(args.mountpoint))):
        if marker:
            kind, _, info = marker.partition(" ")
            text = {"notbtrfs": f"{mount} is not btrfs ({info})",
                    "nosysfs": f"no sysfs data for {mount}",
                    "error": f"{mount}: {info}"}.get(kind, f"{mount}: {marker}")
            state = worst(state, UNKNOWN)
            summary.append(text)
            details.append(f"[UNKNOWN] {text}")
            continue
        try:
            fs_state, problems, ok_summary, fs_details, fs_perf = check_filesystem(
                mount, Filesystem(lines), args)
        except CheckError as err:
            state = worst(state, UNKNOWN)
            summary.append(f"{mount}: {err}")
            continue
        state = worst(state, fs_state)
        summary.append(f"{mount}: " + (", ".join(problems) if problems else ok_summary))
        if len(args.mountpoint) > 1:
            details.append(f"{mount}:")
        details.extend(fs_details)
        perfdata.extend(fs_perf)
    line = f"BTRFS {STATE_NAMES[state]} - " + "; ".join(summary)
    if perfdata:
        line += " | " + " ".join(perfdata)
    return state, "\n".join([line] + details)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_btrfs_sysfs",
        description="Check btrfs unallocated space, metadata space and device errors "
                    "from sysfs, without root. Checks the local machine, or a remote host "
                    "over SSH with -H.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-m", "--mountpoint", action="append",
                        help="btrfs mount point, can be given multiple times (default: /)")
    parser.add_argument("-u", "--unallocated-warning", type=SizeThreshold,
                        default=SizeThreshold("2G"),
                        help="WARNING if less unallocated space, as size (2G, 500M) or "
                             "percentage of the device size (5%%) (default: 2G)")
    parser.add_argument("-U", "--unallocated-critical", type=SizeThreshold,
                        default=SizeThreshold("1G"),
                        help="CRITICAL if less unallocated space (default: 1G)")
    parser.add_argument("-w", "--metadata-warning", type=percent, default=75.0,
                        help="WARNING if metadata usage in percent is at least this "
                             "(default: 75)")
    parser.add_argument("-c", "--metadata-critical", type=percent, default=90.0,
                        help="CRITICAL if metadata usage in percent is at least this "
                             "(default: 90)")
    parser.add_argument("-d", "--data-warning", type=percent,
                        help="WARNING if data usage in percent is at least this "
                             "(default: none)")
    parser.add_argument("-D", "--data-critical", type=percent,
                        help="CRITICAL if data usage in percent is at least this "
                             "(default: none)")
    parser.add_argument("-e", "--errors", choices=("critical", "warning", "ignore"),
                        default="critical",
                        help="state for device error counters above zero (default: critical)")
    remote = parser.add_argument_group("remote host")
    remote.add_argument("-H", "--hostname", help="check this host over SSH instead of the "
                                                 "local machine")
    remote.add_argument("-l", "--user", help="SSH user (default: from ssh configuration)")
    remote.add_argument("-p", "--port", type=int, help="SSH port")
    remote.add_argument("-i", "--identity", help="SSH private key file")
    remote.add_argument("-o", "--ssh-option", action="append", metavar="OPTION",
                        help="extra ssh -o option, can be given multiple times")
    remote.add_argument("--ssh", default="ssh", help="ssh binary (default: ssh)")
    parser.add_argument("-t", "--timeout", type=int, default=10,
                        help="timeout in seconds (default: 10)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.mountpoint = args.mountpoint or ["/"]
    try:
        state, text = run(args)
    except CheckError as err:
        state, text = UNKNOWN, f"BTRFS UNKNOWN - {err}"
    except Exception as err:  # never exit with a traceback and code 1
        state, text = UNKNOWN, f"BTRFS UNKNOWN - internal error: {type(err).__name__}: {err}"
    print(text)
    return state


if __name__ == "__main__":
    sys.exit(main())
