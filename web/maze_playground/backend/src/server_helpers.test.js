import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

process.env.MAZE_PLAYGROUND_NO_LISTEN = '1';
process.env.PYTHON_BIN = process.execPath;

const {
  publicClusterQueues,
  saveWorkspaceTaskSource,
  server,
  writeTextAtomic,
} = await import('./server.js');

async function temporaryDirectory(t) {
  const directory = await fs.mkdtemp(path.join(tmpdir(), 'maze-playground-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

test('cluster queues inspect only current runs and fail closed for private or unknown runs', async () => {
  const coreResponse = {
    status: 'success',
    queues: {
      stopped_workflow_ids: ['old-public', 'old-gaia'],
      ready_tasks: [{ workflow_id: 'public-run', task_id: 'ready' }],
      pending_tasks: [{ workflow_id: 'unknown-run', task_id: 'pending' }],
      retrying_tasks: [],
      running_tasks: [{ workflow_id: 'dynamic-gaia-run', task_id: 'running' }],
      queues: {
        cpu: { tasks: [{ workflow_id: 'public-run', task_id: 'ready' }] },
        gpu: { tasks: [{ workflow_id: 'dynamic-gaia-run', task_id: 'running' }] },
      },
    },
  };
  const loaded = [];
  const result = await publicClusterQueues(coreResponse, async (runId) => {
    loaded.push(runId);
    if (runId === 'unknown-run') throw new Error('run lookup failed');
    return {
      kind: runId === 'dynamic-gaia-run' ? 'dynamic' : 'static',
      metadata: { benchmark: runId === 'dynamic-gaia-run' ? 'gaia' : 'other' },
    };
  });

  assert.deepEqual(loaded.sort(), ['dynamic-gaia-run', 'public-run', 'unknown-run']);
  assert.equal(Object.hasOwn(result.queues, 'stopped_workflow_ids'), false);
  assert.deepEqual(coreResponse.queues.stopped_workflow_ids, ['old-public', 'old-gaia']);
  assert.equal(result.queues.ready_tasks[0].workflow_id, 'public-run');
  assert.match(result.queues.pending_tasks[0].workflow_id, /^gaia-[a-f0-9]{32}$/);
  assert.match(result.queues.running_tasks[0].workflow_id, /^gaia-[a-f0-9]{32}$/);
  assert.equal(
    result.queues.running_tasks[0].workflow_id,
    result.queues.queues.gpu.tasks[0].workflow_id,
  );
});

test('parse-free workspace saves are atomic, allow empty drafts, and preserve file mode', async (t) => {
  const workspaceDir = await temporaryDirectory(t);
  await fs.mkdir(path.join(workspaceDir, 'tasks'));
  const target = path.join(workspaceDir, 'tasks', 'draft.py');
  await fs.writeFile(target, 'old', { mode: 0o600 });
  await fs.chmod(target, 0o600);

  const result = await saveWorkspaceTaskSource(workspaceDir, 'tasks/draft.py', '');

  assert.deepEqual(result, {
    success: true,
    workspaceDir,
    tasksDir: path.join(workspaceDir, 'tasks'),
    relativePath: 'tasks/draft.py',
  });
  assert.equal(await fs.readFile(target, 'utf8'), '');
  assert.equal((await fs.stat(target)).mode & 0o777, 0o600);
});

test('concurrent saves to one task path finish in request order', async (t) => {
  const workspaceDir = await temporaryDirectory(t);
  await fs.mkdir(path.join(workspaceDir, 'tasks'));
  const target = path.join(workspaceDir, 'tasks', 'ordered.py');
  const originalWriteFile = fs.writeFile;
  let releaseFirstWrite;
  const firstWriteBlocked = new Promise((resolve) => {
    releaseFirstWrite = resolve;
  });
  let markFirstWriteStarted;
  const firstWriteStarted = new Promise((resolve) => {
    markFirstWriteStarted = resolve;
  });
  let markSecondWriteStarted;
  const secondWriteStarted = new Promise((resolve) => {
    markSecondWriteStarted = resolve;
  });
  let firstSave;
  let secondSave;

  try {
    fs.writeFile = async (filePath, content, options) => {
      if (String(filePath).startsWith(`${target}.`) && content === 'first') {
        markFirstWriteStarted();
        await firstWriteBlocked;
      }
      if (String(filePath).startsWith(`${target}.`) && content === 'second') {
        markSecondWriteStarted();
      }
      return originalWriteFile.call(fs, filePath, content, options);
    };

    firstSave = saveWorkspaceTaskSource(workspaceDir, 'tasks/ordered.py', 'first');
    await firstWriteStarted;
    secondSave = saveWorkspaceTaskSource(workspaceDir, 'tasks/ordered.py', 'second');
    const secondOvertookFirst = await Promise.race([
      secondWriteStarted.then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), 100)),
    ]);
    assert.equal(secondOvertookFirst, false);
    releaseFirstWrite();
    await Promise.all([firstSave, secondSave]);
  } finally {
    releaseFirstWrite();
    await Promise.allSettled([firstSave, secondSave].filter(Boolean));
    fs.writeFile = originalWriteFile;
  }

  assert.equal(await fs.readFile(target, 'utf8'), 'second');
});

test('workspace task endpoint bypasses Python for parse false and records the mutation', async (t) => {
  const workspaceDir = await temporaryDirectory(t);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  t.after(() => new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  }));
  const { port } = server.address();
  const response = await fetch(`http://127.0.0.1:${port}/api/workspace-tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      workspaceDir,
      relativePath: 'tasks/draft.py',
      code: '',
      parse: false,
    }),
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    success: true,
    workspaceDir,
    tasksDir: path.join(workspaceDir, 'tasks'),
    relativePath: 'tasks/draft.py',
    workspaceId: path.basename(workspaceDir),
    workspaceManifestVersion: 2,
  });
  assert.equal(await fs.readFile(path.join(workspaceDir, 'tasks', 'draft.py'), 'utf8'), '');
  const manifest = JSON.parse(await fs.readFile(path.join(workspaceDir, 'workspace.json'), 'utf8'));
  assert.equal(manifest.manifest_version, 2);
  assert.equal(manifest.last_change.type, 'task_saved');
  assert.equal(manifest.last_change.path, 'tasks/draft.py');

  const originalConsoleError = console.error;
  let parseResponse;
  let parseError;
  try {
    console.error = () => {};
    parseResponse = await fetch(`http://127.0.0.1:${port}/api/workspace-tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workspaceDir,
        relativePath: 'tasks/parsed.py',
        code: 'from maze import task\n@task\ndef parsed(): return {"ok": True}\n',
        parse: true,
      }),
    });
    parseError = (await parseResponse.json()).error;
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    console.error = originalConsoleError;
  }
  assert.equal(parseResponse.status, 500);
  assert.match(parseError, /Python执行失败/);
});

test('parse-free workspace saves reject invalid paths and symbolic-link escapes', async (t) => {
  const root = await temporaryDirectory(t);
  const workspaceDir = path.join(root, 'workspace');
  const tasksDir = path.join(workspaceDir, 'tasks');
  const outsideDir = path.join(root, 'outside');
  await fs.mkdir(tasksDir, { recursive: true });
  await fs.mkdir(outsideDir);

  await assert.rejects(
    saveWorkspaceTaskSource(workspaceDir, '../outside.py', 'bad'),
    /POSIX relative path/,
  );
  await assert.rejects(
    saveWorkspaceTaskSource(workspaceDir, '/tmp/outside.py', 'bad'),
    /POSIX relative path/,
  );
  await assert.rejects(
    saveWorkspaceTaskSource(workspaceDir, 'tasks\\outside.py', 'bad'),
    /POSIX relative path/,
  );
  await assert.rejects(
    saveWorkspaceTaskSource(workspaceDir, 'tasks/invalid.py', null),
    /code must be a string/,
  );

  await fs.symlink(outsideDir, path.join(tasksDir, 'linked-dir'));
  await assert.rejects(
    saveWorkspaceTaskSource(workspaceDir, 'tasks/linked-dir/outside.py', 'bad'),
    /parent directories must not be symbolic links/,
  );
  await assert.rejects(fs.access(path.join(outsideDir, 'outside.py')), /ENOENT/);

  const outsideFile = path.join(outsideDir, 'existing.py');
  await fs.writeFile(outsideFile, 'unchanged');
  await fs.symlink(outsideFile, path.join(tasksDir, 'linked-file.py'));
  await assert.rejects(
    saveWorkspaceTaskSource(workspaceDir, 'tasks/linked-file.py', 'bad'),
    /regular file, not a symbolic link/,
  );
  assert.equal(await fs.readFile(outsideFile, 'utf8'), 'unchanged');
});

test('atomic text writes remove temporary files after a late boundary failure', async (t) => {
  const root = await temporaryDirectory(t);
  const allowedDir = path.join(root, 'allowed');
  const actualDir = path.join(root, 'actual');
  await Promise.all([
    fs.mkdir(allowedDir),
    fs.mkdir(actualDir),
  ]);
  const target = path.join(actualDir, 'task.py');

  await assert.rejects(
    writeTextAtomic(target, 'content', { rootDir: allowedDir }),
    /escaped the workspace tasks directory/,
  );
  assert.deepEqual(await fs.readdir(actualDir), []);
});
