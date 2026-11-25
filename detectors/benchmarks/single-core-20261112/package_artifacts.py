"""Package verified benchmark evidence without environments or caches."""
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    files = [path for path in root.iterdir() if path.is_file() and path.name != 'artifact-manifest.json']
    for folder in ('snapshot', 'results'):
        files.extend(path for path in (root / folder).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    manifest = [{'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size,
                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(files)]
    manifest_path = root / 'artifact-manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    files.append(manifest_path)
    if args.install:
        destination = args.install.resolve()
        expected = Path(r'C:\Users\dimaz\IdeaProjects\Atropos\detectors\benchmarks\single-core-20261112').resolve()
        if destination != expected:
            raise ValueError('Unexpected installation target')
        for path in files:
            target = destination / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest()
    archive = root.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(files):
            bundle.write(path, arcname=root.name + '/' + path.relative_to(root).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
    print(json.dumps({'files': len(files), 'archive': str(archive), 'archive_bytes': archive.stat().st_size,
                      'installed': str(args.install) if args.install else None}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
