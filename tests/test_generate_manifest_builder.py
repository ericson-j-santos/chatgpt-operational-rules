"""Isolated tests for the Builder's safe MANIFEST staging; no user host touched."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/generate_manifest.py'
WRAPPER = SOURCE.with_name('builder_manifest_stage.py')


class ManifestBuilderStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        shutil.copyfile(SOURCE, self.root / 'scripts/generate_manifest.py')
        shutil.copyfile(WRAPPER, self.root / 'scripts/builder_manifest_stage.py')
        (self.root / 'scripts/validate_rules.py').write_text(
            'REQUIRED_PATHS = set()\n'
            'if __name__ == "__main__":\n'
            '    import hashlib, json, pathlib, sys\n'
            '    root = pathlib.Path(__file__).resolve().parents[1]\n'
            '    data = json.loads((root / "MANIFEST.json").read_text())\n'
            '    for item in data["files"]:\n'
            '        payload = (root / item["path"]).read_bytes()\n'
            '        assert item["sha256"] == hashlib.sha256(payload).hexdigest()\n'
            '        assert item["size"] == len(payload)\n',
            encoding='utf-8',
        )
        (self.root / 'README.md').write_text('Versão: 1.6.21\n', encoding='utf-8')
        (self.root / 'MANIFEST.json').write_text('{"version":"1.6.21","generated_at":"2026-01-01T00:00:00Z","files":[]}\n', encoding='utf-8')
        (self.root / 'code.py').write_text('print("a")\n', encoding='utf-8')
        self.git('init', '-q')
        self.git('add', '--all')
        self.git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','initial')
        # Baseline manifest needn't be correct yet; all positive cases regenerate.

    def git(self, *args, check=True):
        return subprocess.run(['git','-C',str(self.root),*args], check=check, text=True, capture_output=True)

    def builder(self):
        return subprocess.run([sys.executable, str(self.root/'scripts/generate_manifest.py'),'--stage-for-builder'], cwd=self.root, text=True, capture_output=True)

    def wrapper(self, *extra_args):
        return subprocess.run(
            [sys.executable, str(self.root/'scripts/builder_manifest_stage.py'), *extra_args],
            cwd=self.root, text=True, capture_output=True,
        )

    def test_source_and_manifest_staged_in_one_index(self):
        (self.root/'code.py').write_text('print("b")\n')
        self.git('add','code.py')
        run=self.builder()
        self.assertEqual(run.returncode, 0, run.stderr)
        paths=set(self.git('diff','--cached','--name-only').stdout.splitlines())
        self.assertEqual(paths, {'code.py','MANIFEST.json'})
        b=self.git('show',':MANIFEST.json').stdout.encode()
        self.assertEqual(b,(self.root/'MANIFEST.json').read_bytes())
        manifest=json.loads(b)
        entry=next(z for z in manifest['files'] if z['path']=='code.py')
        self.assertEqual(entry['sha256'],hashlib.sha256((self.root/'code.py').read_bytes()).hexdigest())
        self.assertEqual(entry['size'],len((self.root/'code.py').read_bytes()))
        self.assertFalse(self.git('diff','--name-only').stdout)
        # Repeating same stage is byte-for-byte idempotent.
        second=self.builder()
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertEqual(b,self.git('show',':MANIFEST.json').stdout.encode())

    def test_new_file_included_only_after_git_add(self):
        (self.root/'new.py').write_text('print(1)\n')
        self.git('add','new.py')
        r=self.builder();self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('new.py',{x['path'] for x in json.loads((self.root/'MANIFEST.json').read_text())['files']})

    def test_unstaged_source_blocks_without_staging_manifest(self):
        (self.root/'code.py').write_text('print("b")\n')
        self.git('add','code.py')
        (self.root/'code.py').write_text('print("c")\n')
        old=(self.root/'MANIFEST.json').read_bytes()
        r=self.builder()
        self.assertNotEqual(r.returncode,0)
        self.assertIn('UNSTAGED_MODIFICATIONS',r.stderr)
        self.assertEqual(old,(self.root/'MANIFEST.json').read_bytes())
        self.assertNotIn('MANIFEST.json',self.git('diff','--cached','--name-only').stdout)

    def test_untracked_file_blocks(self):
        (self.root/'code.py').write_text('print("b")\n')
        self.git('add','code.py')
        (self.root/'untracked.txt').write_text('no')
        r=self.builder()
        self.assertNotEqual(r.returncode,0)
        self.assertIn('UNTRACKED_FILES',r.stderr)

    def test_unstaged_only_blocks_without_manifest_change(self):
        (self.root/'code.py').write_text('print("b")\n')
        before=(self.root/'MANIFEST.json').read_bytes()
        r=self.builder()
        self.assertNotEqual(r.returncode,0)
        self.assertEqual(before,(self.root/'MANIFEST.json').read_bytes())

    def test_no_source_staged_blocks(self):
        r=self.builder()
        self.assertNotEqual(r.returncode,0)
        self.assertIn('NO_SOURCE_STAGED',r.stderr)

    def test_staged_symlink_is_rejected_without_staging_manifest(self):
        target = self.root / 'external.txt'
        target.write_text('external', encoding='utf-8')
        link = self.root / 'symlink.txt'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink unavailable on this test platform')
        self.git('add','symlink.txt')
        # Ignore the external target, which is deliberately not staged.
        target.unlink()
        run = self.builder()
        self.assertNotEqual(run.returncode,0)
        self.assertNotIn('MANIFEST.json',self.git('diff','--cached','--name-only').stdout)

    def test_validation_failure_does_not_stage_manifest(self):
        (self.root / 'code.py').write_text('print("b")\n')
        self.git('add','code.py')
        validator = self.root / 'scripts/validate_rules.py'
        validator.write_text('REQUIRED_PATHS = set()\nif __name__ == "__main__": raise SystemExit(1)\n')
        self.git('add','scripts/validate_rules.py')
        run = self.builder()
        self.assertNotEqual(run.returncode,0)
        self.assertIn('BUILDER_MANIFEST_VALIDATION_FAILED',run.stderr)
        self.assertNotIn('MANIFEST.json',self.git('diff','--cached','--name-only').stdout)


    def test_pinned_wrapper_stages_manifest_with_existing_two_arg_python_contract(self):
        (self.root/'code.py').write_text('print("b")\n')
        self.git('add','code.py')
        run=self.wrapper()
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('BUILDER_MANIFEST_STAGED',run.stdout)
        self.assertEqual(set(self.git('diff','--cached','--name-only').stdout.splitlines()),
                         {'code.py','MANIFEST.json'})
        self.assertEqual((self.root/'MANIFEST.json').read_bytes(),
                         self.git('show',':MANIFEST.json').stdout.encode())

    def test_pinned_wrapper_rejects_caller_arguments_before_mutation(self):
        (self.root/'code.py').write_text('print("b")\n')
        self.git('add','code.py')
        previous=(self.root/'MANIFEST.json').read_bytes()
        run=self.wrapper('--mode','apply')
        self.assertNotEqual(run.returncode,0)
        self.assertIn('BUILDER_MANIFEST_ARGUMENTS_FORBIDDEN',run.stderr)
        self.assertEqual(previous,(self.root/'MANIFEST.json').read_bytes())
        self.assertNotIn('MANIFEST.json',self.git('diff','--cached','--name-only').stdout)

    def test_pinned_wrapper_rejects_changed_generator_even_if_staged(self):
        (self.root/'code.py').write_text('print("b")\n')
        (self.root/'scripts/generate_manifest.py').write_text(
            (self.root/'scripts/generate_manifest.py').read_text()+'# change\n'
        )
        self.git('add','code.py','scripts/generate_manifest.py')
        previous=(self.root/'MANIFEST.json').read_bytes()
        run=self.wrapper()
        self.assertNotEqual(run.returncode,0)
        self.assertIn('BUILDER_MANIFEST_SOURCE_HASH_MISMATCH',run.stderr)
        self.assertEqual(previous,(self.root/'MANIFEST.json').read_bytes())
        self.assertNotIn('MANIFEST.json',self.git('diff','--cached','--name-only').stdout)


if __name__=='__main__':
    unittest.main()
