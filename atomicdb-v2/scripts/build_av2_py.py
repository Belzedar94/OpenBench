#!/usr/bin/env python3
"""Build the `av2` Python extension (crate av2-py) and put it where the
Django shim finds it.

    python atomicdb-v2/scripts/build_av2_py.py [--debug] [--jobs N] [--out DIR]

Runs ``cargo build -p av2-py --features extension-module`` and copies the
library to ``atomicdb-v2/target/av2py/av2.pyd`` (Windows) or ``av2.so``.
``atomicdb/v2shim/bridge.py`` looks there unless ``AV2_PY_DIR`` says
otherwise.  No maturin, no wheel: the module is only for the test suite.
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--jobs', type=int, default=6)
    parser.add_argument('--out', default=os.path.join(WORKSPACE, 'target', 'av2py'))
    args = parser.parse_args()
    profile = 'debug' if args.debug else 'release'
    cmd = ['cargo', 'build', '-p', 'av2-py', '--features', 'extension-module',
           '-j', str(args.jobs)]
    if not args.debug:
        cmd.append('--release')
    env = dict(os.environ)
    # PyO3 must build against the interpreter that will import the module.
    env.setdefault('PYO3_PYTHON', sys.executable)
    print('+', ' '.join(cmd), flush=True)
    subprocess.run(cmd, cwd=WORKSPACE, env=env, check=True)
    target = os.environ.get('CARGO_TARGET_DIR', os.path.join(WORKSPACE, 'target'))
    if sys.platform == 'win32':
        src, dst = os.path.join(target, profile, 'av2.dll'), 'av2.pyd'
    elif sys.platform == 'darwin':
        src, dst = os.path.join(target, profile, 'libav2.dylib'), 'av2.so'
    else:
        src, dst = os.path.join(target, profile, 'libav2.so'), 'av2.so'
    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, dst)
    shutil.copyfile(src, out)
    print(f'av2 extension: {out}')


if __name__ == '__main__':
    main()
