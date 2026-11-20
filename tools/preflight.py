#!/usr/bin/env python3
"""Read-only pre-upload / pre-launch check for the Literary Roulette web folder. Changes nothing, never pushes.

    python3 tools/preflight.py            # run from the project folder (or pass the folder as an argument)
Exit code 1 if anything is a FAIL.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
results = []


def report(level, text):
    results.append(level)
    print(f"{level:5} {text}")


SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".idea"}
files = [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & SKIP)]

# 1. nothing private or huge goes to GitHub
# secret.key and the runtime databases live inside instance/, so ignoring instance/ covers them. Do NOT ignore *.sqlite:
# data/universe_seed.sqlite must be committed so a fresh deploy opens with the full catalogue.
needed = ["instance/", ".env", "__pycache__/", ".venv/", ".DS_Store"]
ignore = (root / ".gitignore").read_text(encoding="utf-8", errors="ignore") if (root / ".gitignore").exists() else ""
missing = [n for n in needed if n not in ignore]
report("FAIL" if missing else "PASS", f".gitignore is missing: {', '.join(missing)}" if missing else ".gitignore covers instance/, databases, keys, caches")
try:
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=20).stdout.splitlines()
except Exception:
    tracked = []
bad = [t for t in tracked if re.search(r"(^|/)(instance/|secret\.key$|\.env$)|\.(sqlite3?|db|pem)$", t) and not t.endswith("universe_seed.sqlite")]
report("FAIL" if bad else "PASS", f"already tracked by git (remove with git rm --cached): {bad[:5]}" if bad else "no private files are tracked by git")
for p in files:
    mb = p.stat().st_size / 1e6
    if mb >= 100:
        report("FAIL", f"{p.relative_to(root)} is {mb:.0f} MB: GitHub rejects files over 100 MB")
    elif mb >= 50:
        report("WARN", f"{p.relative_to(root)} is {mb:.0f} MB: GitHub warns over 50 MB")

# 2. things that must not ship
text_files = [p for p in files if p.suffix in {".py", ".html", ".js", ".json", ".md", ".txt"} and p.stat().st_size < 3_000_000]
hits = {}
for p in text_files:
    body = p.read_text(encoding="utf-8", errors="ignore")
    for label, pattern in [("placeholder [Your ...]", r"\[Your [A-Za-z ]+\]"), ("developer note", r"developer note"),
                           ("debug server", r"debug\s*=\s*True")]:
        if p.name != "preflight.py" and re.search(pattern, body, re.I):
            hits.setdefault(label, []).append(str(p.relative_to(root)))
for label in ("placeholder [Your ...]", "developer note", "debug server"):
    report("FAIL" if label in hits else "PASS", f"{label} found in {hits[label][:4]}" if label in hits else f"no {label}")
books = root / "data" / "books.json"
if books.exists():
    n = len(re.findall(r"pending_verification", books.read_text(encoding="utf-8", errors="ignore")))
    report("WARN" if n else "PASS", f"{n} entries in data/books.json are still pending_verification (rights not checked)" if n else "no pending_verification entries")

# 3. launch settings (environment of THIS shell: run it on the server with the real values)
env = os.environ
for var, why in [("LR_PRODUCTION", "must be 1"), ("LR_TRUSTED_PROXIES", "1 when behind one proxy"), ("LR_CONTACT_EMAIL", "goes in the User-Agent, keeps Gutenberg friendly")]:
    report("PASS" if env.get(var) else "WARN", f"{var} {'is set' if env.get(var) else 'is not set here (set it on the host: ' + why + ')'}")
key = env.get("LR_SECRET_KEY", "")
report("PASS" if len(key) >= 32 else "WARN", "LR_SECRET_KEY is set (32+ chars)" if len(key) >= 32 else "LR_SECRET_KEY not set/short: a key file in instance/ will be used (fine on one machine)")
report("PASS" if env.get("LR_ALLOW_WWW") != "1" else "WARN", "worker does not use www.gutenberg.org" if env.get("LR_ALLOW_WWW") != "1" else "LR_ALLOW_WWW=1: the worker may use www.gutenberg.org (against Gutenberg's robot policy)")
print("\n" + ("NOT READY" if "FAIL" in results else "No blockers") + f": {results.count('FAIL')} fail, {results.count('WARN')} warn")
sys.exit(1 if "FAIL" in results else 0)
