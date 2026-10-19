# Mach-O LinkerBench

Linking is the step you wait for after every edit. Change one line in
Chromium and the compiler finishes in seconds; then the linker reads
45,000 object files and writes a 600 MB library, and you wait again. Every
build of every app pays that cost, which makes linker speed one of the few
things that shortens everyone's edit-build-run loop at once.

Mach-O LinkerBench measures it on the links people actually wait for. It is
fifteen final links of real applications — Chromium, Firefox, Telegram,
clang, rust-analyzer and more — each captured from its native build on a Mac
with every input file and the exact command line. Point it at a linker and
it replays all of them and reports how long each one took, with error bars.

## Results

Upstream LLD ([`3d7d2e95`](https://github.com/llvm/llvm-project/commit/3d7d2e95a1af68dd41ec042ea6bf9b14de65afca))
and Apple's `ld` from Xcode 26.6, on a 10-core M4 Mac mini (16 GB, macOS
26.3) and a Ryzen 7 7700 (8 cores, 124 GB, Linux). Each time is the median of
at least 20 interleaved runs; every 95% confidence interval is within
±1.5%. Lower is better.

| Workload | LLD, M4 | Apple `ld`, M4 | LLD ÷ Apple | LLD, Ryzen |
| --- | ---: | ---: | ---: | ---: |
| `rust-release` | 159 ms | 110 ms | 1.45× | 297 ms |
| `rust-debug` | 640 ms | 384 ms | 1.67× | 1.26 s |
| `sourcekit-lsp` | 598 ms | 318 ms | 1.88× | 1.03 s |
| `godot-ios` | 375 ms | 346 ms | 1.08× | 670 ms |
| `firefox-ios` | 548 ms | 389 ms | 1.41× | 881 ms |
| `llvm-libllvm` | 367 ms | 191 ms | 1.92× | 650 ms |
| `llvm-clang` | 453 ms | 280 ms | 1.62× | 819 ms |
| `telegram` | 1.60 s | 912 ms | 1.75× | 3.40 s |
| `firefox-macos` | 1.07 s | 649 ms | 1.65× | 2.00 s |
| `chromium-macos-sym0` | 5.10 s | — | — | 9.81 s |
| `chromium-macos-sym1` | 5.88 s | — | — | 10.63 s |
| `chromium-macos-sym2` | needs 64 GB | — | — | 11.19 s |
| `chromium-macos-debug-component` | needs 32 GB | — | — | 14.90 s |
| `chromium-ios-sym0` | 1.16 s | — | — | 1.55 s |
| `chromium-ios-sym1` | 1.37 s | — | — | 1.82 s |

On the nine links both linkers can do, LLD takes 1.1 to 1.9 times as long
as Apple's `ld`. Apple's `ld` cannot read the thin archives that Chromium's
build writes, so the Chromium links are LLD-only. The two largest links need
more memory than the Mac mini has.

## Run it

You need macOS or Linux, Python 3, `zstd`, and
[cbdr](https://github.com/asayers/cbdr) (`cargo install cbdr`).

```sh
git clone https://github.com/int3/macho-linker-bench
cd macho-linker-bench
./bench.py --linker /path/to/ld64.lld --apple
```

That fetches each workload, links it with your linker and with Apple's,
deletes it, and moves on to the next. At the end you get `report.md` and
`report.html`:

```
Skipping chromium-macos-sym2: it needs 64 GB of RAM to run without swapping (this machine has 16 GB); --include-large runs it anyway.
Skipping chromium-macos-debug-component: it needs 32 GB of RAM to run without swapping (this machine has 16 GB); --include-large runs it anyway.
godot-ios: fetching
godot-ios: extracting
godot-ios: verified (0.2 GB)
godot-ios: linking (correctness, warmup, sampling)
godot-ios: done; ld64.lld 375.4 ± 1.3 ms, apple 346.0 ± 2.3 ms (0.92×); extraction deleted
rust-release: fetching
...
llvm-clang: done; ld64.lld 453.1 ± 1.1 ms, apple 280.2 ± 1.1 ms (0.62×); extraction deleted
Report: bench-20261018T041530Z/report.md
```

`report.md` opens with each linker's median time and its 95% confidence
interval, and the ratio to the first linker, followed by CPU time and peak
memory for each workload:

| Workload | ld64.lld | apple |
| --- | --- | --- |
| godot-ios | 375.4 ± 1.3 ms | 346.0 ± 2.3 ms (0.92×) |
| rust-release | 158.8 ± 0.4 ms | 109.8 ± 0.9 ms (0.69×) |
| sourcekit-lsp | 598.3 ± 1.5 ms | 318.3 ± 0.3 ms (0.53×) |
| firefox-ios | 548.2 ± 1.2 ms | 388.6 ± 1.2 ms (0.71×) |
| chromium-ios-sym0 | 1.161 ± 0.002 s | Skipped: Apple ld cannot read thin archives |
| ... | | |

The full run downloads 23 GB and needs about 75 GB of free disk at its
peak, because it keeps the downloads so that a second run doesn't fetch
them again; with `--no-cache` the peak is about 61 GB. It takes about 40
minutes on the Mac mini above. Links that need more memory than the machine
has are skipped, since their times would measure swapping rather than the
linker. Linked outputs go to a RAM disk, and the corpora and
linker binaries belong on an internal disk; the script warns if they are
not.

To compare two builds of your own linker on a few workloads:

```sh
./bench.py --linker before=/path/to/old/ld64.lld \
           --linker after=/path/to/new/ld64.lld \
           --workloads rust-release,telegram
```

[BENCHMARKING.md](BENCHMARKING.md) explains the method and the report, and
the options.

## What the workloads cover

Each workload is there because it makes a linker do a different kind of
work, and a linker that is fast on one can be slow on another.

| Linker work | Where it dominates |
| --- | --- |
| Reading and resolving tens of thousands of inputs | `chromium-macos-*` (44,620 objects, 2,314 archives), `chromium-ios-*` |
| Choosing archive members | `llvm-clang` (197 archives, 3,838 members) |
| Loading whole archives into a dylib | `llvm-libllvm` (`-all_load` over 199 archives) |
| Linking against many dylibs | `chromium-macos-debug-component` (459 Chromium dylibs) |
| Debug maps | `chromium-macos-sym1`/`sym2` (2 million entries), `chromium-macos-debug-component` (6.9 million), `firefox-macos`, `rust-debug` |
| Reading very large inputs | `chromium-macos-sym2` (52 GB), `llvm-clang` (19 GB) |
| Branch thunks | `telegram` (an 830 MB executable, beyond arm64's ±128 MB branch range) |
| Identical code folding and order files | `chromium-*` (`--icf=all`, Chromium's order file) |
| Swift and Objective-C metadata | `sourcekit-lsp`, `firefox-ios`, `godot-ios`, `telegram` |
| Export lists and dead stripping | `firefox-macos`, `chromium-macos-*`; dead stripping in most |
| Mixed-language links | `firefox-ios` (Swift, Objective-C, Rust), `firefox-macos` (C++, Rust) |

[CAPTURES.md](CAPTURES.md) describes each capture: the source revision,
build configuration, input counts and flags, and how the captures were
made.

## License

The scripts in this repository are under the [MIT license](LICENSE).
