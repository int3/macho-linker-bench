"""Small real streaming fixtures: no network or large captures needed."""
import argparse
import contextlib
import csv
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench
import fetch


class Streaming(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.dest = self.root / 'corpora'
        source = self.root / 'input'
        source.mkdir()
        (source / 'response.txt').write_text('test capture\n')
        self.raw = subprocess.check_output(['tar', '-cf', '-', '-C', str(source), '.'])
        self.make_assets(self.raw)
        self.patch = patch.object(fetch, 'HERE', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def make_assets(self, raw):
        compressed = subprocess.check_output(['zstd', '-q', '-c'], input=raw)
        parts = [compressed[:len(compressed)//2], compressed[len(compressed)//2:]]
        self.rows = []
        for index, data in enumerate(parts):
            part = f'capture.zst.part-{index}'
            (self.assets / part).write_bytes(data)
            self.rows.append(dict(workload='fixture', raw_file='capture.tar', raw_bytes=str(len(raw)),
                raw_sha256=hashlib.sha256(raw).hexdigest(), zstd_file='capture.zst',
                zstd_bytes=str(len(compressed)), zstd_sha256=hashlib.sha256(compressed).hexdigest(),
                part_file=part, part_bytes=str(len(data)), part_sha256=hashlib.sha256(data).hexdigest()))
        self.manifest()

    def manifest(self):
        with (self.root / 'manifest.tsv').open('w') as output:
            writer = csv.DictWriter(output, fieldnames=list(self.rows[0]), delimiter='\t')
            writer.writeheader()
            writer.writerows(self.rows)

    def run_fetch(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            fetch.fetch('fixture', self.dest, extract=True, assets_dir=self.assets, **kwargs)

    def clean_failure(self, message):
        with self.assertRaisesRegex((RuntimeError, BrokenPipeError), message):
            self.run_fetch()
        self.assertFalse((self.dest / 'fixture').exists())
        self.assertFalse(list(self.dest.glob('.fetch-*')))
        self.assertFalse((self.dest / 'capture.tar').exists())
        self.assertFalse((self.dest / 'capture.zst').exists())

    def test_multipart_and_cached_repeat(self):
        self.run_fetch()
        self.assertEqual((self.dest / 'fixture/response.txt').read_text(), 'test capture\n')
        self.assertEqual({p.name for p in self.dest.iterdir()}, {'fixture', *[r['part_file'] for r in self.rows]})
        import shutil
        shutil.rmtree(self.dest / 'fixture')
        with patch.object(fetch.shutil, 'copyfile', side_effect=AssertionError('must use cached parts')):
            self.run_fetch()

    def test_bad_part(self):
        (self.assets / self.rows[0]['part_file']).write_bytes(b'corrupted')
        self.clean_failure('Part checksum/size mismatch')

    def test_bad_compressed_hash(self):
        for row in self.rows:
            row['zstd_sha256'] = '0' * 64
        self.manifest()
        self.clean_failure('Compressed checksum/size mismatch')

    def test_bad_raw_hash(self):
        for row in self.rows:
            row['raw_sha256'] = '0' * 64
        self.manifest()
        self.clean_failure('Raw capture checksum/size mismatch')

    def test_bad_raw_count(self):
        for row in self.rows:
            row['raw_bytes'] = str(len(self.raw) + 1)
        self.manifest()
        self.clean_failure('Raw capture checksum/size mismatch')

    def test_tar_failure(self):
        self.make_assets(b'this is not a tar archive\n')
        self.clean_failure('extraction failed|Broken pipe')

    def test_keep_tar_and_no_cache(self):
        self.run_fetch(keep_tar=True, no_cache=True)
        self.assertEqual((self.dest / 'capture.tar').read_bytes(), self.raw)
        self.assertEqual({p.name for p in self.dest.iterdir()}, {'fixture', 'capture.tar'})

    def test_keep_existing_valid_tar(self):
        self.dest.mkdir()
        original = self.dest / 'capture.tar'
        original.write_bytes(self.raw)
        inode = original.stat().st_ino
        self.run_fetch(keep_tar=True)
        self.assertEqual(original.stat().st_ino, inode)
        self.assertEqual(original.read_bytes(), self.raw)

    def test_keep_existing_unknown_tar(self):
        self.dest.mkdir()
        original = self.dest / 'capture.tar'
        original.write_bytes(b'preserve this capture')
        with self.assertRaisesRegex(RuntimeError, 'Refusing to overwrite existing raw capture'):
            self.run_fetch(keep_tar=True)
        self.assertEqual(original.read_bytes(), b'preserve this capture')
        self.assertEqual(list(self.dest.iterdir()), [original])

    def test_never_replace_existing_extraction(self):
        self.dest.mkdir()
        (self.dest / 'fixture').mkdir()
        (self.dest / 'fixture/keep').write_text('mine')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.run_fetch()
        self.assertEqual((self.dest / 'fixture/keep').read_text(), 'mine')

    def test_disk_refusal_and_override(self):
        self.dest.mkdir()
        args = argparse.Namespace(keep=False, no_cache=False, ignore_disk_space=False)
        with patch.object(bench.shutil, 'disk_usage', return_value=argparse.Namespace(free=0)):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'insufficient free space'):
                    bench.disk_preflight(self.dest, ['fixture'], fetch.catalog(), args)
                args.ignore_disk_space = True
                bench.disk_preflight(self.dest, ['fixture'], fetch.catalog(), args)

    def test_disk_refusal_does_not_claim_output_directory(self):
        executable = self.root / 'fake-lld'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        out = self.root / 'report'

        def metadata(argv):
            if 'hw.model' in argv:
                return 'test Mac\ntest CPU\n10\n10\n17179869184'
            if 'hw.memsize' in argv:
                return '17179869184'
            return 'test version'

        argv = ['bench.py', '--linker', str(executable), '--workloads', 'fixture',
                '--corpora', str(self.dest), '--out', str(out), '--samples', '2']
        with (patch.object(sys, 'argv', argv),
              patch.object(bench.shutil, 'which', return_value=str(executable)),
              patch.object(bench, 'command', side_effect=metadata),
              patch.object(bench.shutil, 'disk_usage', return_value=argparse.Namespace(free=0)),
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(RuntimeError, 'insufficient free space'):
                bench.main()
        self.assertFalse(out.exists())

    def test_resume_rejects_modified_samples(self):
        directory = self.root / 'result'
        directory.mkdir()
        raw = 'benchmark,wall_time,cpu_time,max_rss_mib\nmine,1,1,1\nmine,1,1,1\n'
        (directory / 'samples.csv').write_text(raw)
        bench.save_json(directory / 'complete.json', dict(cells={'mine': 'No samples'}, note='', sampled=['mine'],
                        samples_sha256=hashlib.sha256(raw.encode()).hexdigest()))
        args = argparse.Namespace(time=None, samples=2)
        self.assertIsNotNone(bench.completed(directory, ['mine'], args))
        args.samples = 3
        self.assertIsNone(bench.completed(directory, ['mine'], args))
        (directory / 'samples.csv').write_text(raw + 'mine,2,2,2\n')
        with self.assertRaisesRegex(RuntimeError, 'saved samples changed'):
            bench.completed(directory, ['mine'], args)


class PartialSamples(unittest.TestCase):
    header = 'benchmark,cpu_time,max_rss_mib,wall_time\n'
    row = 'mine,1,1,1\n'

    def run_sample(self, prior, incoming=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.sample_directory = directory
        (directory / 'samples.csv').write_text(prior)
        process = unittest.mock.Mock()
        process.stdout = io.StringIO(self.header + self.row * 3 if incoming is None else incoming)
        process.returncode = 0
        process.wait.return_value = 0
        args = argparse.Namespace(time=None, samples=5, link_timeout=1)
        with patch.object(bench.subprocess, 'Popen', return_value=process) as spawn, contextlib.redirect_stdout(io.StringIO()):
            raw = bench.sample('cbdr', {'mine': {}}, args, directory)
        self.spawn_calls = spawn.call_count
        return directory, raw

    def test_resume_partial_counts(self):
        prior = self.header + self.row * 3
        directory, raw = self.run_sample(prior)
        self.assertEqual(raw, self.header + self.row * 5)
        backups = list(directory.glob('samples-before-resume-*.csv'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), prior)

    def test_resume_discards_only_torn_final_row(self):
        prior = self.header + self.row * 3 + 'mine,0.2,'
        directory, raw = self.run_sample(prior)
        self.assertEqual(raw, self.header + self.row * 5)
        self.assertEqual(next(directory.glob('samples-before-resume-*.csv')).read_text(), prior)

    def test_resume_complete_csv(self):
        prior = self.header + self.row * 5
        directory, raw = self.run_sample(prior, incoming='')
        self.assertEqual(raw, prior)
        self.assertEqual(self.spawn_calls, 0)

    def test_changed_header_preserves_partial_samples(self):
        prior = self.header + self.row * 3
        with self.assertRaisesRegex(RuntimeError, 'CSV header changed'):
            self.run_sample(prior, incoming='benchmark,wall_time,cpu_time,max_rss_mib\n')
        self.assertEqual((self.sample_directory / 'samples.csv').read_text(), prior)


if __name__ == '__main__':
    unittest.main()
