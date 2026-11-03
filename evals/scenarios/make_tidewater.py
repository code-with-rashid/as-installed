#!/usr/bin/env python3
"""Build the `tidewater` scenario: a Node project using a fictional database
client whose v3 renamed createPool -> openPool and deprecated pool.query() in
favour of pool.run(). The repo's own docs and existing code still show the v2
API, so copying them (or guessing) produces wrong code.

  python make_tidewater.py DEST            installed v3 matches the lockfile
  python make_tidewater.py DEST --stale    lockfile says 3.1.0, node_modules has 2.4.0

Needs node and npm. Works offline: the library is a local tarball.
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import io

V3_JS = r"""'use strict';
const ROWS = { orders: [{ id: 1, total: 30 }, { id: 2, total: 12 }], users: [{ id: 7, name: 'Ada' }] };
function table(sql) {
  const m = /from\s+(\w+)/i.exec(sql);
  if (!m || !ROWS[m[1]]) throw new Error('tidewater: unknown table in: ' + sql);
  return ROWS[m[1]].map(r => ({ ...r }));
}
class Pool {
  constructor(url) { this.url = url; this.closed = false; }
  /** Run a statement. Resolves to { rows, rowCount }. */
  async run(sql) { if (this.closed) throw new Error('pool closed'); const rows = table(sql); return { rows, rowCount: rows.length }; }
  /** @deprecated since 3.0. Use run(); query() resolves to the bare row array and will be removed in 4.0. */
  async query(sql) { process.emitWarning('pool.query() is deprecated, use pool.run()', 'DeprecationWarning'); return table(sql); }
  async close() { this.closed = true; }
}
function openPool(url, opts = {}) {
  if (typeof url !== 'string' || !url.startsWith('tide://')) throw new TypeError('openPool(url): url must start with tide://');
  return new Pool(url, opts);
}
module.exports = { openPool, Pool, VERSION: '%s' };
"""

V3_DTS = """export interface RunResult<T = Record<string, unknown>> {
  rows: T[];
  rowCount: number;
}
export interface PoolOptions { max?: number; idleMs?: number }
export declare class Pool {
  readonly url: string;
  /** Run a statement. Resolves to { rows, rowCount }. */
  run<T = Record<string, unknown>>(sql: string): Promise<RunResult<T>>;
  /** @deprecated since 3.0. Use run(); query() resolves to the bare row array and will be removed in 4.0. */
  query<T = Record<string, unknown>>(sql: string): Promise<T[]>;
  close(): Promise<void>;
}
/** Open a connection pool. `url` must start with `tide://`. */
export declare function openPool(url: string, opts?: PoolOptions): Pool;
export declare const VERSION: string;
"""

V2_JS = r"""'use strict';
function createPool(url) {
  return { async query(sql) { return [{ id: 1, total: 30 }, { id: 2, total: 12 }]; }, async end() {} };
}
module.exports = { createPool, VERSION: '2.4.0' };
"""

CHANGELOG = """# Changelog

## 3.1.0
- `PoolOptions.idleMs`.

## 3.0.0 (breaking)
- `createPool(url)` is now `openPool(url, opts?)`. URLs must use the `tide://` scheme.
- `pool.query(sql)` is deprecated: use `pool.run(sql)`, which resolves to `{ rows, rowCount }`
  instead of the bare row array. `query()` will be removed in 4.0.
- `pool.end()` is now `pool.close()`.

## 2.4.0
- `createPool(url).query(sql)`.
"""


def tarball(dest, version, js, dts):
    buf = io.BytesIO()
    meta = {"name": "tidewater", "version": version, "main": "index.js", "license": "MIT"}
    files = {"package/package.json": json.dumps(meta, indent=2), "package/index.js": js}
    if dts:
        meta["types"] = "index.d.ts"
        files["package/package.json"] = json.dumps(meta, indent=2)
        files["package/index.d.ts"] = dts
        files["package/CHANGELOG.md"] = CHANGELOG
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    path = os.path.join(dest, "vendor", "tidewater-%s.tgz" % version)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return "file:vendor/tidewater-%s.tgz" % version


def write(dest, files):
    for rel, text in files.items():
        p = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(p) or dest, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, shell=(os.name == "nt"), stdout=subprocess.DEVNULL)


def main():
    dest = os.path.abspath(sys.argv[1])
    stale = "--stale" in sys.argv
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    spec3 = tarball(dest, "3.1.0", V3_JS % "3.1.0", V3_DTS)
    spec2 = tarball(dest, "2.4.0", V2_JS, None)
    write(dest, {
        "package.json": json.dumps({"name": "shopdata", "version": "1.0.0", "private": True,
                                    "scripts": {"test": "node --test"},
                                    "dependencies": {"tidewater": spec3}}, indent=2) + "\n",
        "README.md": "# shopdata\n\nData access helpers for the shop. Run tests with `npm test`.\n",
        "docs/database.md": ("# Database access\n\nWe use tidewater. Open a pool once and query it:\n\n"
                             "```js\nconst { createPool } = require('tidewater');\nconst pool = createPool(process.env.DB_URL);\n"
                             "const rows = await pool.query('SELECT * FROM users');\n```\n"),
        "src/users.js": ("'use strict';\nconst tidewater = require('tidewater');\n\n"
                         "async function getUsers(url) {\n  const pool = tidewater.openPool(url);\n"
                         "  const rows = await pool.query('SELECT * FROM users');\n  await pool.close();\n  return rows;\n}\n\n"
                         "module.exports = { getUsers };\n"),
        "test/users.test.js": ("'use strict';\nconst test = require('node:test');\nconst assert = require('node:assert');\n"
                               "const { getUsers } = require('../src/users');\n\n"
                               "test('getUsers', async () => {\n  const rows = await getUsers('tide://local/shop');\n"
                               "  assert.equal(rows[0].name, 'Ada');\n});\n"),
        ".gitignore": "node_modules/\n",
    })
    sh(["npm", "install", "--no-audit", "--no-fund"], dest)
    if stale:
        # The lockfile still pins 3.1.0, but node_modules holds an old 2.4.0 install.
        shutil.rmtree(os.path.join(dest, "node_modules", "tidewater"))
        with tarfile.open(os.path.join(dest, spec2[5:])) as tf:
            tf.extractall(os.path.join(dest, "_x"))
        shutil.move(os.path.join(dest, "_x", "package"), os.path.join(dest, "node_modules", "tidewater"))
        shutil.rmtree(os.path.join(dest, "_x"))
    else:
        os.remove(os.path.join(dest, spec2[5:]))
    sh(["git", "init", "-q"], dest)
    sh(["git", "add", "-A"], dest)
    sh(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-qm", "init"], dest)
    print(dest)


if __name__ == "__main__":
    main()
