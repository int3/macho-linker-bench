#!/usr/bin/env python3
"""Verified, bounded-disk release fetching (used by fetch.sh and bench.py)."""
import argparse
import csv
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

HERE = Path(__file__).resolve().parent
BLOCK = 1024 * 1024


def catalog():
    result = {}
    with (HERE / 'manifest.tsv').open() as source:
        for row in csv.DictReader(source, delimiter='\t'):
            result.setdefault(row['workload'], []).append(row)
    return result


def verified(path, size, digest):
    if not path.is_file() or path.stat().st_size != int(size):
        return False
    sha = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(BLOCK), b''):
            sha.update(block)
    return sha.hexdigest() == digest


def stream(rows, dest, temporary, extract, keep_tar):
    """Hash compressed and raw streams while zstd and tar consume them.

    The writer thread is the equivalent of cat parts | tee >(hash) | zstd;
    the reader is zstd | tee >(hash/byte-count) | tar. No joined file exists.
    Explicit waits check every process, including tar's exit status.
    """
    first = rows[0]
    target = temporary / 'extracted'
    if extract:
        target.mkdir()
    compressed_sha = hashlib.sha256()
    compressed_bytes = 0
    errors = []
    decoder = subprocess.Popen(['zstd', '-dc'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    unpacker = None
    writer = None
    raw_file = None

    def feed():
        nonlocal compressed_bytes
        try:
            for row in rows:
                with (dest / row['part_file']).open('rb') as source:
                    for block in iter(lambda: source.read(BLOCK), b''):
                        compressed_sha.update(block)
                        compressed_bytes += len(block)
                        decoder.stdin.write(block)
        except BaseException as error:
            errors.append(error)
        finally:
            try:
                decoder.stdin.close()
            except BrokenPipeError:
                pass

    try:
        if extract:
            unpacker = subprocess.Popen(['tar', '-xf', '-', '-C', str(target)], stdin=subprocess.PIPE)
        if keep_tar:
            raw_file = (temporary / 'raw').open('wb')
        writer = threading.Thread(target=feed)
        writer.start()
        raw_sha = hashlib.sha256()
        raw_bytes = 0
        for block in iter(lambda: decoder.stdout.read(BLOCK), b''):
            raw_sha.update(block)
            raw_bytes += len(block)
            if raw_file:
                raw_file.write(block)
            if unpacker:
                unpacker.stdin.write(block)
        if unpacker:
            unpacker.stdin.close()
        if raw_file:
            raw_file.close()
        writer.join()
        if decoder.wait() or errors:
            raise RuntimeError(f'decompression/stream failed: {first["workload"]}; {errors}')
        if unpacker and unpacker.wait():
            raise RuntimeError(f'extraction failed: {first["workload"]}')
        if compressed_bytes != int(first['zstd_bytes']) or compressed_sha.hexdigest() != first['zstd_sha256']:
            raise RuntimeError(f'Compressed checksum/size mismatch: {first["workload"]}')
        if raw_bytes != int(first['raw_bytes']) or raw_sha.hexdigest() != first['raw_sha256']:
            raise RuntimeError(f'Raw capture checksum/size mismatch: {first["workload"]}')
        if keep_tar:
            # Exclusive publication also preserves a tarball created concurrently.
            os.link(temporary / 'raw', dest / first['raw_file'])
        if extract:
            # A destination that appeared concurrently must never be replaced.
            if (dest / first['workload']).exists():
                raise RuntimeError(f'extraction directory already exists: {dest / first["workload"]}')
            target.rename(dest / first['workload'])
    finally:
        for process in (unpacker, decoder):
            if process is not None and process.poll() is None:
                process.kill()
        for process in (unpacker, decoder):
            if process is not None:
                process.wait()
        if writer:
            writer.join()
        for process in (unpacker, decoder):
            if process is not None:
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except BrokenPipeError:
                            pass
        if raw_file:
            raw_file.close()


def fetch(workload, dest, *, extract=False, keep_tar=False, public=False, assets_dir=None, no_cache=False):
    rows = catalog()[workload]
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if extract and (dest / workload).exists():
        raise RuntimeError(f'extraction directory already exists: {dest / workload}')
    if keep_tar and (dest / rows[0]['raw_file']).exists():
        raw = dest / rows[0]['raw_file']
        if not verified(raw, rows[0]['raw_bytes'], rows[0]['raw_sha256']):
            raise RuntimeError(f'Refusing to overwrite existing raw capture: {raw}')
        keep_tar = False  # The verified original is already present.
    print(f'{workload}: fetching', flush=True)
    with tempfile.TemporaryDirectory(prefix='.fetch-', dir=dest) as temporary:
        temporary = Path(temporary)
        for row in rows:
            part = row['part_file']
            if not verified(dest / part, row['part_bytes'], row['part_sha256']):
                download = temporary / part
                if assets_dir:
                    shutil.copyfile(Path(assets_dir) / part, download)
                elif public:
                    subprocess.run(['curl', '--fail', '--location', '--retry', '5', '--silent', '--show-error',
                                    '--output', str(download),
                                    f'https://github.com/int3/macho-linker-bench/releases/download/v1/{part}'], check=True)
                else:
                    subprocess.run(['gh', 'release', 'download', 'v1', '--repo', 'int3/macho-linker-bench',
                                    '--pattern', part, '--dir', str(temporary), '--clobber'], check=True)
                if not verified(download, row['part_bytes'], row['part_sha256']):
                    raise RuntimeError(f'Part checksum/size mismatch: {part}')
                os.replace(download, dest / part)
        print(f'{workload}: {"extracting" if extract else "verifying streams"}', flush=True)
        stream(rows, dest, temporary, extract, keep_tar)
    if no_cache:
        for row in rows:
            (dest / row['part_file']).unlink(missing_ok=True)
    print(f'{workload}: verified ({int(rows[0]["raw_bytes"]) / 1e9:.1f} GB)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workload', nargs='?')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--dir', type=Path, default=Path('corpora.noindex'))
    parser.add_argument('--extract', action='store_true')
    parser.add_argument('--keep-tar', action='store_true', help='also keep the original raw capture tarball')
    parser.add_argument('--public', action='store_true', help='download public assets with curl instead of gh')
    parser.add_argument('--no-cache', action='store_true', help='delete parts after successful verification/extraction')
    parser.add_argument('--assets-dir', type=Path, default=os.environ.get('LINKERBENCH_ASSETS_DIR'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    entries = catalog()
    if args.all == bool(args.workload) or (args.workload and args.workload not in entries):
        parser.error('select one manifest workload or --all')
    selected = sorted(entries, key=lambda w: int(entries[w][0]['raw_bytes'])) if args.all else [args.workload]
    for workload in selected:
        fetch(workload, args.dir, extract=args.extract, keep_tar=args.keep_tar, public=args.public,
              assets_dir=args.assets_dir, no_cache=args.no_cache)


if __name__ == '__main__':
    # Turn termination into an exception so temporary extractions are cleaned up.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f'fetch.sh: {error}')
