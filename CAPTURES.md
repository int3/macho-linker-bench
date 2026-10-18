# Capture details

This file describes each capture: where it came from, how it was built, and
what its link looks like. For what the benchmark is and how to run it, start
with the [README](README.md).

- [Build environment](#build-environment)
- [Replaying](#replaying)
- [Download format](#download-format)
- Workloads: [rust-release](#rust-release), [rust-debug](#rust-debug),
  [sourcekit-lsp](#sourcekit-lsp), [godot-ios](#godot-ios),
  [firefox-ios](#firefox-ios), [llvm-libllvm](#llvm-libllvm),
  [llvm-clang](#llvm-clang), [telegram](#telegram),
  [firefox-macos](#firefox-macos), [chromium-macos-sym0/1/2](#chromium-macos),
  [chromium-macos-debug-component](#chromium-macos-debug-component),
  [chromium-ios-sym0/1](#chromium-ios)

## Build environment

All fifteen were built on one Apple M4 Mac mini (10 cores, 16 GB) running
macOS 26.3 (25D125), with Xcode 26.6 (17F113) and the MacOSX26.5 and
iPhoneOS26.5 SDKs. Each recipe checked out pinned source and ran the
project's own build. A small wrapper sat in front of the final link of the
target being captured and added `--reproduce=NAME.repro.tar` to its
`ld64.lld` command. Every other link and build tool ran normally.
Nothing was rewritten afterwards: every input is byte-for-byte what the
build produced, and build paths are preserved as captured.

How LLD was selected depended on the build system: Cargo through an Xcode
Clang driver with `-fuse-ld=lld`, SwiftPM with `-use-ld=lld`, and CMake,
Xcode and GN through the wrapper. Each capture's `version.txt` records the
LLD revision that wrote it: one of three upstream LLVM revisions, listed
per workload below.

Other toolchain versions: Rust 1.98.0 (Homebrew) for rust-analyzer; Swift
6.3.3 (Xcode) for SourceKit-LSP; Rust 1.94.0, Node 22.22.0, npm 10.9.4 and
node-gyp 11.2.0 for Firefox iOS's Application Services; Metal Toolchain
17F109 for Chromium.

The builds are documented, not hermetic. Some host tools and bootstrap
downloads were not pinned, and Firefox desktop's exact Rust toolchain was not
recorded. That doesn't affect the captures themselves, which are identified
by their hashes.

## Replaying

Extract a capture and run the linker from inside its directory:

```sh
cd NAME.repro
ld64.lld @response.txt -o /some/output/path
```

Every path in `response.txt` is relative to that directory, so it doesn't
matter where the capture is extracted or whose machine it is on.

**Which LLD.** Upstream LLD at
[`3d7d2e95`](https://github.com/llvm/llvm-project/commit/3d7d2e95a1af68dd41ec042ea6bf9b14de65afca)
links all fifteen. Older releases don't: LLD 18 handles the Rust and Godot
captures, the LLVM captures need LLD 20 for `-no_warn_duplicate_libraries`,
and Telegram and Chromium use options newer than LLD 20. To build that
revision:

```sh
git clone https://github.com/llvm/llvm-project.git
git -C llvm-project checkout 3d7d2e95a1af68dd41ec042ea6bf9b14de65afca
cmake -S llvm-project/llvm -B llvm-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_PROJECTS=lld \
  '-DLLVM_TARGETS_TO_BUILD=AArch64;X86'
ninja -C llvm-build lld
```

**Chromium iOS.** Its command line passes `-fatal_warnings` and names two
empty SDK directories that `--reproduce` did not record (it stores files,
not empty directories). `python3 prepare.py <capture dir>` recreates them,
leaving the response and inputs untouched.

**Apple's `ld`.** `python3 prepare.py <capture dir> --apple` writes an
`apple-response.txt` next to the original. It adds `-ld_new` and drops the
flags Apple's linker rejects: LLD's `--icf`, `-O`, the `-mllvm` outlining
option, `--read-workers`, `--slop_scale` and diagnostic options. Run it with Xcode
26.6 selected:

```sh
xcrun ld @apple-response.txt -o /some/output/path
```

This works for the nine captures that aren't Chromium. Chromium's GN build writes
thin archives, which Apple's `ld` rejects. Apple's output is not expected to
match LLD's byte for byte.

**When comparing outputs**, keep both the capture directory and the output
basename the same between runs. Debug-map paths and the UUID depend on them.
Some captures warn about search paths that don't exist or (for the Rust
runtime objects) a newer object deployment target. Those warnings are
expected. Responses that write a `-map`, `-dependency_info` or
`-object_path_lto` file write it relative to the capture directory, so keep
that directory writable.

## Download format

The captures are assets on the [`v1`
release](https://github.com/int3/macho-linker-bench/releases/tag/v1). Each
`.repro.tar` was compressed with `zstd -6`, without `--long`, so plain
`zstd -d` decompresses it. Files over 1.9 GiB are split into numbered
`.part-aa`, `.part-ab`, ... pieces to stay under GitHub's 2 GiB asset limit.
In total that's 22 assets, 23.4 GB, from 129 GB of captures.

[`manifest.tsv`](manifest.tsv) lists every part in concatenation order, with
sizes and SHA-256s for the part, the joined `.zst` and the original tar.
**The tar's SHA-256 identifies the capture.** `fetch.sh` checks all three
levels, keeps verified parts across interrupted downloads, and never
overwrites an existing extraction. Once the repository is public,
`fetch.sh --public` downloads with `curl` instead of `gh`.

## Workloads

In the tables below:

- **Input** counts the files the link reads. Archive members are counted
  separately from standalone objects. In a thin archive the members are
  themselves standalone files, so for Chromium the two counts overlap.
- **Debug map** is the number of debug-map (STABS) entries in LLD's output:
  one per symbol the debugger can find via the object files. The DWARF
  itself stays in the objects.

### rust-release

[rust-analyzer](https://github.com/rust-lang/rust-analyzer), the Rust
language server, built with `cargo build --locked --release`. A small number
of objects plus 207 rlibs: mostly a test of archive member selection, with
`-dead_strip`.

| | |
| --- | --- |
| Source | rust-analyzer `9074e9b4c6bc31d7986ccf4e22d18af70e5508da`, its `Cargo.lock` |
| Output | `rust-analyzer` executable, macOS 11.0+ |
| Input | 18 objects; 207 archives (1,485 members); 58 `.tbd`; 0.33 GB |
| Debug info | 414 inputs carry DWARF (from runtime libraries); debug map empty |
| Flags | `-dead_strip` |
| Captured with | LLD `b615ebdd35cc` |
| Download | 88 MB |

### rust-debug

The same source and toolchain as rust-release, built with `cargo build
--locked` (debug). It has more codegen units, and nearly every input carries
DWARF.

| | |
| --- | --- |
| Output | `rust-analyzer` executable, macOS 11.0+ |
| Input | 134 objects; 207 archives (6,362 members); 58 `.tbd`; 0.90 GB |
| Debug info | 6,104 inputs carry DWARF; 681,934 debug-map entries |
| Flags | `-dead_strip` |
| Captured with | LLD `b615ebdd35cc` |
| Download | 144 MB |

### sourcekit-lsp

[SourceKit-LSP](https://github.com/swiftlang/sourcekit-lsp), the Swift
language server, built by SwiftPM in release mode with
`--force-resolved-versions`. Nearly 3,000 directly linked Swift objects,
with Swift and Objective-C metadata.

| | |
| --- | --- |
| Source | SourceKit-LSP `a463483aba304581bad19a0d96d80091af3c8b52`; `Package.resolved` SHA-256 `b8ab43afae041b954b12829c66fd1882bddcf2d3ce15c02d177308afe76b1997` |
| Output | `sourcekit-lsp` executable, macOS 15.0+ |
| Input | 2,808 objects; 4 archives (310 members); 142 `.tbd`; 0.42 GB |
| Debug info | 2,807 inputs carry DWARF; 632,250 debug-map entries |
| Flags | `-dead_strip` |
| Captured with | LLD `b615ebdd35cc` |
| Download | 89 MB |

The response's `-platform_version` gives SDK 15.0 although the build used
the 26.5 SDK. That is what SwiftPM passed.

### godot-ios

The Platformer demo exported as an iOS app from the
[Godot](https://github.com/godotengine/godot) engine, release build. SCons
built the iOS export templates and a macOS editor, the editor exported the
project, and Xcode linked the app. Almost all the code is the C++ engine in
one large archive, behind a small Swift and Objective-C wrapper.

| | |
| --- | --- |
| Source | Godot 4.7.2-stable `ed1daf0bf001b61586d9930840f2f1394092c079`; godot-demo-projects `6ad6167e0577fe3622c18546138f456b107ce93c`; SCons 4.8.1; MoltenVK 1.4.2 for iOS |
| Output | `platformer` executable, iOS 15.0+ |
| Input | 3 objects; 8 archives (2,547 members); 69 `.tbd`; 0.22 GB |
| Debug info | 188 debug-map entries |
| Flags | `-dead_strip`, `-dependency_info`, `-object_path_lto` |
| Captured with | LLD `b615ebdd35cc` |
| Download | 41 MB |

### firefox-ios

[Firefox for iOS](https://github.com/mozilla-mobile/firefox-ios), the Fennec
scheme, release, with Mozilla's Rust components built locally from
Application Services. `ENABLE_DEBUG_DYLIB=NO` makes the `Client` app a
single real link rather than a launcher around a debug dylib. Swift UI code,
Objective-C, Rust and a dozen embedded frameworks meet here.

| | |
| --- | --- |
| Source | firefox-ios `e97ed6cf7e808b17201ed8f02ea09192e5ab7c86`; application-services `c0877e1f5293602d80379cff9be860c0ad236e61`; NSS 3.125 / NSPR 4.39 |
| Output | `Client` executable, iOS 15.0+ |
| Input | 1,145 objects; 9 archives (1,976 members); 12 dylibs; 142 `.tbd`; 0.54 GB |
| Debug info | 1,928 inputs carry DWARF; 303,706 debug-map entries |
| Flags | `-ObjC`, `-dead_strip`, `--icf=none`, `-dependency_info`, `-object_path_lto` |
| Captured with | LLD `9eb6467f3f03` |
| Download | 127 MB |

### llvm-libllvm

`libLLVM.dylib` from a RelWithDebInfo build of
[LLVM](https://github.com/llvm/llvm-project) with Clang, all targets, built
with `LLVM_BUILD_LLVM_DYLIB=ON`. The dylib link pulls every member of about
200 C++ component archives (`-all_load`), so there is no member selection:
everything is loaded.

| | |
| --- | --- |
| Source | llvm-project `b615ebdd35cc1c13d722c81ecf52af990ad29c6a` |
| Output | `libLLVM.24.0git.dylib`, macOS 13.0+ |
| Input | 1 object; 199 archives (3,245 members); 1 dylib; 45 `.tbd`; 13.3 GB |
| Debug info | 2,961 inputs carry DWARF; 398,432 debug-map entries |
| Flags | `-dylib`, `-all_load`, `-dead_strip` |
| Captured with | LLD `c1b661fde124` |
| Download | 2.3 GB (2 parts) |

### llvm-clang

The `clang` executable from the same build. With
`LLVM_LINK_LLVM_DYLIB=OFF`, clang links the LLVM and Clang component
archives statically, so this is the archive-selection counterpart to
libLLVM: 18.9 GB of archives, from which the linker pulls what clang needs.

| | |
| --- | --- |
| Source | same as llvm-libllvm |
| Output | `clang-24` executable, macOS 13.0+ |
| Input | 5 objects; 197 archives (3,838 members); 1 dylib; 43 `.tbd`; 18.9 GB |
| Debug info | 3,558 inputs carry DWARF; 581,730 debug-map entries |
| Flags | none of note |
| Captured with | LLD `c1b661fde124` |
| Download | 3.3 GB (2 parts) |

### telegram

[Telegram Desktop](https://github.com/telegramdesktop/tdesktop) for macOS, a
Release build of the upstream tree with its test API configuration
(`TDESKTOP_API_TEST=ON`). It is C++ and Qt with bundled media libraries and
some Swift and Objective-C. The 830 MB executable is far beyond arm64's
±128 MB branch range, so the linker has to place branch thunks. This is the
thunk stress test.

| | |
| --- | --- |
| Source | tdesktop v7.2.6 `80158983dba09d3bf5d96701f21473d6c34bf5f5`, submodules at their pinned commits |
| Output | `Telegram` executable, macOS 11.0+ |
| Input | 1,513 objects; 74 archives (6,198 members); 152 `.tbd`; 5.0 GB |
| Debug info | 1,889 inputs carry DWARF; 183,260 debug-map entries |
| Flags | `-ObjC`, `--slop_scale=1024`, `-dependency_info` |
| Captured with | LLD `c1b661fde124` |
| Download | 954 MB |

Two flags were added to the generated link before capture. `-L
$SDK/usr/lib/swift` resolves the Swift autolinks. `--slop_scale=1024` gives
LLD's thunk placement enough headroom: without it, upstream LLD fails this
link with an out-of-range branch.

### firefox-macos

[Firefox](https://github.com/mozilla-firefox/firefox) desktop's `XUL`, the
library that holds almost all of the browser, from an optimized
(`--enable-optimize`) arm64 build. It is C++ with a large Rust component
(built with the tree's default Rust LTO settings) and Objective-C, and has an
exported-symbol list.

| | |
| --- | --- |
| Source | firefox `f54181e69218689d62e9fd89e9d49c253ac43469` |
| Output | `XUL` dylib, macOS 11.0+ |
| Input | 2,331 objects; 8 archives (8,220 members); 5 dylibs; 127 `.tbd`; 5.7 GB |
| Debug info | 10,260 inputs carry DWARF; 1,339,857 debug-map entries |
| Flags | `-dylib`, `-dead_strip`, `-exported_symbols_list` |
| Captured with | LLD `b615ebdd35cc` |
| Download | 1.5 GB |

### chromium-macos

The `Chromium Framework` library, which holds nearly all of the desktop
browser, at three debug-info levels from one source checkout. The GN
arguments are `target_cpu="arm64"`, `is_debug=false`,
`is_component_build=false` and `use_lld=true`, with `symbol_level` 0, 1 or 2.
This is the release-style monolithic link, not the component build used in
day-to-day Chromium development.

Chromium's link is the largest in the set by object count. It has 44,620
objects, many of them reached through 2,109 thin archives. It also runs
`--icf=all`, Chromium's order file (`framework.order`) and an export list.
`symbol_level=1` adds line tables and `symbol_level=2` full DWARF, which
grows the input from 2.8 GB to 52 GB while the output stays about the same
size.

| | sym0 | sym1 | sym2 |
| --- | ---: | ---: | ---: |
| Input | 2.8 GB | 7.6 GB | 51.9 GB |
| Inputs carrying DWARF | 409 | 45,947 | 45,955 |
| Debug-map entries | 0 | 2,045,716 | 2,047,608 |
| Download | 537 MB | 1.6 GB | 9.0 GB (5 parts) |

| | |
| --- | --- |
| Source | Chromium `285a6752d67d53c813bc909ed13a2ae0e930c0ed` (154.0.8023.0); depot_tools `ed9c87f6f12f6b87210e7025d4a36a5a72a2ccd4` |
| Output | `Chromium Framework` dylib, macOS 13.0+ |
| Input | 44,620 objects; 2,314 archives, 2,109 of them thin (25,849 members); 172 `.tbd` |
| Flags | `-dylib`, `-dead_strip`, `-ObjC`, `--icf=all`, `-order_file`, `-exported_symbols_list`, `-fatal_warnings`, `--read-workers=2` |
| Captured with | LLD `c1b661fde124` |

The sym2 link reads more input than the mini's 16 GB of memory can cache, so
its time depends heavily on what else is in memory.

### chromium-macos-debug-component

The link Chromium developers wait on most: `libchrome_dll.dylib` from a
debug component build. Here `is_component_build=true` splits the browser
into hundreds of shared libraries so an edit relinks one library instead of
the whole browser, and `libchrome_dll.dylib` is the biggest of them. The GN
arguments are `target_cpu="arm64"`, `is_debug=true`,
`is_component_build=true` and `use_lld=true`, with the debug default
`symbol_level=2`. It uses the same source checkout as the release captures.

Compared with the release framework, this link has fewer objects but full
debug info everywhere, links against 459 other Chromium dylibs, and has no
ICF or export list. Its debug map is the largest in the set.

| | |
| --- | --- |
| Source | same Chromium and depot_tools as chromium-macos |
| Output | `libchrome_dll.dylib`, macOS 13.0+ |
| Input | 16,939 objects; 853 archives, 772 of them thin (6,638 members); 459 dylibs; 139 `.tbd`; 19.3 GB |
| Debug info | every object carries DWARF; 6,922,450 debug-map entries |
| Flags | `-dylib`, `-ObjC`, `-order_file`, `-fatal_warnings`, `--read-workers=2` |
| Captured with | LLD `c1b661fde124` |
| Download | 3.1 GB (2 parts) |

Like sym2, this link needs more memory than a 16 GB machine has, so its
times there are noisy.

### chromium-ios

Chromium for iOS: the `Chromium` executable of `//ios/chrome/app:chrome`, a
device build, release, at `symbol_level` 0 and 1. The GN arguments are
`target_os="ios"`, `target_environment="device"`, `target_cpu="arm64"`,
`is_debug=false`, `is_component_build=false`, `use_lld=true` and
`ios_enable_code_signing=false`. Like the desktop build it uses thin
archives and `--icf=all`. It also needs the two empty SDK directories
recreated before linking (see [Replaying](#replaying)).

| | sym0 | sym1 |
| --- | ---: | ---: |
| Input | 0.68 GB | 1.56 GB |
| Inputs carrying DWARF | 250 | 16,844 |
| Debug-map entries | 21 | 522,081 |
| Download | 151 MB | 366 MB |

| | |
| --- | --- |
| Source | same Chromium and depot_tools as desktop |
| Output | `Chromium` executable, iOS 18.0+ |
| Input | 16,128 objects; 905 archives, 779 of them thin (9,160 members); 2 dylibs; 109 `.tbd` |
| Flags | `-ObjC`, `-dead_strip`, `--icf=all`, `-fatal_warnings` |
| Captured with | LLD `c1b661fde124` |
