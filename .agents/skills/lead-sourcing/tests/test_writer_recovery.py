"""Real process interruption must preserve saved state and release its writer."""
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import record_route
from run_coordination import locked

SLOW_FINALIZER = '''import sys,time
from pathlib import Path
sys.path.insert(0, SCRIPTS)
import run_attempt

def preflight(path, document):
    Path(str(path)+'.ready').write_text('locked')
    print('locked',flush=True)
    time.sleep(60)
    raise AssertionError('test finalizer should have been terminated')
run_attempt.delivery_preflight = preflight
run_attempt.finalize_run(Path(sys.argv[1]))
'''


class FinalizationRecoveryTests(unittest.TestCase):
    def test_terminated_and_killed_finalizer_preserves_results_and_releases_ownership(self):
        for method in ('terminate', 'kill'):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'results.json'
                original = b'{"accepted": [], "preserved": true}\n'
                path.write_bytes(original)
                child = subprocess.Popen([sys.executable, '-c',
                    SLOW_FINALIZER.replace('SCRIPTS', repr(str(SCRIPTS))), str(path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    with selectors.DefaultSelector() as ready:
                        ready.register(child.stdout, selectors.EVENT_READ)
                        self.assertTrue(ready.select(20), 'finalizer did not acquire its lock')
                    self.assertEqual(child.stdout.readline().strip(), 'locked')
                    # An active owner cannot be bypassed by another writer.
                    with self.assertRaises(OSError):
                        with locked(path, blocking=False):
                            record_route.mutate(path, lambda document: {'overwritten': True})
                    self.assertEqual(path.read_bytes(), original)
                    with self.assertRaises(FileExistsError):
                        fd = os.open(path.with_name(path.name + '.lock'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        os.close(fd)
                    getattr(child, method)()
                    child.communicate(timeout=10)
                    self.assertNotEqual(child.returncode, 0)
                    self.assertEqual(path.read_bytes(), original)
                    self.assertTrue(os.path.samestat(path.with_name(path.name + '.lock').stat(),
                                                    path.with_name(path.name + '.write.lock').stat()))
                    record_route.mutate(path, lambda document: {**document, 'resumed': True})
                    self.assertEqual(json.loads(path.read_text()),
                                     {'accepted': [], 'preserved': True, 'resumed': True})
                    self.assertTrue(path.with_name(path.name + '.write.lock').exists())
                    self.assertFalse(path.with_name(path.name + '.lock').exists())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=10)

    def test_exception_and_external_change_preserve_atomic_write_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'results.json'
            path.write_text('{}')
            def concurrent_edit(document):
                path.write_text('{"external": true}')
                return {'lost': True}
            with self.assertRaisesRegex(OSError, 'results changed'):
                record_route.mutate(path, concurrent_edit)
            self.assertEqual(json.loads(path.read_text()), {'external': True})
            record_route.mutate(path, lambda document: document)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink check')
    def test_lock_symlink_is_rejected_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'results.json'
            path.write_text('{}')
            target = Path(directory) / 'unrelated'
            target.write_text('preserve')
            path.with_name(path.name + '.write.lock').symlink_to(target)
            with self.assertRaises(OSError):
                record_route.mutate(path, lambda document: document)
            self.assertEqual(target.read_text(), 'preserve')

    @unittest.skipUnless(os.name == 'posix', 'POSIX executable test wrapper')
    def test_real_exporter_timeout_preserves_stage_and_reaps_locked_finalizer(self):
        node = os.environ.get('TYCHE_WORKSPACE_NODE') or shutil.which('node')
        if not node:
            self.skipTest('Node runtime required')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'results.json'
            original = b'{"preserved": true}\n'
            path.write_bytes(original)
            wrapper = root / 'slow-python'
            program = SLOW_FINALIZER.replace('SCRIPTS', repr(str(SCRIPTS)))
            # The exporter supplies script path, results path, --finalize.
            wrapper.write_text('#!' + sys.executable + '\nimport sys\nsys.argv.pop(1)\n' + program)
            wrapper.chmod(0o700)
            preload = root / 'short-timeout.cjs'
            preload.write_text('const cp=require("node:child_process");\n'
                'const original=cp.spawnSync;\n'
                'cp.spawnSync=(command,args,options)=>original(command,args,\n'
                '  {...options,timeout:args.includes("--finalize")?5000:options.timeout});\n'
                'require("node:module").syncBuiltinESMExports();\n')
            env = {**os.environ, 'TYCHE_WORKSPACE_PYTHON': str(wrapper),
                   'TYCHE_WORKSPACE_NODE_MODULES': str(root)}
            result = subprocess.run([node, '--require', str(preload),
                str(SCRIPTS / 'export_xlsx.mjs'), str(path)], env=env,
                capture_output=True, text=True, timeout=25)
            self.assertTrue(Path(str(path)+'.ready').exists(), result.stderr)
            self.assertEqual(result.returncode, 2, result.stdout)
            failure = json.loads(result.stderr.strip().splitlines()[-1])
            self.assertEqual(failure['failure_kind'], 'export_timeout')
            self.assertEqual(failure['stage'], 'finalization')
            self.assertIn('ETIMEDOUT', failure['error'])
            self.assertEqual(path.read_bytes(), original)
            record_route.mutate(path, lambda document: {**document, 'resumed': True})
            self.assertTrue(json.loads(path.read_text())['resumed'])
            self.assertFalse((root / 'leads.xlsx').exists())


if __name__ == '__main__':
    unittest.main()
