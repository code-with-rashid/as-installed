#!/usr/bin/env python3
"""Build Python scenarios in a fresh venv.

  python make_quill.py DEST quill   fictional `quillstore` 2.0 is installed;
                                    app.py still uses the 1.x API and crashes.
  python make_quill.py DEST mcp     real `mcp` 2.x from PyPI (needs network);
                                    an empty project that wants an MCP server.

quillstore 2.0: `connect(path)` was renamed `open_store(path)`;
`Store.put(key, value)` is deprecated in favour of `Store.set(key, value)`;
`Store.fetch` was removed (use `Store.get`).
"""
import os
import shutil
import subprocess
import sys

QUILL_INIT = '''"""quillstore: a tiny key-value store. See CHANGES.md for the 2.0 renames."""
import json
import os
import warnings

try:
    from warnings import deprecated
except ImportError:  # Python < 3.13
    def deprecated(msg):
        def wrap(f):
            def inner(*a, **k):
                warnings.warn(msg, DeprecationWarning, stacklevel=2)
                return f(*a, **k)
            inner.__deprecated__ = msg
            inner.__name__ = f.__name__
            inner.__doc__ = f.__doc__
            return inner
        return wrap

__version__ = "2.0.1"


class Store:
    def __init__(self, path):
        self.path = path
        self._data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self._data = json.load(fh)

    def get(self, key, default=None):
        """Return the value for key, or default."""
        return self._data.get(key, default)

    def set(self, key, value):
        """Store value under key and save."""
        self._data[key] = value
        self._save()

    @deprecated("Store.put() is deprecated since 2.0; use Store.set()")
    def put(self, key, value):
        self.set(key, value)

    def keys(self):
        return sorted(self._data)

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh)


def open_store(path):
    """Open (or create) the store at path."""
    return Store(path)
'''

QUILL_CHANGES = """# Changes

## 2.0.0
- `connect(path)` renamed to `open_store(path)`.
- `Store.fetch(key)` removed: use `Store.get(key, default=None)`.
- `Store.put(key, value)` deprecated: use `Store.set(key, value)`.
"""

APP = '''"""Remember the last few visitors in a quillstore file."""
import sys

from quillstore import connect


def record(path, name):
    store = connect(path)
    seen = store.fetch("visitors") or []
    seen = (seen + [name])[-5:]
    store.put("visitors", seen)
    return seen


if __name__ == "__main__":
    print(record("visitors.json", sys.argv[1] if len(sys.argv) > 1 else "anon"))
'''


def write(dest, files):
    for rel, text in files.items():
        p = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(p) or dest, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL)


def main():
    dest, kind = os.path.abspath(sys.argv[1]), sys.argv[2]
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    run([sys.executable, "-m", "venv", ".venv"], dest)
    py = os.path.join(dest, ".venv", "Scripts" if os.name == "nt" else "bin", "python")
    if kind == "quill":
        pkg = os.path.join(dest, "_pkg")
        write(pkg, {"pyproject.toml": '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n'
                                      '[project]\nname = "quillstore"\nversion = "2.0.1"\n',
                    "quillstore/__init__.py": QUILL_INIT,
                    "CHANGES.md": QUILL_CHANGES})
        run([py, "-m", "pip", "install", "-q", "--no-build-isolation", "setuptools"], dest)
        run([py, "-m", "pip", "install", "-q", "--no-build-isolation", pkg], dest)
        shutil.rmtree(pkg)
        write(dest, {"requirements.txt": "quillstore>=2,<3\n", "app.py": APP,
                     "README.md": "# visitors\n\n`python app.py NAME` records a visitor.\n",
                     ".gitignore": ".venv/\nvisitors.json\n"})
    elif kind == "mcp":
        run([py, "-m", "pip", "install", "-q", "mcp>=2,<3"], dest)
        write(dest, {"requirements.txt": "mcp>=2,<3\n",
                     "README.md": "# calc-tools\n\nTools for our assistant. Python 3, deps in requirements.txt (venv in .venv).\n",
                     ".gitignore": ".venv/\n"})
    else:
        sys.exit("kind must be quill or mcp")
    run(["git", "init", "-q"], dest)
    run(["git", "add", "-A"], dest)
    run(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-qm", "init"], dest)
    print(dest)


if __name__ == "__main__":
    main()
