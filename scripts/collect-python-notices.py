"""Collect installed locked-dependency license texts for the release package."""
from importlib import metadata
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
names = []
for line in (root / 'backend/requirements.lock').read_text().splitlines():
    if line and not line.startswith('#'):
        names.append(re.split(r'[=\[]', line)[0])
notices = ['Dependency notices for the locked Python build environment. Components retain their own licenses.']
for name in sorted(names):
    dist = metadata.distribution(name)
    paths = [p for p in dist.files or [] if any(
        part.lower().startswith(('license', 'copying', 'notice')) for part in p.parts)]
    notices.append(f'\n{name} {dist.version}\n' + '-' * 60)
    if paths:
        for item in paths:
            path = Path(dist.locate_file(item))
            if path.is_file():
                notices.append(str(item) + '\n' + path.read_text(encoding='utf-8', errors='replace'))
    else:
        notices.append(dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See upstream project license.')
        notices.extend(dist.metadata.get_all('Project-URL') or [])
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if not python_license.is_file():
    raise RuntimeError('CPython license text is required for release packaging')
notices.extend(['\nCPython\n' + '-' * 60, python_license.read_text(encoding='utf-8')])
target = root / 'backend/dist/Variant1Backend/_internal/THIRD_PARTY_LICENSES.txt'
target.write_text('\n'.join(notices), encoding='utf-8')
print('Collected locked Python dependency and CPython notices.')
