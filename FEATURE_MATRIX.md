# Linker feature matrix

Which optional linker features each workload's link uses. Everything here comes
from the linker command line recorded in each capture (`response.txt`), plus
the build configuration described in [CAPTURES.md](CAPTURES.md). For the kind
of work each workload stresses, see "What the workloads cover" in the
[README](README.md).

| Workload | Build | Output | Dead stripping | ICF | Order file | LTO | Exports | Fixups | ObjC selector stubs | Other |
| --- | --- | --- | :-: | :-: | :-: | :-: | --- | --- | :-: | --- |
| `rust-release` | Cargo `--release` | executable | ✓ | | | | | bind opcodes | | |
| `rust-debug` | Cargo debug | executable | ✓ | | | | | bind opcodes | | |
| `sourcekit-lsp` | SwiftPM release | executable | ✓ | | | | | chained | ✓ | |
| `godot-ios` | release export | executable | ✓ | | | ✓ | | chained | ✓ | |
| `firefox-ios` | Xcode `Fennec` configuration | executable | ✓ | `none` | | ✓ | `-export_dynamic` | chained | ✓ | `-ObjC` |
| `llvm-libllvm` | CMake `RelWithDebInfo` | dylib | ✓ | | | | | chained | | `-all_load` |
| `llvm-clang` | CMake `RelWithDebInfo` | executable | | | | | `-export_dynamic` | chained | | |
| `telegram` | Xcode Release | executable | | | | | | bind opcodes, `-O 3` | ✓ | `-ObjC` |
| `firefox-macos` | `--enable-optimize` developer build | dylib | ✓ | | | | exported-symbols list | bind opcodes | ✓ | |
| `chromium-macos-sym0`/`sym1`/`sym2` | GN `is_debug=false` | dylib | ✓ | `all` | ✓ | | exported-symbols list | chained | ✓ | `-ObjC` |
| `chromium-macos-debug-component` | GN `is_debug=true`, component | dylib | | | ✓ | | | chained | ✓ | `-ObjC` |
| `chromium-ios-sym0`/`sym1` | GN `is_debug=false` | executable | ✓ | `all` | | | | chained (`-fixup_chains`) | ✓ | `-ObjC` |

Notes on the columns:

- **Dead stripping** is `-dead_strip`. Without it, the linker doesn't compute
  liveness at all.
- **ICF** is the `--icf=` mode. An empty cell means the default, which is no
  folding.
- **LTO** means the link includes bitcode and passes LTO options
  (`-object_path_lto`).
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

- **Shipping configurations.** None of the workloads is a project's official
  release configuration, such as Chromium's `is_official_build` (ThinLTO and
  PGO) or Firefox's official builds. So LTO appears only in two small links,
  `godot-ios` and `firefox-ios`.
- **ICF and order files on non-Chromium links.** Only Chromium uses them.
- **Debug builds.** `rust-debug` and `chromium-macos-debug-component` are the
  only ones.
