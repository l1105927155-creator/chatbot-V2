import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import {
  chmodSync,
  closeSync,
  fchmodSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SOURCE_FIELDS = ['platform_id', 'bot_id', 'conversation_key'];
const ORDINARY_FIELDS = ['read', 'write', 'delete'];
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;
const WILDCARD_CHARACTERS = /[*?\[\]{}]/u;

export class ScopeConflictError extends Error {
  constructor(message = 'scope revision conflict') {
    super(message);
    this.name = 'ScopeConflictError';
  }
}

function hasExactKeys(value, keys) {
  return value !== null
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function validateSource(source) {
  if (!hasExactKeys(source, SOURCE_FIELDS)) {
    throw new TypeError('source must contain exactly platform_id, bot_id, and conversation_key');
  }
  for (const field of SOURCE_FIELDS) {
    const value = source[field];
    if (typeof value !== 'string' || value.length === 0 || CONTROL_CHARACTERS.test(value)) {
      throw new TypeError(`source.${field} must be a non-empty string without control characters`);
    }
  }
  return Object.fromEntries(SOURCE_FIELDS.map((field) => [field, source[field]]));
}

function validateOrdinary(ordinary) {
  if (!hasExactKeys(ordinary, ORDINARY_FIELDS)) {
    throw new TypeError('ordinary must contain exactly read, write, and delete arrays');
  }

  const result = {};
  for (const operation of ORDINARY_FIELDS) {
    if (!Array.isArray(ordinary[operation])) {
      throw new TypeError(`ordinary.${operation} must be an array`);
    }
    const normalized = [];
    const seen = new Set();
    for (const resource of ordinary[operation]) {
      if (typeof resource !== 'string' || resource.length === 0 || CONTROL_CHARACTERS.test(resource)) {
        throw new TypeError(`${operation} resources must be non-empty path strings without control characters`);
      }
      if (!path.isAbsolute(resource) || path.resolve(resource) !== resource) {
        throw new TypeError(`${operation} resources must be exact absolute normalized paths`);
      }
      if (resource.split(path.sep).includes('..') || WILDCARD_CHARACTERS.test(resource)) {
        throw new TypeError(`${operation} resources cannot contain traversal or wildcard syntax`);
      }
      if (!seen.has(resource)) {
        normalized.push(resource);
        seen.add(resource);
      }
    }
    result[operation] = normalized;
  }
  return result;
}

function validateRevision(revision) {
  if (!Number.isSafeInteger(revision) || revision < 0) {
    throw new TypeError('revision must be a non-negative safe integer');
  }
}

function readCurrent(filePath, identity) {
  let document;
  try {
    const stat = lstatSync(filePath);
    if (!stat.isFile()) throw new TypeError('scope file must be a regular file');
    document = JSON.parse(readFileSync(filePath, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return { revision: 0, ordinary: { read: [], write: [], delete: [] } };
    throw new Error(`invalid scope file: ${error.message}`, { cause: error });
  }

  if (!hasExactKeys(document, ['identity', 'revision', 'ordinary'])
    || !hasExactKeys(document.identity, SOURCE_FIELDS)) {
    throw new TypeError('scope file has an invalid structure');
  }
  for (const field of SOURCE_FIELDS) {
    if (document.identity[field] !== identity[field]) {
      throw new TypeError('scope file identity does not match its conversation');
    }
  }
  validateRevision(document.revision);
  return { revision: document.revision, ordinary: validateOrdinary(document.ordinary) };
}

function commitScope(root, identity, expectedRevision, ordinary) {
  const filePath = scopeFilePath(root, identity);
  const current = readCurrent(filePath, identity);
  if (current.revision !== expectedRevision) throw new ScopeConflictError();
  if (current.revision === Number.MAX_SAFE_INTEGER) throw new RangeError('scope revision is exhausted');

  const revision = current.revision + 1;
  const document = JSON.stringify({ identity, revision, ordinary });
  const tempPath = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
  let tempFd;
  try {
    tempFd = openSync(tempPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
    writeFileSync(tempFd, document, 'utf8');
    fsyncSync(tempFd);
    const fd = tempFd;
    tempFd = undefined;
    closeSync(fd);
    renameSync(tempPath, filePath);
  } catch (error) {
    if (tempFd !== undefined) {
      try { closeSync(tempFd); } catch { /* preserve the original write failure */ }
    }
    try { rmSync(tempPath, { force: true }); } catch { /* preserve the original write failure */ }
    throw error;
  }
  return { revision, ordinary: structuredClone(ordinary) };
}

function scopeFilePath(root, identity) {
  const digest = createHash('sha256').update(JSON.stringify(SOURCE_FIELDS.map((field) => identity[field]))).digest('hex');
  return path.join(root, `${digest}.json`);
}

function ensureLockFile(lockPath) {
  const noFollow = constants.O_NOFOLLOW ?? 0;
  const fd = openSync(lockPath, constants.O_RDWR | constants.O_CREAT | noFollow, 0o600);
  try {
    if (!fstatSync(fd).isFile()) throw new TypeError('scope lock path must be a regular file');
    fchmodSync(fd, 0o600);
  } finally {
    closeSync(fd);
  }
}

function cliCommit() {
  try {
    const request = JSON.parse(readFileSync(0, 'utf8'));
    if (typeof request.root !== 'string' || request.root.length === 0) throw new TypeError('invalid scope root');
    const identity = validateSource(request.source);
    validateRevision(request.expected_revision);
    const ordinary = validateOrdinary(request.ordinary);
    const store = new ScopeStore(request.root);
    const result = commitScope(store.root, identity, request.expected_revision, ordinary);
    process.stdout.write(`${JSON.stringify({ ok: true, revision: result.revision })}\n`);
  } catch (error) {
    const conflict = error instanceof ScopeConflictError;
    process.stdout.write(`${JSON.stringify({ ok: false, kind: conflict ? 'conflict' : 'error', message: error.message })}\n`);
    process.exitCode = conflict ? 3 : 2;
  }
}

export class ScopeStore {
  constructor(root) {
    if (typeof root !== 'string' || root.length === 0) {
      throw new TypeError('root must be a non-empty path string');
    }
    const resolvedRoot = path.resolve(root);
    mkdirSync(resolvedRoot, { recursive: true, mode: 0o700 });
    const rootStat = lstatSync(resolvedRoot);
    if (!rootStat.isDirectory()) throw new TypeError('scope root must be a directory, not a symbolic link');
    chmodSync(resolvedRoot, 0o700);
    Object.defineProperty(this, 'root', {
      value: resolvedRoot,
      enumerable: true,
      writable: false,
      configurable: false,
    });
  }

  get(source) {
    const identity = validateSource(source);
    const filePath = this.#filePath(identity);
    const current = readCurrent(filePath, identity);
    return { revision: current.revision, ordinary: structuredClone(current.ordinary) };
  }

  update(source, expectedRevision, ordinary) {
    const identity = validateSource(source);
    validateRevision(expectedRevision);
    const normalizedOrdinary = validateOrdinary(ordinary);
    const filePath = this.#filePath(identity);
    const lockPath = `${filePath}.lock`;
    ensureLockFile(lockPath);
    const child = spawnSync('flock', [
      '-n', '-E', '75', lockPath, process.execPath, fileURLToPath(import.meta.url), '--commit',
    ], {
      input: JSON.stringify({ root: this.root, source: identity, expected_revision: expectedRevision, ordinary: normalizedOrdinary }),
      encoding: 'utf8',
      maxBuffer: 64 * 1024,
    });
    if (child.status === 75 && String(child.stdout ?? '').trim() === '') {
      throw new ScopeConflictError('scope is being updated by another process');
    }

    const confirmPublished = () => {
      const current = this.get(identity);
      if (current.revision === expectedRevision + 1
        && JSON.stringify(current.ordinary) === JSON.stringify(normalizedOrdinary)) return current;
      return undefined;
    };
    if (child.error) {
      const confirmed = confirmPublished();
      if (confirmed) return confirmed;
      throw new Error(`cannot complete flock scope update: ${child.error.message}`, { cause: child.error });
    }

    let response;
    try {
      response = JSON.parse(child.stdout);
    } catch (error) {
      const confirmed = confirmPublished();
      if (confirmed) return confirmed;
      throw new Error(`scope update subprocess returned invalid output: ${child.stderr || error.message}`, { cause: error });
    }
    if (response.ok !== true) {
      if (response.kind === 'conflict') throw new ScopeConflictError(response.message);
      const confirmed = confirmPublished();
      if (confirmed) return confirmed;
      throw new Error(`scope update failed: ${response.message}`);
    }
    if (child.status !== 0 || !Number.isSafeInteger(response.revision)
      || response.revision !== expectedRevision + 1) {
      const confirmed = confirmPublished();
      if (confirmed) return confirmed;
      throw new Error(`scope update subprocess exited unexpectedly${child.stderr ? `: ${child.stderr.trim()}` : ''}`);
    }
    return { revision: response.revision, ordinary: structuredClone(normalizedOrdinary) };
  }

  #filePath(identity) {
    return scopeFilePath(this.root, identity);
  }
}

if (process.argv[2] === '--commit' && path.resolve(process.argv[1] ?? '') === fileURLToPath(import.meta.url)) {
  cliCommit();
}
