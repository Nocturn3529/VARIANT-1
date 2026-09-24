"""Stage pinned, hash-verified native runtime files into bin/.

Build tool only. Archives stay in a build cache. Manifest-listed files are
written to bin; Unix archives also recreate soname symlinks and executable bits.
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


def apply_mode(path: Path, mode: int) -> None:
    try:
        current = path.stat().st_mode
        os.chmod(path, current | (mode & 0o111))
    except OSError:
        pass


def link_sonames(package: tarfile.TarFile, output: Path, extracted_names: set[str]) -> int:
    made = 0
    for info in package.getmembers():
        if not info.issym():
            continue
        link_name = Path(info.name).name
        target_name = Path(info.linkname).name
        if not link_name or not target_name:
            continue
        if target_name not in extracted_names and not (output / target_name).exists():
            continue
        dest = output / link_name
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        os.symlink(target_name, dest)
        made += 1
    return made


def tar_mode_for(path: Path) -> str:
    name = path.name.lower()
    if name.endswith('.tar.gz') or name.endswith('.tgz'):
        return 'r:gz'
    if name.endswith('.tar.xz'):
        return 'r:xz'
    if name.endswith('.tar.bz2'):
        return 'r:bz2'
    return 'r:*'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'config' / 'native-runtime.json')
    parser.add_argument('--cache', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(ROOT))) / 'VARIANT-1-build-cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'bin')
    parser.add_argument('--replace', action='store_true')
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
        if target.exists() and target.is_file() and sha(target) != row['sha256'] and not args.replace:
            raise ValueError('Different native bytes already exist: ' + str(target) + '; use --replace explicitly')

    def publish(stream, row, mode=0):
        target = output / row['file']
        part = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
        try:
            with part.open('wb') as out:
                shutil.copyfileobj(stream, out, 1024 * 1024)
            if sha(part) != row['sha256']:
                raise ValueError('Native file checksum mismatch: ' + row['file'])
            os.replace(part, target)
            if mode:
                apply_mode(target, mode)
        finally:
            part.unlink(missing_ok=True)

    extracted_names: set[str] = set()
    for archive in manifest['archives']:
        archive_rows = [r for r in rows if r['source_archive'] == archive['file']]
        selected = {
            r['member']: r for r in archive_rows
            if (not (output / r['file']).is_file() or sha(output / r['file']) != r['sha256'])
        }
        path = fetch(archive['url'], cache / archive['file'], archive['sha256'])
        if selected:
            print('Verified archive: ' + archive['file'], flush=True)
        if path.suffix == '.zip':
            if selected:
                with zipfile.ZipFile(path) as package:
                    for member, row in selected.items():
                        with package.open(member) as stream:
                            publish(stream, row)
                        extracted_names.add(row['file'])
                selected.clear()
        else:
            with tarfile.open(path, tar_mode_for(path)) as package:
                if selected:
                    for member, row in list(selected.items()):
                        try:
                            info = package.getmember(member)
                        except KeyError:
                            continue
                        if not info.isfile():
                            continue
                        stream = package.extractfile(info)
                        if stream is None:
                            continue
                        with stream:
                            publish(stream, row, mode=info.mode)
                        extracted_names.add(row['file'])
                        del selected[member]
                extracted_names.update(r['file'] for r in archive_rows)
                # Second pass: chmod from archive for already-present files.
                for row in archive_rows:
                    try:
                        info = package.getmember(row['member'])
                    except KeyError:
                        continue
                    target = output / row['file']
                    if target.is_file() and info.isfile():
                        apply_mode(target, info.mode)
                links = link_sonames(package, output, extracted_names)
                if links:
                    print(f'Restored {links} soname symlink(s) from {archive["file"]}', flush=True)
        if selected:
            raise ValueError('Native archive is missing required members')
    licenses = ROOT / 'assets/licenses/native'
    licenses.mkdir(parents=True, exist_ok=True)
    for notice in manifest['license_sources']:
        fetch(notice['url'], licenses / notice['file'])
    print(f'Prepared {len(rows)} verified native files with upstream notices; no models installed.')


if __name__ == '__main__':
    main()
