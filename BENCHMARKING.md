# Benchmarking

`bench.py` fetches each capture, links it with every linker you give it,
and writes one report with 95% confidence intervals. Install Python 3,
[zstd](https://facebook.github.io/zstd/),
[GitHub CLI](https://cli.github.com/) (or use `--public`), and
[cbdr](https://github.com/asayers/cbdr):

```sh
cargo install cbdr
./bench.py --linker /path/to/ld64.lld --apple
```

Without `--workloads`, this runs all 15 workloads in `manifest.tsv`, smallest
first. Each missing capture is fetched and extracted under `corpora.noindex/`, linked,
and then deleted. Downloaded parts stay there as a cache. Extractions that
were already there are always preserved; `--keep` also preserves new ones.
Use `--corpora /path/on/a/roomy/disk` to put the inputs and cache elsewhere.

The first `--linker` is the baseline. Give it a name with
`--linker mine=/path/to/ld64.lld`, or let the script use the filename.
Repeat `--linker` to compare several. `--apple` adds the Xcode `ld` that
`xcrun` finds; set `DEVELOPER_DIR` to pick an Xcode. Apple's linker is
skipped, with a note in the report, on captures it cannot read.

```sh
./bench.py --linker upstream=/path/to/ld64.lld --apple \
  --corpora /path/to/corpora --out bench-comparison \
  --workloads rust-release,godot-ios --samples 3
```

Rerun the same command with the same `--out` to resume. Completed workloads
are included in the final report without fetching or linking again.
Count-based runs also reuse valid partial samples; time-budget runs retry
the interrupted workload. Changing linkers, binaries, workloads or
measurement settings requires a new output directory.

## Disk and time

All 15 captures download **23.35 GB**. Streaming extraction verifies every
part, the combined compressed stream and the raw tar's hash and byte count.
It never writes a combined compressed file or a raw tar. Only one new
extraction is needed at a time.

Measured capture sizes give these peak input-storage requirements (decimal
GB, excluding linked outputs and reports):

| Selection | Download | Peak with cache | Peak with `--no-cache` |
| --- | --- | --- | --- |
| All 15 | 23.35 GB | 75.36 GB | 60.99 GB |
| `--skip-large` (13) | 11.27 GB | 30.13 GB | 22.17 GB |

The preflight estimates the extra space needed on the corpus disk and
refuses if it will not fit; `--ignore-disk-space` overrides it. `--keep`
raises the estimate, because extractions accumulate.

The two largest links need more memory than their inputs suggest:
`chromium-macos-debug-component` needs 32 GB of RAM and `chromium-macos-sym2`
64 GB to run without swapping. On a machine with less, the script skips them
and says so, since their times would measure the disk rather than the linker.
`--include-large` runs them anyway; `--skip-large` always leaves them out.

A full run with two linkers took about 40 minutes on a 16 GB M4 Mac mini
(13 workloads), plus the time to download and extract.

For a separate fetch, use `./fetch.sh rust-release --extract`. It retains
verified parts; `--keep-tar` additionally saves the original capture tarball.
`--public` uses curl for public downloads. Advanced local testing can set
`LINKERBENCH_ASSETS_DIR` to a directory of release parts in either command.

## What it does

For each workload, every linker first links once to check that it works. A
linker that fails is reported and left out of that workload. Each linker
then gets a warmup link. After that, cbdr runs the linkers in random order
until each has at least `--samples` runs (default 20), or until the
`--time` budget runs out. Interleaving the linkers matters: slow drift in
the machine's state, such as thermals, background work or the page cache,
then affects every linker equally instead of whichever happened to run
last.

Timed links write to an automatically created APFS RAM disk on macOS, or
`/dev/shm` tmpfs on Linux. Creating output files on a disk filesystem can
occasionally stall; a RAM disk measures the linker rather than the disk.
Untimed correctness links measure the largest output for each workload, then
the runner reserves twice that size plus 256 MiB (at least 512 MiB, rounded
to 64 MiB). It recreates storage for each workload and deletes every linked
output immediately. The RAM disk is detached on exit, including errors and
Ctrl-C. The runner refuses allocations over 25% of physical RAM and refuses
insufficient tmpfs space. Use a larger machine or `--output-dir DIR` to choose
a disk directory explicitly. On macOS, an explicit external directory still
requires `--allow-external-output`. The report header records the storage
policy and each workload's actual output location.

## Reading the report

The report goes in `--out`, by default `bench-<date>/`, and contains:

- **`report.md`** starts with one table of every linker's wall time as a
  ratio to the baseline, one row per workload, which is usually all you
  need. A table per workload follows: median wall time with its bootstrap 95%
  confidence interval, median CPU time (user plus system), median peak memory and the
  ratio.
- **`summary-wall.md`** collects the per-workload wall medians and intervals
  in one table.
- **`report.html`** shows the same numbers as bar charts with error bars.
- **One directory per workload** holds the raw samples, the exact link
  commands and logs.

The error bars are confidence intervals for the *median*, rather than the
spread of individual runs. A ratio of `0.59 [0.55, 0.63]` estimates that the
linker takes 59% of the baseline's median time, with a 95% confidence interval
from 55% to 63%. An interval that includes 1.0 means the run did not show a
difference. Wall and ratio intervals use 10,000 bootstrap resamples with a
fixed seed (0); the ratio resamples each linker's measurements independently.
Tables show median ± CI half-width, followed by the actual bounds in
brackets, since bootstrap intervals can be asymmetric.

A noise note appears if a median's CI half-width exceeds 2% of the median,
or any run exceeds 1.5× the median. It gives the range and the number of
such outliers. Wide intervals call for more samples or a quieter machine.
Use `./bench.py --report-only DIR` to regenerate old reports from their raw
`samples.csv` files with these statistics; output defaults to `DIR/regen`.

The report header records the machine, each linker's version and hash, and
the exact command, so a report can be compared with another or rerun later.

## Getting stable numbers

- Quit other applications and wait for builds to finish. The script marks
  a report as contaminated if Ninja is running during the measurement.
- Compare linkers in the same run, not across runs. Ratios are more
  comparable between machines and days than absolute times are.
- Keep both linker binaries and corpora on an internal disk. Linker text
  pages evicted from the page cache can stall while being read back from a
  USB drive; the script warns about external linker binaries after resolving
  symlinks, and records the warning in the report. Reading inputs from an
  external drive also causes stalls; the script warns when `--corpora` is
  on an external volume on macOS.

## Other options

| Option | Meaning |
| --- | --- |
| `--corpora DIR` | input and download-cache directory (default `corpora.noindex/` beside the script; the `.noindex` suffix stops Spotlight from indexing fresh extractions, which slows the first links on macOS) |
| `--no-cache` | delete a workload's downloaded parts after successful extraction |
| `--keep` | keep extractions created by this run |
| `--skip-large` | always omit the two largest desktop Chromium captures |
| `--include-large` | run them even when the machine has less RAM than they need |
| `--public` | fetch public release assets with curl instead of GitHub CLI |
| `--ignore-disk-space` | override the free-space check |
| `--out DIR` | combined report directory; reuse it to resume |
| `--threads N` | pass `--threads=N` to the LLD linkers (Apple's is unaffected) |
| `--extra-args 'NAME=ARGS'` | extra flags for one linker, e.g. `'mine=--icf=none'` |
| `--warmups N` | warmup links per linker per workload (default 1) |
| `--time 10m` | sample each workload for a fixed time instead of a fixed count |
| `--output-dir DIR` | override automatic RAM storage for linked outputs |
| `--link-timeout S` | give up on a single link after `S` seconds (default 600) |
| `--report-only DIR` | rebuild from saved samples into `DIR/regen` (use `--out` to choose), without linking |
