#!/usr/bin/env node
// Compare two model/start JSON objects, snapshots, or saved text artifacts.
import { readFileSync } from 'node:fs'
import { compareSnapshots, comparable } from '../sandbox/request-snapshot.mjs'

try {
  if (process.argv.length !== 4) throw new Error('usage: node scripts/compare-request-snapshots.mjs BEFORE.json AFTER.json')
  const read = path => {
    const value = JSON.parse(readFileSync(path, 'utf8'))
    const snapshot = value.snapshot ?? value.metadata ?? value
    if (snapshot.version !== 1 || !snapshot.input || !Array.isArray(snapshot.tools)) {
      throw new Error('expected a version 1 request snapshot, model/start event, or text artifact')
    }
    return snapshot
  }
  const before = read(process.argv[2]), after = read(process.argv[3])
  const result = compareSnapshots(before, after)
  for (const layer of ['sections', 'contexts', 'registry_catalog', 'catalog', 'tools']) {
    const a = new Map((before[layer] ?? []).map(item => [item.name, item]))
    const b = new Map((after[layer] ?? []).map(item => [item.name, item]))
    result[layer] = [...new Set([...a.keys(), ...b.keys()])]
      .filter(name => JSON.stringify(comparable(a.get(name))) !== JSON.stringify(comparable(b.get(name))))
  }
  process.stdout.write(JSON.stringify(result, null, 2) + '\n')
} catch (error) {
  process.stderr.write(error.message + '\n')
  process.exitCode = 2
}
