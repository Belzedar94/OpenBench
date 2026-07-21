#!/usr/bin/env python3
"""Archivador de chunks DATAGEN: descarga a la torre, verifica sha256 y purga
del VPS. Fail-closed: nunca borra remoto sin copia local verificada.
Programado por tarea de Windows (hourly). Log: junto al archivo."""
import hashlib, os, subprocess, sys, datetime

KEY  = os.path.expanduser(r"~\.ssh\spellbench_key")
HOST = "root@178.104.66.19"
ARCH = r"F:\OpenBench\archive"
LOG  = os.path.join(ARCH, "archive.log")

def ssh(*cmd, **kw):
    return subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", HOST, *cmd],
                          capture_output=True, text=True, timeout=kw.get("t", 120))

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def main():
    os.makedirs(ARCH, exist_ok=True)
    man = ssh("/opt/openbench/archive_manifest.py", t=180)
    if man.returncode != 0:
        log(f"ERROR manifiesto: {man.stderr[:200]}"); return 1
    done = purged = errors = 0
    for line in man.stdout.strip().splitlines():
        try:
            rel, sha, size = line.strip().split("|")
            local = os.path.join(ARCH, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            if not (os.path.exists(local) and os.path.getsize(local) == int(size)
                    and sha256(local) == sha):
                r = subprocess.run(["scp", "-i", KEY, "-o", "BatchMode=yes", "-q",
                                    f"{HOST}:/opt/openbench/Media/{rel}", local],
                                   capture_output=True, timeout=600)
                if r.returncode != 0 or sha256(local) != sha:
                    errors += 1; log(f"FALLO descarga/sha: {rel}"); continue
                done += 1
            ssh("rm", "-f", f"/opt/openbench/Media/{rel}")
            purged += 1
        except Exception as e:
            errors += 1; log(f"EXCEPCION {line[:60]}: {e}")
    log(f"archivados {done} nuevos, purgados {purged}, errores {errors}")
    return 0 if not errors else 1

def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] {msg}\n")

if __name__ == "__main__":
    sys.exit(main())
