from __future__ import annotations
import hashlib
import array
import ast
from difflib import SequenceMatcher
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
try:
    from . import workbench_compile
except ImportError:
    import workbench_compile
try:
    from . import baseline_workflow, build_jobs, chd_intake, studio_settings, ui_selection, workbench_progress as progress
    from .build_jobs import resolve_build_jobs
except ImportError:
    import baseline_workflow
    import build_jobs
    import chd_intake
    import studio_settings
    import ui_selection
    import workbench_progress as progress
    from build_jobs import resolve_build_jobs
PIN_ROOT = None
SOURCE = None
UI = None
TOOLCHAIN_PIN = None
PACKAGE = None
KIT_ROOTS = []
KIT_INVENTORY = None
OVERLAY_ARCHIVES = []
OVERLAY_ARCHIVES_ENV = 'WORKBENCH_OVERLAY_ARCHIVES'
SOURCE_ENV = 'WORKBENCH_SOURCE_REPO'
UI_ENV = 'WORKBENCH_UI_REPO'
TOOLCHAIN_PIN_ENV = 'WORKBENCH_TOOLCHAIN_PIN'
TOOLCHAIN_ENV = 'WORKBENCH_TOOLCHAIN'
PACKAGE_ENV = 'WORKBENCH_PACKAGE'
KIT_ROOTS_ENV = 'WORKBENCH_KIT_ROOTS'
PLATFORMS = ('windows-x64', 'linux-x64', 'macos-x64', 'macos-arm64')
PIN_REQUIRED = ('name', 'repo', 'branch', 'commit', 'tree', 'boot_state_version', 'recomp_ui_ref', 'toolchain_pin', 'status')
PIN_BUILDABLE = ('test', 'qualified')
PIN_IDENTITY = tuple((k for k in PIN_REQUIRED if k != 'status')) + ('tas_stateio_compatibility', 'development_stub', 'ui_host_contract')

class BuildStop(RuntimeError):
    pass

class KitSerialCollision(BuildStop):

    def __init__(self, message, record):
        super().__init__(message)
        self.record = record

class ProgramSet(BuildStop):

    def __init__(self, plan):
        names = ', '.join((p['program'] for p in plan['programs']))
        super().__init__(f"The selected discs are the {len(plan['programs'])} programs of one set, {plan['title']} ({names}). Each program is built on its own and the products are joined in one folder.")
        self.plan = plan

    def bind(self, serials, paths):
        by_serial = dict(zip(serials, map(str, paths)))
        for program in self.plan['programs']:
            program['discs'] = [by_serial[s] for s in program['serials'] if s in by_serial]
        return self

def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def identity(path):
    path = Path(path)
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha(path)}

def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

def git(repo, *args):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if p.returncode:
        raise BuildStop(f"Local source identity is unavailable: {repo}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode().strip()

def load_pin(path):
    path = Path(path).resolve()
    pin = json.loads(path.read_text(encoding='utf-8-sig'))
    missing = [key for key in PIN_REQUIRED if key != 'status' and (not pin.get(key))]
    if missing:
        raise BuildStop('This core pin lacks required PSX build identities: ' + ', '.join(missing))
    if any((not re.fullmatch('[0-9a-f]{40}', str(pin[k])) for k in ('commit', 'tree'))):
        raise BuildStop('The pin must contain full commit and tree hashes.')
    return pin
NO_FETCH_ENV = 'WORKBENCH_NO_FETCH'

def pin_ui_commitish(pin):
    ref = pin.get('recomp_ui_ref')
    if not ref:
        return None
    if isinstance(ref, dict):
        return ref.get('commit') or ref.get('ref')
    parts = str(ref).split()
    return parts[1] if len(parts) == 2 else str(ref)

def has_commit(repo, ref):
    try:
        git(repo, 'cat-file', '-e', f'{ref}^{{commit}}')
    except BuildStop:
        return False
    return True

def input_default(name, fallback, environ=None):
    value = (os.environ if environ is None else environ).get(name, '').strip()
    if value:
        return Path(value)
    return None if fallback is None else Path(fallback)

def environment_kit_roots(environ=None):
    value = (os.environ if environ is None else environ).get(KIT_ROOTS_ENV, '')
    return [Path(item.strip()) for item in value.split(os.pathsep) if item.strip()]

def require_pin_sources(pin, source_repo, ui_repo, receipt, environ=None, ui_ref=None):
    environ = os.environ if environ is None else environ
    wanted = (('framework (psxrecomp)', source_repo, pin.get('commit'), pin.get('branch'), 'source_repo'), ('recomp-ui', ui_repo, ui_ref or pin_ui_commitish(pin), None, 'ui_repo'))
    for label, repo, ref, branch, setting in wanted:
        if not repo or not ref:
            continue
        repo = Path(repo)
        if setting == 'ui_repo':
            from . import source_snapshot
            if source_snapshot.validate(repo, ref):
                continue
        try:
            git(repo, 'rev-parse', '--git-dir')
        except BuildStop:
            raise BuildStop(f'The {label} clone the pin needs is not a git repository: {repo}. Clone it there, or change {studio_settings.describe(setting)}.') from None
        if has_commit(repo, ref):
            continue
        fetched = None
        if environ.get(NO_FETCH_ENV) != '1':
            try:
                result = subprocess.run(['git', '-C', str(repo), 'fetch', '--quiet', '--tags', 'origin'], capture_output=True, timeout=300)
                fetched = {'repo': str(repo), 'wanted': ref, 'ok': result.returncode == 0, 'error': result.stderr.decode(errors='replace').strip() or None}
            except (OSError, subprocess.TimeoutExpired) as error:
                fetched = {'repo': str(repo), 'wanted': ref, 'ok': False, 'error': str(error)}
            receipt.setdefault('source_fetch', []).append(fetched)
        if has_commit(repo, ref):
            continue
        if fetched is None:
            tried = f' Fetching is switched off ({NO_FETCH_ENV}=1).'
        elif fetched['ok']:
            tried = ' `git fetch origin` ran and did not bring it.'
        else:
            tried = f" `git fetch origin` failed: {fetched['error']}."
        where = f'origin {branch}' if branch else '<the remote and branch that hold it>'
        raise BuildStop(f'The pin needs {label} commit {ref}, which the local clone {repo} does not have.{tried} Fetch it (git -C "{repo}" fetch {where}), then build again. Nothing was built.')

def registry_pin_files(root):
    root = Path(root)
    direct = sorted(root.glob('*/PIN.json'))
    pins_dirs = {path.parent for path in direct}
    grouped = sorted((path for path in root.glob('*/*/PIN.json') if path.parent.parent not in pins_dirs))
    return direct + grouped

def pins(root=PIN_ROOT, pin_file=None, ui_root=None, ui_files=None):
    binding = ui_selection.discover_registry(root, ui_root)
    root = Path(binding['pin_root']) if binding['pin_root'] else None
    result = []
    paths = [Path(pin_file).resolve()] if pin_file else registry_pin_files(root) if root else []
    for path in paths:
        try:
            pin = load_pin(path)
            result.append({'path': str(path), 'name': pin['name'], 'status': pin['status'], 'development_stub': bool(pin.get('development_stub')), 'error': None, 'core': {'commit': pin['commit'], 'tree': pin['tree']}, 'ui_options': ui_selection.options(pin, binding['ui_root'], ui_files or ())})
        except (ValueError, OSError, BuildStop) as e:
            result.append({'path': str(path), 'name': path.parent.name, 'status': 'invalid', 'error': str(e)})
    return {'schema': 2, 'pins': result, 'registry': binding, 'message': '' if result else binding['message'] or f'No PIN.json records in {root}.'}

def check_compiler_capability(source, input_mode):
    if 'resolve_overlay_compiler_path(tk_dir, line)' not in source:
        raise BuildStop('Private lean Clang needs a pin with toolchain-relative compiler lookup. Select the derived portable-Clang TEST pin or a later compatible pin.')
    if input_mode == 'thin' and '"compiler-store.sha256"' not in source:
        raise BuildStop('Thin shared Clang needs a pin with strict shared-store validation. Select the shared-overlay TEST pin or build as portable.')
SUBMODULE_FALLBACK = None
SUBMODULE_FALLBACK_ENV = 'WORKBENCH_SUBMODULE_FALLBACK'

def submodule_fallback(environ=None):
    return input_default(SUBMODULE_FALLBACK_ENV, SUBMODULE_FALLBACK, environ)

def snapshot(repo, ref, dest, records):
    from . import source_snapshot
    if source_snapshot.export(repo, ref, dest, records):
        return
    if git(repo, 'rev-parse', '--show-prefix'):
        raise BuildStop(f'Local submodule objects are missing at {repo}. Initialize them before the offline build.')
    commit = git(repo, 'rev-parse', f'{ref}^{{commit}}')
    tree = git(repo, 'rev-parse', f'{commit}^{{tree}}')
    proc = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', commit], capture_output=True)
    if proc.returncode:
        raise BuildStop(f'Cannot export source commit {commit} from {repo}.')
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
        archive.extractall(dest, filter='data')
    records.append({'repo': str(repo), 'commit': commit, 'tree': tree, 'destination': str(dest)})
    for row in git(repo, 'ls-tree', '-r', commit).splitlines():
        metadata, name = row.split('\t', 1)
        mode, kind, oid = metadata.split()
        if kind == 'commit':
            child = repo / name
            if not child.exists() or git(child, 'rev-parse', '--show-prefix'):
                fallback = submodule_fallback()
                if fallback is not None:
                    child = fallback / name
            if not (child / '.git').exists():
                raise BuildStop(f'The source repository {repo} has no checkout of its submodule {name} (commit {oid[:9]}). Clone it with --recurse-submodules, or run `git -C "{repo}" submodule update --init --recursive`, then build again.')
            snapshot(child, oid, dest / name, records)

def inventory(root):
    return [{'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha(p)} for p in sorted(root.rglob('*')) if p.is_file() and '.git' not in p.relative_to(root).parts]

def record_inputs(paths, receipt):
    for path in paths:
        item = identity(path)
        if item not in receipt['inputs']:
            receipt['inputs'].append(item)

def check_inputs(receipt):
    for before in receipt['inputs']:
        if identity(before['path']) != before:
            raise BuildStop(f"An input changed during the build: {before['path']}. Use a new output and retry.")

def stage_disc(source, dest, receipt, chdman=None):
    source = Path(source)
    if source.suffix.lower() == '.cue':
        from backend import stage_disc as stage_cue
        tracks = chd_intake.validate_cue(source)
        record_inputs([source] + [source.parent / track['path'] for track in tracks], receipt)
        result = stage_cue(source, dest)
    elif source.suffix.lower() in ('.bin', '.chd'):
        record_inputs([source], receipt)
        dest.mkdir(parents=True)
        result = dest / source.name
        shutil.copy2(source, result)
    else:
        raise BuildStop('Select a CUE, raw BIN, or CHD disc image.')
    sbi = source.with_suffix('.sbi')
    if sbi.is_file():
        record_inputs([sbi], receipt)
        shutil.copy2(sbi, result.with_suffix('.sbi'))
    return result

def read_chd_boot_serial(chd, chdman, work):
    try:
        with tempfile.TemporaryDirectory(dir=work) as temp:
            out = Path(temp)
            proc = subprocess.run([str(chdman), 'extractcd', '-i', str(chd), '-o', str(out / 's.cue'), '-ob', str(out / 's.bin')], capture_output=True)
            if proc.returncode != 0:
                return None
            bin_path = out / 's.bin'
            if not bin_path.is_file():
                return None
            with bin_path.open('rb') as stream:
                for user_off in (24, 16):
                    stream.seek(16 * 2352)
                    raw = stream.read(2352)
                    if len(raw) != 2352 or (raw[user_off - 1:user_off + 5][:5] != b'CD001' and raw[user_off + 1:user_off + 6] != b'CD001'):
                        continue
                    pvd = raw[user_off:user_off + 2048]
                    root = pvd[156:190]
                    extent = int.from_bytes(root[2:6], 'little')
                    size = int.from_bytes(root[10:14], 'little')
                    if not 0 < size <= 65536:
                        continue
                    stream.seek(extent * 2352)
                    directory = b''.join((stream.read(2352)[user_off:user_off + 2048] for _ in range((size + 2047) // 2048)))[:size]
                    entry = find_dir_entry(directory, b'SYSTEM.CNF')
                    if entry is None:
                        if find_dir_entry(directory, b'PSX.EXE') is None:
                            return None
                        return baseline_workflow.volume_serial(pvd[40:72].decode('ascii', 'replace'))
                    cnf_lba, cnf_size = entry
                    stream.seek(cnf_lba * 2352)
                    raw = stream.read(2352)
                    text = raw[user_off:user_off + min(cnf_size, 2048)].decode('ascii', 'replace')
                    match = re.search('BOOT\\s*=\\s*cdrom:\\\\*([^;\\r\\n]+)', text, re.IGNORECASE)
                    if not match:
                        return None
                    boot = match.group(1).replace('\\', '/').lstrip('/')
                    return normalize_boot_serial(boot)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None

def find_dir_entry(directory, name):
    position = 0
    while position < len(directory):
        length = directory[position]
        if length == 0:
            position = (position // 2048 + 1) * 2048
            continue
        if position + 34 > len(directory):
            return None
        record = directory[position:position + length]
        name_length = record[32]
        entry_name = record[33:33 + name_length].decode('ascii', errors='replace')
        if entry_name.split(';')[0].upper() == name.decode().upper():
            return (int.from_bytes(record[2:6], 'little'), int.from_bytes(record[10:14], 'little'))
        position += length
    return None

def normalize_boot_serial(boot):
    stem = boot.replace('\\', '/').split('/')[-1].split(';')[0].strip()
    match = re.match('([A-Za-z]{4})_(\\d{3})\\.(\\d{2})$', stem)
    if match:
        return f'{match.group(1)}-{match.group(2)}{match.group(3)}'.upper()
    return stem.upper()

def read_chd_disc_facts(chd, psx_chd, lib_path):
    try:
        try:
            from . import chd_source
        except ImportError:
            import chd_source
        with chd_source.ChdTrackSource(chd, psx_chd, lib_path) as source:
            read_user = source.read_user
            pvd = read_user(None, 16)
            if pvd[1:6] != b'CD001':
                return None
            root_extent = int.from_bytes(pvd[158:162], 'little')
            root_size = int.from_bytes(pvd[166:170], 'little')
            if not 0 < root_size <= 65536:
                return None
            root = bytearray()
            for i in range((root_size + 2047) // 2048):
                root += read_user(None, root_extent + i)
            entries = {}
            blob, i = (bytes(root[:root_size]), 0)
            while i < len(blob):
                reclen = blob[i]
                if reclen == 0:
                    i = (i // 2048 + 1) * 2048
                    if i >= len(blob):
                        break
                    continue
                if i + reclen > len(blob):
                    break
                extent = int.from_bytes(blob[i + 2:i + 6], 'little')
                size = int.from_bytes(blob[i + 10:i + 14], 'little')
                namelen = blob[i + 32]
                name = blob[i + 33:i + 33 + namelen].split(b';')[0]
                if name not in (b'\x00', b'\x01'):
                    entries[name.decode('ascii', 'replace').upper()] = (extent, size)
                i += reclen
            volume = pvd[40:72].decode('ascii', 'replace').strip()
            cnf = entries.get('SYSTEM.CNF')
            if cnf is None:
                entry = entries.get(baseline_workflow.BIOS_FALLBACK_BOOT)
                serial = baseline_workflow.volume_serial(volume)
                if entry is None or serial is None:
                    return None
                return {'volume': volume, 'entries': len(entries), 'boot': baseline_workflow.BIOS_FALLBACK_BOOT, 'boot_size': entry[1], 'serial': serial, 'system_cnf': None, 'boot_source': baseline_workflow.FALLBACK_BOOT_SOURCE, 'serial_source': 'volume identifier'}
            body, rem, lba = (bytearray(), cnf[1], cnf[0])
            while rem > 0:
                body += read_user(None, lba)[:min(2048, rem)]
                rem -= min(2048, rem)
                lba += 1
            text = bytes(body).decode('ascii', 'replace')
            match = re.search('BOOT\\s*=\\s*cdrom:\\\\*([^;\\r\\n]+)', text, re.IGNORECASE)
            if not match:
                return None
            boot = match.group(1).replace('\\', '/').lstrip('/').upper()
            entry = entries.get(boot)
            return {'volume': volume, 'entries': len(entries), 'boot': boot, 'boot_size': entry[1] if entry else None, 'serial': normalize_boot_serial(boot), 'system_cnf': cnf, 'boot_source': baseline_workflow.SYSTEM_CNF_BOOT, 'serial_source': 'boot executable name'}
    except Exception:
        return None

def preflight_kit_match(selected, chdman, kit_roots, explicit_kit, receipt, fw=None, toolchain=None):
    serials = []
    reader = 'none'
    psx_chd = None
    lib_path = None
    facts_by_disc = {}
    if any((Path(path).suffix.lower() != '.chd' for path in selected)):
        return None
    if fw is not None:
        psx_chd = ensure_libchdr(fw, receipt.get('output') or fw.parent, receipt, toolchain=toolchain)
        if psx_chd is not None:
            lib_path = psx_chd.find_libchdr(fw.parent, fw) or psx_chd.find_libchdr(None, fw)
            if lib_path is not None:
                reader = 'libchdr'
                held = Path(tempfile.mkdtemp(prefix='psxwb-libchdr-'))
                shutil.copy2(lib_path, held / Path(lib_path).name)
                lib_path = held / Path(lib_path).name
    for path in selected:
        source = Path(path)
        serial = None
        if psx_chd is not None and lib_path is not None:
            facts = read_chd_disc_facts(source, psx_chd, lib_path)
            if facts is not None:
                serial = facts['serial']
                facts_by_disc[str(source)] = facts
        if serial is None:
            serial = read_chd_boot_serial(source, chdman, Path(receipt['output']))
            if serial is not None:
                reader = 'chdman'
        if serial is None:
            return None
        serials.append(serial)
    if not serials:
        return None
    diagnostics = []
    try:
        kit = find_kit(serials, kit_roots, explicit_kit, diagnostics)
    except ProgramSet as programs:
        raise programs.bind(serials, selected)
    return {'serials': serials, 'kit': kit, 'diagnostics': diagnostics, 'reader': reader, 'psx_chd': psx_chd, 'lib_path': lib_path, 'disc_facts': facts_by_disc}

def ensure_libchdr(fw, output, receipt, env=None, toolchain=None):
    psx_chd = load_psx_chd(fw)
    if psx_chd is not None:
        return psx_chd
    reader_build = fw.parent / 'build-chdr'
    packs = [Path(toolchain) / 'bin'] if toolchain else []
    packs.append(fw.parent / 'toolchain' / 'bin')

    def tool(name):
        for pack in packs:
            for candidate in (pack / (name + '.exe'), pack / name):
                if candidate.is_file():
                    return str(candidate)
        return name
    cmake_exe = tool('cmake')
    env = dict(os.environ if env is None else env)
    if toolchain and packs[0].is_dir():
        env['PATH'] = str(packs[0]) + os.pathsep + env.get('PATH', '')
    ninja = tool('ninja')
    output = Path(output)
    try:
        run_command([str(cmake_exe), '-S', str(fw / 'recompiler'), '-B', str(reader_build), '-G', 'Ninja', *([f'-DCMAKE_MAKE_PROGRAM={ninja}'] if ninja != 'ninja' else []), '-DCMAKE_BUILD_TYPE=Release', f"-DCMAKE_C_COMPILER={tool('clang')}", f"-DCMAKE_CXX_COMPILER={tool('clang++')}", '-DBUILD_TESTING=OFF', '-DPSXRECOMP_ENABLE_CHD=ON', '-DWITH_LZMA_ASM=OFF', '-DPSXRECOMP_STATIC_CLI=ON', '-DPSX_DEPS_OFFLINE=ON'], output, env, output, receipt, 'chd-reader:configure')
        run_command([str(cmake_exe), '--build', str(reader_build), '--target', 'chdr', f'-j{resolve_build_jobs()}'], output, env, output, receipt, 'chd-reader')
        built = next(iter(reader_build.glob('libchdr*.dll')), None) or next(iter(reader_build.rglob('libchdr*.dll')), None)
        if built is not None:
            destination = fw / 'recompiler' / 'build'
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built, destination / built.name)
            receipt['chd_reader'] = {'path': str(destination / built.name), 'source': str(reader_build)}
    except Exception as error:
        print(f'note: libchdr build skipped ({error})', flush=True)
        receipt['chd_reader'] = {'status': 'unavailable', 'error': str(error), 'source': str(reader_build)}
    psx_chd = load_psx_chd(fw)
    if psx_chd is None:
        receipt.setdefault('chd_reader', {'status': 'unavailable', 'source': str(reader_build)})
        receipt.setdefault('warnings', []).append("The pin's libchdr disc reader could not be built (see chd-reader.log), so disc facts come from chdman, which extracts whole tracks and is several times slower. The build result is the same.")
    return psx_chd

def bios_profiles(fw):
    found = {}
    bios_dir = Path(fw) / 'bios'
    if not bios_dir.is_dir():
        return found
    for profile in sorted(bios_dir.glob('*.toml')):
        try:
            text = profile.read_text(encoding='utf-8-sig')
            program = tomllib.loads(text).get('program') or {}
        except (OSError, ValueError):
            continue
        image = program.get('image') if isinstance(program, dict) else None
        digest = str((image or {}).get('sha256') or '').strip().lower()
        if len(digest) != 64:
            declared = re.search('SHA-?256\\s+([0-9a-fA-F]{64})', text)
            digest = declared.group(1).lower() if declared else ''
        if len(digest) == 64:
            found.setdefault(digest, profile.stem)
    return found

def region_bios_stem(serials):
    for serial in serials:
        prefix = str(serial).replace('-', '').upper()[:4]
        if prefix in ('SCES', 'SLES'):
            return 'SCPH5552'
        if prefix in ('SCUS', 'SLUS', 'LSP'):
            return 'SCPH1001'
        if prefix in ('SIPS', 'SLPS', 'SLPM', 'SCPS', 'ESPM', 'PAPX', 'PCPX'):
            return 'SCPH5500'
    return None

def resolve_bios_config(receipt, fw, serials, bios):
    profiles = bios_profiles(fw)
    try:
        digest = sha(bios).lower()
    except OSError:
        digest = ''
    if digest and digest in profiles:
        receipt['bios_selection'] = {'source': 'rom_sha256', 'stem': profiles[digest]}
        return profiles[digest]
    stem = region_bios_stem(serials)
    if stem is not None:
        receipt['bios_selection'] = {'source': 'serial_region', 'stem': stem}
        return stem
    receipt['bios_selection'] = {'source': 'unresolved', 'stem': None}
    return None

def profile_image_pin(fw, stem):
    try:
        program = tomllib.loads((Path(fw) / 'bios' / f'{stem}.toml').read_text(encoding='utf-8-sig')).get('program') or {}
    except (OSError, ValueError):
        return None
    image = program.get('image') if isinstance(program, dict) else None
    digest = str((image or {}).get('sha256') or '').strip().lower()
    return digest if len(digest) == 64 else None

def check_bios_identity(fw, stem, bios, receipt):
    expected = profile_image_pin(fw, stem)
    if expected is None:
        return
    try:
        actual = sha(bios).lower()
    except OSError:
        return
    if actual == expected:
        return
    matches = []
    try:
        size = Path(bios).stat().st_size
        for sibling in sorted(Path(bios).parent.iterdir()):
            if sibling.is_file() and sibling.stat().st_size == size and (sibling != Path(bios)) and (sha(sibling).lower() == expected):
                matches.append(str(sibling))
    except OSError:
        pass
    receipt['bios_identity'] = {'stem': stem, 'expected_sha256': expected, 'actual_sha256': actual, 'matching_files': matches}
    hint = f' This file in the same folder matches: {matches[0]}. Select it as the BIOS file.' if matches else ' Select a BIOS dump with that hash.'
    raise BuildStop(f'The BIOS file {Path(bios).name} is not the {stem} dump this game needs. {stem} expects SHA-256 {expected}; the file hashes to {actual}.{hint}')

def set_bios_config(path, stem):
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')
    line = f'bios_config = "psxrecomp/bios/{stem}.toml"'
    if re.search('(?m)^\\s*bios_config\\s*=', text):
        text = re.sub('(?m)^\\s*bios_config\\s*=.*$', line, text)
    elif re.search('(?m)^\\[recompiler\\]\\s*$', text):
        text = re.sub('(?m)^(\\[recompiler\\]\\s*)$', '\\1\\n' + line, text, count=1)
    else:
        text = text.rstrip('\n') + f'\n\n[recompiler]\n{line}\n'
    path.write_text(text, encoding='utf-8')

def ensure_kit_bios_config(config, kit_name, receipt, fw, serials, bios):
    sections = tomllib.loads(Path(config).read_text(encoding='utf-8-sig'))
    if sections.get('recompiler', {}).get('bios_config'):
        return sections
    stem = resolve_bios_config(receipt, fw, serials, bios)
    if stem is None:
        raise BuildStop(f'Kit {kit_name} declares no recompiler.bios_config, and no BIOS profile matches the selected ROM or the disc region. Add bios_config to the kit.')
    set_bios_config(config, stem)
    receipt.setdefault('warnings', []).append(f"Kit {kit_name} declares no bios_config; the build copy uses {stem} (from {receipt['bios_selection']['source']}).")
    return tomllib.loads(Path(config).read_text(encoding='utf-8-sig'))

def load_psx_chd(fw):
    tools = fw / 'tools'
    module_path = tools / 'psx_chd.py'
    if not module_path.is_file():
        return None
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        import psx_chd
        import importlib
        importlib.reload(psx_chd)
    except Exception:
        return None
    if psx_chd.find_libchdr(fw.parent, fw) is None and psx_chd.find_libchdr(None, fw) is None:
        return None
    return psx_chd

def read_chd_boot_serial_psx_chd(psx_chd, chd, lib_path):
    try:
        with psx_chd.ChdDisc(chd, psx_chd.LibChdr(lib_path)) as disc:
            track = next((t for t in disc.tracks if not t.is_audio), None)
            if track is None:
                return None

            def sector(lba, user_off):
                frame = track.chd_frame_start + lba
                hunk_index = frame // disc.frames_per_hunk
                within = frame % disc.frames_per_hunk
                hunk = disc._hunk(hunk_index)
                start = within * psx_chd.CD_FRAME_SIZE
                return hunk[start:start + psx_chd.RAW_SECTOR_SIZE]
            for user_off in (24, 16):
                raw = sector(16, user_off)
                if len(raw) < user_off + 2048:
                    continue
                pvd = raw[user_off:user_off + 2048]
                if pvd[1:6] != b'CD001':
                    continue
                root = pvd[156:190]
                extent = int.from_bytes(root[2:6], 'little')
                size = int.from_bytes(root[10:14], 'little')
                if not 0 < size <= 65536:
                    continue
                directory = bytearray()
                for lba in range(extent, extent + (size + 2047) // 2048):
                    raw = sector(lba, user_off)
                    if len(raw) < user_off + 2048:
                        return None
                    directory += raw[user_off:user_off + 2048]
                entry = find_dir_entry(bytes(directory[:size]), b'SYSTEM.CNF')
                if entry is None:
                    return None
                raw = sector(entry[0], user_off)
                if len(raw) < user_off + 2048:
                    return None
                text = raw[user_off:user_off + min(entry[1], 2048)].decode('ascii', 'replace')
                match = re.search('BOOT\\s*=\\s*cdrom:\\\\*([^;\\r\\n]+)', text, re.IGNORECASE)
                if not match:
                    return None
                return normalize_boot_serial(match.group(1).replace('\\', '/').lstrip('/'))
    except Exception:
        return None
    return None

def iso_boot_serial_from_track(blob):
    for user_off in (24, 16):
        pvd = sector_user(blob, 16, user_off)
        if pvd is None or pvd[1:6] != b'CD001':
            continue
        root = pvd[156:190]
        extent = int.from_bytes(root[2:6], 'little')
        size = int.from_bytes(root[10:14], 'little')
        if not 0 < size <= 65536:
            continue
        directory = bytearray()
        for lba in range(extent, extent + (size + 2047) // 2048):
            sector = sector_user(blob, lba, user_off)
            if sector is None:
                return None
            directory += sector
        entry = find_dir_entry(bytes(directory[:size]), b'SYSTEM.CNF')
        if entry is None:
            return None
        cnf = sector_user(blob, entry[0], user_off)
        if cnf is None:
            return None
        text = cnf[:entry[1]].decode('ascii', 'replace')
        match = re.search('BOOT\\s*=\\s*cdrom:\\\\*([^;\\r\\n]+)', text, re.IGNORECASE)
        if not match:
            return None
        return normalize_boot_serial(match.group(1).replace('\\', '/').lstrip('/'))
    return None

def sector_user(blob, lba, user_off):
    start = lba * 2352 + user_off
    if start < 0 or start + 2048 > len(blob):
        return None
    return blob[start:start + 2048]

def chd_digests_libchdr(chd, psx_chd, lib_path):
    frame_size = psx_chd.CD_FRAME_SIZE
    raw_size = psx_chd.RAW_SECTOR_SIZE
    try:
        with psx_chd.ChdDisc(chd, psx_chd.LibChdr(lib_path)) as disc:
            tracks, whole = ([], hashlib.sha1())
            for track in disc.tracks:
                per_track, size = (hashlib.sha1(), 0)
                frame = track.chd_frame_start
                end = frame + track.frames
                while frame < end:
                    hunk_index = frame // disc.frames_per_hunk
                    first = frame % disc.frames_per_hunk
                    count = min(disc.frames_per_hunk - first, end - frame)
                    hunk = disc._hunk(hunk_index)
                    if track.is_audio:
                        words = array.array('H')
                        words.frombytes(hunk)
                        words.byteswap()
                        hunk = words.tobytes()
                    base = first * frame_size
                    for i in range(count):
                        offset = base + i * frame_size
                        sector = hunk[offset:offset + raw_size]
                        per_track.update(sector)
                        whole.update(sector)
                    size += count * raw_size
                    frame += count
                tracks.append({'number': track.number, 'type': track.type, 'size': size, 'sha1': per_track.hexdigest()})
            return {'reader': 'libchdr', 'track_count': len(tracks), 'total_bytes': sum((t['size'] for t in tracks)), 'sha1': whole.hexdigest(), 'tracks': tracks}
    except Exception:
        return None

def kit_disc_name(prep):
    for key in ('cue_name', 'bin_name'):
        stem = Path(str(prep.get(key) or '')).stem
        if stem and (not re.fullmatch('(?i)disc\\s*\\d*', stem)):
            return stem
    return None

def title_stem(name):
    return re.split('\\s*\\(', Path(str(name)).stem, maxsplit=1)[0].strip().casefold()

def other_title(disc, prep):
    expected = kit_disc_name(prep)
    if not expected:
        return False
    theirs, ours = (title_stem(expected), title_stem(Path(disc).name))
    return bool(theirs) and bool(ours) and (theirs != ours)

def disc_revision_stop(disc, prep, receipt, kit, size, sha1):
    expected = kit_disc_name(prep)
    sizes = [int(s) for s in prep.get('known_sizes') or []]
    made_from = expected or 'the dump recorded in its prepare_disc digests'
    if sizes:
        made_from += f' ({sizes[0]:,} bytes)'
    matches = []
    if expected:
        matches = [str(p) for p in (Path(disc).parent / (expected + s) for s in ('.chd', '.cue')) if p.is_file() and p != Path(disc)]
    if other_title(disc, prep):
        record = {'selected': str(disc), 'selected_size': size, 'selected_sha1': sha1, 'kit': str(kit) if kit else None, 'kit_made_from': expected, 'kit_sizes': sizes}
        kit_label = f'Kit {Path(kit).name}' if kit else 'The kit'
        return KitSerialCollision(f"{kit_label} has this disc's boot serial but was made from a different game, {made_from}; the selected disc is {Path(disc).name} (data track {size:,} bytes, SHA-1 {sha1}). Two games share that serial, so the kit does not apply to this disc.", record)
    receipt['disc_revision'] = {'selected': str(disc), 'expected': expected, 'expected_sizes': sizes, 'expected_sha1': [str(x).lower() for x in prep.get('known_sha1') or []], 'selected_size': size, 'selected_sha1': sha1, 'matching_files': matches}
    hint = f' This file in the same folder has that name: {matches[0]}. Select it as the disc.' if matches else ' Select that revision of the disc.'
    kit_label = f'Kit {Path(kit).name}' if kit else 'The kit'
    return BuildStop(f'{kit_label} was made from {made_from}; the selected disc {Path(disc).name} is a different revision (data track {size:,} bytes, SHA-1 {sha1}).{hint}')

def verify_chd_libchdr(chd, psx_chd, lib_path, prep, receipt, boot=True, kit=None):
    digests = chd_digests_libchdr(chd, psx_chd, lib_path)
    if digests is None:
        return False
    sizes = [int(s) for s in prep.get('known_sizes') or []]
    sha1s = [str(x).lower() for x in prep.get('known_sha1') or []]
    if not sizes and (not sha1s):
        return True
    tracks = digests.get('tracks') or []
    ok = digests['sha1'] in sha1s or any((t['sha1'] in sha1s for t in tracks))
    if not ok and sizes and (not sha1s):
        ok = digests['total_bytes'] in sizes or any((t['size'] in sizes for t in tracks))
    if not ok and (not boot):
        receipt.setdefault('warnings', []).append(f"{Path(chd).name} was not checked against the kit's digests: prepare_disc describes the boot disc only.")
        receipt.setdefault('disc_verification', []).append({'path': str(chd), 'reader': 'libchdr', 'size': digests['total_bytes'], 'sha1': digests['sha1'], 'tracks': digests['track_count'], 'result': 'not covered by kit digests'})
        return True
    if not ok:
        data = tracks[0] if tracks else {'size': digests['total_bytes'], 'sha1': digests['sha1']}
        raise disc_revision_stop(chd, prep, receipt, kit, data['size'], data['sha1'])
    receipt.setdefault('disc_verification', []).append({'path': str(chd), 'reader': 'libchdr', 'size': digests['total_bytes'], 'sha1': digests['sha1'], 'tracks': digests['track_count']})
    return True

def intake_disc_contract(explicit_kit, preflight, count):
    kit = Path(explicit_kit) if explicit_kit else (preflight or {}).get('kit')
    prep, boot_serial = ({}, None)
    if kit is not None and (Path(kit) / 'game.toml').is_file():
        sections = tomllib.loads((Path(kit) / 'game.toml').read_text(encoding='utf-8-sig'))
        prep = sections.get('prepare_disc') or {}
        boot_serial = (sections.get('game') or {}).get('id')
    serials = (preflight or {}).get('serials') or []
    if boot_serial in serials and len(serials) == count:
        flags = [serial == boot_serial for serial in serials]
    else:
        flags = [n == 0 for n in range(count)]
    return (kit, prep, flags)

def verify_image_digests(disc, prep, receipt, kit=None):
    disc = Path(disc)
    sizes = [int(s) for s in prep.get('known_sizes') or []]
    md5s = [str(x).lower() for x in prep.get('known_md5') or []]
    sha1s = [str(x).lower() for x in prep.get('known_sha1') or []]
    if not (sizes or md5s or sha1s):
        return
    body = disc
    if disc.suffix.lower() == '.cue':
        match = re.search('(?im)^\\s*FILE\\s+(?:"([^"]+)"|(\\S+))\\s+BINARY', disc.read_text(encoding='utf-8-sig', errors='replace'))
        if not match:
            return
        body = disc.parent / (match.group(1) or match.group(2))
        if not body.is_file():
            return
    md5, sha1 = (hashlib.md5(), hashlib.sha1())
    with body.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            md5.update(chunk)
            sha1.update(chunk)
    size = body.stat().st_size
    ok = md5.hexdigest() in md5s or sha1.hexdigest() in sha1s or (not md5s and (not sha1s) and (size in sizes))
    if not ok:
        raise disc_revision_stop(disc, prep, receipt, kit, size, sha1.hexdigest())
    receipt.setdefault('disc_verification', []).append({'path': str(disc), 'reader': 'file', 'size': size, 'sha1': sha1.hexdigest()})

def generation_disc(source, temp, receipt, chdman, psx_chd=None, lib_path=None):
    source = Path(source)
    paths = [source]
    if source.suffix.lower() == '.cue':
        paths += [source.parent / t['path'] for t in chd_intake.validate_cue(source)]
    elif source.suffix.lower() not in ('.bin', '.chd'):
        raise BuildStop('Select a CUE, raw BIN, or CHD disc image.')
    if source.with_suffix('.sbi').is_file():
        paths.append(source.with_suffix('.sbi'))
    record_inputs(paths, receipt)
    if source.suffix.lower() != '.chd':
        return source
    if psx_chd is not None and lib_path is not None:
        try:
            cue = psx_chd.extract(source, temp, f'{source.stem}.cue', layout='single', bin_name=f'{source.stem}.bin')
            receipt.setdefault('chd_intake', []).append({'schema': 1, 'result': 'pass', 'reader': 'libchdr', 'source': str(source), 'cue': {'path': Path(cue).name}})
            return Path(cue)
        except Exception as error:
            print(f'note: libchdr read failed ({error}); using chdman', flush=True)
    record_inputs([chdman], receipt)
    if receipt.get('output'):
        output = Path(receipt['output'])
        result = chd_intake.extract(source, temp, chdman, runner=lambda argv, phase: run_command(argv, output, os.environ.copy(), output, receipt, f'{temp.name}-chd-{phase}', return_result=True))
    else:
        result = chd_intake.extract(source, temp, chdman)
    result.setdefault('reader', 'chdman')
    receipt.setdefault('chd_intake', []).append(result)
    return temp / result['cue']['path']

def runtime_media(selected, bios, product, mode, receipt):
    if mode == 'thin':
        return (list(map(str, selected)), str(bios))
    discs = [str(stage_disc(p, product / 'inputs' / f'disc-{n + 1}', receipt)) for n, p in enumerate(selected)]
    local_bios = product / 'inputs' / Path(bios).name
    local_bios.parent.mkdir(parents=True, exist_ok=True)
    record_inputs([bios], receipt)
    shutil.copy2(bios, local_bios)
    return (discs, str(local_bios))
SETTINGS_FORMAT = 2

def runtime_settings_text(disc, bios, fast_boot=None):
    text = f'settings_format = {SETTINGS_FORMAT}\n\n[disc]\npath = ' + json.dumps(disc) + '\n[bios]\npath = ' + json.dumps(bios) + '\n'
    if fast_boot is not None:
        text += '[video]\nfast_boot         = ' + ('true' if fast_boot else 'false') + '\n'
    return text

def chosen_fast_boot(runtime_settings):
    return runtime_settings['fast_boot'] if runtime_settings.get('source') == 'operator' else None

def write_runtime_media(config, product, discs, bios, serials, mode, fast_boot=None):
    product.mkdir(parents=True, exist_ok=True)

    def reference(path):
        return Path(os.path.relpath(path, product)).as_posix() if mode == 'portable' else str(path)
    disc_refs = [reference(p) for p in discs]
    bios_ref = reference(bios) if bios else ""
    runtime_config = product / 'game.toml'
    shutil.copy2(config, runtime_config)
    set_game_media(runtime_config, disc_refs, serials)
    (product / 'disc.cfg').write_text(disc_refs[0] + '\n', encoding='utf-8')
    (product / 'bios.cfg').write_text(bios_ref + '\n', encoding='utf-8')
    (product / 'settings.toml').write_text(runtime_settings_text(disc_refs[0], bios_ref, fast_boot), encoding='utf-8')

def read_chd_boot_executable(chd, psx_chd, lib_path):
    try:
        try:
            from . import chd_source
        except ImportError:
            import chd_source
        with chd_source.ChdTrackSource(chd, psx_chd, lib_path) as source:
            read_user = source.read_user
            pvd = read_user(None, 16)
            if pvd[1:6] != b'CD001':
                return (None, None)
            root_extent = int.from_bytes(pvd[158:162], 'little')
            root_size = int.from_bytes(pvd[166:170], 'little')
            if not 0 < root_size <= 65536:
                return (None, None)
            root = bytearray()
            for i in range((root_size + 2047) // 2048):
                root += read_user(None, root_extent + i)
            entries = iso_entries(bytes(root[:root_size]))
            cnf = entries.get('SYSTEM.CNF')
            if cnf is None:
                boot = baseline_workflow.BIOS_FALLBACK_BOOT
            else:
                text = read_iso_file(read_user, cnf).decode('ascii', 'replace')
                match = re.search('BOOT\\s*=\\s*cdrom:\\\\*([^;\\r\\n]+)', text, re.IGNORECASE)
                if not match:
                    return (None, None)
                boot = match.group(1).replace('\\', '/').lstrip('/')
            parts = [p for p in boot.split('/') if p]
            if len(parts) > 1:
                sub = entries.get(parts[0].upper())
                if sub is None:
                    return (None, None)
                body = bytearray()
                for i in range((sub[1] + 2047) // 2048):
                    body += read_user(None, sub[0] + i)
                entries = iso_entries(bytes(body[:sub[1]]))
            entry = entries.get(parts[-1].upper())
            if entry is None:
                return (None, None)
            return (boot, read_iso_file(read_user, entry))
    except Exception:
        return (None, None)

def iso_entries(blob):
    entries, i = ({}, 0)
    while i < len(blob):
        reclen = blob[i]
        if reclen == 0:
            i = (i // 2048 + 1) * 2048
            if i >= len(blob):
                break
            continue
        if i + reclen > len(blob):
            break
        size = int.from_bytes(blob[i + 10:i + 14], 'little')
        namelen = blob[i + 32]
        name = blob[i + 33:i + 33 + namelen].split(b';')[0]
        if name not in (b'\x00', b'\x01'):
            entries[name.decode('ascii', 'replace').upper()] = (int.from_bytes(blob[i + 2:i + 6], 'little'), size)
        i += reclen
    return entries

def read_iso_file(read_user, entry):
    out, rem, lba = (bytearray(), entry[1], entry[0])
    while rem > 0:
        take = min(2048, rem)
        out += read_user(None, lba)[:take]
        rem -= take
        lba += 1
    return bytes(out)

def stage_boot_executable(config, disc, output, boot_body=None, boot_name=None):
    sections = tomllib.loads(config.read_text(encoding='utf-8-sig'))
    game, prep = (sections['game'], sections.get('prepare_disc', {}))
    if boot_body is None:
        boot = boot_name or baseline_workflow._boot_executable(disc)
        staged = Path(disc).parent / boot
        boot_body = staged.read_bytes() if staged.is_file() else baseline_workflow._iso_file(disc, boot)
    for relative in (game['exe'], str(Path(prep.get('out_dir', 'prepared_disc')) / prep.get('boot_exe', Path(game['exe']).name))):
        dest = output / relative
        if not dest.resolve().is_relative_to(output.resolve()):
            raise BuildStop('The kit boot executable path escapes its output directory.')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(boot_body)

def bios_stage_path(fw, stem):
    profile = fw / 'bios' / (stem + '.toml')
    rom = tomllib.loads(profile.read_text(encoding='utf-8'))['program']['rom']
    dest = (fw / rom).resolve()
    if not dest.is_relative_to(fw.resolve()) or dest.exists():
        raise BuildStop('The pinned BIOS staging path must be a new file inside the exported framework.')
    return dest

def read_toolchain(toolchain_pin, toolchain):
    for key, value in (('toolchain_pin', toolchain_pin), ('toolchain', toolchain)):
        if value is None:
            raise BuildStop(f'A build path is not set: {studio_settings.describe(key)}. Nothing was built.')
    toolchain_pin = Path(toolchain_pin)
    if not toolchain_pin.is_file():
        raise BuildStop(f"The toolchain pin file does not exist: {toolchain_pin}. Change {studio_settings.describe('toolchain_pin')}.")
    bound = tomllib.loads(toolchain_pin.read_text(encoding='utf-8'))
    toolchain = Path(toolchain).resolve()
    metadata_path = toolchain / 'retcomm-toolchain.json'
    if not metadata_path.is_file():
        raise BuildStop(f"No toolchain is installed at {toolchain} (no retcomm-toolchain.json). Change {studio_settings.describe('toolchain')}.")
    return (bound, toolchain, json.loads(metadata_path.read_text()))

def default_kit_roots(receipt=None, inventory=None, environ=None):
    environ = os.environ if environ is None else environ
    named_inventory = inventory or environ.get('WORKBENCH_KIT_INVENTORY') or KIT_INVENTORY
    path = Path(named_inventory) if named_inventory else None
    roots = []
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig')) if path else {}
        if data.get('schema') == 'psx.release-kit-inventory.v1' and data.get('kit_root'):
            roots.append(Path(data['kit_root']))
    except (OSError, ValueError, AttributeError):
        pass
    if receipt is not None:
        receipt['kit_inventory'] = {'path': str(path) if path else None, 'used': bool(roots)}
        if path and (not roots):
            receipt.setdefault('warnings', []).append(f'No canonical kit inventory at {path}; matching against the named kit roots only.')
    if receipt is not None and environment_kit_roots(environ):
        receipt['kit_roots_env'] = [str(root) for root in environment_kit_roots(environ)]
    ordered = []
    for root in environment_kit_roots(environ) + roots + KIT_ROOTS:
        if root not in ordered:
            ordered.append(root)
    return ordered

def checked_kit_roots(explicit_roots, explicit_kit, receipt, inventory=None):
    if explicit_kit is not None and (not Path(explicit_kit).is_dir()):
        raise BuildStop(f'The kit folder named with --kit does not exist: {explicit_kit}.')
    if explicit_roots:
        missing = [str(root) for root in explicit_roots if not Path(root).is_dir()]
        if missing:
            raise BuildStop('These kit roots are not folders: ' + ', '.join(missing) + '. Correct --kits-root; nothing was searched.')
        roots = [Path(root) for root in explicit_roots]
    else:
        roots = default_kit_roots(receipt, inventory)
        named = environment_kit_roots()
        missing = [str(root) for root in named if not root.is_dir()]
        if missing:
            raise BuildStop(f'These kit roots named by {KIT_ROOTS_ENV} are not folders: ' + ', '.join(missing) + f'. Correct {KIT_ROOTS_ENV} or pass --kits-root; nothing was searched. ' + f"Change {studio_settings.describe('kit_roots')}.")
        canonical = next((root for root in roots if root not in named), None)
        if receipt.get('kit_inventory', {}).get('used') and canonical is not None and (not canonical.is_dir()):
            raise BuildStop(f"The canonical kit root {canonical} named by {receipt['kit_inventory']['path']} is not a folder. Correct the inventory or pass --kits-root.")
        for root in [root for root in roots if not root.is_dir()]:
            receipt.setdefault('warnings', []).append(f'Default kit root {root} is not a folder; skipped.')
        roots = [root for root in roots if root.is_dir()]
        if not roots:
            raise BuildStop(f"None of the default kit roots exists. Pass --kits-root or --kit, or list the roots in {KIT_ROOTS_ENV}: {studio_settings.describe('kit_roots')}.")
    receipt['kit_roots'] = [str(root) for root in roots]
    print('kit roots searched: ' + '; '.join(receipt['kit_roots']), flush=True)
    return roots

def require_scaffold_consent(args, roots, diagnostics, receipt):
    receipt['scaffold_policy'] = 'automatic public-edition scaffold'

def drop_colliding_kit(collision, args, kit_roots, receipt):
    if getattr(args, 'kit', None):
        raise collision
    receipt['kit_serial_collision'] = collision.record
    receipt.setdefault('warnings', []).append(str(collision) + ' Building without that kit.')
    print('note: ' + str(collision), flush=True)
    require_scaffold_consent(args, kit_roots, [str(collision)], receipt)
    return Path(collision.record['kit']).resolve() if collision.record.get('kit') else None

def find_kit(serials, roots, explicit=None, diagnostics=None, exclude=()):
    paths = [Path(explicit) / 'game.toml'] if explicit else [p for root in roots if root.is_dir() for p in sorted(root.glob('*/game.toml'))]
    matches, candidates, errors = ([], [], [])
    skip = {Path(p).resolve() for p in exclude if p}
    for path in dict.fromkeys((p.resolve() for p in paths)):
        if path.parent in skip:
            continue
        try:
            game = tomllib.loads(path.read_text(encoding='utf-8-sig'))['game']
        except (OSError, ValueError, KeyError) as error:
            errors.append(f'{path}: {error}')
            continue
        boot = game.get('id') if isinstance(game, dict) else None
        if not isinstance(boot, str) or not boot:
            errors.append(f'{path}: game.id does not declare a boot serial')
            continue
        candidates.append((path.parent, game))
        if boot in serials and (path.parent / 'CMakeLists.txt').is_file():
            matches.append((path.parent, boot))
    if not matches:
        reason = f"No buildable source kit matches the selected boot serials: {', '.join(serials)}."
        if candidates:
            closest, game = max(candidates, key=lambda item: max((SequenceMatcher(None, item[1]['id'], serial).ratio() for serial in serials), default=0))
            why = f"it requires boot disc {game['id']}, which was not selected" if game['id'] not in serials else 'it has no CMakeLists.txt and is not a buildable source kit'
            reason += f" Closest kit by serial: {game.get('name', closest.name)} ({closest}); {why}. Serial similarity does not establish compatibility."
        elif errors:
            reason += ' No readable kit with a boot serial was found. ' + '; '.join(errors)
        else:
            reason += ' No game.toml files were found in the kit roots.'
        if diagnostics is not None:
            diagnostics.append(reason)
        return None
    first_root = matches[0][0].parent
    local = [(p, boot) for p, boot in matches if p.parent == first_root]
    if len(local) > 1 or len({boot for _, boot in matches}) > 1:
        plan = None if explicit else program_set_plan(local, serials)
        if plan is not None:
            raise ProgramSet(plan)
        alternatives = '; '.join((f'{p} (boot {boot})' for p, boot in matches))
        raise BuildStop(f'More than one kit matches the selected discs. Select one with --kit: {alternatives}')
    return matches[0][0]

def program_set_plan(matches, serials):
    if len(matches) < 2 or len({boot for _, boot in matches}) != len(matches):
        return None
    declared, shared = ([], None)
    for kit, _boot in matches:
        try:
            identity = read_retail_identity(kit)
        except BuildStop:
            return None
        group = identity.get('set')
        own = identity.get('serials')
        if not isinstance(group, dict) or not isinstance(own, list) or (not own):
            return None
        key = (group.get('name'), tuple(group.get('serials') or ()), tuple(group.get('programs') or ()))
        if not key[0] or not key[1] or len(key[1]) != len(set(key[1])) or (shared not in (None, key)):
            return None
        shared = key
        program = group.get('program')
        if not isinstance(program, str) or program not in key[2] or any((program == d['program'] for d in declared)):
            return None
        if any((serial not in key[1] for serial in own)):
            return None
        exe_name = group.get('exe_name')
        if not isinstance(exe_name, str) or not exe_name or Path(exe_name).name != exe_name:
            return None
        declared.append({'program': program, 'kit': str(kit), 'serials': list(own), 'positions': [key[1].index(serial) + 1 for serial in own], 'exe_name': exe_name, 'shortcut': group.get('shortcut') or exe_name.replace('_', ' '), 'title': group.get('title')})
    owned = [serial for d in declared for serial in d['serials']]
    if len(owned) != len(set(owned)) or set(owned) != set(shared[1]) or set(serials) != set(shared[1]) or (len(serials) != len(shared[1])):
        return None
    declared.sort(key=lambda d: shared[2].index(d['program']))
    title = next((d.pop('title') for d in declared if d.get('title')), shared[0])
    for d in declared:
        d.pop('title', None)
    return {'name': shared[0], 'title': title, 'serials': list(shared[1]), 'programs': declared}

def read_retail_identity(kit):
    manifest = kit / 'project-manifest.toml'
    if not manifest.is_file():
        return {}
    try:
        sections = tomllib.loads(manifest.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise BuildStop(f'Kit {kit} has a project-manifest.toml that cannot be read: {error}')
    identity = sections.get('retail_identity')
    return identity if isinstance(identity, dict) else {}

def declared_players(kit):
    manifest = kit / 'project-manifest.toml'
    if not manifest.is_file():
        return None
    try:
        sections = tomllib.loads(manifest.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise BuildStop(f'Kit {kit} has a project-manifest.toml that cannot be read: {error}')
    project = sections.get('project')
    if not isinstance(project, dict):
        return None
    players = project.get('players')
    return players if isinstance(players, int) and (not isinstance(players, bool)) and (players > 0) else None

def check_declared_disc_set(kit, serials, receipt, identity=None):
    if identity is None:
        identity = read_retail_identity(kit)
    if not identity:
        receipt.setdefault('warnings', []).append(f'Kit {kit} has no [retail_identity] declaration in project-manifest.toml. A multi-disc title cannot be checked for a complete, ordered disc set.')
        return
    declared = identity.get('serials')
    if not isinstance(declared, list) or not all((isinstance(s, str) for s in declared)) or (not declared):
        raise BuildStop(f'Kit {kit} has an invalid [retail_identity] serials list. Restore its ordered disc serials.')
    count = identity.get('disc_count')
    if isinstance(count, int) and (not isinstance(count, bool)) and (count != len(declared)):
        raise BuildStop(f"Kit {kit} declares disc_count = {count} but lists {len(declared)} serials. Correct the kit's retail identity.")
    receipt['declared_disc_serials'] = list(declared)
    missing = [s for s in declared if s not in serials]
    extra = [s for s in serials if s not in declared]
    if not missing and (not extra):
        return
    if is_multi_program(kit, identity):
        raise BuildStop(f"Kit {kit} declares {len(declared)} discs and more than one program: {', '.join(declared)}. Missing: {', '.join(missing) or 'none'}; not in the kit declaration: {', '.join(extra) or 'none'}. One generated program cannot run the other discs. Build each program separately, or select every declared disc and generate one kit per program.")
    receipt.setdefault('warnings', []).append(f"Kit {kit} declares {len(declared)} disc(s): {', '.join(declared)}, but this build generates from {', '.join(serials)}. Missing: {', '.join(missing) or 'none'}; not in the kit declaration: {', '.join(extra) or 'none'}. The kit does not declare separate programs, so the unselected discs are expected to be content for the same program; confirm the title is not multi-program.")

def is_multi_program(kit, identity=None):
    if identity is None:
        identity = read_retail_identity(kit)
    model = str(identity.get('program_model', '')).lower()
    if 'distinct program' in model or 'multi-program' in model or 'independent-program' in model:
        return True
    entries = identity.get('entry_points')
    if isinstance(entries, list) and len({str(e).upper() for e in entries}) > 1:
        return True
    return False

def use_kit_media(kit, discs, identities, receipt):
    game = tomllib.loads((kit / 'game.toml').read_text(encoding='utf-8-sig'))['game']
    serials = [item['serial'] for item in identities]
    boot = game['id']
    if len(serials) != len(set(serials)):
        raise BuildStop('More than one selected disc has the same serial. Select one image per serial.')
    if boot not in serials:
        raise BuildStop(f'Kit {kit} requires boot disc {boot}, which was not selected.')
    roster = game.get('disc_serials')
    warnings = []
    if roster:
        if not isinstance(roster, list) or not all((isinstance(s, str) for s in roster)) or len(roster) != len(set(roster)) or (boot not in roster):
            raise BuildStop(f'Kit {kit} has an invalid disc_serials roster. Restore its boot serial and unique disc entries.')
        missing = [s for s in roster if s not in serials]
        extra = [s for s in serials if s not in roster]
        if missing or extra:
            raise BuildStop(f"Kit {kit} requires its declared disc roster. Missing: {', '.join(missing) or 'none'}; not in the kit roster: {', '.join(extra) or 'none'}.")
        used = [boot] + [s for s in roster if s != boot]
    else:
        used = [boot] + [s for s in serials if s != boot]
        warnings.append(f"Kit {kit} has no disc_serials roster. Using the selected discs with boot disc {boot} first: {', '.join(used)}. The kit does not verify this disc roster or multi-disc gameplay.")
    indices = [serials.index(s) for s in used]
    used_discs = [discs[i] for i in indices]
    used_identities = [identities[i] for i in indices]
    declared = read_retail_identity(kit)
    if not declared:
        warnings.append(f'Kit {kit} has no [retail_identity] declaration in project-manifest.toml. A multi-disc title cannot be checked for a complete, ordered disc set.')
    else:
        check_declared_disc_set(kit, serials, receipt, declared)
    receipt.update(kit_roster_missing=not bool(roster), selected_disc_identities=identities, selected_discs=list(map(str, discs)), disc_identities=used_identities, discs=list(map(str, used_discs)))
    receipt.setdefault('warnings', []).extend(warnings)
    for warning in warnings:
        print(f'WARNING: {warning}', flush=True)
    return (used_discs, used)

def copy_kit(kit, output, receipt):
    files = [p for p in kit.iterdir() if p.is_file() and (p.name in ('CMakeLists.txt', 'VERSION', 'game.toml', 'game_options.toml', 'symbols.toml', 'psx_symbols.h', 'psx_symbols_overlays.h', 'project-manifest.toml', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md') or p.name.startswith('codegen_setup.'))]
    for folder in ('seeds', 'src', 'scripts', 'tools', 'mods', 'assets', 'launcher_assets', 'cmake'):
        root = kit / folder
        if root.is_dir():
            files.extend((p for p in root.rglob('*') if p.is_file() and '.git' not in p.parts and ('__pycache__' not in p.parts)))
    for path in files:
        if path.suffix.lower() in ('.bin', '.cue', '.chd', '.iso', '.exe', '.dll', '.pyc'):
            raise BuildStop(f'Unexpected private or binary input in kit templates: {path}')
        record_inputs([path], receipt)
        dest = output / path.relative_to(kit)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    if not (output / 'CMakeLists.txt').is_file():
        raise BuildStop('The matching kit has no CMakeLists.txt. Restore its owned-input source kit.')
SCAFFOLD_NETPLAY_OFF = 'set(PSX_NETPLAY OFF CACHE BOOL "Accepted offline release" FORCE)'
SCAFFOLD_NETPLAY_ON = '# Netplay (build): on for titles with two or more players.\nif(EXISTS "${PSXRECOMP_ROOT}/lib/recomp-net/CMakeLists.txt")\n    set(PSX_NETPLAY ON CACHE BOOL "Netplay" FORCE)\nendif()'
SCAFFOLD_NETPLAY_ARGUMENTS = '    ENABLE_NETPLAY_IF_PRESENT\n    NETPLAY_LOBBY_URL "ws://netplay.retcomm.net:8765"'
SCAFFOLD_CMAKE = 'cmake_minimum_required(VERSION 3.20)\nset(PSXRECOMP_ROOT "${{CMAKE_CURRENT_SOURCE_DIR}}/psxrecomp" CACHE PATH\n    "Path to psxrecomp")\nif(NOT EXISTS "${{PSXRECOMP_ROOT}}/runtime/runtime.cmake")\n    message(FATAL_ERROR\n        "PSXRecomp runtime is missing: ${{PSXRECOMP_ROOT}}/runtime/runtime.cmake\\n"\n        "This project expects the psxrecomp framework checkout at ${{PSXRECOMP_ROOT}}.\\n"\n        "Run: git submodule update --init --recursive\\n"\n        "If psxrecomp/ is not a submodule, set -DPSXRECOMP_ROOT=<path-to-psxrecomp>.")\nendif()\n\nproject({project} LANGUAGES C CXX)\n\n# --- Opt-in product surfaces (OFF by default in the framework) -------------\n# Generated by the workbench census for a title with no hand-authored kit.\nset(PSX_RECOMP_UI {ui} CACHE BOOL "Shared launcher" FORCE)\nset(PSX_DEBUG_TOOLS OFF CACHE BOOL "Production build" FORCE)\nset(PSX_STATIC_RUNTIME {static_runtime} CACHE BOOL "Portable runtime" FORCE)\nset(PSX_ENABLE_VULKAN ON CACHE BOOL "Vulkan renderer" FORCE)\n{netplay_switch}\nset(PSX_SETUP_WIZARD {wizard} CACHE BOOL "First-run setup" FORCE)\nset(PSXRECOMP_BIOS_STEMS "{bios_stem}" CACHE STRING "Accepted BIOS" FORCE)\nset(PSXRECOMP_BIOS_STEM "{bios_stem}" CACHE STRING "Accepted BIOS" FORCE)\nset(PSXRECOMP_BIOS_PROFILE "${{PSXRECOMP_ROOT}}/bios/{bios_stem}.toml" CACHE FILEPATH "Accepted BIOS" FORCE)\ninclude("${{PSXRECOMP_ROOT}}/runtime/runtime.cmake")\n\npsxrecomp_add_game_runtime(psx-runtime\n{netplay_arguments}\n\n{wizard_flag}\n    EXE_NAME "{exe_name}"\n    WINDOW_TITLE "{window_title}"\n    DEFAULT_GAME_CONFIG_PATH "game.toml"\n    MAX_PLAYERS {players}\n\n\n    GEN_MARKER "generated/{gen_marker}"\n    GEN_FULL_GLOB "generated/{gen_glob}"\n{codegen_sources}\n)\n\n# Stage runtime configs next to the exe for direct build/ launches. Only copy\n# files that exist - never fail POST_BUILD when game_options.toml is absent.\nforeach(_PSX_RUNTIME_CONFIG game.toml game_options.toml)\n    if(EXISTS "${{CMAKE_CURRENT_SOURCE_DIR}}/${{_PSX_RUNTIME_CONFIG}}")\n        add_custom_command(TARGET psx-runtime POST_BUILD\n            COMMAND ${{CMAKE_COMMAND}} -E copy_if_different\n                "${{CMAKE_CURRENT_SOURCE_DIR}}/${{_PSX_RUNTIME_CONFIG}}"\n                "$<TARGET_FILE_DIR:psx-runtime>/${{_PSX_RUNTIME_CONFIG}}"\n            COMMENT "Staging ${{_PSX_RUNTIME_CONFIG}}"\n            VERBATIM)\n    endif()\nendforeach()\n'
SCAFFOLD_CODEGEN_C = '/* Title config for psxrecomp/host/psxrecomp_codegen_host.\n * Wired only when the build opts into -DPSX_SETUP_WIZARD=ON.\n * Generated by the workbench census for a title with no hand-authored kit. */\n\n#include "codegen_setup.h"\n\n#include "psxrecomp_codegen_host.h"\n\nstatic const PsxrecompCodegenHostConfig kCodegenConfig = {{\n    .display_name = "{display_name}",\n    .project_root_env = "{env_prefix}_PROJECT_ROOT",\n    .build_dir_env = "{env_prefix}_BUILD_DIR",\n    .force_setup_env = "{env_prefix}_FORCE_SETUP",\n    .psxrecomp_cli_relpath = "psxrecomp/psxrecomp_cli.py",\n    .seed_cfg_relpath = "game.toml",\n    .game_toml_relpath = "game.toml",\n    .gen_marker_relpath = "generated/{gen_marker}",\n    .build_dir_name = "build-release",\n    .cmake_target = "psx-runtime",\n    .exe_basename = "{exe_name}",\n    .prepare_note =\n        "Uses your legal disc with the local psxrecomp SDK to generate "\n        "BIOS + game C, then cmake --build. The product lives under "\n        "build-release/; reopening this setup exe forwards there.",\n    .prepare_note_windows =\n        "Uses your legal disc with the local psxrecomp SDK to generate "\n        "BIOS + game C, then quits and rebuilds via a helper. Afterward, "\n        "this setup exe forwards to build-release/ (bios, mods, settings).",\n    .prepare_note_no_cmake =\n        "Uses your legal disc with the local psxrecomp SDK to generate "\n        "BIOS + game C. Rebuild into build-release/, then relaunch this "\n        "setup exe (it forwards to the product build).",\n}};\n\nvoid psx_game_codegen_setup_apply(RecompLauncherCGameInfo* gi) {{\n    psxrecomp_codegen_host_apply(gi, &kCodegenConfig);\n}}\n\nvoid psx_game_codegen_relaunch_or_exit(const char* disc_path) {{\n    psxrecomp_codegen_host_relaunch_or_exit(disc_path);\n}}\n\nvoid psx_game_codegen_forward_if_built(int argc, char** argv) {{\n    psxrecomp_codegen_host_forward_if_built(&kCodegenConfig, argc, argv);\n}}\n'
SCAFFOLD_CODEGEN_H = '#ifndef CODEGEN_SETUP_H\n#define CODEGEN_SETUP_H\n\n#include "recomp_launcher.h"\n\n#ifdef __cplusplus\nextern "C" {\n#endif\n\nvoid psx_game_codegen_setup_apply(RecompLauncherCGameInfo* gi);\nvoid psx_game_codegen_relaunch_or_exit(const char* disc_path);\nvoid psx_game_codegen_forward_if_built(int argc, char** argv);\n\n#ifdef __cplusplus\n}\n#endif\n\n#endif\n'

def _cmake_token(text):
    token = re.sub('[^0-9A-Za-z_]', '_', str(text))
    token = re.sub('_+', '_', token).strip('_')
    return token or 'PsxGameRecompiled'

def _env_token(text):
    token = re.sub('[^0-9A-Za-z]', '_', str(text)).upper()
    return re.sub('_+', '_', token).strip('_') or 'PSX_GAME'
STATIC_RUNTIME_FORCED_ON = re.compile('set\\s*\\(\\s*PSX_STATIC_RUNTIME\\s+ON\\b[^)]*\\bFORCE\\b[^)]*\\)', re.I)

def module_input_paths(module_build):
    if not module_build:
        return []
    config = Path(module_build['sdl3_cmake_dir'])
    return [Path(module_build[key]) for key in ('record', 'module', 'sdl3_dll')] + sorted((p for p in config.rglob('*') if p.is_file()))

def module_cmake_args(receipt, output):
    module = receipt.get('ui_module_build')
    if not module:
        return []
    values = (('PSX_LAUNCHER_MODULE', 'ON'), ('PSX_STATIC_RUNTIME', 'OFF'), ('RECOMP_UI_ENABLE_MODS', 'ON'), ('RECOMP_LAUNCHER_MODULE_FILE', Path(module['module']).as_posix()), ('SDL3_DIR', Path(module['sdl3_cmake_dir']).as_posix()), ('PSX_SDL3_FETCH', 'OFF'), ('RECOMP_UI_ROOT', (Path(output) / 'recomp-ui').as_posix()))
    return [f'--cmake-extra=-D{name}={value}' for name, value in values]

def refuse_forced_static_runtime(output, receipt):
    path = Path(output) / 'CMakeLists.txt'
    if receipt.get('ui_module_build') and path.is_file() and STATIC_RUNTIME_FORCED_ON.search(path.read_text(encoding='utf-8-sig')):
        raise BuildStop(f'{path} forces PSX_STATIC_RUNTIME ON, but a launcher-module build needs it OFF. Use a kit whose recipe leaves PSX_STATIC_RUNTIME unforced, or the bundled UI.')

def _c_string(text):
    return str(text).replace('\\', '\\\\').replace('"', '\\"')

def scaffold_kit(output, facts, config, bios_stem, players, receipt):
    output = Path(output)
    sections = tomllib.loads(Path(config).read_text(encoding='utf-8-sig'))
    game = sections.get('game', {})
    boot = str(game.get('exe', '')).rsplit('/', 1)[-1] or str(facts.get('boot_exe') or 'boot')
    marker = f'{boot}_dispatch.c'
    exe_name = _cmake_token(game.get('name') or facts.get('display_name'))
    title = _c_string(game.get('name') or facts.get('display_name') or '')
    setup = False
    netplay = int(players) >= 2
    (output / 'CMakeLists.txt').write_text(SCAFFOLD_CMAKE.format(netplay_switch=SCAFFOLD_NETPLAY_ON if netplay else SCAFFOLD_NETPLAY_OFF, netplay_arguments=SCAFFOLD_NETPLAY_ARGUMENTS if netplay else '', project=exe_name, ui='ON', wizard='ON' if setup else 'OFF', static_runtime='OFF' if receipt.get('ui_module_build') else 'ON', wizard_flag='    ENABLE_SETUP_WIZARD\n' if setup else '', bios_stem=bios_stem, exe_name=exe_name, window_title=title, players=int(players), gen_marker=marker, gen_glob=marker.replace('_dispatch.c', '_full_*.c'), codegen_sources='    CODEGEN_SETUP_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/codegen_setup.c"\n'), encoding='utf-8')
    (output / 'codegen_setup.h').write_text(SCAFFOLD_CODEGEN_H, encoding='utf-8')
    (output / 'codegen_setup.c').write_text(SCAFFOLD_CODEGEN_C.format(display_name=title, gen_marker=marker, exe_name=exe_name, env_prefix=_env_token(game.get('name') or facts.get('display_name'))), encoding='utf-8')
    names = ('CMakeLists.txt', 'codegen_setup.c', 'codegen_setup.h')
    record_inputs([output / name for name in names], receipt)
    receipt['scaffold'] = {'kind': 'census', 'files': list(names), 'gen_marker': marker, 'exe_name': exe_name, 'bios_stem': bios_stem, 'players': int(players), 'netplay': netplay, 'setup_wizard': setup}

def operator_players(args):
    players = getattr(args, 'players', None)
    if players is None:
        return 1
    if isinstance(players, bool) or not isinstance(players, int) or (not 1 <= players <= 8):
        raise BuildStop(f'--players must be a whole number from 1 to 8 (got {players!r}).')
    return players

def set_game_media(path, discs, serials):
    text = path.read_text(encoding='utf-8-sig')
    tomllib.loads(text)
    sections = re.split('(?m)(^\\[[^\\n]+\\]\\s*$)', text)
    for i in range(1, len(sections), 2):
        if sections[i].strip() == '[game]':
            lines = iter(sections[i + 1].splitlines(keepends=True))
            keep = []
            for line in lines:
                if not re.match('\\s*(?:disc|discs|disc_serials)\\s*=', line):
                    keep.append(line)
                    continue
                declaration = line
                while True:
                    try:
                        tomllib.loads(declaration)
                        break
                    except tomllib.TOMLDecodeError:
                        following = next(lines, None)
                        if following is None:
                            raise BuildStop('Cannot read the complete disc declaration in game.toml.')
                        declaration += following
            body = ''.join(keep)
            sections[i + 1] = '\n' + '\n'.join(['disc = ' + json.dumps(discs[0]), 'discs = ' + json.dumps(discs), 'disc_serials = ' + json.dumps(serials)]) + '\n' + body
    updated = ''.join(sections)
    parsed = tomllib.loads(updated)['game']
    if parsed['discs'] != discs or parsed['disc_serials'] != serials:
        raise BuildStop('Cannot write the multi-disc roster. Review game.toml.')
    path.write_text(updated, encoding='utf-8')

def set_game_players(path, players):
    text = path.read_text(encoding='utf-8-sig')
    sections = tomllib.loads(text)
    current = sections.get('game', {}).get('players')
    if current == players:
        return
    if not isinstance(current, int) or isinstance(current, bool):
        raise BuildStop('Cannot set players: [game] has no integer players field to replace.')
    updated = re.sub('(?m)^(\\s*players\\s*=\\s*)\\S+', lambda m: m.group(1) + str(players), text, count=1)
    parsed = tomllib.loads(updated)['game']['players']
    if parsed != players:
        raise BuildStop('Cannot write the declared player count. Review game.toml.')
    path.write_text(updated, encoding='utf-8')
HOST_TOOLCHAIN_OVERRIDES = ('RETCOMM_TOOLCHAIN_DIR', 'PSXRECOMP_TOOLCHAIN_DIR', 'TOOLCHAIN_DIR', 'BPE_TOOLCHAIN_DIR')
HOST_OWN_FOLDERS = {'LOCALAPPDATA': 'LocalAppData', 'RETCOMM_DATA_HOME': 'retcomm-data', 'RETCOMM_TOOLCHAIN_CACHE': 'toolchain-cache', 'RETCOMM_TOOLCHAIN_DIR': 'toolchain', 'PSXRECOMP_TOOLCHAIN_DIR': 'toolchain'}

def host_start_environment(env, folder):
    folder = Path(folder)
    own = {name: folder / part for name, part in HOST_OWN_FOLDERS.items()}
    for path in set(own.values()):
        path.mkdir(parents=True, exist_ok=True)
    kept = {key: value for key, value in env.items() if not key.upper().startswith('RETCOMM_') and key.upper() not in HOST_TOOLCHAIN_OVERRIDES and (key.upper() != 'LOCALAPPDATA')}
    return kept | {name: str(path) for name, path in own.items()} | {'RETCOMM_TOOLCHAIN_SKIP_UPDATE': '1'}

def host_start_wrote(folder):
    folder = Path(folder)
    files = [path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file()] if folder.is_dir() else []
    toolchain = [name for name in files if name.startswith(('toolchain/', 'toolchain-cache/')) or '/toolchains/' in '/' + name.lower()]
    return {'files': len(files), 'toolchain': sorted(toolchain)[:20]}

def stdout_log(log):
    log = Path(log)
    return log.with_name(log.stem + '.stdout.log')

def selfcheck_report(log):
    path = stdout_log(log)
    if not path.is_file():
        raise BuildStop(f"No setup self-check report: the step's stdout was not kept beside {log}.")
    try:
        report = json.loads(path.read_text(encoding='utf-8', errors='replace'))
    except ValueError:
        report = None
    if not isinstance(report, dict) or 'sources_missing' not in report:
        raise BuildStop(f"No setup self-check report in {path}: the program's stdout is not one report. Both streams, in the order they arrived, are in {log}.")
    return report

def run_command(argv, cwd, env, output, receipt, name, *, return_result=False, success_codes=(0,), watch=None, keep_stdout=False):
    progress.configure_stdio()
    occurrence = 1 + sum((c.get('stage') == name for c in receipt['commands']))
    log = output / (name.replace(':', '-') + (f'#{occurrence}' if occurrence > 1 else '') + '.log')
    command = {'stage': name, 'argv': list(map(str, argv)), 'cwd': str(cwd), 'log': str(log)}
    receipt['commands'].append(command)
    actual = command['argv']
    if len(actual) > 1 and Path(actual[1]).name == 'psxrecomp_cli.py':
        adapter = Path(__file__).with_name('workbench_child.py')
        record_inputs([adapter, Path(progress.__file__)], receipt)
        actual = [actual[0], '-u', str(adapter), *actual[1:]]
        command['execution_argv'] = actual
    children = set()
    active_tools = {}
    parent = progress.stage(receipt, output, name, label=name.replace('-', ' ').capitalize(), log=log, start=True)
    parent_id = parent['id']

    def child_id(event):
        args = event['argv']
        tool = Path(args[0]).stem.lower()
        kind = 'bios-generate' if tool == 'psxrecomp-bios' else 'generate' if tool == 'psxrecomp-game' else 'configure' if tool == 'cmake' and '--build' not in args else 'compile'
        return (f'{name}:{kind}', kind)

    def tick():
        progress.stage(receipt, output, parent_id)
        for stage_id in children:
            progress.stage(receipt, output, stage_id)
    with log.open('w', encoding='utf-8') as log_stream:

        def line(channel, text):
            target = parent_id
            nested = False
            if text.startswith(progress.CHILD_PREFIX):
                event = json.loads(text[len(progress.CHILD_PREFIX):])
                base, label = child_id(event)
                stage_id = active_tools.get(base, base)
                nested = True
                if event['event'] == 'start':
                    row = progress.stage(receipt, output, base, label=label, log=log, start=True)
                    active_tools[base] = row['id']
                    children.add(row['id'])
                    return
                if event['event'] == 'end':
                    progress.stage(receipt, output, stage_id, 'done' if event['exit_code'] == 0 else 'failed')
                    children.discard(stage_id)
                    return
                channel, text, target = (event['channel'], event['line'], stage_id)
            log_stream.write(text + '\n')
            log_stream.flush()
            if watch is not None:
                watch.line(text)
            print(f'[tool:{target}:{channel}] {text}', flush=True)
            value = progress.tool_progress(text)
            if value:
                progress.stage(receipt, output, target, progress=value)
            if (nested or not command.get('execution_argv')) and re.search('\\[\\d+/\\d+\\].*Linking', text):
                link = f'{name}:link'
                progress.stage(receipt, output, link, 'done', label='Link', log=log, start=True, checkpoint=True, detail='Ninja reported link completion; link duration is not available. ' + text)
        try:
            result = progress.stream(actual, cwd=cwd, env=env, on_line=line, on_tick=tick)
            command['exit_code'] = result.returncode
            if keep_stdout:
                stdout_log(log).write_bytes(result.stdout)
                command['stdout_log'] = str(stdout_log(log))
            if result.returncode not in success_codes:
                raise BuildStop(f'{name} failed (exit {result.returncode}). Read {log}.\n' + log.read_text(encoding='utf-8', errors='replace')[-1800:])
        except Exception as error:
            for stage_id in [*children, parent_id]:
                progress.stage(receipt, output, stage_id, 'failed', detail=str(error))
            command['elapsed_seconds'] = parent['elapsed_seconds']
            raise
    for stage_id in [*children, parent_id]:
        progress.stage(receipt, output, stage_id, 'done')
    command['elapsed_seconds'] = parent['elapsed_seconds']
    if return_result:
        result.stdout = result.stdout.decode('utf-8', errors='replace')
        result.stderr = result.stderr.decode('utf-8', errors='replace')
        return result
    return log

def rebuild_with_one_retry(rebuild, output, env, receipt, build_dir, toolchain):
    watch = workbench_compile.FailureWatch(build_dir)
    try:
        return run_command(rebuild, output, env, output, receipt, 'rebuild', watch=watch)
    except BuildStop as stop:
        failures = watch.finish()
        if failures:
            receipt['compile_failures'] = failures
        reason = workbench_compile.retry_reason(failures, receipt.get('optimize_pgo'))
        if reason is not None:
            if failures:
                receipt['compile_retry'] = {'retried': False, 'reason': reason}
            raise
        objects = [failure['object'] for failure in failures]
        record = {'retried': True, 'objects': objects, 'first_exit_codes': [f['exit_code'] for f in failures]}
        receipt['compile_retry'] = record
        debug_dir = output / 'compile-retry-ccache'
        retry_env = {**env, 'CCACHE_DEBUG': '1', 'CCACHE_DEBUGDIR': str(debug_dir)}
        ninja = Path(toolchain) / 'bin' / ('ninja.exe' if os.name == 'nt' else 'ninja')
        try:
            retry_log = run_command([ninja, '-C', build_dir, *objects], output, retry_env, output, receipt, 'rebuild-compile-retry')
            record.update(retry_exit_code=0, retry_log=str(retry_log))
        except BuildStop as again:
            record.update(retry_exit_code=receipt['commands'][-1].get('exit_code'), passed=False)
            raise BuildStop(f"{len(objects)} compile step(s) failed without any output, and failed again when run alone: {', '.join(objects)}. Host state at the first failure is in the receipt (compile_failures). The retry said:\n{again}") from stop
        finally:
            ccache_log = workbench_compile.gather_ccache_logs(debug_dir, output / 'rebuild-compile-retry.ccache.log')
            if ccache_log is not None:
                record['ccache_log'] = str(ccache_log)
        record['passed'] = True
        record['first_failed_stage'] = receipt.pop('failed_stage', None)
        receipt.setdefault('warnings', []).append(f"{len(objects)} compile step(s) exited with no output and passed when run again: {', '.join(objects)} (build). The build went on. Host state at the failure is in the receipt (compile_failures).")
        return run_command(rebuild, output, env, output, receipt, 'rebuild')

def overlay_archive_folders(explicit=None, environ=None):
    environ = os.environ if environ is None else environ
    named = [Path(p) for p in explicit or []]
    named += [Path(p) for p in environ.get(OVERLAY_ARCHIVES_ENV, '').split(os.pathsep) if p.strip()]
    return named + [p for p in OVERLAY_ARCHIVES if p not in named]

def stage_overlay_archives(fw, output, receipt, explicit=None):
    module = ast.parse((fw / 'tools/release_stage.py').read_text(encoding='utf-8'))
    declared = next((ast.literal_eval(n.value) for n in module.body if isinstance(n, ast.Assign) and any((isinstance(t, ast.Name) and t.id == 'TOOLCHAIN_PINS' for t in n.targets))))['win']
    cache = output / '.cache/overlay-toolchain'
    cache.mkdir(parents=True, exist_ok=True)
    for part in ('python', 'tcc'):
        name = declared[part + '_url'].rsplit('/', 1)[1]
        folders = overlay_archive_folders(explicit)
        sources = [folder / name for folder in folders]
        source = next((p for p in sources if p.is_file() and sha(p) == declared[part + '_sha256']), None)
        if source is None:
            wrong = [str(p) for p in sources if p.is_file()]
            raise BuildStop(f"Offline overlay archive is missing: {name} with SHA-256 {declared[part + '_sha256']} (from {declared[part + '_url']}). Looked in: {'; '.join((str(f) for f in folders))}." + (f" A file of that name with a different hash is at: {'; '.join(wrong)}." if wrong else '') + f" Put the exact archive in one of those folders, or name its folder with --overlay-archives or {OVERLAY_ARCHIVES_ENV} ({studio_settings.describe('overlay_archives')}), then build again.")
        record_inputs([source], receipt)
        shutil.copy2(source, cache / name)

def isolate_toolchain_environment(fw, receipt):
    path = fw / 'tools/toolchain_pack.py'
    source = path.read_text(encoding='utf-8')
    before = sha(path)
    signature = 'def register_toolchain_user_env(pack_root: Path, log=None) -> Path:'
    index = source.find(signature)
    if index < 0:
        raise BuildStop('Selected core has no supported toolchain activation function')
    body = source.find('\n', index) + 1
    first = body
    if source[body:].lstrip().startswith('"""'):
        begin = source.index('"""', body)
        body = source.index('"""', begin + 3) + 3
        body = source.index('\n', body) + 1
    guard = '    if os.environ.get("PSX_STUDIO_PROJECT_ONLY") == "1":\n        activate_toolchain_bin(Path(pack_root) / "bin", log=log)\n        return Path(pack_root)\n'
    source = source[:body] + guard + source[body:]
    path.write_text(source, encoding='utf-8')
    receipt['toolchain_activation_adaptation'] = {'file': 'tools/toolchain_pack.py', 'before_sha256': before, 'after_sha256': sha(path), 'reason': 'process-only activation; no latest link, login PATH or registry publication'}

def build_environment(toolchain, output, jobs):
    env = os.environ.copy()
    env.setdefault('WORKBENCH_SHARED_COMPILER_ROOT', str(Path(env.get('LOCALAPPDATA', str(output))) / 'RPT' / 'compilers'))
    env.update(PSXRECOMP_TOOLCHAIN_DIR=str(toolchain), RETCOMM_TOOLCHAIN_DIR=str(toolchain), PSX_DEPS_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', CMAKE_BUILD_PARALLEL_LEVEL=str(jobs), CCACHE_DIR=str(output / '.cache/ccache'), CCACHE_BASEDIR=str(output), HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9', ALL_PROXY='http://127.0.0.1:9', NO_PROXY='')
    env.update(PSX_STUDIO_PROJECT_ONLY='1', RETCOMM_DATA_HOME=str(output / '.data'), LOCALAPPDATA=str(output / '.appdata'), APPDATA=str(output / '.roaming'), USERPROFILE=str(output / '.home'), HOME=str(output / '.home'), RETCOMM_TOOLCHAIN_CACHE=str(output / '.toolchains'))
    env['PATH'] = str(toolchain / 'bin') + os.pathsep + env.get('PATH', '')
    return env

def build_emitters(fw, toolchain, jobs, output, env, receipt, run=None):
    run = run or run_command
    cmake = toolchain / 'bin/cmake.exe'
    emitter_build = fw / 'recompiler/build'
    env['PSXRECOMP_GAME'] = str(emitter_build / 'psxrecomp-game.exe')
    env['PSXRECOMP_BIOS'] = str(emitter_build / 'psxrecomp-bios.exe')
    run([cmake, '-S', fw / 'recompiler', '-B', emitter_build, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_COMPILER=clang', '-DCMAKE_CXX_COMPILER=clang++', '-DBUILD_TESTING=OFF', '-DPSXRECOMP_ENABLE_CHD=OFF', '-DPSXRECOMP_STATIC_CLI=ON', '-DPSX_DEPS_OFFLINE=ON'], output, env, output, receipt, 'configure-emitters')
    run([cmake, '--build', emitter_build, '--target', 'psxrecomp-game', 'psxrecomp-bios', f'-j{jobs}'], output, env, output, receipt, 'build-emitters')
    return emitter_build

def operator_choices(platforms=None, tests=None):
    selected = list(dict.fromkeys(platforms or ['windows-x64']))
    requested = set(tests or [])
    return {'build_platforms': selected, 'automated_tests': {platform: {'checklist_row': row, 'selected': platform in requested, 'status': 'requested' if platform in requested else 'not run (operator choice)'} for platform, row in zip(PLATFORMS, ('B4', 'B5', 'B6', None))}}

def receipt_chdman(receipt, environ=None):
    named = receipt.get('chdman')
    if named and named != 'None':
        return Path(named)
    package = input_default(PACKAGE_ENV, PACKAGE, environ)
    if package is None:
        raise BuildStop(f"A build path is not set: {studio_settings.describe('package')}. Nothing was built.")
    return package / 'native/chdman/chdman.exe'

def require_path_settings(args, receipt, environ=None):
    environ = os.environ if environ is None else environ
    values = {'source_repo': getattr(args, 'source_repo', None), 'ui_repo': getattr(args, 'ui_repo', None), 'toolchain_pin': getattr(args, 'toolchain_pin', None), 'toolchain': getattr(args, 'toolchain', None), 'package': getattr(args, 'chdman', None), 'overlay_archives': overlay_archive_folders(getattr(args, 'overlay_archives', None), environ)}
    needed = ['source_repo', 'ui_repo', 'toolchain_pin', 'toolchain', 'overlay_archives']
    if not getattr(args, 'kit', None) and (not getattr(args, 'kits_root', None)):
        values['kit_roots'] = default_kit_roots(None, None, environ)
        needed.append('kit_roots')
    receipt['path_settings'] = {'record': str(studio_settings.record_path(environ=environ)), 'host': studio_settings.host_name(), 'values': {key: [str(p) for p in value] if isinstance(value, list) else str(value) if value else None for key, value in values.items()}}
    stop = studio_settings.require(values, needed)
    if stop:
        raise BuildStop(stop)

def remove_owned_file(path, *, missing_ok=False):
    path = Path(path)
    try:
        path.unlink(missing_ok=missing_ok)
    except PermissionError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & stat.S_IWRITE:
            raise
        path.chmod(info.st_mode | stat.S_IWRITE)
        path.unlink(missing_ok=missing_ok)

def build(args):
    try:
        from . import workbench_output
    except ImportError:
        import workbench_output
    return workbench_output.build(args)

def _build_workspace(args):
    try:
        from . import workbench_output
    except ImportError:
        import workbench_output
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise BuildStop('The output directory is occupied. Select a new empty directory.')
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'build-receipt.json').open('x', encoding='utf-8') as f:
        f.write('{"status":"started"}\n')
    receipt = {'schema': 1, 'status': 'started', 'inputs': [], 'commands': [], 'sources': [], 'game_launched': False, 'disp_native': None, 'native_dispatch_note': 'Not measured: no completed optional Windows replay.'}
    receipt.update(operator_choices(args.platform, args.test))
    output_kind = getattr(args, 'output_kind', 'private')
    input_mode = getattr(args, 'input_mode', 'portable') if output_kind == 'private' else 'no-inputs'
    receipt.update(stage_history=str(getattr(args, 'stage_history', output.parent / 'workbench-stage-history.json')), optimize_pgo=bool(getattr(args, 'optimize_pgo', False)), startup_checks=bool(getattr(args, 'startup_checks', False)), overlay_compiler={'kind': 'shared-clang' if input_mode == 'thin' else 'bundled-clang', 'backend': 'gcc'} if output_kind == 'private' else None, repair_shared_toolchain=bool(getattr(args, 'repair_shared_toolchain', False)), output=str(output), output_kind=output_kind, input_mode=input_mode, chdman=str(args.chdman), package_mode='no-inputs' if output_kind == 'setup' else 'not requested', selected_disc_paths=list(map(str, args.disc)), selected_bios_path=str(args.bios))
    temporary = None
    staged_bios = None
    current = None

    def advance(name=None, label=None):
        nonlocal current
        if current:
            progress.stage(receipt, output, current, 'done')
        current = progress.stage(receipt, output, name, label=label, start=True)['id'] if name else None
    try:
        receipt['pin'] = load_pin(args.pin)
        receipt['ui_selection'] = ui_selection.select(receipt['pin'], getattr(args, 'ui', None))
        receipt['ui_module_build'] = ui_selection.module_artifacts(receipt['pin'], receipt['ui_selection'])
        advance('pin-tools', 'Validate pin and tools')
        require_path_settings(args, receipt)
        require_pin_sources(receipt['pin'], getattr(args, 'source_repo', None), getattr(args, 'ui_repo', None), receipt, ui_ref=receipt['ui_selection']['ui']['commit'])
        if os.name != 'nt':
            raise BuildStop('This build entry supports Windows only.')
        if sys.version_info[:2] != (3, 12):
            raise BuildStop('Run this build with Python 3.12, as required by TOOLCHAIN-PIN.toml.')
        platforms = set(receipt['build_platforms'])
        if 'windows-x64' not in platforms or not platforms <= {'windows-x64', 'linux-x64', 'macos-x64'}:
            raise BuildStop('Select Windows x64, optionally with Linux x64 and macOS x64. macOS ARM64 builds are not available.')
        if platforms & {'linux-x64', 'macos-x64'} and (output_kind != 'private' or input_mode != 'portable'):
            raise BuildStop('Linux and macOS builds need a private build with Portable installation.')
        if receipt['automated_tests']['linux-x64']['selected'] or receipt['automated_tests']['macos-x64']['selected']:
            raise BuildStop('Linux and macOS package tests are not available yet. Their builds are still checked by two headless starts.')
        for platform, test in receipt['automated_tests'].items():
            if test['selected'] and (platform not in receipt['build_platforms'] or platform == 'macos-arm64'):
                test['status'] = 'not run (platform unavailable)'
                raise BuildStop('Select the matching build before its test. macOS ARM64 tests remain on hardware hold.')
        if receipt['ui_module_build'] and (platforms != {'windows-x64'} or output_kind != 'private' or receipt['automated_tests']['windows-x64']['selected']):
            raise BuildStop('A launcher-module UI builds a private Windows x64 product, without the package test, for now. Select Windows x64 alone and a private build, or the bundled UI.')
        pin = receipt['pin']
        receipt['pin'] = pin
        record_inputs([args.pin, Path(sys.executable), args.toolchain_pin, *Path(__file__).parent.glob('*.py'), *module_input_paths(receipt['ui_module_build'])], receipt)
        repo = args.source_repo.resolve()
        if git(repo, 'rev-parse', f"{pin['commit']}^{{tree}}") != pin['tree']:
            raise BuildStop('The pin tree does not match its commit. Ask the integrator to correct PIN.json.')

        def repo_key(value):
            return re.sub('\\.git$', '', value.lower().replace('git@github.com:', 'https://github.com/').rstrip('/'))
        if repo_key(git(repo, 'remote', 'get-url', 'origin')) != repo_key(pin['repo']):
            raise BuildStop('The local source repository does not match PIN.json repo.')
        if output_kind == 'private':
            check_compiler_capability(git(repo, 'show', f"{pin['commit']}:runtime/src/main.cpp"), input_mode)
        if output_kind == 'setup' or receipt['automated_tests']['windows-x64']['selected']:
            try:
                from . import workbench_package
            except ImportError:
                import workbench_package
            workbench_package.preflight(repo, pin, receipt)
            if args.kit:
                workbench_package.catalog_recipe(Path(args.kit))
        bound, toolchain, metadata = read_toolchain(args.toolchain_pin, args.toolchain)
        expected = bound['pack']
        if (metadata.get('id'), metadata.get('version'), metadata.get('os')) != (expected['id'], expected['version'], 'windows-x64'):
            raise BuildStop('Install the Windows toolchain version named by TOOLCHAIN-PIN.toml.')
        tp = pin['toolchain_pin']
        if isinstance(tp, dict):
            if tp.get('sha256') != sha(args.toolchain_pin):
                raise BuildStop('The pin toolchain record does not match TOOLCHAIN-PIN.toml SHA-256.')
        elif str(tp) not in (str(args.toolchain_pin), args.toolchain_pin.as_posix(), sha(args.toolchain_pin)):
            description = f"retcomm {metadata['id']} {metadata['version']} (llvm {metadata['pins']['llvm']}, llvm-mingw {metadata['pins']['llvm_mingw']}, cmake {metadata['pins']['cmake']}, ninja {metadata['pins']['ninja']})"
            if tp != description:
                raise BuildStop('The installed toolchain does not match the full PIN.json toolchain description.')
        receipt['toolchain'] = {'root': str(toolchain), 'metadata': metadata, 'files': inventory(toolchain)}
        selected = [str(p) if Path(p).is_absolute() else str(Path(p).absolute()) for p in args.disc]
        bios = str(args.bios) if Path(args.bios).is_absolute() else str(Path(args.bios).absolute())
        advance('source-export', 'Export pinned sources')
        fw = output / 'psxrecomp'
        snapshot(repo, pin['commit'], fw, receipt['sources'])
        isolate_toolchain_environment(fw, receipt)
        advance('kit-preflight', 'Match kit before extraction')
        kit_roots = checked_kit_roots(args.kits_root, args.kit, receipt)
        preflight = None
        preflight_psx_chd = None
        preflight_lib_path = None
        try:
            preflight = preflight_kit_match(selected, args.chdman, kit_roots, args.kit, receipt, fw=fw, toolchain=getattr(args, 'toolchain', None))
        except BuildStop:
            raise
        except Exception as error:
            print(f'note: kit preflight skipped ({error})', flush=True)
        if preflight is not None:
            preflight_psx_chd = preflight.get('psx_chd')
            preflight_lib_path = preflight.get('lib_path')
            receipt['kit_preflight'] = {'serials': preflight['serials'], 'reader': preflight['reader'], 'kit': str(preflight['kit']) if preflight['kit'] else None}
            if preflight.get('disc_facts'):
                receipt['disc_facts'] = {Path(p).name: {'volume': f['volume'], 'entries': f['entries'], 'boot': f['boot'], 'boot_size': f['boot_size']} for p, f in preflight['disc_facts'].items()}
            if preflight['kit'] is None:
                receipt['kit_diagnostics'] = preflight['diagnostics']
                require_scaffold_consent(args, kit_roots, preflight['diagnostics'], receipt)
                print('note: no source kit matched; continuing to census', flush=True)
            else:
                check_declared_disc_set(preflight['kit'], preflight['serials'], receipt)
        advance('disc-intake', 'Read selected discs')
        temporary = tempfile.TemporaryDirectory(prefix='generation-', dir=output)
        record_inputs([bios], receipt)
        discs = []
        boot_bodies = {}
        prep_kit, prep, boot_flags = intake_disc_contract(args.kit, preflight, len(selected))
        colliding_kits = []
        for n, p in enumerate(selected):
            source = Path(p)
            if preflight_psx_chd is not None and preflight_lib_path is not None and (source.suffix.lower() == '.chd'):
                try:
                    verified = verify_chd_libchdr(source, preflight_psx_chd, preflight_lib_path, prep, receipt, boot=boot_flags[n], kit=prep_kit)
                except KitSerialCollision as collision:
                    colliding_kits.append(drop_colliding_kit(collision, args, kit_roots, receipt))
                    prep_kit, prep = (None, {})
                    verified = verify_chd_libchdr(source, preflight_psx_chd, preflight_lib_path, prep, receipt, boot=boot_flags[n], kit=None)
                if verified:
                    name, body = read_chd_boot_executable(source, preflight_psx_chd, preflight_lib_path)
                    if body is not None:
                        record_inputs([source], receipt)
                        boot_bodies[str(source)] = (name, body)
                        receipt.setdefault('chd_intake', []).append({'schema': 1, 'result': 'pass', 'reader': 'libchdr-inplace', 'source': str(source), 'boot_exe': name, 'boot_bytes': len(body), 'extraction': 'skipped'})
                        discs.append(source)
                        continue
            discs.append(generation_disc(p, Path(temporary.name) / f'disc-{n + 1}', receipt, args.chdman, psx_chd=preflight_psx_chd, lib_path=preflight_lib_path))
        advance('disc-identities', 'Identify selected discs')
        identity_by_path = {}
        for path, facts in (preflight or {}).get('disc_facts', {}).items():
            body = boot_bodies.get(str(Path(path))) or boot_bodies.get(str(path))
            if body is not None and facts.get('serial'):
                identity_by_path[str(Path(path))] = {'path': body[0], 'serial': facts['serial'], 'length': len(body[1]), 'sha256': hashlib.sha256(body[1]).hexdigest(), 'boot_source': facts.get('boot_source'), 'serial_source': facts.get('serial_source')}
        identities = []
        for p in discs:
            known = identity_by_path.get(str(Path(p)))
            identities.append(known if known is not None else baseline_workflow.disc_boot_identity(p))
        receipt.update(selected_disc_identities=identities, selected_discs=list(map(str, discs)))
        serials = [p['serial'] for p in identities]
        for path, item in zip(discs, identities):
            if not item['serial']:
                raise BuildStop(f"{Path(path).name} names no disc serial: it boots {item['path']} ({item.get('boot_source') or 'SYSTEM.CNF'}), and neither that name nor the volume identifier is a serial. A kit cannot be matched to it.")
        advance('kit-match', 'Match source kit')
        kit_diagnostics = []
        try:
            kit = find_kit(serials, kit_roots, args.kit, kit_diagnostics, exclude=colliding_kits)
        except ProgramSet as programs:
            raise programs.bind(serials, selected)
        if kit is not None:
            receipt['kit'] = str(kit)
            if output_kind == 'setup' or receipt['automated_tests']['windows-x64']['selected']:
                workbench_package.catalog_recipe(kit)
                receipt['package_kit_preflight'] = {'status': 'passed', 'recipe': identity(kit / 'CMakeLists.txt')}
            before_kit_media = (discs, serials, len(receipt.setdefault('warnings', [])))
            discs, serials = use_kit_media(kit, discs, identities, receipt)
            receipt['selected_discs'] = list(map(str, args.disc))
            receipt['generation_discs'] = list(map(str, discs))
            if Path(discs[0]).suffix.lower() in ('.cue', '.bin') and str(discs[0]) in selected:
                kit_prep = tomllib.loads((kit / 'game.toml').read_text(encoding='utf-8-sig')).get('prepare_disc') or {}
                try:
                    verify_image_digests(discs[0], kit_prep, receipt, kit)
                except KitSerialCollision as collision:
                    discs, serials, kept_warnings = before_kit_media
                    del receipt['warnings'][kept_warnings:]
                    for key in ('kit', 'package_kit_preflight', 'generation_discs', 'kit_roster_missing', 'disc_identities', 'discs'):
                        receipt.pop(key, None)
                    drop_colliding_kit(collision, args, kit_roots, receipt)
                    kit_diagnostics.append(str(collision))
                    kit = None
        if kit is None and receipt.get('kit_serial_collision'):
            receipt['kit_diagnostics'] = kit_diagnostics or [receipt['warnings'][-1]]
        elif kit is None:
            receipt['kit_diagnostics'] = kit_diagnostics
            for message in kit_diagnostics:
                print(message, flush=True)
            require_scaffold_consent(args, kit_roots, kit_diagnostics, receipt)
        receipt['kit_review'] = 'reviewed kit' if kit is not None else 'unreviewed census scaffold'
        boot_header = (fw / 'runtime/include/boot_state.h').read_text()
        version = re.search('#define\\s+BOOT_STATE_VERSION\\s+(\\d+)', boot_header)
        if not version or int(version[1]) != int(pin['boot_state_version']):
            raise BuildStop('The source boot-state version differs from PIN.json.')
        jobs = resolve_build_jobs(getattr(args, 'build_jobs', None))
        receipt['build_jobs'] = jobs
        env = build_environment(toolchain, output, jobs)
        if kit is None:
            advance()
            run_command([sys.executable, fw / 'tools/new_project_layout/probe_disc.py', discs[0], '--json-out', output / 'census.json', '--write-game-toml', output / 'game.toml', '--write-seeds', output / 'seeds/ghidra_funcs.txt', '--write-boot-exe', output / 'disc', '--disc-rel', str(discs[0]), '--out-dir', 'disc'], output, env, output, receipt, 'census')
            census_config = output / 'game.toml'
            stem = resolve_bios_config(receipt, fw, serials, bios)
            if stem is None:
                receipt.setdefault('warnings', []).append(f"No BIOS profile matched the supplied ROM for {', '.join(serials)}. The config omits bios_config, so generation defaults to SCPH1001.")
            else:
                set_bios_config(census_config, stem)
            players = operator_players(args)
            set_game_players(census_config, players)
            receipt['declared_players'] = players
            receipt['declared_players_source'] = 'operator (--players)' if getattr(args, 'players', None) else 'default'
            facts = {}
            probe = output / 'census.json'
            if probe.is_file():
                try:
                    facts = json.loads(probe.read_text(encoding='utf-8-sig'))
                except ValueError:
                    facts = {}
            if stem is None:
                raise BuildStop(' '.join(kit_diagnostics) + ' No BIOS profile could be selected, so the build scaffold cannot be written. Supply a BIOS whose hash matches a framework profile, then retry.')
            scaffold_kit(output, facts, census_config, stem, players, receipt)
            receipt['disc_identities'] = receipt['selected_disc_identities']
            receipt['recipe_origin'] = 'automatic census scaffold'
            kit = None
            config = census_config
            sections = tomllib.loads(config.read_text(encoding='utf-8-sig'))
            fast_boot = getattr(args, 'fast_boot', None)
            if fast_boot is None:
                fast_boot = bool(sections.get('runtime', {}).get('fast_boot', False))
            receipt['runtime_settings'] = {'fast_boot': fast_boot, 'source': 'operator' if getattr(args, 'fast_boot', None) is not None else 'census'}
            if output_kind == 'private':
                workbench_output.lean_recipe(output, receipt)
        if kit is not None:
            progress.stage(receipt, output, 'census', 'skipped', detail='Known source kit matched')
            copy_kit(kit, output, receipt)
            if output_kind == 'private':
                workbench_output.lean_recipe(output, receipt)
            config = output / 'game.toml'
            sections = ensure_kit_bios_config(config, kit.name, receipt, fw, serials, bios)
            fast_boot = getattr(args, 'fast_boot', None)
            if fast_boot is None:
                fast_boot = bool(sections.get('runtime', {}).get('fast_boot', False))
            receipt['runtime_settings'] = {'fast_boot': fast_boot, 'source': 'operator' if getattr(args, 'fast_boot', None) is not None else 'kit'}
            for section, keys in (('game', ('exe',)), ('recompiler', ('out_dir', 'seeds', 'bios_config')), ('prepare_disc', ('out_dir',))):
                for key in keys:
                    value = sections.get(section, {}).get(key)
                    if value and (Path(value).is_absolute() or not (output / value).resolve().is_relative_to(output)):
                        raise BuildStop(f'Kit {section}.{key} escapes the new output. Correct the kit configuration.')
            if output_kind == 'private':
                set_game_media(config, list(map(str, discs)), serials)
            players = declared_players(kit)
            if players is not None:
                set_game_players(config, players)
                receipt['declared_players'] = players
        if output_kind != 'setup' or receipt['automated_tests']['windows-x64']['selected']:
            profile = sections.get('recompiler', {}).get('bios_config') or 'SCPH1001.toml'
            check_bios_identity(fw, Path(profile).stem, bios, receipt)
        ui_ref = receipt['ui_selection']['ui']['commit']
        snapshot(args.ui_repo.resolve(), ui_ref, output / 'recomp-ui', receipt['sources'])
        if output_kind == 'setup' or receipt['automated_tests']['windows-x64']['selected']:
            workbench_package.source_admission(output, env, receipt)
        receipt['source_files'] = {'framework': inventory(fw), 'ui': inventory(output / 'recomp-ui')}
        if output_kind == 'private':
            check_compiler_capability((fw / 'runtime/src/main.cpp').read_text(encoding='utf-8'), input_mode)
        offline_gate = fw / 'cmake/psx_dependency_archive.cmake'
        if not offline_gate.is_file() or 'PSX_DEPS_OFFLINE' not in offline_gate.read_text():
            raise BuildStop('This pin has no offline dependency gate. Ask the integrator for an offline-capable pin.')
        cli = fw / 'psxrecomp_cli.py'
        stage_overlay_archives(fw, output, receipt, getattr(args, 'overlay_archives', None))
        cmake = toolchain / 'bin/cmake.exe'
        emitter_build = fw / 'recompiler/build'
        advance()
        for platform, test in receipt['automated_tests'].items():
            if not test['selected']:
                progress.stage(receipt, output, f'test-{platform}', 'skipped', detail='Not selected')
        build_emitters(fw, toolchain, jobs, output, env, receipt)
        try:
            from . import workbench_package
        except ImportError:
            import workbench_package
        if output_kind == 'setup':
            progress.stage(receipt, output, 'private-product', 'skipped', detail='Setup-only package requested')
            package = workbench_package.make_package(output, None, env, receipt)
            receipt['status'] = 'setup-package-pass'
            receipt['qualification'] = 'Inputs-free setup package; no private product or gameplay qualification.'
            if receipt['automated_tests']['windows-x64']['selected']:
                receipt['automated_tests']['windows-x64']['status'] = 'running'
                stem = Path(sections['recompiler']['bios_config']).stem
                workbench_package.replay(output, package, receipt['setup_exe_name'], [Path(p) for p in selected], Path(bios), stem, serials, env, receipt)
            check_inputs(receipt)
            return receipt
        record_inputs([bios], receipt)
        bios_config = fw / 'bios' / Path(sections['recompiler']['bios_config']).name
        stem = bios_config.stem
        build_dir = output / 'build-release'
        advance('runtime-media', 'Prepare runtime media')
        runtime_discs, local_bios = runtime_media(selected, bios, build_dir, input_mode, receipt)
        receipt.update(discs=runtime_discs, bios=local_bios, runtime_config=str(build_dir / 'game.toml'), build_config=str(config))
        staged_boot = boot_bodies.get(str(Path(discs[0]).resolve())) or boot_bodies.get(str(discs[0]))
        if staged_boot is not None:
            stage_boot_executable(config, discs[0], output, boot_body=staged_boot[1], boot_name=staged_boot[0])
        else:
            stage_boot_executable(config, discs[0], output)
        staged_bios = bios_stage_path(fw, stem)
        advance()
        run_command([sys.executable, cli, 'generate', '--project-root', output, '--config', config, '--disc', discs[0], '--bios', local_bios, '--bios-stem', stem, '--no-toolchain-download', '--json-progress'], output, env, output, receipt, 'generate')
        temporary.cleanup()
        temporary = None
        set_game_media(config, [Path(os.path.relpath(p, output)).as_posix() for p in runtime_discs] if input_mode == 'portable' else runtime_discs, serials)
        receipt['runtime_settings']['written_to_settings'] = chosen_fast_boot(receipt['runtime_settings']) is not None
        write_runtime_media(config, build_dir, runtime_discs, local_bios, serials, input_mode, fast_boot=True if receipt['optimize_pgo'] else chosen_fast_boot(receipt['runtime_settings']))
        pgo_args = ['--force-pgo', '--disc', runtime_discs[0], '--pgo-mute', '--pgo-hide-video'] if receipt['optimize_pgo'] else ['--no-pgo']
        if receipt['optimize_pgo']:
            receipt['pgo_training'] = {'fast_boot': True, 'bios': local_bios, 'disc': runtime_discs[0], 'settings': str(build_dir / 'settings.toml'), 'call_hle_override': False}
            receipt['game_launched'] = True
        rebuild = [sys.executable, cli, 'rebuild', '--project-root', output, '--config', config, '--build-dir', build_dir, '--target', 'psx-runtime', *pgo_args, '--no-toolchain-download', '--cmake-extra=-DPSX_DEPS_OFFLINE=ON', '--cmake-extra=-DPSX_SETUP_WIZARD=OFF', *module_cmake_args(receipt, output), '--json-progress']
        optimization = getattr(args, 'compiler_optimization', 'optimized')
        import compiler_options
        rebuild += compiler_options.arguments(optimization, not getattr(args, 'disable_lto', False))
        if getattr(args, 'diagnostics', False):
            rebuild += ['--diagnostic-dir', str(output / 'build-diagnostics')]
        receipt['compiler_options'] = {'optimization': optimization, 'lto': not getattr(args, 'disable_lto', False), 'diagnostics': getattr(args, 'diagnostics', False)}
        refuse_forced_static_runtime(output, receipt)
        rebuild_log = rebuild_with_one_retry(rebuild, output, env, receipt, build_dir, toolchain)
        if receipt['optimize_pgo']:
            workbench_output.pgo_result(rebuild_log, receipt)
        workbench_output.verify_lean_compile(build_dir, receipt)
        receipt['compiler_options_verified'] = compiler_options.verify(build_dir, optimization, not getattr(args, 'disable_lto', False))
        advance('runtime-config', 'Validate runtime files')
        exe_name = (build_dir / 'psxrecomp_exe_name-psx-runtime.txt').read_text().strip()
        if not exe_name or Path(exe_name).name != exe_name:
            raise BuildStop('The build did not publish a safe executable filename.')
        executable = build_dir / (exe_name + '.exe')
        write_runtime_media(config, build_dir, runtime_discs, local_bios, serials, input_mode, fast_boot=chosen_fast_boot(receipt['runtime_settings']))
        (build_dir / 'saves').mkdir(exist_ok=True)
        overlay = build_dir / 'overlay_toolchain'
        for required in ('python/python.exe', 'psxrecomp-game.exe', 'compile_overlays.py', 'compiler.txt', 'include/overlay_codegen_hash.h'):
            if not (overlay / required).is_file():
                raise BuildStop(f'The pin did not stage overlay_toolchain/{required}. Restore staging before testing gameplay.')
        headers = list(build_dir.rglob('overlay_codegen_hash.h'))
        if not headers:
            raise BuildStop('The build did not produce overlay_codegen_hash.h. Ask the pin integrator to restore codegen identity staging.')
        header = headers[0]
        if header.parent != build_dir:
            shutil.copy2(header, build_dir / header.name)
        advance()
        if receipt['startup_checks']:
            if 'psxrecomp_codegen_host.c' not in (build_dir / 'build.ninja').read_text(encoding='utf-8'):
                raise BuildStop('The product was built without the setup host, so it cannot answer the setup self-check. The recipe must keep PSX_RECOMP_UI ON and CODEGEN_SETUP_SOURCES.')
            log = run_command([executable, '--setup-selfcheck'], build_dir, host_start_environment(env, output / 'setup-selfcheck-host'), output, receipt, 'setup-selfcheck', keep_stdout=True)
            advance('selfcheck-contract', 'Validate setup self-check')
            check = selfcheck_report(log)
            if check.get('sources_missing') is not False:
                raise BuildStop('Setup self-check did not confirm generated sources. Read setup-selfcheck.log.')
            advance()
            receipt['selfcheck'] = check
        else:
            receipt['selfcheck'] = {'status': 'not-requested'}
        receipt['executable'] = identity(executable)
        if receipt['automated_tests']['windows-x64']['selected']:
            package = workbench_package.make_package(output, exe_name, env, receipt)
            receipt['automated_tests']['windows-x64']['status'] = 'running'
            workbench_package.replay(output, package, exe_name, [Path(p) for p in runtime_discs], Path(local_bios), stem, serials, env, receipt)
        else:
            progress.stage(receipt, output, 'package', 'skipped', detail='Private build without package test')
        advance('input-integrity', 'Verify original inputs')
        cache_text = (build_dir / 'CMakeCache.txt').read_text(encoding='utf-8')
        glslc = re.search('(?m)^GLSLC_EXE:FILEPATH=(.+)$', cache_text)
        if glslc and Path(glslc[1]).is_file():
            sdk = Path(glslc[1]).parent.parent
            receipt['vulkan_sdk_inputs'] = {'root': str(sdk), 'include_root': str(sdk / 'Include'), 'files': inventory(sdk / 'Include') + [identity(p) for p in (sdk / 'Bin').glob('*') if p.is_file()]}
        receipt['executable'] = identity(executable)
        check_inputs(receipt)
        advance()
        receipt['status'] = 'development-build-pass' if pin.get('development_stub') else 'build-pass'
        receipt['qualification'] = 'Build, setup check and optional B4 passed; no TAS or whole-product qualification.' if receipt['game_launched'] else 'Build and setup check only. Alex must test gameplay. Native dispatch is unmeasured.'
    except Exception as e:
        receipt['status'] = 'human-stop'
        if isinstance(e, ProgramSet):
            receipt['program_set'] = e.plan
        for test in receipt['automated_tests'].values():
            if test['status'] == 'running':
                test['status'] = 'failed'
        receipt['next_action'] = str(e)
        if not receipt.get('failed_stage'):
            progress.stage(receipt, output, current or 'build-validation', 'failed', detail=str(e), log=receipt['commands'][-1]['log'] if receipt['commands'] else None)
        (output / 'HUMAN-STOP.txt').write_text(str(e) + '\n', encoding='utf-8')
    finally:
        if temporary is not None:
            temporary.cleanup()
        if staged_bios is not None and staged_bios.is_relative_to(output):
            remove_owned_file(staged_bios, missing_ok=True)
        progress.finish(receipt, output, receipt['status'] == 'human-stop', receipt.get('next_action'))
        receipt['outputs'] = inventory(output)
        receipt['outputs'] = [p for p in receipt['outputs'] if p['path'] != 'build-receipt.json']
        write_json(output / 'build-receipt.json', receipt)
    return receipt

def add_commands(commands):
    listing = commands.add_parser('pins', help='List immutable local PIN.json records')
    listing.add_argument('--pin-root', type=Path, default=PIN_ROOT)
    listing.add_argument('--ui-root', type=Path)
    listing.add_argument('--pin', type=Path, help='Inspect one local PIN.json without changing the shared registry.')
    listing.add_argument('--ui', type=Path, action='append', default=[], help="Evaluate one local UI.json against each listed pin, as Studio's Load UI does; repeatable.")
    parser = commands.add_parser('build', help='Build an owned-input kit from a pin; gameplay tests require explicit --test')
    parser.add_argument('--pin', type=Path, required=True)
    parser.add_argument('--bios', required=True)
    parser.add_argument('--disc', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--keep-development-files', action='store_true', help='Retain the separate development workspace after success.')
    parser.add_argument('--optimize-pgo', action='store_true', help='Opt in to the pin training and optimized rebuild; failure stops delivery.')
    parser.add_argument('--repair-shared-toolchain', action='store_true', help='Repair a corrupt shared thin compiler when no game uses it; preserve the old directory in quarantine.')
    parser.add_argument('--fast-boot', action='store_true', default=None, help='Explicit runtime boot-skip override; otherwise retain the kit setting.')
    parser.add_argument('--input-mode', choices=('portable', 'thin'), default='portable')
    parser.add_argument('--output-kind', choices=('private', 'setup'), default='private')
    parser.add_argument('--platform', action='append', choices=PLATFORMS, help='Build platform; defaults to windows-x64. Other platforms are not yet available.')
    parser.add_argument('--test', action='append', choices=PLATFORMS, help='Request a platform test. Default: no replay tests; record operator choice.')
    parser.add_argument('--kit', type=Path)
    parser.add_argument('--kits-root', type=Path, action='append')
    parser.add_argument('--allow-scaffold', action='store_true', help='When no reviewed kit matches, build an unreviewed census scaffold instead of stopping.')
    parser.add_argument('--players', type=int, help='Reviewed player count for a scaffold build (1-8). Two or more turns netplay on. A kit states its own count, so this is ignored when a kit matches.')
    setting = ' Studio setting {} (Workbench Studio > Settings).'
    package = input_default(PACKAGE_ENV, PACKAGE)
    parser.add_argument('--source-repo', type=Path, default=input_default(SOURCE_ENV, SOURCE), help=f'The psxrecomp clone. Default: {SOURCE_ENV}, then the' + setting.format('source_repo'))
    parser.add_argument('--ui-repo', type=Path, default=input_default(UI_ENV, UI), help=f'The recomp-ui clone. Default: {UI_ENV}, then the' + setting.format('ui_repo'))
    parser.add_argument('--ui', type=Path, help='Explicit compatible UI.json. Omit to use the UI bundled by the selected pin.')
    parser.add_argument('--toolchain-pin', type=Path, default=input_default(TOOLCHAIN_PIN_ENV, TOOLCHAIN_PIN), help=f'TOOLCHAIN-PIN.toml. Default: {TOOLCHAIN_PIN_ENV}, then the' + setting.format('toolchain_pin'))
    toolchain = studio_settings.default_value('toolchain')
    parser.add_argument('--toolchain', type=Path, default=input_default(TOOLCHAIN_ENV, toolchain), help=f'The installed cmake-clang pack. Default: {TOOLCHAIN_ENV}, then the Studio setting toolchain, then %%LOCALAPPDATA%%/RetComM/toolchains/cmake-clang-v1/1.0.14.')
    parser.add_argument('--chdman', type=Path, default=package / 'native/chdman/chdman.exe' if package else None, help=f'chdman, the fallback disc reader when libchdr cannot be used. Default: native/chdman/chdman.exe under {PACKAGE_ENV}, then under the' + setting.format('package'))
    parser.add_argument('--overlay-archives', type=Path, action='append', help="Folder holding the pin's offline overlay archives (python embed, tcc); repeatable. Also WORKBENCH_OVERLAY_ARCHIVES, then the" + setting.format('overlay_archives'))
    parser.add_argument('--run-root', type=Path, help="Folder for this build's run folder (work tree and evidence). Default: WORKBENCH_RUN_ROOT, then %%LOCALAPPDATA%%/PSXWorkbench/builds.")
    runs = commands.add_parser('runs', help='List Workbench run folders and what retention would remove (build)')
    runs.add_argument('--run-root', type=Path)
    runs.add_argument('--keep-failed', type=int, help='Stopped runs that keep their work tree (default 10; -1 keeps all).')
    runs.add_argument('--prune', action='store_true', help='Remove what the listing marks; without it this is a dry run.')
if __name__ == '__main__':
    import workbench
    raise SystemExit(workbench.main())
