import os
from pathlib import Path
import sys
import zipfile
TOOL = 'workbench zip_step 1'
FLAGS = {'r', 'q'}
LEVEL = 6

def describe():
    import platform
    import zlib
    return {'tool': TOOL, 'python': platform.python_version(), 'zlib': zlib.ZLIB_RUNTIME_VERSION, 'compression': f'deflate level {LEVEL}'}

def parse(argv):
    flags, rest = (set(), [])
    for argument in argv:
        if argument.startswith('-') and len(argument) > 1:
            flags.update(argument[1:])
        else:
            rest.append(argument)
    if flags != FLAGS or len(rest) != 2 or rest[1] != '.':
        raise ValueError('this zip does one thing, `zip -r -q <archive> .`; it was called as: zip ' + ' '.join(argv))
    return (Path(rest[0]), Path('.'))

def make(archive, source):
    archive, source = (Path(archive), Path(source).resolve())
    if archive.exists():
        raise ValueError(f'{archive} exists already; the packager removes an old package before it calls zip')
    target = archive.resolve()
    files = folders = 0
    try:
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=LEVEL) as output:
            for folder, names, members in os.walk(source):
                names.sort()
                relative = Path(folder).relative_to(source)
                if relative.parts:
                    output.write(folder, relative.as_posix() + '/')
                    folders += 1
                for member in sorted(members):
                    path = Path(folder) / member
                    if path.resolve() == target:
                        continue
                    output.write(path, (relative / member).as_posix())
                    files += 1
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return (files, folders)

def main(argv=None):
    try:
        archive, source = parse(sys.argv[1:] if argv is None else argv)
        make(archive, source)
    except (ValueError, OSError) as error:
        print(f'zip error: {error}', file=sys.stderr)
        return 2
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
