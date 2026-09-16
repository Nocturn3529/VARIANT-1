"""Stage the release's pinned, hash-verified Windows x64 CPU/CUDA runtime.

This is a build tool, not an inference/model installer. Archives remain in a
build cache. Only manifest-listed files are written to bin; models are not read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, target, digest=None):
    if target.is_file() and (not digest or sha(target) == digest):
        return target
    part = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with urllib.request.urlopen(url, timeout=120) as response, part.open('wb') as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
        if digest and sha(part) != digest:
            raise ValueError('Archive checksum mismatch: ' + target.name)
        os.replace(part, target)
        return target
    finally:
        part.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'config' / 'native-runtime.json', help='Hash manifest JSON')
    parser.add_argument('--cache', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(ROOT))) / 'VARIANT-1-build-cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'bin')
    parser.add_argument('--replace', action='store_true', help='Replace only manifest-listed files with verified release bytes')
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not manifest.get('files'):
        raise SystemExit('Native manifest has no files (stub?): ' + str(manifest_path))
    cache, output = args.cache.resolve(), args.output.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    rows = manifest['files']
    for row in rows:
        if Path(row['file']).name != row['file']:
            raise ValueError('Native output must be a filename')
        target = output / row['file']
        if target.exists() and sha(target) != row['sha256'] and not args.replace:
            raise ValueError('Different native bytes already exist: ' + str(target) + '; use --replace explicitly')

    def publish(stream, row):
        target = output / row['file']
        part = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
        try:
            with part.open('wb') as out:
                shutil.copyfileobj(stream, out, 1024 * 1024)
            if sha(part) != row['sha256']:
                raise ValueError('Native file checksum mismatch: ' + row['file'])
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)

    for archive in manifest['archives']:
        selected = {r['member']: r for r in rows if r['source_archive'] == archive['file']
                    and (not (output / r['file']).is_file() or sha(output / r['file']) != r['sha256'])}
        if not selected:
            continue
        path = fetch(archive['url'], cache / archive['file'], archive['sha256'])
        print('Verified archive: ' + archive['file'], flush=True)
        if path.suffix == '.zip':
            with zipfile.ZipFile(path) as package:
                for member, row in selected.items():
                    with package.open(member) as stream:
                        publish(stream, row)
            selected.clear()
        else:
            with tarfile.open(path, 'r|xz') as package:
                for member in package:
                    row = selected.get(member.name)
                    if row is not None and member.isfile():
                        with package.extractfile(member) as stream:
                            publish(stream, row)
                        del selected[member.name]
                        if not selected:
                            break
        if selected:
            raise ValueError('Native archive is missing required members')
    licenses = ROOT / 'assets/licenses/native'
    licenses.mkdir(parents=True, exist_ok=True)
    for notice in manifest['license_sources']:
        fetch(notice['url'], licenses / notice['file'])
    print(f'Prepared {len(rows)} verified native files with upstream notices; no models installed.')


if __name__ == '__main__':
    main()
