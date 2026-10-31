"""Verify distribution contents and its bundled Windows runtime without media."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile


def run(argv, cwd, env):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=30,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def main():
    archive = Path(sys.argv[1]).resolve()
    wanted = archive.with_suffix('.zip.sha256').read_text(encoding='ascii').split()[0]
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert actual == wanted, 'Archive checksum differs'
    with tempfile.TemporaryDirectory(prefix='studio-package-', dir=archive.parent) as folder:
        root = Path(folder)
        with zipfile.ZipFile(archive) as package:
            for name in package.namelist():
                assert (root/name).resolve().is_relative_to(root), 'Unsafe archive member'
            package.extractall(root)
        app = root/'WorkbenchStudio'
        inventory = (app/'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines()
        for line in inventory:
            digest, name = line.split('  ', 1)
            assert hashlib.sha256((app/name).read_bytes()).hexdigest() == digest, name
        prohibited = {'.chd','.cue','.iso','.sbi','.sav','.mcr','.key','.pem'}
        for path in app.rglob('*'):
            relative = path.relative_to(app)
            assert not any(part in {'public-studio-data','history','cache','__pycache__','.git'} for part in relative.parts), relative
            assert path.suffix.lower() not in prohibited, relative
            if path.is_file() and path.suffix in {'.py','.ps1','.json','.toml','.cmd'}:
                text = path.read_text(encoding='utf-8',errors='replace')
                assert not re.search(r'[IZ]:[/\\](?:Projects|Share)|gh[pousr]_[A-Za-z0-9_]{24,}', text), relative
            if path.is_file() and path.suffix == '.py':
                compile(path.read_text(encoding='utf-8'),str(path),'exec')
        env = os.environ.copy()
        env.update(LOCALAPPDATA=str(root/'profile'),PYTHONDONTWRITEBYTECODE='1')
        request = root/'request.json'
        request.write_text(json.dumps(dict(action='settings',data_root=str(root/'data'))),encoding='utf-8')
        process = run([str(app/'runtime/python.exe'),'-B',str(app/'native_bridge.py'),str(request)],app,env)
        assert process.returncode == 0, process.stderr
        settings = json.loads(process.stdout)['settings']
        assert not settings['bios_files'] and not settings['output_folder'] and not settings['startup_checks']
        assert settings['core_pin'] == str(app/'versions/PIN.json') and settings['ui_pin'] == str(app/'versions/UI.json')
        assert not (root/'profile').exists() and not (root/'data').exists(), 'Startup created payload cache/settings'
        sdl = ctypes.WinDLL(str(app/'SDL3.dll'))
        sdl.SDL_GetVersion.restype = ctypes.c_int
        assert sdl.SDL_GetVersion() == 3004010, 'Unexpected SDL version'
        ctypes.windll.kernel32.FreeLibrary.argtypes = [ctypes.c_void_p]
        assert ctypes.windll.kernel32.FreeLibrary(sdl._handle), 'Could not release SDL provider'
        del sdl
        preview = run([str(app/'Workbench Studio.exe'),'--preview',str(root/'native.png'),'--data-root',str(root/'preview-data')],app,env)
        if preview.returncode == 0:
            assert (root/'native.png').is_file(), 'Native preview missing'
            native = 'hidden preview rendered'
        elif os.environ.get('GITHUB_ACTIONS') == 'true' and preview.returncode in {3,4}:
            native = 'SKIP pixels: runner hidden-window/OpenGL setup unavailable (exit '+str(preview.returncode)+'); SDL and bundled bridge passed'
            print(native)
        else:
            raise AssertionError('Native hidden preview failed: '+str(preview.returncode)+' '+preview.stderr)
        assert not (root/'profile').exists(), 'Native startup created payload cache'
        result = dict(status='pass',sha256=actual,inventory_files=len(inventory),blank_settings=True,
                      bundled_python_bridge=True,shared_sdl_version=3004010,native=native,
                      source_payload_cache_created=False,media_bundled=False)
        (archive.parent/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
