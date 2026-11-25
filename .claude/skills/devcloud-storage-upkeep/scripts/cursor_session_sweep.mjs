#!/usr/bin/env node
// Expire local Cursor ACP SDK sessions through the Cursor SDK, with live BB ownership checks.
import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readdirSync, readFileSync, readlinkSync,
  realpathSync, renameSync, rmdirSync, statfsSync, unlinkSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { basename, dirname, isAbsolute, join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

const DAY = 86_400_000;
const AGENT_ID = /^agent-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const SESSION_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const ARCHIVE_ID = new RegExp(`^checkpoint:(${AGENT_ID.source.slice(1, -1)}):${SESSION_ID.source.slice(1, -1)}$`);
const ARCHIVE_FILE = /^[0-9a-f]{64}\.db$/;
const SIDECARS = ['-wal', '-shm', '-journal'];
const TERMINAL_RUN = new Set(['FINISHED', 'ERROR', 'CANCELLED', 'EXPIRED']);
const INACTIVE_THREAD = new Set(['idle', 'error']);
const missing = (error) => ['ENOENT', 'ESRCH'].includes(error.code);
const within = (path, dir) => path === dir || path.startsWith(dir + '/');
const hash = (id) => createHash('sha256').update(id).digest('hex');
const ro = (path) => new DatabaseSync(path, { readOnly: true });
const json = (path) => JSON.parse(readFileSync(path, 'utf8'));

class Keep extends Error {}

function physical(path, optional = false) {
  let info;
  try {
    info = lstatSync(path);
  } catch (error) {
    if (optional && error.code === 'ENOENT') return null;
    throw error;
  }
  if (info.isSymbolicLink() || realpathSync(path) !== path) throw new Keep('symlink or non-canonical path');
  return info;
}

function listing(path, optional = false) {
  if (!physical(path, optional)) return [];
  return readdirSync(path, { withFileTypes: true });
}

function mounts() {
  return readFileSync('/proc/self/mountinfo', 'utf8').trim().split('\n').map((line) =>
    line.split(' ')[4].replace(/\\([0-7]{3})/g, (_, octal) => String.fromCharCode(parseInt(octal, 8))));
}

function mounted(path, root, mountPaths) {
  return mountPaths.some((mount) => within(mount, root) &&
    (within(mount, path) || within(path, mount)));
}

function checkCatalog(state, root, mountPaths) {
  for (const name of ['index.db', 'index.db-wal', 'index.db-shm', 'index.db-journal']) {
    const path = join(state, name);
    if (mounted(path, root, mountPaths)) throw new Keep('mount intersects index');
    const file = physical(path, name !== 'index.db');
    if (file && (!file.isFile() || file.nlink !== 1)) throw new Keep('unexpected SDK index file');
  }
}

function directoryEvidence(path, allowed, required, root, mountPaths) {
  if (mounted(path, root, mountPaths)) throw new Keep('mount intersects session');
  const dir = physical(path, true);
  if (!dir) return { entries: [], mtime: 0, bytes: 0, stamp: null };
  if (!dir.isDirectory()) throw new Keep('unexpected session layout');
  const abs = [dir];
  const entries = readdirSync(path).sort();
  if (required && !entries.includes(required)) throw new Keep('missing session database');
  for (const name of entries) {
    if (!allowed.test(name)) throw new Keep('unexpected session file');
    const file = physical(join(path, name));
    if (!file.isFile() || file.nlink !== 1) throw new Keep('unexpected session file type');
    abs.push(file);
  }
  return {
    entries,
    mtime: Math.max(...abs.map((st) => st.mtimeMs)),
    bytes: abs.reduce((sum, st) => sum + st.blocks * 512, 0),
    stamp: JSON.stringify([entries, abs.map((st) => [st.dev, st.ino, st.mtimeMs, st.ctimeMs, st.size])]),
  };
}

// An unreadable process entry is different from a PID/FD that disappeared.
function processReferences(roots) {
  const paths = [];
  for (const pid of readdirSync('/proc')) {
    if (!/^\d+$/.test(pid) || Number(pid) === process.pid) continue;
    const proc = join('/proc', pid);
    try {
      for (const entry of ['cwd', ...readdirSync(join(proc, 'fd')).map((fd) => 'fd/' + fd)]) {
        try {
          const path = readlinkSync(join(proc, entry)).replace(/ \(deleted\)$/, '');
          if (roots.some((root) => within(path, root))) paths.push(path);
        } catch (error) {
          if (!missing(error)) throw error;
        }
      }
    } catch (error) {
      if (!missing(error)) throw error;
    }
  }
  return paths;
}

function sessionOwners(acp) {
  const owners = new Map();
  for (const project of listing(acp).filter((entry) => entry.isDirectory())) {
    for (const entry of listing(join(acp, project.name))) {
      const session = basename(entry.name, '.jsonl');
      if (!entry.name.endsWith('.jsonl') || !SESSION_ID.test(session)) continue;
      const path = join(acp, project.name, entry.name);
      const before = physical(path);
      if (!before.isFile()) throw new Error('unexpected ACP history file');
      const records = new Set();
      try {
        for (const line of readFileSync(path, 'utf8').split('\n').filter(Boolean)) {
          const row = JSON.parse(line);
          if (row.sessionId !== session ||
            !['session_meta', 'session_checkpoint'].includes(row.type) || !isAbsolute(row.cwd ?? '')) continue;
          const agentId = AGENT_ID.test(row.sdkSessionId) ? row.sdkSessionId :
            row.type === 'session_checkpoint' ? ARCHIVE_ID.exec(row.sdkSessionId)?.[1] : undefined;
          if (!agentId) continue;
          if (row.type === 'session_checkpoint' && row.checkpointId !== row.sdkSessionId) {
            throw new Error('checkpoint ID disagrees with SDK agent ID');
          }
          records.add(JSON.stringify([row.cwd, agentId]));
        }
      } catch (error) {
        throw new Error('cannot validate ACP history ' + path, { cause: error });
      }
      const after = physical(path);
      if (after.ino !== before.ino || after.size !== before.size || after.ctimeMs !== before.ctimeMs) {
        throw new Error('ACP history changed during inventory: ' + path);
      }
      for (const key of records) {
        const claims = owners.get(key) ?? [];
        claims.push({ session, path, stat: after });
        owners.set(key, claims);
      }
    }
  }
  return owners;
}

function threadEvidence(bb, claims, cutoff) {
  const ids = new Set();
  for (const claim of claims ?? []) {
    const history = physical(claim.path);
    if (history.dev !== claim.stat.dev || history.ino !== claim.stat.ino ||
      history.ctimeMs !== claim.stat.ctimeMs) throw new Keep('ACP history changed');
    if (history.mtimeMs > cutoff) throw new Keep('recent ACP history');
    const threads = bb.prepare(`
      SELECT DISTINCT t.id, t.provider_id, t.status, t.archived_at, t.updated_at, t.status_changed_at,
        EXISTS(SELECT 1 FROM queued_thread_messages q WHERE q.thread_id=t.id) AS queued,
        EXISTS(SELECT 1 FROM pending_interactions p WHERE p.thread_id=t.id AND p.resolved_at IS NULL) AS interaction
      FROM events e JOIN threads t ON t.id=e.thread_id
      WHERE e.type='thread/identity' AND e.scope_kind='thread'
        AND e.provider_thread_id=? AND t.deleted_at IS NULL
    `).all(claim.session);
    if (!threads.length) throw new Keep('BB identity unknown');
    for (const thread of threads) {
      if (thread.provider_id !== 'acp-cursor-sdk') throw new Keep('unexpected BB provider');
      if (!INACTIVE_THREAD.has(thread.status) || thread.queued || thread.interaction) {
        throw new Keep('BB thread busy');
      }
      if (thread.archived_at === null &&
        (!Number.isFinite(thread.updated_at) || Math.max(thread.updated_at, thread.status_changed_at) > cutoff)) {
        throw new Keep('recent BB thread');
      }
      ids.add(thread.id);
    }
  }
  if (!ids.size) throw new Keep('ACP ownership unknown');
  return [...ids].sort();
}

function check({ root, state, index, bb, owners, cutoff }, id, refs, mountPaths) {
  const row = index.prepare('SELECT * FROM agents WHERE agent_id=?').get(id);
  if (!row) throw new Keep('index row disappeared');
  if (!['IDLE', 'ARCHIVED'].includes(row.status) || row.active_run_id !== null) {
    throw new Keep('SDK agent busy');
  }
  const dates = [row.created_at, row.updated_at];
  const runs = index.prepare(`
    SELECT r.*, (SELECT max(e.created_at) FROM run_events e WHERE e.run_id=r.run_id) AS last_event
    FROM runs r WHERE r.agent_id=?`).all(id);
  for (const run of runs) {
    if (!TERMINAL_RUN.has(run.status)) throw new Keep('SDK run busy');
    dates.push(run.created_at, run.updated_at, run.last_event ?? run.updated_at);
  }
  const stamps = dates.map(Date.parse);
  if (stamps.some((stamp) => !Number.isFinite(stamp))) throw new Keep('unknown SDK record date');
  if (stamps.some((stamp) => stamp > cutoff)) throw new Keep('recent SDK index record');

  const agent = join(state, 'agents', 'agent-' + hash(id));
  const owner = join(state, 'acp-run-owners', hash(id));
  const claims = owners.get(JSON.stringify([row.workspace_ref, id]));
  const threads = threadEvidence(bb, claims, cutoff);
  if (refs.some((path) => within(path, agent) || within(path, owner) ||
    claims.some((claim) => path === claim.path) || path === state ||
    (dirname(path) === state && /^index\.db(?:-(?:wal|shm|journal))?$/.test(basename(path))))) {
    throw new Keep('live cwd or file descriptor');
  }
  const data = directoryEvidence(agent, /^store\.db(?:-(?:wal|shm|journal))?$/, 'store.db', root, mountPaths);
  const lock = directoryEvidence(owner, /^(?:lock\.db|run\.json)$/, 'lock.db', root, mountPaths);
  if (Math.max(data.mtime, lock.mtime) > cutoff) throw new Keep('recent session files');
  if (lock.entries.includes('run.json')) {
    const record = json(join(owner, 'run.json'));
    if (record.agentId !== id || typeof record.runId !== 'string' ||
      (runs.length && !runs.some((run) => run.run_id === record.runId))) {
      throw new Keep('ACP run owner disagrees with index');
    }
  }
  return { agent_id: id, path: agent, threads, workspace: row.workspace_ref,
    bytes: data.bytes + lock.bytes, owner, lock, data, runs: runs.map((run) => run.run_id) };
}

function sdk(path) {
  const packageFile = join(path, 'package.json');
  const pkg = json(packageFile);
  if (pkg.name !== '@cursor/sdk') throw new Error('expected installed @cursor/sdk package');
  const load = createRequire(packageFile);
  const { Agent } = load(join(path, 'dist/cjs/index.js'));
  const { SqliteLocalAgentStore } = load(join(path, 'dist/cjs/sqlite.js'));
  if (typeof Agent?.delete !== 'function' || typeof SqliteLocalAgentStore?.open !== 'function') {
    throw new Error('SDK local delete API unavailable');
  }
  return { Agent, SqliteLocalAgentStore, version: pkg.version };
}

async function erase(context, plan, currentRefs, cursor) {
  const { state, index } = context;
  const storage = await cursor.SqliteLocalAgentStore.open({ workspaceRef: plan.workspace, stateRoot: state });
  let ownerLock;
  try {
    if (plan.lock.stamp) {
      ownerLock = new DatabaseSync(join(plan.owner, 'lock.db'), { readOnly: false });
      try {
        // Holding the same SQLite lock must not refresh the on-disk age via a journal.
        if (ownerLock.prepare('PRAGMA journal_mode=MEMORY').get().journal_mode !== 'memory') {
          throw new Keep('unexpected ACP lock journal mode');
        }
        ownerLock.exec('BEGIN IMMEDIATE');
      } catch (error) {
        if (error.errcode === 5) throw new Keep('ACP run lock busy');
        throw error;
      }
    }
    const freshMounts = mounts();
    checkCatalog(state, context.root, freshMounts);
    const fresh = check(context, plan.agent_id, currentRefs(), freshMounts);
    if (fresh.data.stamp !== plan.data.stamp || fresh.lock.stamp !== plan.lock.stamp ||
      JSON.stringify(fresh.threads) !== JSON.stringify(plan.threads)) throw new Keep('session changed after preview');

    // SDK v1.0.32 deletes runs before events. Remove those exact events first so a
    // retry after interruption can still identify events by the surviving run rows.
    for (const runId of fresh.runs) await storage.runEvents.delete({ filter: { runIds: [runId] } });
    const store = {
      agents: {
        ...storage.agents,
        async delete(input) {
          // The SDK deletes catalog identity last. Keep it available for retry
          // until the ACP receipt and its exact owner's lock have been retired.
          if (ownerLock) {
            for (const name of [...fresh.lock.entries].reverse()) unlinkSync(join(fresh.owner, name));
            rmdirSync(fresh.owner);
          }
          return storage.agents.delete(input);
        },
      },
      checkpoints: storage.checkpoints,
      runs: storage.runs,
      runEvents: storage.runEvents,
    };
    await cursor.Agent.delete(fresh.agent_id, { cwd: fresh.workspace, store });
    if (index.prepare('SELECT 1 FROM agents WHERE agent_id=?').get(fresh.agent_id) ||
      index.prepare('SELECT 1 FROM runs WHERE agent_id=?').get(fresh.agent_id) ||
      fresh.runs.some((runId) => index.prepare('SELECT 1 FROM run_events WHERE run_id=?').get(runId)) ||
      existsSync(fresh.path) || existsSync(fresh.owner)) throw new Error('SDK deletion did not remove all session data');
  } finally {
    ownerLock?.close();
    await storage.dispose();
  }
}

function publicItem(plan) {
  return { agent_id: plan.agent_id, path: plan.path, thread_ids: plan.threads, bytes: plan.bytes };
}

function archiveFiles(path, archiveRoot, mountPaths) {
  return ['', ...SIDECARS].flatMap((suffix) => {
    const file = path + suffix;
    if (mounted(file, archiveRoot, mountPaths)) throw new Keep('mount intersects checkpoint archive');
    const stat = physical(file, Boolean(suffix));
    if (!stat) return [];
    if (!stat.isFile() || stat.nlink !== 1) throw new Keep('unexpected checkpoint archive file');
    return [{ path: file, stat }];
  });
}

function checkArchiveRefs(plan, refs) {
  const token = hash(plan.agent_id);
  if (refs.some((path) => path === plan.path || SIDECARS.some((suffix) => path === plan.path + suffix) ||
    path.includes('/agents/agent-' + token + '/') || path.includes('/acp-run-owners/' + token + '/'))) {
    throw new Keep('live checkpoint archive or SDK source reference');
  }
}

function checkArchiveIdentity(db, plan) {
  const snapshots = db.prepare('SELECT id, agent FROM snapshots').all();
  if (!snapshots.length || snapshots.some((snapshot) => {
    const agent = JSON.parse(snapshot.agent);
    return ARCHIVE_ID.exec(snapshot.id)?.[1] !== plan.agent_id ||
      agent.agentId !== plan.agent_id || agent.cwd !== plan.workspace;
  })) throw new Keep('checkpoint archive identity disagrees with ACP');
}

function checkArchiveSource(plan, roots, projects, mountPaths) {
  const workspaceHash = createHash('md5').update(plan.workspace).digest('hex');
  const token = hash(plan.agent_id);
  for (const state of roots.get(workspaceHash) ?? []) {
    if (physical(join(state, 'agents', 'agent-' + token), true) ||
      physical(join(state, 'acp-run-owners', token), true)) throw new Keep('checkpoint archive source files retained');
    const path = join(state, 'index.db');
    if (!physical(path, true)) continue;
    checkCatalog(state, projects, mountPaths);
    const db = ro(path);
    try {
      if (db.prepare('SELECT 1 FROM agents WHERE agent_id=? UNION SELECT 1 FROM runs WHERE agent_id=?')
        .get(plan.agent_id, plan.agent_id)) throw new Keep('checkpoint archive source retained');
    } finally {
      db.close();
    }
  }
}

function eraseArchive(plan, { root, bb, cutoff, currentRefs, sdkRoots, projects }) {
  const uri = pathToFileURL(plan.path);
  uri.searchParams.set('mode', 'rw');
  const db = new DatabaseSync(uri);
  try {
    // Exclusive locking also excludes WAL readers. Keep the lock across the
    // journal switch so SQLite flushes its WAL before any paths are removed.
    db.exec('PRAGMA locking_mode=EXCLUSIVE; BEGIN EXCLUSIVE');
    const [{ stat }] = archiveFiles(plan.path, root, mounts());
    if (stat.dev !== plan.stat.dev || stat.ino !== plan.stat.ino ||
      stat.mtimeMs !== plan.stat.mtimeMs || stat.ctimeMs !== plan.stat.ctimeMs) {
      throw new Keep('checkpoint archive changed after preview');
    }
    checkArchiveIdentity(db, plan);
    db.exec('COMMIT');
    if (db.prepare('PRAGMA journal_mode=DELETE').get().journal_mode !== 'delete') {
      throw new Keep('checkpoint archive journal still in use');
    }
    db.exec('BEGIN EXCLUSIVE');
    const mountPaths = mounts();
    const files = archiveFiles(plan.path, root, mountPaths);
    if (files[0].stat.dev !== stat.dev || files[0].stat.ino !== stat.ino) {
      throw new Keep('checkpoint archive changed after preview');
    }
    const threads = threadEvidence(bb, plan.claims, cutoff);
    if (JSON.stringify(threads) !== JSON.stringify(plan.threads)) throw new Keep('archive owner changed');
    checkArchiveSource(plan, sdkRoots, projects, mountPaths);
    checkArchiveRefs(plan, currentRefs());
    // SAFE: exact hash and SQLite owner proved, canonical unmounted regular
    // files, exclusive DB lock held. Retire the database last for retryability.
    for (const file of files.reverse()) unlinkSync(file.path);
  } catch (error) {
    if (error.errcode === 5 || error.errcode === 6) throw new Keep('checkpoint archive busy');
    throw error;
  } finally {
    db.close();
  }
}

function sweepArchives(context, output) {
  const { root, refs, mountPaths, owners, bb, cutoff,
    knownAgents, expiringAgents, reviewed, execute, sdkRoots, projects } = context;
  const identities = new Map([...owners].map(([key, claims]) => [hash(key) + '.db', { key, claims }]));
  for (const { name } of listing(root, true).filter((file) => ARCHIVE_FILE.test(file.name))) {
    output.archive_examined++;
    const path = join(root, name);
    try {
      const owner = identities.get(name);
      if (!owner) throw new Keep('checkpoint archive ACP ownership unknown');
      const [workspace, id] = JSON.parse(owner.key);
      if (knownAgents.has(id) && !expiringAgents.has(id)) throw new Keep('checkpoint archive source retained');
      const threads = threadEvidence(bb, owner.claims, cutoff);
      const files = archiveFiles(path, root, mountPaths);
      const dataFiles = files.filter(({ path: file, stat }) => file === path ||
        (stat.size > 0 && !file.endsWith('-shm')));
      if (dataFiles.some(({ stat }) => stat.mtimeMs > cutoff)) throw new Keep('recent checkpoint archive');
      const [{ stat }] = files;
      const bytes = files.reduce((sum, file) => sum + file.stat.blocks * 512, 0);
      const plan = { path, stat, bytes, agent_id: id, workspace, threads, claims: owner.claims };
      checkArchiveRefs(plan, refs);
      if (!knownAgents.has(id)) checkArchiveSource(plan, sdkRoots, projects, mountPaths);
      // With no rollback or WAL data, a read-only immutable open avoids
      // creating WAL sidecars during preview. Execution still revalidates the
      // live database under its exclusive lock.
      const uri = pathToFileURL(path);
      if (dataFiles.length === 1) uri.searchParams.set('immutable', '1');
      const db = ro(uri);
      try {
        checkArchiveIdentity(db, plan);
      } finally {
        db.close();
      }
      if (reviewed && !reviewed.has(JSON.stringify([path, id]))) throw new Keep('archive outside reviewed preview');
      output.archive_candidates.push(publicItem(plan));
      if (execute) {
        eraseArchive(plan, context);
        output.archive_removed.push(publicItem(plan));
      }
    } catch (error) {
      if (error instanceof Keep) output.kept[error.message] = (output.kept[error.message] ?? 0) + 1;
      else output.errors.push({ path, error: error.message });
    }
  }
}

async function sweep(options, output) {
  const { root, acp, 'bb-db': bbPath } = options;
  const cursor = options.execute ? sdk(options.sdk) : null;
  let reviewed;
  if (options['reviewed-preview']) {
    const preview = json(options['reviewed-preview']);
    if (preview.mode !== 'preview' || !preview.complete || preview.root !== root) {
      throw new Error('expected a complete preview of the same Cursor projects root');
    }
    reviewed = new Set([...preview.candidates, ...(preview.archive_candidates ?? [])]
      .map(({ path, agent_id }) => JSON.stringify([path, agent_id])));
  }
  const cutoff = Date.now() - 14 * DAY;
  const archiveRoot = join(dirname(acp), 'checkpoints');
  const currentRefs = () => processReferences([root, acp, archiveRoot]);
  const refs = currentRefs();
  const mountPaths = mounts();
  const owners = sessionOwners(acp);
  const bb = ro(bbPath);
  const sdkRoots = new Map();
  const knownAgents = new Set();
  const expiringAgents = new Set();
  output.sdk_version = cursor?.version ?? null;
  output.cutoff = new Date(cutoff).toISOString();
  try {
    for (const project of listing(root).filter((entry) => entry.isDirectory())) {
      const sdkRoot = join(root, project.name, 'sdk-agent-store');
      for (const entry of listing(sdkRoot, true).filter((item) => /^[0-9a-f]{32}$/.test(item.name) && item.isDirectory())) {
        const state = join(sdkRoot, entry.name);
        const matchingRoots = sdkRoots.get(entry.name) ?? [];
        matchingRoots.push(state);
        sdkRoots.set(entry.name, matchingRoots);
        const path = join(state, 'index.db');
        if (!existsSync(path)) continue;
        let index;
        try {
          checkCatalog(state, root, mountPaths);
          index = ro(path);
          const context = { root, state, index, bb, owners, cutoff };
          for (const { agent_id: id } of index.prepare('SELECT agent_id FROM agents UNION SELECT agent_id FROM runs').all()) {
            knownAgents.add(id);
          }
          for (const { agent_id: id } of index.prepare('SELECT agent_id FROM agents ORDER BY agent_id').all()) {
            output.examined++;
            let plan;
            try {
              if (!AGENT_ID.test(id)) throw new Keep('unknown SDK identity format');
              plan = check(context, id, refs, mountPaths);
              if (reviewed && !reviewed.has(JSON.stringify([plan.path, id]))) throw new Keep('outside reviewed preview');
              output.candidates.push(publicItem(plan));
              if (options.execute) {
                await erase(context, plan, currentRefs, cursor);
                output.removed.push(publicItem(plan));
              }
              expiringAgents.add(id);
            } catch (error) {
              if (error instanceof Keep) {
                output.kept[error.message] = (output.kept[error.message] ?? 0) + 1;
              } else {
                output.errors.push({ path, agent_id: id, error: error.message });
                if (plan) break; // Stop this store after a partial SDK operation.
              }
            }
          }
        } catch (error) {
          output.errors.push({ path, error: error.message });
        } finally {
          index?.close();
        }
      }
    }
    // An incomplete SDK inventory cannot prove that an archive is orphaned.
    if (!output.errors.length) sweepArchives({ root: archiveRoot, currentRefs, refs, mountPaths,
      owners, bb, cutoff, knownAgents, expiringAgents, reviewed, execute: options.execute,
      sdkRoots, projects: root }, output);
  } finally {
    bb.close();
  }
}

const { values: options } = parseArgs({ options: {
  root: { type: 'string' }, acp: { type: 'string' }, 'bb-db': { type: 'string' },
  sdk: { type: 'string' }, report: { type: 'string' }, 'reviewed-preview': { type: 'string' }, execute: { type: 'boolean' }, help: { type: 'boolean' },
} });
if (options.help) {
  console.log('cursor_session_sweep.mjs --root <physical projects> --acp <sessions> --bb-db <local bb.db>\n' +
    '  [--execute --sdk <installed @cursor/sdk package> [--reviewed-preview <JSON path>]] [--report <JSON path>]');
  process.exit(0);
}
const output = { schema_version: 1, mode: options.execute ? 'execute' : 'preview',
  ts: new Date().toISOString(), run_id: process.env.BB_AUTOMATION_RUN_ID ?? null,
  examined: 0, candidates: [], removed: [], archive_examined: 0, archive_candidates: [],
  archive_removed: [], kept: {}, errors: [] };
function avail() {
  const st = statfsSync(options.root);
  return st.bavail * st.bsize;
}
try {
  for (const key of ['root', 'acp', 'bb-db', ...(options.execute ? ['sdk'] : [])]) {
    if (!options[key] || !isAbsolute(options[key])) throw new Error('expected absolute --' + key);
    options[key] = resolve(options[key]);
    if (!physical(options[key])) throw new Error('missing --' + key);
  }
  if (options['reviewed-preview'] && !options.execute) throw new Error('--reviewed-preview requires --execute');
  output.root = options.root;
  output.available_before = avail();
  await sweep(options, output);
  output.available_after = avail();
} catch (error) {
  output.errors.push({ error: error.message });
}
output.complete = output.errors.length === 0;
output.candidate_bytes = [...output.candidates, ...output.archive_candidates].reduce((sum, item) => sum + item.bytes, 0);
output.removed_bytes = [...output.removed, ...output.archive_removed].reduce((sum, item) => sum + item.bytes, 0);
if (options.report) {
  const temporary = options.report + '.' + process.pid + '.tmp';
  writeFileSync(temporary, JSON.stringify(output, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  try {
    renameSync(temporary, options.report);
  } catch (error) {
    unlinkSync(temporary);
    throw error;
  }
  const { candidates, removed, archive_candidates, archive_removed, ...summary } = output;
  console.log(JSON.stringify({ ...summary, candidates: candidates.length, removed: removed.length,
    archive_candidates: archive_candidates.length, archive_removed: archive_removed.length, report: options.report }));
} else {
  console.log(JSON.stringify(output));
}
process.exitCode = output.errors.length ? 1 : 0;
