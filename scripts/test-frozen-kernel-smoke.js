'use strict';

/**
 * Exercise the separately frozen Variant1Kernel through the real host manager.
 *
 * The smoke covers the managed Python runtime without ambient Python, exact
 * data.v1 imports, and pandas/Arrow
 * capsule persistence.  Source-mode tests cannot prove these package layouts.
 * ``--astb`` selects the broader release-qualification board for persistent
 * state, mount fencing, continuity, and both legal mutation shapes.
 */
const {execFileSync} = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {randomUUID} = require('crypto');

const root = path.join(__dirname, '..');
const backend = path.join(root, 'backend');
const isWin = process.platform === 'win32';
const python = isWin
  ? path.join(backend, '.venv', 'Scripts', 'python.exe')
  : path.join(backend, '.venv', 'bin', 'python');
const executable = path.join(
  backend,
  'dist',
  'Variant1Kernel',
  isWin ? 'Variant1Kernel.exe' : 'Variant1Kernel',
);
const scratch = path.join(
  os.tmpdir(),
  `variant1-frozen-kernel-smoke-${randomUUID()}`,
);

if (!fs.existsSync(python)) {
  throw new Error('backend venv is missing; run `npm run setup:backend`');
}
if (!fs.existsSync(executable)) {
  throw new Error(`frozen kernel is missing: ${executable}`);
}

const forbiddenRuntimeNames = new Set([
  'kokoro_onnx', 'phonemizer', 'espeakng_loader', 'onnxruntime', 'neutts', 'kittentts', 'piper', 'soundfile',
  'IPython', 'ipykernel', 'jupyter_client', 'jupyter_core', 'zmq',
  'debugpy', '_pydevd_bundle', 'traitlets', 'prompt_toolkit', 'jedi', 'parso',
]);
const pendingRuntimePaths = [path.dirname(executable)];
if (fs.existsSync(path.join(path.dirname(executable), '_internal/pyarrow/tests'))) {
  throw new Error('Arrow regression datasets must not ship inside the kernel');
}
while (pendingRuntimePaths.length) {
  const current = pendingRuntimePaths.pop();
  for (const entry of fs.readdirSync(current, {withFileTypes: true})) {
    if (forbiddenRuntimeNames.has(entry.name)) {
      throw new Error(`retired Jupyter/debugger payload remains: ${path.join(current, entry.name)}`);
    }
    if (entry.isDirectory()) pendingRuntimePaths.push(path.join(current, entry.name));
  }
}
fs.mkdirSync(scratch, {recursive: false});
fs.mkdirSync(path.join(scratch, 'tmp'));

const packagingTests = [
  'tests/test_kernel_runtime.py::test_frozen_worker_has_no_system_python_dependency',
  'tests/test_kernel_runtime.py::test_data_runtime_profile_is_enforced_before_model_code',
  'tests/test_kernel_runtime.py::test_host_capsule_round_trips_pandas_through_scoped_cas',
];
const astbTests = [
  'tests/test_kernel_runtime.py::test_persistent_kernel_executes_state_and_typed_read_proxy',
  'tests/test_session_catalog.py::test_kernel_mount_sync_retains_acquired_proxies_and_cross_chat_routing',
  'tests/test_kernel_runtime.py::test_per_chat_continuity_policy_survives_manager_restart_and_restores',
  'tests/test_mutation.py::test_disposable_worker_has_same_user_python_and_routes_normal_proxy',
  'tests/test_mutation.py::test_mutate_live_remount_probation_and_reset',
  'tests/test_mutation.py::test_atomic_model_api_remounts_inside_the_same_cell',
  'tests/test_mutation.py::test_atomic_create_requires_tests_and_activates_one_vacancy',
  'tests/test_mutation.py::test_off_hides_authoring_keeps_overlay_live_and_pauses_probation',
];
const tests = process.argv.includes('--astb') ? astbTests : packagingTests;

try {
  execFileSync(
    python,
    [
      '-m', 'pytest', '-q', ...tests,
      '-p', 'no:cacheprovider',
      '--basetemp', path.join(scratch, 'pytest'),
    ],
    {
      cwd: backend,
      env: {
        ...process.env,
        VARIANT1_TEST_KERNEL_EXE: executable,
        TMP: path.join(scratch, 'tmp'),
        TEMP: path.join(scratch, 'tmp'),
      },
      stdio: 'inherit',
    },
  );
} finally {
  fs.rmSync(scratch, {recursive: true, force: true, maxRetries: 3, retryDelay: 100});
}
