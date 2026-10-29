#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

from common import CHECKPOINT_ROOT, env, load_config


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.part')
    with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        ctype = r.headers.get('content-type', '')
        if 'text/html' in ctype:
            raise RuntimeError(f'Checkpoint URL resolved to HTML instead of a file: {r.url}')
        total = int(r.headers.get('content-length') or 0)
        done = 0
        with tmp.open('wb') as f:
            for chunk in r.iter_content(8 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f'\r{dest.name}: {done/1e6:.1f}/{total/1e6:.1f} MB', end='', flush=True)
    print()
    tmp.replace(dest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('profile', nargs='?', default=env('PS_PROFILE', '512'), choices=['512', '4k'])
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    cfg = load_config()['model']
    custom = os.environ.get('PS_CHECKPOINT_URL')
    if args.profile == '512':
        url = custom or cfg['checkpoint_512_url']
        dest = CHECKPOINT_ROOT / 'pansplat' / 'pansplat-512.ckpt'
    else:
        url = custom or cfg['checkpoint_4k_url']
        dest = CHECKPOINT_ROOT / 'pansplat' / 'pansplat-2048.ckpt'

    if dest.is_file() and dest.stat().st_size > 50_000_000 and not args.force:
        print(json.dumps({'profile': args.profile, 'checkpoint': str(dest), 'bytes': dest.stat().st_size, 'cached': True}, indent=2))
        return

    print(f'Downloading PanSplat {args.profile} checkpoint...')
    download(url, dest)
    if dest.stat().st_size < 50_000_000:
        raise RuntimeError(f'Checkpoint is unexpectedly small: {dest.stat().st_size} bytes')
    print(json.dumps({'profile': args.profile, 'checkpoint': str(dest), 'bytes': dest.stat().st_size, 'cached': False}, indent=2))


if __name__ == '__main__':
    main()
