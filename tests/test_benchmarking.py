import contextlib
import io
import json
import math
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench


class Statistics(unittest.TestCase):
    def raw(self, values):
        return 'benchmark,wall_time,cpu_time,max_rss_mib\n' + ''.join(
            f'{name},{v},{v * 2},{v * 3}\n' for name, wall in values.items() for v in wall)

    def test_medians_and_ratio_ignore_one_stall(self):
        result = bench.summarize(self.raw({'base': [1] * 29 + [10], 'other': [2] * 30}), ['base', 'other'])
        self.assertEqual(result['base']['medians'], dict(wall_time=1, cpu_time=2, max_rss_mib=3))
        self.assertEqual(result['base']['interval'], (1, 1))
        self.assertEqual(result['other']['ratio'], 2)
        self.assertIsNone(result['base']['ratio'])
        self.assertEqual(result['base']['outliers'], 1)
        self.assertTrue(result['base']['noisy'])
        self.assertIn('1 of 30 runs over 1.5× the median (max 10.000 s)', bench.noise_note('base', result['base']))

    def test_wide_ci_without_outliers_is_noisy(self):
        result = bench.summarize(self.raw({'base': [1, 1.1, 1.2, 1.3]}), ['base'])['base']
        self.assertTrue(result['noisy'])
        self.assertEqual(result['outliers'], 0)
        self.assertIn('exceeds 2%', bench.noise_note('base', result))

    def test_boundary_and_constant(self):
        result = bench.summarize(self.raw({'base': [1] * 29 + [1.5]}), ['base'])['base']
        self.assertFalse(result['noisy'])

    def test_bootstrap_repeatable_and_non_degenerate(self):
        values = [1, 2, 3, 4, 6]
        self.assertEqual(bench.median_interval(values), bench.median_interval(values))
        self.assertEqual(bench.median_interval(values), (1, 6))

    def test_time_text(self):
        self.assertEqual(bench.time_text(0.15767, 0.00062), '157.7 ± 0.6 ms')
        self.assertEqual(bench.time_text(5.8224, 0.11706), '5.822 ± 0.117 s')
        self.assertEqual(bench.time_text(0.5, math.nan), '500.0 ± n/a ms')

    def test_invalid_and_singleton_samples(self):
        for value in [0, -1, math.nan, math.inf]:
            with self.assertRaises(ValueError):
                bench.summarize(self.raw({'base': [value]}), ['base'])
        result = bench.summarize(self.raw({'base': [1]}), ['base'])['base']
        self.assertTrue(math.isnan(result['ci']))

    def test_report_only_old_statistics_and_missing_cells(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)
            (source / 'one').mkdir()
            (source / 'one/samples.csv').write_text(self.raw({'base': [1] * 29 + [10], 'other': [2] * 30}))
            (source / 'report.md').write_text('''# Mach-O LinkerBench

Machine: fixture

## Wall ratio to base (95% CI)

| Workload | base | other |
| --- | --- | --- |
| one | 1 | 2 |
| two | no | no |

A workload note.

## one

| Linker | Samples | Wall mean ± 95% CI (ms) |
| --- | --- | --- |
| base | 30 | 4 |
| other | 30 | 4 |

base noisy: samples ranged 1–10 s; likely memory pressure or I/O

Skipped note for two.

## two

| Linker | Samples | Wall mean ± 95% CI (ms) |
| --- | --- | --- |
| base | — | Skipped: fixture |
| other | — | Failed: fixture |

Mean wall CIs use Python Student t.
''')
            with contextlib.redirect_stdout(io.StringIO()):
                bench.report_only(source, source / 'regen')
            md = (source / 'regen/report.md').read_text()
            self.assertIn('| one | 1.000 ± 0.000 s | 2.000 ± 0.000 s (2.00×) |', md)
            self.assertNotIn('[', md.split('## one')[0])
            self.assertIn('1 of 30 runs', md)
            self.assertIn('Skipped note for two.', md)
            self.assertIn('Skipped: fixture', md)
            self.assertNotIn('Student', md)
            self.assertNotIn('mean', md)
            wall_summary = (source / 'regen/summary-wall.md').read_text()
            self.assertIn('1.000 ± 0.000 s', wall_summary)
            self.assertNotIn('mean', wall_summary)
            html = (source / 'regen/report.html').read_text()
            self.assertIn('1000.0 ± 0.0 ms', html)
            self.assertIn('2.00×', html)
            # Regeneration also supports replacing the reports in place.
            with contextlib.redirect_stdout(io.StringIO()):
                bench.report_only(source, source)
            self.assertEqual((source / 'report.md').read_text(), md)


class Storage(unittest.TestCase):
    def test_explicit_directory_bypasses_ram_limit(self):
        with tempfile.TemporaryDirectory() as parent:
            with bench.OutputStorage(Path(parent), 1) as storage:
                probe = storage.begin_workload()
                self.assertEqual(storage.prepare(10**12), probe)
            self.assertFalse(probe.exists())

    def test_refuses_large_allocation_before_attaching(self):
        with bench.OutputStorage(None, 1024**3) as storage, patch.object(bench.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '25%'):
                storage.prepare(1024**3)
            run.assert_not_called()

    def test_mac_cleanup_on_format_error_and_interrupt(self):
        for error in [RuntimeError('format failed'), KeyboardInterrupt()]:
            def run(argv, **kwargs):
                if 'attach' in argv:
                    self.assertIn('ram://1048576', argv)
                    return subprocess.CompletedProcess(argv, 0, '/dev/disk99\n', '')
                if 'erasevolume' in argv:
                    raise error
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(bench.sys, 'platform', 'darwin'), patch.object(bench.subprocess, 'run', side_effect=run) as process:
                with self.assertRaises(type(error)):
                    with bench.OutputStorage(None, 16 * 1024**3) as storage:
                        storage.prepare(10 * 1024**2)
                self.assertEqual(process.call_args.args[0], ['/usr/bin/hdiutil', 'detach', '/dev/disk99'])
                self.assertIsNone(storage.device)

    def test_linux_tmpfs_capacity_refusal(self):
        mount = '1 2 0:3 / /dev/shm rw - tmpfs shm rw\n'
        with (patch.object(bench.sys, 'platform', 'linux'),
              patch.object(bench.Path, 'read_text', return_value=mount),
              patch.object(bench.shutil, 'disk_usage', return_value=unittest.mock.Mock(free=0))):
            with bench.OutputStorage(None, 16 * 1024**3) as storage:
                with self.assertRaisesRegex(RuntimeError, 'insufficient RAM filesystem'):
                    storage.prepare(1)

    def test_linux_missing_tmpfs_refusal(self):
        with patch.object(bench.sys, 'platform', 'linux'), patch.object(bench.Path, 'read_text', return_value=''):
            with bench.OutputStorage(None, 16 * 1024**3) as storage:
                with self.assertRaisesRegex(RuntimeError, 'require tmpfs'):
                    storage.prepare(1)

    def test_symlink_resolved_for_storage_warning_driver_kept(self):
        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / 'lld'
            real.touch()
            link = Path(root) / 'ld64.lld'
            link.symlink_to(real)
            with patch.object(bench.sys, 'platform', 'darwin'), patch.object(bench, 'internal_volume', return_value=False):
                warning = bench.linker_storage_warning('trial', link)
            self.assertIn(str(real.resolve()), warning)
            self.assertIn('page-ins of the linker itself', warning)
            self.assertTrue(link.is_symlink())

    def test_correctness_output_size_recorded_and_deleted(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output'
            spec = dict(output=str(output), log=str(Path(root) / 'log'), cwd=root, timeout=10,
                        argv=[sys.executable, '-c', 'from pathlib import Path; Path("output").write_bytes(b"abc")'])
            sizes = []
            result = bench.measure(spec, sizes)
            self.assertEqual(sizes, [3])
            self.assertFalse(output.exists())
            self.assertEqual(set(result), set(bench.METRICS))

class CleanupAndCapacity(unittest.TestCase):
    def test_detach_retried_and_survives_directory_cleanup_error(self):
        storage = bench.OutputStorage(None, 16 * 1024**3)
        storage.device = '/dev/disk99'
        storage.timed = unittest.mock.Mock()
        storage.timed.cleanup.side_effect = OSError('cleanup failed')
        def run(argv, **kwargs):
            if '-force' not in argv:
                raise subprocess.CalledProcessError(1, argv)
        with patch.object(bench.subprocess, 'run', side_effect=run) as process:
            with self.assertRaisesRegex(OSError, 'cleanup failed'):
                storage.release_workload()
        self.assertIsNone(storage.device)
        self.assertEqual(process.call_args.args[0], ['/usr/bin/hdiutil', 'detach', '-force', '/dev/disk99'])

    def test_linux_capacity_and_successful_cleanup(self):
        mount = '1 2 0:3 / /dev/shm rw - tmpfs shm rw\n'
        temporary = unittest.mock.Mock(name='temporary')
        temporary.name = '/dev/shm/linkerbench-test'
        with (patch.object(bench.sys, 'platform', 'linux'),
              patch.object(bench.Path, 'read_text', return_value=mount),
              patch.object(bench.shutil, 'disk_usage', return_value=unittest.mock.Mock(free=8 * 1024**3)),
              patch.object(bench.tempfile, 'TemporaryDirectory', return_value=temporary)):
            with bench.OutputStorage(None, 16 * 1024**3) as storage:
                self.assertEqual(storage.prepare(300 * 1024**2), Path(temporary.name))
                self.assertEqual(storage.capacity, 896 * 1024**2)
            temporary.cleanup.assert_called_once()

    def test_external_output_guard(self):
        with tempfile.TemporaryDirectory() as root:
            argv = ['bench.py', '--output-dir', root, '--linker', 'fake']
            with (patch.object(bench.sys, 'argv', argv),
                  patch.object(bench.sys, 'platform', 'darwin'),
                  patch.object(bench, 'internal_volume', return_value=False)):
                with self.assertRaisesRegex(SystemExit, 'allow-external-output'):
                    bench.main()


if __name__ == '__main__':
    unittest.main()
