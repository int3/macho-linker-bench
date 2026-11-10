import contextlib
import csv
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench

FIXTURE = Path(__file__).parent / 'fixtures/lld-time-trace.json'


class TimeTrace(unittest.TestCase):
    def test_real_lld_totals_and_seconds(self):
        metrics = bench.trace_metrics(FIXTURE)
        self.assertEqual(set(metrics), {'time_trace_ExecuteLinker', 'time_trace_Load input files',
                                        'time_trace_markLive', 'time_trace_LTO'})
        self.assertEqual(metrics['time_trace_markLive'], 0.050155)
        self.assertEqual(metrics['time_trace_Load input files'], 0.211368)

    def test_measure_flags_metrics_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            script = ('import pathlib, shutil, sys; '
                      'trace = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--time-trace=")); '
                      f'shutil.copyfile({str(FIXTURE)!r}, trace); '
                      'pathlib.Path("output").write_bytes(b"abc")')
            spec = dict(output=str(output), log=str(Path(root) / 'log'), cwd=root, timeout=10,
                        time_trace=True, argv=[sys.executable, '-c', script])
            self.assertEqual(bench.measure(spec)['time_trace_markLive'], 0.050155)
            self.assertFalse(list(Path(root).glob('linkerbench-trace-*')))
            spec['argv'] = [sys.executable, '-c', 'from pathlib import Path; Path("output").write_bytes(b"abc")']
            with self.assertRaises(ValueError):
                bench.measure(spec)
            self.assertFalse(list(Path(root).glob('linkerbench-trace-*')))
            spec['argv'] = [sys.executable, '-c', 'raise SystemExit(1)']
            with self.assertRaisesRegex(RuntimeError, 'link exited'):
                bench.measure(spec)
            self.assertFalse(list(Path(root).glob('linkerbench-trace-*')))

    def test_measure_keeps_lto_cache_between_links(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            cache = Path(root) / 'cache'
            cache.mkdir()
            # LLD keeps the last -cache_path_lto; the captured one comes first.
            script = ('import pathlib, sys; '
                      'i = len(sys.argv) - 1 - sys.argv[::-1].index("-cache_path_lto"); '
                      'cache = pathlib.Path(sys.argv[i + 1]); '
                      'cache.joinpath(str(len(list(cache.iterdir())))).write_bytes(b"x"); '
                      'pathlib.Path("output").write_bytes(b"abc")')
            spec = dict(output=str(output), log=str(Path(root) / 'log'), cwd=root, timeout=10,
                        argv=[sys.executable, '-c', script, '-cache_path_lto', '/build/cache',
                              '-cache_path_lto', str(cache), '-o', str(output)])
            for _ in range(2):
                bench.measure(spec)
            self.assertEqual(sorted(p.name for p in cache.iterdir()), ['0', '1'])

    def samples(self):
        raw = io.StringIO()
        metrics = bench.trace_metrics(FIXTURE)
        keys = ['benchmark', *bench.METRICS, *metrics, 'time_trace_new phase']
        writer = csv.DictWriter(raw, fieldnames=keys)
        writer.writeheader()
        for name, factor in [('base', 1), ('new', 0.5)]:
            for _ in range(3):
                phases = {key: value * factor for key, value in metrics.items()}
                phases['time_trace_LTO'] = 0.00001
                phases['time_trace_new phase'] = 0.02 if name == 'new' else 'NaN'
                if name == 'new':
                    phases['time_trace_Load input files'] = 'NaN'
                writer.writerow(dict(benchmark=name, wall_time=1, cpu_time=2, max_rss_mib=3, **phases))
        return raw.getvalue()

    def test_phase_table_missing_floor_order_and_regeneration(self):
        summaries = bench.summarize(self.samples(), ['base', 'new'])
        rows = bench.phase_rows(summaries['base'], summaries['new'])
        self.assertEqual([row[0] for row in rows], ['ExecuteLinker', 'Load input files', 'markLive', 'new phase'])
        self.assertEqual(rows[2], ['markLive', '50.2 ± 0.0 ms', '25.1 ± 0.0 ms', '[-50.0%, -50.0%]'])
        self.assertEqual(rows[1][2:], ['', ''])
        self.assertEqual(rows[-1][1], '')
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            (out / 'one').mkdir()
            (out / 'one/samples.csv').write_text(self.samples())
            bench.report(out, ['Machine: fixture'], [('one', {'base': '', 'new': ''}, summaries, ''),
                                                   ('two', {'base': 'Skipped', 'new': 'Skipped'}, {}, 'A workload note.')])
            md = (out / 'report.md').read_text()
            self.assertIn('| Phase | base median ± 95% CI | new median ± 95% CI | Change in median (95% CI) |', md)
            self.assertIn('below 1%', md)
            self.assertNotIn('| LTO |', md)
            with contextlib.redirect_stdout(io.StringIO()):
                bench.report_only(out, out / 'regen')
            self.assertEqual(md, (out / 'regen/report.md').read_text())

    def test_refuses_apple(self):
        argv = ['bench.py', '--linker', f'apple={sys.executable}', '--time-trace', '--workloads', 'rust-release']
        with (patch.object(sys, 'argv', argv), patch.object(bench, 'command', return_value='Apple ld'),
              patch.object(bench.sys, 'platform', 'freebsd'), patch.object(bench.shutil, 'which', return_value=sys.executable),
              contextlib.redirect_stderr(io.StringIO()) as error):
            with self.assertRaises(SystemExit):
                bench.main()
        self.assertIn('--time-trace requires LLD; apple reports: Apple ld', error.getvalue())

    def test_resume_with_phase_metrics(self):
        header = 'benchmark,cpu_time,max_rss_mib,wall_time,time_trace_markLive,time_trace_missing\n'
        row = 'mine,1,1,1,0.1,NaN\n'
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'samples.csv').write_text(header + row * 3)
            process = unittest.mock.Mock(returncode=0)
            process.stdout = io.StringIO(header + row * 3)
            args = unittest.mock.Mock(time=None, samples=5, link_timeout=1)
            with patch.object(bench.subprocess, 'Popen', return_value=process), contextlib.redirect_stdout(io.StringIO()):
                raw = bench.sample('cbdr', {'mine': {}}, args, directory)
            self.assertEqual(raw, header + row * 5)


if __name__ == '__main__':
    unittest.main()
