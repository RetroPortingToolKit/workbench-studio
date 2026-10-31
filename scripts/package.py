"""Package source and a rebuilt frontend with the checked CPython runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-dir', type=Path, default=root/'build/native')
    parser.add_argument('--output', type=Path, default=root/'build/package/WorkbenchStudio')
    parser.add_argument('--runtime-archive', type=Path, default=root/'dist/WorkbenchStudio-Windows-preview.zip')
    args = parser.parse_args()
    expected = json.loads((root/'DEPENDENCIES.json').read_text(encoding='utf-8'))['checked_distribution']['sha256']
    if digest(args.runtime_archive) != expected:
        raise ValueError('Runtime archive differs from the checked distribution')
    native = args.native_dir/'Workbench Studio.exe'
    if not native.is_file():
        raise ValueError('Build the native frontend first, or pass --native-dir')
    destination = args.output.resolve()
    if destination.exists():
        raise ValueError('Output exists; choose a fresh --output directory')
    shutil.copytree(root/'src', destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    with zipfile.ZipFile(args.runtime_archive) as archive:
        prefix = 'WorkbenchStudio/runtime/'
        for member in archive.infolist():
            if member.filename.startswith(prefix) and not member.is_dir():
                relative = Path(member.filename.removeprefix('WorkbenchStudio/'))
                target = destination/relative
                if not target.resolve().is_relative_to(destination):
                    raise ValueError('Unsafe runtime archive member')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    shutil.copy2(native, destination/native.name)
    shutil.copy2(root/'src/versions/sdl-install/bin/SDL3.dll', destination/'SDL3.dll')
    shutil.copy2(root/'docs/QUICKSTART.txt', destination/'QUICKSTART.txt')
    shutil.copy2(root/'LICENSE', destination/'LICENSE')
    (destination/'Workbench Studio.cmd').write_text('@echo off\nset "PYTHONDONTWRITEBYTECODE=1"\nstart "" /D "%~dp0" "%~dp0Workbench Studio.exe" %*\n', encoding='utf-8')
    members = {p.relative_to(destination).as_posix(): digest(p) for p in sorted(destination.rglob('*')) if p.is_file()}
    (destination/'SHA256SUMS.txt').write_text(''.join(value+'  '+name+'\n' for name,value in members.items()), encoding='utf-8')
    target = destination.parent/'WorkbenchStudio-Windows-preview.zip'
    if target.exists():
        raise ValueError('Archive exists; choose a fresh output parent directory')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                archive.write(path, Path('WorkbenchStudio')/path.relative_to(destination))
    checksum = digest(target)
    target.with_suffix('.zip.sha256').write_text(checksum+'  '+target.name+'\n',encoding='ascii')
    print(json.dumps(dict(archive=str(target),bytes=target.stat().st_size,sha256=checksum)))


if __name__ == '__main__':
    main()
