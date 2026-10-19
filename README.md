# check_btrfs_sysfs

Monitoring plugin for [btrfs](https://btrfs.readthedocs.io) file systems,
compatible with Nagios, Naemon, Icinga and other tools that use the
[Monitoring Plugins](https://www.monitoring-plugins.org/doc/guidelines.html)
interface.

It reads `/sys/fs/btrfs/<UUID>/`, which every user can read, so it needs
**no root, no sudo and no btrfs ioctls**. It runs on the monitored host, or
on the monitoring server and reads a remote host's sysfs over SSH (`-H`), in
which case nothing has to be installed on the monitored host.

The plugin is a single Python 3 file without dependencies outside the
standard library.

## Why `df` is not enough on btrfs

btrfs assigns ("allocates") the device space in chunks to either data or
metadata. `df` and `check_disk` show the free space for **data** only. Writes
can fail with "No space left on device" while `df` still shows gigabytes free:

* when all device space is allocated (**unallocated** space is gone), and
* the metadata chunks are full, so metadata cannot grow any more.

This usually happens after data was written and deleted: the data chunks are
only partly used, but still allocated. A balance (`btrfs balance start
-dusage=50 /`) packs them together and returns the space. It only relocates
chunks that are at most as full as the `-dusage` value, so it does nothing
when all chunks are fuller than that.

## What is checked

| Check | Description | Default thresholds |
|---|---|---|
| Unallocated space | device size minus all allocated chunks | WARNING < 2G, CRITICAL < 1G |
| Metadata usage | used metadata plus the global reserve, in percent of the metadata chunks plus the unallocated space metadata could still grow into | WARNING ≥ 75%, CRITICAL ≥ 90% |
| Data usage | used data in percent of the data chunks plus the unallocated space, like `df` | none (perfdata only) |
| Device errors | the write, read, flush, corruption and generation error counters of each device, and missing devices | CRITICAL if above zero |

Metadata usage is computed against the space metadata could still grow to, so
it only gets high when the file system is (almost) fully allocated. A
balance hint with the reclaimable size is shown when unallocated space is
low and at least 1 GiB of the data chunks is unused. sysfs only tells how
full the data chunks are on average, not each chunk. If that average is
above 50%, the hint suggests the average rounded up to 5 as the `-dusage`
value: at least one chunk is not fuller than the average, so the balance
relocates at least that one, if the other chunks have room for its data.

Data usage has no thresholds by default, because `check_disk` usually covers
it already. Set `-d`/`-D` to check it here as well.

Performance data per mount point: `unallocated`, `metadata_pct`,
`metadata_used`, `data_pct`, `data_used` and `errors`.

## Requirements

* Linux 5.14 or newer on the monitored host (for the device error counters in
  sysfs), with `findmnt` (util-linux) and a POSIX shell
* Python 3.7 or newer where the plugin runs
* for remote checks: the OpenSSH client and key login to the monitored host
  as any user

## Installation

```sh
install -m 755 check_btrfs_sysfs.py /usr/lib/nagios/plugins/check_btrfs_sysfs
```

or `pip install .`, which creates a `check_btrfs_sysfs` command.

## Bash completion

`completions/check_btrfs_sysfs` completes the options, the local btrfs mount
points for `-m` (any directory with `-H`), the states for `-e`, host names,
users and the key file. The packages install it; otherwise copy it to
bash-completion's directory:

```sh
install -D -m 0644 completions/check_btrfs_sysfs /usr/share/bash-completion/completions/check_btrfs_sysfs
```

## Usage

```
check_btrfs_sysfs [-m MOUNTPOINT ...] [-u SIZE] [-U SIZE] [-w PCT] [-c PCT]
                  [-d PCT] [-D PCT] [-e {critical,warning,ignore}]
                  [-H HOST [-l USER] [-p PORT] [-i KEYFILE] [-o OPTION]]
                  [-t TIMEOUT]
```

* `-m`: btrfs mount point, can be given multiple times (default `/`)
* `-u`/`-U`: minimum unallocated space for WARNING/CRITICAL, as size (`2G`,
  `500M`, plain numbers are GiB) or percentage of the device size (`5%`)
* `-w`/`-c`: metadata usage in percent for WARNING/CRITICAL
* `-d`/`-D`: data usage in percent for WARNING/CRITICAL
* `-e`: state for device error counters above zero
* `-H`: check this host over SSH; `-l`, `-p`, `-i` and `-o` are passed to
  `ssh`, which is called with `BatchMode=yes`, so settings from
  `~/.ssh/config` apply

Examples:

```sh
check_btrfs_sysfs
check_btrfs_sysfs -m / -m /srv -u 10G -U 5G
check_btrfs_sysfs -H server.example.org -l monitor -m /
```

Example output of a fully allocated file system:

```
BTRFS CRITICAL - /: unallocated 12.0 MiB (< 1.0 GiB), metadata 82% used (101.5 MiB left) | ...
[CRITICAL] unallocated 12.0 MiB of 30.0 GiB (1 device)
[WARNING] metadata 82% used: 462.5 MiB of 564.0 MiB incl. 73.2 MiB global reserve (dup)
[OK] data 91% used: 26.2 GiB of 28.8 GiB (single)
  about 2.7 GiB of the data chunks are unused, but they are 91% full on average, so -dusage=50 may relocate nothing
  'btrfs balance start -dusage=95 /' relocates at least one chunk; if it fails with 'No space left on device', delete data first
```

Device error counters stay until they are reset with
`btrfs device stats -z MOUNTPOINT` (as root).

## Naemon / Nagios configuration example

```
define command {
  command_name  check_btrfs_sysfs
  command_line  $USER1$/check_btrfs_sysfs -H $HOSTADDRESS$ -m $ARG1$ $ARG2$
}

define service {
  use                  generic-service
  host_name            server.example.org
  service_description  Btrfs /
  check_command        check_btrfs_sysfs!/
  check_interval       30
}
```

## Limitations

* The device size is taken from the block device. If the btrfs device was
  resized to less than the block device, unallocated space is overestimated.
* With multiple devices of different sizes in a RAID profile, part of the
  unallocated space may not be usable. Finding that out needs the chunk
  tree, which only root can read (see `check_btrfs` from
  [python-btrfs](https://github.com/knorrie/python-btrfs) for that case).
  For RAID5/6 the usable space is an estimate.

## Development

```sh
python3 -m unittest discover -s tests -v
```

## License

Copyright (C) 2026 Thomas Wagner

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation; either version 2 of the License, or (at your option) any
later version. See [LICENSE](LICENSE) for the full text.
