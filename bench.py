#!/usr/bin/env python3
"""Fetch and benchmark Mach-O captures with cbdr; Python 3, no pip packages."""
import argparse
import csv
import datetime
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import random
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from fetch import catalog, fetch

HERE = Path(__file__).resolve().parent
METRICS = ('wall_time', 'cpu_time', 'max_rss_mib')


def measure(spec, output_sizes=None):
    """cbdr's custom measurement protocol: one JSON object per invocation.

    Python startup and output cleanup are outside wall_time. wait4 measures
    the linker itself, including all its threads, rather than the wrapper.
    """
    output = Path(spec['output'])
    child = None

    def kill_child(*_):
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    old_handler = signal.signal(signal.SIGTERM, kill_child)
    timer = threading.Timer(spec['timeout'], kill_child)
    try:
        with open(spec['log'], 'ab') as log:
            start = time.perf_counter()
            child = subprocess.Popen(spec['argv'], cwd=spec['cwd'], stdout=log,
                                     stderr=log, start_new_session=True)
            timer.start()
            _, status, usage = os.wait4(child.pid, 0)
            wall = time.perf_counter() - start
            child.returncode = os.waitstatus_to_exitcode(status)
        if child.returncode:
            raise RuntimeError(f"link exited {child.returncode} (timeout {spec['timeout']}s); see {spec['log']}")
        if not output.is_file() or not output.stat().st_size:
            raise RuntimeError(f'link produced no nonempty output: {output}')
        if output_sizes is not None:
            output_sizes.append(output.stat().st_size)
        rss_bytes = usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
        return dict(wall_time=wall, cpu_time=usage.ru_utime + usage.ru_stime,
                    max_rss_mib=rss_bytes / 1024**2)
    finally:
        timer.cancel()
        if child is not None and child.returncode is None:
            kill_child()
            _, status, _ = os.wait4(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(status)
        signal.signal(signal.SIGTERM, old_handler)
        output.unlink(missing_ok=True)


def command(argv):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=30)
    return result.stdout.strip()


def internal_volume(path):
    """Resolve symlinks and query the nearest existing ancestor, even before fetch."""
    path = Path(path).resolve()
    while not path.exists():
        path = path.parent
    device = subprocess.check_output(['/bin/df', '-P', str(path)], text=True, timeout=30).splitlines()[-1].split()[0]
    info = subprocess.check_output(['/usr/sbin/diskutil', 'info', '-plist', device], timeout=30)
    return bool(plistlib.loads(info).get('Internal'))


def linker_storage_warning(name, path):
    if sys.platform == 'darwin' and not internal_volume(path):
        return (f'WARNING: {name} linker binary resolves to {Path(path).resolve()} off the internal disk; '
                'links can stall on page-ins of the linker itself.')
    return ''


def noise_note(name, summary):
    wall = summary['wall']
    message = (f"{name}: {summary['outliers']} of {len(wall)} runs over 1.5× the median "
               f"(max {max(wall):.3f} s); range {min(wall):.3f}–{max(wall):.3f} s")
    if summary['ci'] > 0.02 * summary['medians']['wall_time']:
        message += f"; median CI half-width {100 * summary['ci'] / summary['medians']['wall_time']:.2f}% exceeds 2%"
    return message + '.'


class OutputStorage:
    """Correctness links size a bounded RAM filesystem before timed links."""
    def __init__(self, override, ram):
        self.override, self.ram = override, ram
        self.device = None
        self.temporary = None
        self.timed = None
        self.capacity = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release_workload()

    def release_workload(self):
        # Defer a second Ctrl-C/termination until the device is detached.
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            try:
                if self.timed is not None:
                    self.timed.cleanup()
                    self.timed = None
            finally:
                if self.device is not None:
                    try:
                        subprocess.run(['/usr/bin/hdiutil', 'detach', self.device], check=True,
                                       capture_output=True, timeout=60)
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                        subprocess.run(['/usr/bin/hdiutil', 'detach', '-force', self.device], check=True,
                                       capture_output=True, timeout=60)
                    self.device = None
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    def begin_workload(self):
        self.release_workload()
        self.capacity = 0
        # Sizing links are untimed, and use the internal disk on macOS.
        self.temporary = tempfile.TemporaryDirectory(prefix='linkerbench-probe-',
            dir=self.override or ('/tmp' if sys.platform == 'darwin' else None))
        return Path(self.temporary.name)

    def prepare(self, largest_output):
        if self.override is not None:
            return Path(self.temporary.name)
        mib = 1024**2
        required = max(512 * mib, 2 * largest_output + 256 * mib)
        required = ((required + 64 * mib - 1) // (64 * mib)) * 64 * mib
        if required > self.ram // 4:
            raise RuntimeError(f'RAM outputs need {required / mib:.0f} MiB, exceeding the '
                               '25% of physical RAM limit; choose --output-dir explicitly or use a larger machine')
        self.capacity = required
        if sys.platform == 'darwin':
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
            try:
                # Register the device before delivering a pending interruption.
                attached = subprocess.run(['/usr/bin/hdiutil', 'attach', '-nomount',
                    f'ram://{required // 512}'], text=True, capture_output=True, check=True, timeout=60)
                match = re.search(r'/dev/disk\d+', attached.stdout)
                if not match:
                    raise RuntimeError(f'cannot identify RAM disk: {attached.stdout}')
                self.device = match.group()
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            name = f'LinkerBench-{os.getpid()}-{time.time_ns()}'
            subprocess.run(['/usr/sbin/diskutil', 'erasevolume', 'APFS', name, self.device],
                           check=True, capture_output=True, timeout=60)
            parent = Path('/Volumes') / name
            # A RAM disk allocates its memory on first write, so the first
            # few links would also pay for that. Touch every block up front.
            fill = parent / 'fill'
            with fill.open('wb') as f:
                chunk = bytes(64 * mib)
                try:
                    while True:
                        f.write(chunk)
                except OSError:
                    pass
            fill.unlink()
        elif sys.platform.startswith('linux'):
            parent = Path('/dev/shm')
            mounts = Path('/proc/self/mountinfo').read_text().splitlines()
            if not any(line.split()[4] == '/dev/shm' and line.split(' - ')[1].split()[0] == 'tmpfs'
                       for line in mounts):
                raise RuntimeError('RAM outputs require tmpfs at /dev/shm; choose --output-dir explicitly')
        else:
            raise RuntimeError('automatic RAM outputs support macOS and Linux; choose --output-dir explicitly')
        free = shutil.disk_usage(parent).free
        # APFS reserves some of the advertised capacity for metadata.
        needed = largest_output + 128 * mib
        if free < needed or (sys.platform.startswith('linux') and free < required):
            raise RuntimeError(f'insufficient RAM filesystem space at {parent}: need '
                               f'{(required if sys.platform.startswith("linux") else needed) / mib:.0f} MiB, '
                               f'have {free / mib:.0f} MiB; choose --output-dir explicitly')
        self.timed = tempfile.TemporaryDirectory(prefix='linkerbench-', dir=parent)
        return Path(self.timed.name)


def named(value):
    name, sep, rest = value.partition('=')
    if not sep or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise argparse.ArgumentTypeError('expected NAME=VALUE (letters, digits, _, ., - in NAME)')
    return name, rest


def linker_argument(value):
    if '=' in value:
        return named(value)
    name = re.sub(r'[^A-Za-z0-9_.-]+', '-', Path(value).name).strip('-_.')
    if not name:
        raise argparse.ArgumentTypeError('cannot derive a linker name; use NAME=PATH')
    return name, value


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def completed(directory, names, args):
    path = directory / 'complete.json'
    if not path.is_file():
        return None
    state = json.loads(path.read_text())
    samples = directory / 'samples.csv'
    raw = samples.read_text() if samples.is_file() else ''
    if hashlib.sha256(raw.encode()).hexdigest() != state['samples_sha256']:
        raise RuntimeError(f'saved samples changed: {samples}; use a fresh --out directory')
    summaries = summarize(raw, names) if raw else {}
    minimum = 1 if args.time else args.samples
    if any(name not in summaries or summaries[name]['samples'] < minimum for name in state['sampled']):
        return None
    return state['cells'], summaries, state['note']


def disk_preflight(corpora, workloads, entries, args):
    """Simulate the sequential run; free space already excludes existing files."""
    parts = {row['part_file']: int(row['part_bytes']) for w in workloads for row in entries[w]}
    present = {part: (corpora / part).stat().st_size if (corpora / part).is_file() else 0 for part in parts}
    initial_cache = sum(present.values())
    cache, retained, peak = initial_cache, 0, initial_cache
    for workload in workloads:
        if (corpora / workload).exists():
            continue
        rows = entries[workload]
        cache += sum(max(0, int(r['part_bytes']) - present[r['part_file']]) for r in rows)
        extracted = int(rows[0]['raw_bytes'])
        peak = max(peak, cache + retained + extracted)
        if args.keep:
            retained += extracted
        if args.no_cache:
            cache -= sum(max(present[r['part_file']], int(r['part_bytes'])) for r in rows)
    needed = max(0, peak - initial_cache)
    # Directory allocation, response preparation, and replacement of bad parts.
    required = int(needed * 1.05) + (256 * 1024**2 if needed else 0)
    free = shutil.disk_usage(corpora).free
    gib = 1024**3
    print(f'Disk estimate: peak cache + new extractions {peak/gib:.2f} GiB; '
          f'additional space with margin {required/gib:.2f} GiB; free {free/gib:.2f} GiB.', flush=True)
    if free < required and not args.ignore_disk_space:
        raise RuntimeError(f'insufficient free space in {corpora}: need {required/gib:.2f} GiB, '
                           f'have {free/gib:.2f} GiB; try --no-cache / --skip-large, '
                           'or override with --ignore-disk-space')


def bootstrap_interval(values):
    ordered = sorted(values)
    def percentile(p):
        position = (len(ordered) - 1) * p
        index = int(position)
        return ordered[index] + (ordered[index + 1] - ordered[index]) * (position - index)
    return percentile(0.025), percentile(0.975)


def median_interval(values):
    if len(values) < 2:
        return math.nan, math.nan
    rng = random.Random(0)
    return bootstrap_interval(statistics.median(rng.choices(values, k=len(values)))
                              for _ in range(10_000))


def summarize(raw, names):
    rows = list(csv.DictReader(io.StringIO(raw)))
    summaries = {}
    for name in names:
        sample = [row for row in rows if row['benchmark'] == name]
        if not sample:
            continue
        medians = {key: statistics.median(float(row[key]) for row in sample) for key in METRICS}
        n = len(sample)
        wall = [float(row['wall_time']) for row in sample]
        if any(not math.isfinite(value) or value <= 0 for value in wall):
            raise ValueError(f'{name}: wall samples must be finite and positive')
        low, high = median_interval(wall)
        ci = (high - low) / 2
        outliers = sum(value > 1.5 * medians['wall_time'] for value in wall)
        noisy = ci > 0.02 * medians['wall_time'] or outliers > 0
        summaries[name] = dict(medians=medians, ci=ci, interval=(low, high), samples=n, ratio=None,
                               wall=wall, noisy=noisy, outliers=outliers)

    base = names[0]
    for name, summary in summaries.items():
        if name != base and base in summaries:
            summary['ratio'] = summary['medians']['wall_time'] / summaries[base]['medians']['wall_time']
    return summaries


def sample(cbdr, specs, args, directory):
    argv = [cbdr, 'sample', '--bench', str(Path(__file__).resolve())]
    if args.time:
        argv += ['--timeout', args.time]
    argv += [name + ':' + json.dumps(spec) for name, spec in specs.items()]
    (directory / 'sample-command.txt').write_text(shlex.join(argv) + '\n')
    samples = directory / 'samples.csv'
    previous = samples.read_text() if samples.is_file() else ''
    saved = ''
    counts = dict.fromkeys(specs, 0)
    if previous:
        # Preserve every interrupted attempt, including a possibly torn final row.
        backup = directory / f'samples-before-resume-{time.time_ns()}.csv'
        backup.write_text(previous)
        if not args.time:
            saved = previous[:previous.rfind('\n') + 1]
            if saved:
                reader = csv.DictReader(io.StringIO(saved))
                if set(reader.fieldnames or []) != {'benchmark', *METRICS}:
                    raise RuntimeError(f'invalid saved sample header: {samples}')
                for row in reader:
                    name = row['benchmark']
                    if name not in specs or None in row or any(row.get(key) is None for key in METRICS):
                        raise RuntimeError(f'invalid saved sample row or inactive linker: {samples}')
                    if (any(not math.isfinite(float(row[key])) or float(row[key]) < 0 for key in METRICS)
                            or float(row['wall_time']) == 0):
                        raise RuntimeError(f'invalid saved sample value: {samples}')
                    counts[name] += 1
                print(f'{directory.name}: resuming {sum(counts.values())} saved samples', flush=True)
                if min(counts.values()) >= args.samples:
                    return saved
    stopped = False
    with open(directory / 'sample.log', 'a') as log:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            header = process.stdout.readline()
            if saved and header != saved.splitlines(keepends=True)[0]:
                raise RuntimeError(f'cbdr CSV header changed: {samples}; use a fresh --out')
            # Line buffering keeps completed samples available after interruption.
            with samples.open('w', buffering=1) as raw:
                raw.write(saved or header)
                for line in process.stdout:
                    raw.write(line)
                    name = next(csv.reader([line]))[0]
                    counts[name] += 1
                    if not args.time and min(counts.values()) >= args.samples:
                        stopped = True
                        # Let any in-flight link finish; cbdr exits on broken pipe.
                        break
        finally:
            process.stdout.close()
            try:
                process.wait(timeout=args.link_timeout + 60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if not stopped and process.returncode:
        raise RuntimeError(f'cbdr sample failed ({process.returncode}); see {directory / "sample.log"}')
    return samples.read_text()


def time_text(seconds, ci):
    """Median ± CI half-width, in ms below a second and in s above."""
    scale, unit, digits = (1000, 'ms', 1) if seconds < 1 else (1, 's', 3)
    spread = f'{ci * scale:.{digits}f}' if math.isfinite(ci) else 'n/a'
    return f'{seconds * scale:.{digits}f} ± {spread} {unit}'


def wall_text(summary):
    return time_text(summary['medians']['wall_time'], summary['ci'])


def ratio_text(summary):
    ratio = summary.get('ratio') if summary else None
    return f'{ratio:.2f}×' if ratio else '—'


def result_text(summary):
    """A linker's wall time, followed by its ratio to the baseline when it has one."""
    if not summary:
        return 'n/a'
    return wall_text(summary) + (f' ({ratio_text(summary)})' if summary.get('ratio') else '')


def status_text(name, cells, summaries):
    """Console form of one linker's result: its time, or why it has none."""
    if name in summaries:
        return result_text(summaries[name])
    return cells.get(name, 'n/a').split(':')[0].lower()


def bar_chart(title, bars, unit):
    """Bars of (label, value, CI half-width or None)."""
    bars = [(label, value, ci) for label, value, ci in bars if ci is None or math.isfinite(ci)]
    if not bars:
        return ''
    scale = 550 / max(value + (ci or 0) for _, value, ci in bars)
    svg = [f'<h2>{html.escape(title)}</h2><svg role="img" aria-label="{html.escape(title)}; 95% confidence intervals" '
           f'viewBox="0 0 1100 {len(bars) * 48 + 30}" style="width:100%">']
    if unit == '×':
        svg.append(f'<path d="M {220 + scale} 10 V {len(bars) * 48 + 15}" stroke="#888" stroke-dasharray="4"/>')
    for i, (label, value, ci) in enumerate(bars):
        y = i * 48 + 25
        svg += [f'<text x="0" y="{y + 5}">{html.escape(label)}</text>',
                f'<rect x="220" y="{y - 12}" width="{value * scale}" height="24" fill="#4b88b6"/>']
        if ci is None:
            svg.append(f'<text x="790" y="{y + 5}">{value:.2f}{unit}</text>')
            continue
        lo, hi = 220 + max(0, value - ci) * scale, 220 + (value + ci) * scale
        svg += [f'<path d="M {lo} {y} H {hi} M {lo} {y-7} V {y+7} M {hi} {y-7} V {y+7}" stroke="black" fill="none"/>',
                f'<text x="790" y="{y + 5}">{value:.1f} ± {ci:.1f} {unit}</text>']
    return '\n'.join(svg) + '</svg>'


def report(out, metadata, results):
    text = ['# Mach-O LinkerBench', '', *metadata, '']
    page = ['<h1>Mach-O LinkerBench</h1>', *[f'<p>{html.escape(line)}</p>' for line in metadata]]

    def table(title, headers, rows):
        text.extend([f'## {title}', '', '| ' + ' | '.join(headers) + ' |',
                     '| ' + ' | '.join('---' for _ in headers) + ' |',
                     *['| ' + ' | '.join(row) + ' |' for row in rows], ''])
        page.extend([f'<h2>{html.escape(title)}</h2><table><tr>',
                     *[f'<th>{html.escape(h)}</th>' for h in headers], '</tr>',
                     *['<tr>' + ''.join(f'<td>{html.escape(cell)}</td>' for cell in row) + '</tr>' for row in rows], '</table>'])

    names = list(results[0][1])
    table(f'Median wall time ± 95% CI, with ratio to {names[0]}', ['Workload', *names],
          [[w, *[result_text(s.get(n)) if n in s else cells[n] for n in names]] for w, cells, s, _ in results])
    page.append(bar_chart(f'Wall time ratio to {names[0]} (lower is faster)',
                [(f'{w} / {n}', v['ratio'], None) for w, _, s, _ in results for n, v in s.items() if v['ratio']], '×'))
    for workload, cells, summaries, note in results:
        if note:
            text.extend([note, ''])
            page.append(f'<p>{html.escape(note)}</p>')
        rows = []
        for name, status in cells.items():
            s = summaries.get(name)
            if not s:
                rows.append([name, '—', status, '—', '—', '—'])
                continue
            rows.append([name, str(s['samples']), wall_text(s), f'{s["medians"]["cpu_time"] * 1000:.2f}',
                         f'{s["medians"]["max_rss_mib"]:.1f}', ratio_text(s)])
        table(workload, ['Linker', 'Samples', 'Wall median ± 95% CI', 'CPU median (ms)', 'Peak RSS median (MiB)', f'Ratio to {names[0]}'], rows)
        for name, summary in summaries.items():
            if summary['noisy']:
                noise = noise_note(name, summary)
                text.extend([noise, ''])
                page.append(f'<p>{html.escape(noise)}</p>')
        page.append(bar_chart(workload + ' wall time', [(n, s['medians']['wall_time'] * 1000,
                    s['ci'] * 1000) for n, s in summaries.items()], 'ms'))
    footer = ('Wall CIs use a 95% percentile bootstrap of sample medians (10,000 resamples, seed 0); '
              'the ± value is half the interval\'s width. Ratios divide median wall times. '
              'cbdr is used only for sampling. CPU and peak RSS are sample medians; '
              'n/a indicates an unavailable CI; raw samples are in each original workload directory.')
    wall_rows = [[workload, *[wall_text(summaries[name]) if name in summaries else cells[name]
                             for name in names]] for workload, cells, summaries, _ in results]
    (out / 'summary-wall.md').write_text(
        '| Workload | ' + ' | '.join(f'{name} median ± 95% CI' for name in names) + ' |\n'
        + '| ' + ' | '.join('---' for _ in range(len(names) + 1)) + ' |\n'
        + '\n'.join('| ' + ' | '.join(row) + ' |' for row in wall_rows) + '\n')
    (out / 'report.md').write_text('\n'.join(text) + '\n' + footer + '\n')
    (out / 'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>Mach-O LinkerBench</title>'
        '<style>body{font:16px system-ui;margin:2rem;max-width:1200px;overflow-wrap:anywhere}'
        'table{border-collapse:collapse}td,th{padding:.5rem;text-align:left;border-bottom:1px solid #ddd}svg{font-size:14px}</style>'
        + '\n'.join(page) + '<p>' + html.escape(footer) + '</p></html>')


def report_only(source, out):
    """Reuse the saved report's metadata, cell statuses and workload order."""
    sections = (source / 'report.md').read_text().split('\n## ')
    if len(sections) < 3 or not sections[1].startswith(('Wall ratio to ', 'Median wall time ')):
        raise ValueError('report-only requires an existing bench.py report.md')
    metadata = sections[0].splitlines()[2:]
    state_file = source / 'run.json'
    if not any(line.startswith('Linked outputs:') for line in metadata) and state_file.is_file():
        output_dir = json.loads(state_file.read_text()).get('identity', {}).get('output_dir')
        if output_dir:
            metadata.append(f'Linked outputs: {output_dir} (saved run settings).')
    results = []
    summary_tail = sections[1].splitlines()
    last_row = max(i for i, line in enumerate(summary_tail) if line.startswith('| '))
    note = '\n'.join(line for line in summary_tail[last_row + 1:] if line.strip())
    for section in sections[2:]:
        workload = section.splitlines()[0]
        cells = {}
        trailing = []
        in_table = False
        for line in section.splitlines()[1:]:
            if line.startswith('| '):
                in_table = True
                row = line[2:-2].split(' | ')
                if row[0] not in ('Linker', '---'):
                    cells[row[0]] = row[2] if row[1] == '—' else 'No samples'
            elif in_table and line.strip():
                trailing.append(line)
        if not cells:
            raise ValueError(f'no saved linker table for {workload}')
        samples = source / workload / 'samples.csv'
        summaries = summarize(samples.read_text(), list(cells)) if samples.is_file() else {}
        results.append((workload, cells, summaries, note))
        # Existing workload notes precede the next workload's heading.
        note = '\n'.join(line for line in trailing
                         if ' noisy: samples ranged ' not in line
                         and not line.startswith(('Mean wall CIs', 'Wall and ratio CIs', 'Wall CIs'))
                         and not re.match(r'^.+: \d+ of \d+ runs over 1\.5× the median', line))
    out.mkdir(parents=True, exist_ok=True)
    report(out, metadata, results)
    print(f'Report: {out / "report.md"}\nCharts: {out / "report.html"}', flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--linker', action='append', type=linker_argument, default=[], metavar='[NAME=]PATH')
    parser.add_argument('--apple', action='store_true', help='include active Xcode ld (DEVELOPER_DIR is respected)')
    parser.add_argument('--workloads', help='comma-separated manifest workload names; default: all manifest workloads, smallest first')
    parser.add_argument('--corpora', type=Path, default=HERE / 'corpora.noindex')
    parser.add_argument('--report-only', type=Path, metavar='DIR',
                        help='regenerate reports from saved CSVs and report.md, without running links or cbdr; default output: DIR/regen')
    parser.add_argument('--out', type=Path, help='report directory (existing runs resume); default: ./bench-DATE')
    parser.add_argument('--no-cache', action='store_true', help='delete downloaded parts after extraction')
    parser.add_argument('--keep', action='store_true', help='keep extractions created by this run')
    parser.add_argument('--skip-large', action='store_true', help='omit desktop Chromium sym2 and debug-component')
    parser.add_argument('--include-large', action='store_true', help='run sym2 and debug-component even without the RAM they need')
    parser.add_argument('--ignore-disk-space', action='store_true', help='override the free-space preflight')
    parser.add_argument('--public', action='store_true', help='fetch public assets with curl instead of gh')
    parser.add_argument('--assets-dir', type=Path, default=os.environ.get('LINKERBENCH_ASSETS_DIR'), help=argparse.SUPPRESS)
    stopping = parser.add_mutually_exclusive_group()
    stopping.add_argument('--samples', type=int, default=20, help='minimum samples per cell (default 20; counts may vary)')
    stopping.add_argument('--time', help='cbdr time budget per workload, e.g. 10m; counts may vary')
    parser.add_argument('--warmups', type=int, default=1, help='warmups per cell, minimum 1 (cbdr runs one itself)')
    parser.add_argument('--threads', type=int, help='pass --threads=N to LLD-family linkers')
    parser.add_argument('--extra-args', action='append', type=named, default=[], metavar="NAME='ARGS'")
    parser.add_argument('--output-dir', type=Path, help='override RAM output storage with this directory')
    parser.add_argument('--allow-external-output', action='store_true', help='allow linked outputs off the internal macOS disk (may distort timings)')
    parser.add_argument('--link-timeout', type=float, default=600, help='timeout for each link in seconds (default 600)')
    args = parser.parse_args()
    if args.report_only:
        source = args.report_only.resolve()
        return report_only(source, (args.out or source / 'regen').resolve())
    if args.samples < 2 or args.warmups < 1 or args.link_timeout <= 0 or (args.threads is not None and args.threads < 1):
        parser.error('samples >= 2, warmups >= 1, threads >= 1, link-timeout > 0 required')
    if args.output_dir is not None:
        args.output_dir = args.output_dir.resolve()
        args.output_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'darwin' and args.output_dir is not None and not internal_volume(args.output_dir):
        if not args.allow_external_output:
            sys.exit('bench.py: output directory is not on the internal macOS data volume; use the default RAM disk or --allow-external-output (external I/O may distort timings).')
    cbdr = shutil.which('cbdr')
    if not cbdr and (Path.home() / '.cargo/bin/cbdr').is_file():
        cbdr = str(Path.home() / '.cargo/bin/cbdr')
    if not cbdr:
        parser.error('cbdr missing: run cargo install cbdr')
    linkers = dict(args.linker)
    if len(linkers) != len(args.linker) or (args.apple and 'apple' in linkers):
        parser.error('linker names must be unique; --apple reserves apple')
    if args.apple:
        linkers['apple'] = command(['/usr/bin/xcrun', '--find', 'ld'])
    if not linkers:
        parser.error('provide --linker NAME=PATH and/or --apple')
    extras = {name: [] for name in linkers}
    for name, value in args.extra_args:
        if name not in linkers:
            parser.error(f'extra args for unknown linker {name}')
        extras[name].extend(shlex.split(value))
    entries = catalog()
    corpora = args.corpora.resolve()
    workloads = args.workloads.split(',') if args.workloads else list(entries)
    if not workloads or len(set(workloads)) != len(workloads) or any(w not in entries for w in workloads):
        parser.error('select distinct manifest workloads')
    if sys.platform == 'darwin':
        ram = int(command(['/usr/sbin/sysctl', '-n', 'hw.memsize']))
    else:
        ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    ram = int(ram)
    # RAM each link needs to run without swapping: the linker's peak RSS
    # plus the inputs it reads, which must stay in the page cache.
    large = {'chromium-macos-debug-component': 32, 'chromium-macos-sym2': 64}
    for w in [w for w in workloads if w in large]:
        if args.skip_large or (ram < large[w] * 1024**3 * 0.95 and not args.include_large):
            workloads.remove(w)
            if not args.skip_large:
                print(f'Skipping {w}: it needs {large[w]} GB of RAM to run without swapping '
                      f'(this machine has {ram / 1024**3:.0f} GB); --include-large runs it anyway.', flush=True)
    if not workloads:
        parser.error('no workloads remain')
    workloads.sort(key=lambda w: int(entries[w][0]['raw_bytes']))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = (args.out or Path(f'bench-{stamp}')).resolve()
    machine = f'{platform.platform()}; {os.cpu_count()} cores'
    if sys.platform.startswith('linux'):
        cpu = next((l.split(':', 1)[1].strip() for l in open('/proc/cpuinfo') if l.startswith('model name')), 'unknown CPU')
        machine = f'{cpu}; {os.cpu_count()} logical cores; {ram / 1024**3:.0f} GiB RAM; {platform.platform()}'
    if sys.platform == 'darwin':
        model, chip, physical, logical, ram = command(['/usr/sbin/sysctl', '-n', 'hw.model',
            'machdep.cpu.brand_string', 'hw.physicalcpu', 'hw.logicalcpu', 'hw.memsize']).splitlines()
        machine = f'{model}; {chip}; {physical} cores / {logical} logical; {int(ram) / 1024**3:g} GiB RAM; macOS ' + command(['/usr/bin/sw_vers', '-productVersion'])
    metadata = [f'Machine: {machine}', f'Date (UTC): {stamp}',
                f'Command: `{shlex.join(sys.argv)}`']
    if sys.platform == 'darwin' and not any(part.endswith('.noindex') for part in corpora.parts):
        print('Warning: Spotlight indexes freshly extracted captures for a minute or more, which slows '
              'the first links; give --corpora a directory whose name ends in .noindex.', flush=True)
        metadata.append('WARNING: corpora directory is indexed by Spotlight; early links may be slowed.')
    if sys.platform == 'darwin' and not internal_volume(corpora):
        print('Warning: --corpora is not on the internal disk; reading inputs from an external drive '
              'makes some links much noisier.', flush=True)
        metadata.append('WARNING: corpora on an external volume; input I/O may distort timings.')
    if args.allow_external_output:
        metadata.append('WARNING: --allow-external-output enabled; external output I/O may distort timings.')
    metadata.append(f'Linked outputs: {args.output_dir} (explicit override).' if args.output_dir else
                    'Linked outputs: automatically sized APFS RAM disk per workload (macOS), tmpfs /dev/shm (Linux); correctness sizing links use temporary disk storage.')
    versions = {}
    hashes = {}
    for name, value in linkers.items():
        # Keep the ld64.lld symlink: argv[0] selects the Mach-O driver.
        path = Path(shutil.which(value) or value).expanduser().absolute()
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f'not an executable linker: {path}')
        warning = linker_storage_warning(name, path)
        if warning:
            print(warning, flush=True)
            metadata.append(warning)
        linkers[name] = str(path)
        versions[name] = command([str(path), '-v' if name == 'apple' else '--version']).split('\n', 1)[0]
        digest = hashlib.sha256()
        with path.open('rb') as binary:
            for block in iter(lambda: binary.read(1024 * 1024), b''):
                digest.update(block)
        hashes[name] = digest.hexdigest()
        metadata += [f'{name}: `{path}`; SHA-256 `{digest.hexdigest()[:12]}`; ' + versions[name]]
    identity = dict(linkers=linkers, hashes=hashes, extras=extras, workloads=workloads,
                    corpora=str(corpora), samples=args.samples, time=args.time, warmups=args.warmups,
                    threads=args.threads, timeout=args.link_timeout, output_dir=str(args.output_dir) if args.output_dir else None,
                    allow_external_output=args.allow_external_output,
                    manifest_sha256=hashlib.sha256((HERE / 'manifest.tsv').read_bytes()).hexdigest())
    state_file = out / 'run.json'
    if state_file.is_file():
        previous = json.loads(state_file.read_text())
        if previous['identity'] != identity:
            parser.error('existing --out has different benchmark settings or linker binaries; use a new --out')
        metadata = previous['metadata']
    else:
        if out.exists() and any(out.iterdir()):
            parser.error('existing --out has no resumable run.json; use a new --out')
    resumed = {w: completed(out / w, list(linkers), args) for w in workloads}
    corpora.mkdir(parents=True, exist_ok=True)
    disk_preflight(corpora, [w for w in workloads if not resumed[w]], entries, args)
    if not state_file.is_file():
        out.mkdir(parents=True, exist_ok=True)
        save_json(state_file, dict(identity=identity, metadata=metadata))
    results = []
    failed = False
    with OutputStorage(args.output_dir, int(ram)) as storage:
        for workload in workloads:
            directory = out / workload
            directory.mkdir(exist_ok=True)
            if resumed[workload]:
                cells, summaries, note = resumed[workload]
                results.append((workload, cells, summaries, note))
                print(f'{workload}: done (resumed); ' + ', '.join(f'{n} {status_text(n, cells, summaries)}' for n in linkers), flush=True)
                report(out, metadata, results)
                continue
            created = False
            workload_failed = False
            cells, specs, summaries = {}, {}, {}
            note = ''
            heavy = command(['/usr/bin/pgrep', '-fl', r'(^|/)(ninja|autoninja|siso)( |$)']) if sys.platform == 'darwin' else ''
            if heavy:
                note = 'CONTAMINATED — build processes are running; these timings are functional-test data only.'
                (directory / 'heavy-processes-before.txt').write_text(heavy + '\n')
                print(note, flush=True)
            try:
                if not (corpora / workload).exists():
                    fetch(workload, corpora, extract=True, public=args.public,
                          assets_dir=args.assets_dir, no_cache=args.no_cache)
                    created = True
                print(f'{workload}: linking (correctness, warmup, sampling)', flush=True)
                captures = list((corpora / workload).rglob('response.txt'))
                if len(captures) != 1:
                    raise RuntimeError('expected one response.txt; extract this workload with fetch.sh --extract')
                capture = captures[0].parent
                tokens = shlex.split(captures[0].read_text())
                thin = False
                for token in tokens:
                    archive = capture / token
                    if token.endswith('.a') and archive.is_file():
                        with archive.open('rb') as archive_file:
                            thin |= archive_file.read(8) == b'!<thin>\n'
                prep = [sys.executable, str(HERE / 'prepare.py'), str(capture)]
                if args.apple and not thin and not (capture / 'apple-response.txt').exists():
                    prep.append('--apple')
                with open(directory / 'prepare.log', 'w') as log:
                    subprocess.run(prep, stdout=log, stderr=log, check=True, timeout=60)
                scratch = storage.begin_workload()
                output_sizes = []
                for name, path in linkers.items():
                    if name == 'apple' and thin:
                        cells[name] = 'Skipped: Apple ld cannot read thin archives'
                        continue
                    response = 'apple-response.txt' if name == 'apple' else 'response.txt'
                    output = str(Path(scratch) / 'linked-output')
                    argv = [path, '@' + response, *extras[name]]
                    if args.threads and 'lld' in versions[name].lower() and name != 'apple':
                        argv.append(f'--threads={args.threads}')
                    argv += ['-o', output]
                    spec = dict(argv=argv, cwd=str(capture), output=output,
                                timeout=args.link_timeout, log=str(directory / f'{name}-link.log'))
                    (directory / f'{name}-command.txt').write_text(f'cd {shlex.quote(str(capture))}\n{shlex.join(argv)}\n')
                    try:
                        measure(spec, output_sizes)  # Correctness and sizing, outside timing samples.
                        specs[name] = spec
                        cells[name] = 'No samples'
                    except (OSError, RuntimeError) as error:
                        with (directory / 'runner-errors.log').open('a') as log:
                            log.write(traceback.format_exc())
                        cells[name] = 'Failed correctness/warmup: ' + str(error)
                        failed = workload_failed = True
                if specs:
                    timed = storage.prepare(max(output_sizes))
                    metadata.append(f'Outputs for {workload}: {timed}; capacity {storage.capacity / 1024**2:.0f} MiB.'
                                    if storage.capacity else f'Outputs for {workload}: {timed} (explicit override).')
                    save_json(state_file, dict(identity=identity, metadata=metadata))
                    for name, spec in specs.items():
                        spec['output'] = str(timed / 'linked-output')
                        spec['argv'][-1] = spec['output']
                        (directory / f'{name}-command.txt').write_text(f'cd {shlex.quote(str(capture))}\n{shlex.join(spec["argv"])}\n')
                        for _ in range(args.warmups - 1):
                            measure(spec)
                    raw = sample(cbdr, specs, args, directory)
                    summaries = summarize(raw, list(linkers))
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                with (directory / 'runner-errors.log').open('a') as log:
                    log.write(traceback.format_exc())
                note += '\nFailed: ' + str(error)
                failed = workload_failed = True
                for name in linkers:
                    cells.setdefault(name, note)
            finally:
                storage.release_workload()
                if created and not args.keep:
                    shutil.rmtree(corpora / workload)
            if sys.platform == 'darwin':
                heavy = command(['/usr/bin/pgrep', '-fl', r'(^|/)(ninja|autoninja|siso)( |$)'])
                if heavy:
                    (directory / 'heavy-processes-after.txt').write_text(heavy + '\n')
                    if 'CONTAMINATED' not in note:
                        note += '\nCONTAMINATED — build processes are running; these timings are functional-test data only.'
            if not workload_failed:
                raw = (directory / 'samples.csv').read_text() if specs else ''
                minimum = 1 if args.time else args.samples
                if any(n not in summaries or summaries[n]['samples'] < minimum for n in specs):
                    raise RuntimeError(f'{workload}: incomplete samples; rerun with the same --out to retry')
                save_json(directory / 'complete.json', dict(cells=cells, note=note, sampled=list(specs),
                          samples_sha256=hashlib.sha256(raw.encode()).hexdigest()))
            results.append((workload, cells, summaries, note))
            print(f'{workload}: done' + (' (failed)' if workload_failed else '') + '; ' +
                  ', '.join(f'{n} {status_text(n, cells, summaries)}' for n in linkers) +
                  ('; extraction deleted' if created and not args.keep else ''), flush=True)
            report(out, metadata, results)
    print(f'Report: {out / "report.md"}\nCharts: {out / "report.html"}', flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        if len(sys.argv) == 2 and sys.argv[1].startswith('{'):
            print(json.dumps(measure(json.loads(sys.argv[1]))))
        else:
            sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(f'bench.py: {error}', file=sys.stderr)
        sys.exit(1)
