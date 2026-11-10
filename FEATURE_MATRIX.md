# Linker feature matrix

Which optional linker features each workload's link uses. Everything here comes
from the linker command line recorded in each capture (`response.txt`), plus
the build configuration described in [CAPTURES.md](CAPTURES.md). For the kind
of work each workload stresses, see "What the workloads cover" in the
[README](README.md).

| Workload | Build | Output | Dead stripping | ICF | Order file | Exports | Fixups | ObjC selector stubs | LTO | Other |
| --- | --- | --- | :-: | :-: | :-: | --- | --- | :-: | --- | --- |
| `rust-release` | Cargo `--release` | executable | ✓ | | | | bind opcodes | | none | |
| `rust-release-thinlto` | Cargo `--release` | executable | ✓ | | | | bind opcodes | | ThinLTO | |
| `rust-debug` | Cargo debug | executable | ✓ | | | | bind opcodes | | none | |
| `sourcekit-lsp` | SwiftPM release | executable | ✓ | | | | chained | ✓ | none | |
| `godot-ios` | release export | executable | ✓ | | | | chained | ✓ | none | |
| `godot-ios-thinlto` | release export | executable | ✓ | | | | chained | ✓ | ThinLTO | |
| `firefox-ios` | Xcode `Fennec` configuration | executable | ✓ | `none` | | `-export_dynamic` | chained | ✓ | none | `-ObjC` |
| `llvm-libllvm` | CMake `RelWithDebInfo` | dylib | ✓ | | | | chained | | none | `-all_load` |
| `llvm-libllvm-thinlto` | CMake `RelWithDebInfo` | dylib | ✓ | | | | chained | | ThinLTO | `-all_load` |
| `llvm-clang` | CMake `RelWithDebInfo` | executable | | | | `-export_dynamic` | chained | | none | |
| `llvm-clang-thinlto` | CMake `RelWithDebInfo` | executable | | | | `-export_dynamic` | chained | | ThinLTO | |
| `telegram` | Xcode Release | executable | | | | | bind opcodes, `-O 3` | ✓ | none | `-ObjC` |
| `firefox-macos` | `--enable-optimize` developer build | dylib | ✓ | | | exported-symbols list | bind opcodes | ✓ | none | |
| `chromium-macos-sym0`/`sym1`/`sym2` | GN `is_debug=false` | dylib | ✓ | `all` | ✓ | exported-symbols list | chained | ✓ | none | `-ObjC` |
| `chromium-macos-debug-component` | GN `is_debug=true`, component | dylib | | | ✓ | | chained | ✓ | none | `-ObjC` |
| `chromium-ios-sym0`/`sym1` | GN `is_debug=false` | executable | ✓ | `all` | | | chained (`-fixup_chains`) | ✓ | none | `-ObjC` |

Notes on the columns:

- **Dead stripping** is `-dead_strip`. Without it, the linker doesn't compute
  liveness at all.
- **ICF** is the `--icf=` mode. An empty cell means the default, which is no
  folding.
- **Exports**: `-export_dynamic` makes every external symbol a dead-stripping
  root. An exported-symbols list restricts the exports to the listed symbols.
- **Fixups**: chained fixups are the default for macOS 13 and later and for
  iOS 13.4 and later, and are used unless disabled. Older deployment targets
  get classic dyld bind opcodes. `-O` above 1 only affects bind opcodes, so
  `telegram` is the only link where it changes the output. `firefox-macos`
  passes `-O 3` and then `-O 0`.
- **ObjC selector stubs** means the inputs reference `_objc_msgSend$<selector>`
  stubs, which the linker synthesizes. No workload uses Objective-C class
  selector stubs (`_objc_msgSendClass$…`).

Gaps:

- **LTO.** Four workloads contain ThinLTO bitcode; full LTO is not covered.
  The other workloads have native inputs. `-object_path_lto` alone does not
  establish that a link uses LTO. ThinLTO timings use each linker's own warm
  cache (see [BENCHMARKING.md](BENCHMARKING.md)).
- **Shipping configurations.** None of the workloads is a project's official
  release configuration, such as Chromium's `is_official_build` (ThinLTO and
  PGO) or Firefox's official builds.
- **ICF and order files on non-Chromium links.** Only Chromium uses them.
- **Debug builds.** `rust-debug` and `chromium-macos-debug-component` are the
  only ones.
