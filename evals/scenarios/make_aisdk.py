#!/usr/bin/env python3
"""Build the `aisdk` scenario: a TypeScript project with the real AI SDK 7
(`ai@7`, `@ai-sdk/openai`) installed. The API most models remember for
returning a chat stream, `result.toUIMessageStreamResponse()`, still exists in
7.x but is deprecated: a quiet break that type-checks.

  python make_aisdk.py DEST       (needs network: installs from npm)
"""
import json
import os
import shutil
import subprocess
import sys


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
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    write(dest, {
        "package.json": json.dumps({"name": "chat-api", "version": "1.0.0", "private": True, "type": "module",
                                    "scripts": {"typecheck": "tsc --noEmit"}}, indent=2) + "\n",
        "tsconfig.json": json.dumps({"compilerOptions": {"target": "ES2022", "module": "NodeNext",
                                                         "moduleResolution": "NodeNext", "strict": True,
                                                         "skipLibCheck": True, "noEmit": True, "lib": ["ES2022", "DOM"]},
                                     "include": ["app/**/*.ts"]}, indent=2) + "\n",
        "README.md": "# chat-api\n\nRoute handlers for the chat UI (useChat on the client). `npm run typecheck` must pass.\n",
        ".gitignore": "node_modules/\n",
    })
    sh(["npm", "install", "--no-audit", "--no-fund", "ai@7", "@ai-sdk/openai", "zod"], dest)
    sh(["npm", "install", "--no-audit", "--no-fund", "-D", "typescript", "@types/node"], dest)
    sh(["git", "init", "-q"], dest)
    sh(["git", "add", "-A"], dest)
    sh(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-qm", "init"], dest)
    print(dest)


if __name__ == "__main__":
    main()
