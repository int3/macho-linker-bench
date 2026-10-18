#!/usr/bin/env python3
"""Restore omitted search directories; optionally derive an Apple ld response."""
import argparse
import pathlib
import shlex

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('capture', type=pathlib.Path, help='extracted directory containing response.txt')
parser.add_argument('--apple', action='store_true', help='also write apple-response.txt')
args = parser.parse_args()
root = args.capture.resolve()
response = root / 'response.txt'
tokens = shlex.split(response.read_text())
for i, token in enumerate(tokens[:-1]):
    if token not in ('-L', '-F'):
        continue
    relative = pathlib.Path(tokens[i + 1])
    if relative.is_absolute():
        # Some captures retain unused host search paths (Firefox: /usr/local/lib).
        # Preserve the response without creating directories outside the capture.
        continue
    if '..' in relative.parts:
        raise SystemExit(f'Unexpected search path: {relative}')
    directory = root / relative
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
        print(f'Restored {relative}')
if args.apple:
    remove = {'--icf=none', '--icf=all', '--color-diagnostics',
              '--no-warn-duplicate-rpath', '--strict-auto-link',
              '--no-call-graph-profile-sort'}
    result = ['-ld_new']
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in remove or token.startswith(('--read-workers=', '--slop_scale=')):
            i += 1
        elif (i + 1 < len(tokens) and
              ((token == '-O' and tokens[i + 1].isdigit()) or
               (token == '-mllvm' and tokens[i + 1] == '-enable-linkonceodr-outlining'))):
            i += 2
        else:
            # LLVM/Apple response quoting, not shell single-quote escaping.
            result.append('"' + token.replace('\\', '\\\\').replace('"', '\\"') + '"')
            i += 1
    destination = root / 'apple-response.txt'
    if destination.exists():
        raise SystemExit(f'Refusing to overwrite {destination}')
    destination.write_text('\n'.join(result) + '\n')
    print(f'Wrote {destination}')
