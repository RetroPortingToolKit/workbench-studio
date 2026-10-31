import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tomllib
import zipfile
try:
    from . import pin_build as pb
except ImportError:
    import pin_build as pb
SETUP_MARKERS = (('tools/program_set.py', 'def join_products('), ('psxrecomp_cli.py', '--set-disc'), ('tools/package_setup_host.sh', '--set)'))
SET_FILE = 'set.toml'
PROGRAMS_FOLDER = 'programs'
SHARED_TREES = ('psxrecomp', 'recomp-ui')
HOST_FILES = ('CMakeLists.txt', 'codegen_setup.c', 'codegen_setup.h')
ROOT_FILES = (SET_FILE, *HOST_FILES, 'README-SETUP.txt', 'VERSION', 'psx_game_version.txt', 'README.md')
ROOT_ASSET_FOLDERS = ('assets/fonts/', 'assets/img/')
ROOT_NOTICES = ('licenses/toolchain/SDL3-LICENSE.txt', 'licenses/toolchain/zlib-LICENSE.txt', 'licenses/toolchain/llvm-mingw-LICENSE.TXT')
LICENCE_FILES = ('psxrecomp/LICENSE', 'psxrecomp/THIRD_PARTY_ATTRIBUTION.md', 'psxrecomp/tools/licenses/LGPL-2.1.txt', 'recomp-ui/LICENSE', *ROOT_NOTICES)
PACKAGED_PROGRAMS = ('psxrecomp/recompiler/build/psxrecomp-game.exe', 'psxrecomp/recompiler/build/psxrecomp-bios.exe')
PRIVATE_SUFFIXES = ('.bin', '.cue', '.chd', '.iso', '.img', '.ccd', '.sub', '.mdf', '.mds', '.pbp', '.ecm', '.sbi', '.rom', '.mcd', '.mcr', '.sav', '.state')
BINARY_SUFFIXES = ('.exe', '.dll', '.so', '.dylib', '.o', '.obj', '.a', '.lib', '.pdb')
PROGRAM_FOLDERS_REFUSED = ('generated', 'build-release', 'build-setup', 'build-diagnostic', 'cache', '.cache', 'dist', 'disc', 'prepared_disc', 'saves', 'psxrecomp', 'recomp-ui', '__pycache__', '.git')

def missing_setup_markers(repo, commit):
    missing = []
    for path, marker in SETUP_MARKERS:
        try:
            text = pb.git(repo, 'show', f'{commit}:{path}')
        except pb.BuildStop:
            text = ''
        if marker not in text:
            missing.append(f'{path} ({marker})')
    return missing

def has_setup_support(repo, commit):
    return not missing_setup_markers(repo, commit)

def pin_label(pin):
    return f"{pin.get('name') or 'the selected pin'} ({str(pin.get('commit'))[:9]})"

def require_setup_support(repo, pin):
    missing = missing_setup_markers(repo, pin['commit'])
    if missing:
        raise pb.BuildStop(f'Pin {pin_label(pin)} cannot make a setup package for a set of programs: its framework lacks ' + ', '.join(missing) + '. Build the set as a private product (Private output), or choose a pin that has setup packages for program sets (pin H or later).')

def safe_name(value):
    return isinstance(value, str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]*', value) is not None

def set_exe_name(plan):
    name = re.sub('[^A-Za-z0-9]+', '_', plan.get('title') or plan['name']).strip('_')
    if not name:
        raise pb.BuildStop(f"The set {plan['name']} has no title to name its setup program from. Give [retail_identity.set] a title in the kits.")
    taken = [program['exe_name'] for program in plan['programs']]
    if name.lower() in (other.lower() for other in taken):
        raise pb.BuildStop(f"The set's setup program would be named {name}, which is also the name of one of its programs. One folder holds both. Give the programs' exe_name a suffix in the kits' [retail_identity.set].")
    return name

def set_bios_stem(configs):
    stems = {}
    for program, text in configs.items():
        profile = (tomllib.loads(text).get('recompiler') or {}).get('bios_config') or 'SCPH1001.toml'
        stems[program] = Path(profile).stem
    if len(set(stems.values())) != 1:
        raise pb.BuildStop('The programs of this set name different BIOS profiles (' + ', '.join((f'{program}: {stem}' for program, stem in stems.items())) + "). One setup locates one BIOS. Make the kits' [recompiler] bios_config agree.")
    return next(iter(stems.values()))
HOST_GAME_KEYS = ('exe', 'load_address', 'entry_pc', 'text_size', 'stack_base')
HOST_REQUIRED = ('exe', 'load_address', 'text_size')

def host_config(plan, configs, bios_stem):
    first = plan['programs'][0]['program']
    sections = tomllib.loads(configs[first])
    game, recompiler = (sections.get('game') or {}, sections.get('recompiler') or {})
    lacking = [f'[game] {key}' for key in HOST_REQUIRED if game.get(key) in (None, '')]
    if not recompiler.get('seeds'):
        lacking.append('[recompiler] seeds')
    if lacking:
        raise pb.BuildStop(f"The kit of program {first} lacks {', '.join(lacking)} in its game.toml. The set's setup program reads them from set.toml before it can open its window (build). Complete the kit's game.toml.")
    folder = PurePosixPath(PROGRAMS_FOLDER) / first

    def rooted(value):
        return (folder / str(value).replace('\\', '/')).as_posix()
    host_game = {'name': plan['title']}
    for key in HOST_GAME_KEYS:
        if game.get(key) not in (None, ''):
            host_game[key] = rooted(game[key]) if key == 'exe' else game[key]
    return {'program': first, 'game': host_game, 'recompiler': {'bios_config': f'psxrecomp/bios/{bios_stem}.toml', 'seeds': rooted(recompiler['seeds']), 'out_dir': 'generated'}}

def placeholder_discs(count):
    return [f'disc/disc-{number + 1}.cue' for number in range(count)]

def _value(value):
    if isinstance(value, list):
        return '[' + ', '.join((_value(item) for item in value)) + ']'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))

def set_toml_text(plan, exe_name, bios_stem, host=None):
    programs = plan['programs']
    for program in programs:
        if not safe_name(program['program']):
            raise pb.BuildStop(f"Program name {program['program']!r} cannot name a folder in the package. Use letters, digits, '-' and '_' in [retail_identity.set] program.")
        if not safe_name(program['exe_name']):
            raise pb.BuildStop(f"Executable name {program['exe_name']!r} of program {program['program']} is not a plain file name. Correct [retail_identity.set] exe_name.")
    if len({program['exe_name'].lower() for program in programs}) != len(programs):
        raise pb.BuildStop('Two programs of this set have the same exe_name. One folder holds both.')
    lines = ["# Written by Workbench Studio from the kits' [retail_identity.set] declarations. Do not edit.", '[set]']
    for key, value in (('name', plan['name']), ('title', plan['title']), ('exe_name', exe_name), ('bios_stem', bios_stem), ('serials', plan['serials']), ('programs', [program['program'] for program in programs])):
        lines.append(f'{key} = {_value(value)}')
    lines += ['', '[game]']
    if host:
        lines += ["# The next keys are here only so that this set's setup program can load the file and open", f"# its window (build). They are program {host['program']}'s. The setup program runs no game,", "# and no tool reads them as a program's values: each program is generated and built from", '# its own game.toml.']
        lines += [f'{key} = {_value(value)}' for key, value in host['game'].items()]
    lines += [f"discs = {_value(placeholder_discs(len(plan['serials'])))}", f"disc_serials = {_value(plan['serials'])}"]
    if host:
        lines += ['', '[recompiler]', "# As above: read by the setup program's loader only."]
        lines += [f'{key} = {_value(value)}' for key, value in host['recompiler'].items()]
    for program in programs:
        lines += ['', f"[program.{program['program']}]"]
        for key, value in (('folder', f"{PROGRAMS_FOLDER}/{program['program']}"), ('exe_name', program['exe_name']), ('shortcut', program['shortcut']), ('serials', program['serials']), ('positions', program['positions'])):
            lines.append(f'{key} = {_value(value)}')
    text = '\n'.join(lines) + '\n'
    parsed = tomllib.loads(text)
    if parsed['set']['serials'] != plan['serials'] or list(parsed['program']) != parsed['set']['programs']:
        raise pb.BuildStop("Cannot write set.toml for this set. Review the kits' [retail_identity.set].")
    if 'id' in parsed['game']:
        raise pb.BuildStop("set.toml must not carry [game] id: the setup program would take one program's serial as the set's.")
    return text

def write_set_toml(path, plan, exe_name, bios_stem, host=None):
    data = set_toml_text(plan, exe_name, bios_stem, host).encode('utf-8')
    Path(path).write_bytes(data)
    return data

def listing_problems(names, plan, exe_name):
    problems = []
    host = exe_name + '.exe'
    programs = [program['program'] for program in plan['programs']]
    allowed_root = {*ROOT_FILES, host}
    present = set()
    for name in names:
        path = PurePosixPath(name)
        parts = path.parts
        if path.is_absolute() or '..' in parts or '\\' in name or (parts and ':' in parts[0]):
            problems.append(f'{name}: a path outside the package folder')
            continue
        present.add(name)
        suffix = path.suffix.lower()
        if suffix in PRIVATE_SUFFIXES:
            problems.append(f'{name}: a disc image, BIOS image or save file')
            continue
        if len(parts) == 1:
            if name not in allowed_root:
                problems.append(f"{name}: not a file of a set package's root")
            continue
        top = parts[0]
        if top == PROGRAMS_FOLDER:
            if len(parts) < 3 or parts[1] not in programs:
                problems.append(f"{name}: not inside a declared program's folder")
                continue
            refused = parts[2] if len(parts) > 3 and parts[2] in PROGRAM_FOLDERS_REFUSED else next((part for part in parts[2:-1] if part in ('__pycache__', '.git')), None)
            if refused:
                problems.append(f'{name}: a program folder must not carry {refused}/ (made by the setup, or private)')
            elif suffix in BINARY_SUFFIXES:
                problems.append(f"{name}: a compiled file in a program's kit export")
        elif top in SHARED_TREES:
            if suffix in BINARY_SUFFIXES and name not in PACKAGED_PROGRAMS:
                problems.append(f'{name}: a compiled file other than the two recompilers')
        elif top == 'assets':
            if not name.startswith(ROOT_ASSET_FOLDERS):
                problems.append(f"{name}: the root's assets/ holds the wizard's fonts and images only")
            elif suffix in BINARY_SUFFIXES:
                problems.append(f"{name}: a compiled file among the wizard's assets")
        elif top == 'licenses':
            if name not in ROOT_NOTICES:
                problems.append(f"{name}: the root's licenses/ holds the three toolchain licence texts only")
        else:
            problems.append(f'{name}: {top}/ is not a folder of a set package')
    required = [host, SET_FILE, *HOST_FILES, *LICENCE_FILES, *(f'{PROGRAMS_FOLDER}/{program}/{member}' for program in programs for member in ('game.toml', 'CMakeLists.txt'))]
    return [f'{name}: missing' for name in required if name not in present] + problems

def check_package(package, set_toml, plan, exe_name, notices=None):
    with zipfile.ZipFile(package) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        problems = listing_problems(names, plan, exe_name)
        if SET_FILE in names and archive.read(SET_FILE) != set_toml:
            problems.append(f'{SET_FILE}: not the bytes Studio wrote')
        for member, data in (notices or {}).items():
            if member in names and archive.read(member) != data:
                problems.append(f'{member}: not the bytes Studio wrote')
        for program in plan['programs']:
            member = f"{PROGRAMS_FOLDER}/{program['program']}/game.toml"
            if member not in names:
                continue
            try:
                game = tomllib.loads(archive.read(member).decode('utf-8-sig')).get('game') or {}
            except (ValueError, UnicodeDecodeError) as error:
                problems.append(f'{member}: cannot be read ({error})')
                continue
            discs = game.get('discs') or ([game['disc']] if game.get('disc') else [])
            if discs != placeholder_discs(len(discs)):
                problems.append(f'{member}: names a disc path of the build machine')
    if problems:
        shown = '; '.join(problems[:12]) + (f'; and {len(problems) - 12} more' if len(problems) > 12 else '')
        raise pb.BuildStop("The set's setup package is not what a set package may hold: " + shown + ". Nothing was delivered. Review the packager's file list for a set.")
    return {'files': len(names), 'programs': [program['program'] for program in plan['programs']], 'host': exe_name + '.exe', 'licences': list(LICENCE_FILES), 'set_toml': 'unchanged'}
JOIN_RECORD_KEYS = ('known_variants', 'line_unions', 'per_program', 'shared_files', 'set_discs', 'not_taken')

def framework_sources(program_receipts):
    found = {}
    for receipt in program_receipts:
        for record in receipt.get('sources') or []:
            name = Path(record['destination']).name
            if name not in SHARED_TREES:
                continue
            if name in found and found[name]['commit'] != record['commit']:
                raise pb.BuildStop(f"The programs of this set were built from different {name} commits ({found[name]['commit'][:9]} and {record['commit'][:9]}). Build the set again.")
            found.setdefault(name, record)
    missing = [name for name in SHARED_TREES if name not in found]
    if missing:
        raise pb.BuildStop("The programs' build receipts do not name the source of " + ' and '.join(missing) + '.')
    return found

def write_set_version(source, plan, record):
    versions = {}
    for program in plan['programs']:
        path = Path(source) / PROGRAMS_FOLDER / program['program'] / 'VERSION'
        versions[program['program']] = path.read_text(encoding='utf-8-sig').strip() if path.is_file() else None
    if len(set(versions.values())) != 1:
        raise pb.BuildStop('The programs of this set have different versions (' + ', '.join((f"{program}: {version or 'none'}" for program, version in versions.items())) + "). One package has one version. Make the kits' VERSION files agree.")
    version = next(iter(versions.values()))
    if not version:
        record.setdefault('warnings', []).append("No kit of this set has a VERSION file; the set's setup program is stamped 0.0.0.")
        return None
    (Path(source) / 'VERSION').write_text(version + '\n', encoding='utf-8', newline='\n')
    return version

def packager_command(bash, source, setup, plan, exe_name, emitters):
    source = Path(source)
    command = [bash, source / 'psxrecomp/tools/package_setup_host.sh', '--root', source, '--build-dir', setup, '--artifact', 'windows-x64', '--zip-prefix', 'workbench', '--exe-name', exe_name, '--display-name', plan['title'], '--set', source / SET_FILE, '--recompiler-build', emitters, '--omit-openbios']
    return [str(part).replace('\\', '/') for part in command]

def has_kit_check(fw):
    try:
        return '"kit-check"' in (Path(fw) / 'tools/program_set.py').read_text(encoding='utf-8')
    except OSError:
        return False

def kit_check(fw, plan, work, env, record, run=None):
    run = run or pb.run_command
    command = [sys.executable, Path(fw) / 'tools/program_set.py', 'kit-check', *(f"{program['program']}={program['kit']}" for program in plan['programs'])]
    try:
        log = run(command, work, env, work, record, 'set-kit-check')
    except pb.BuildStop as stop:
        lines = [line.strip() for line in str(stop).splitlines() if line.strip().startswith('differs:')]
        if not lines:
            raise
        raise pb.BuildStop(f'The kits of this set disagree about {len(lines)} file(s) that the shared folder holds once: ' + '; '.join((line[len('differs:'):].strip() for line in lines)) + '. Make the kits agree. No program was built.') from stop
    record['kit_check'] = {'status': 'passed', 'kits': [program['kit'] for program in plan['programs']]}
    return log

def join_disagreements(work):
    try:
        record = json.loads((Path(work) / 'program-set-join.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record.get('disagreements') if isinstance(record, dict) else None

def early_admission(work, fw, plan, env, record, run=None):
    try:
        from . import workbench_package as single
    except ImportError:
        import workbench_package as single
    run = run or pb.run_command
    work, fw = (Path(work), Path(fw))
    if not single.BASH.is_file():
        raise pb.BuildStop(single.NEEDS_BASH)
    stage = work / 'admission-stage'
    stage.mkdir()
    try:
        configs = {}
        scratch = {'inputs': []}
        for program in plan['programs']:
            folder = stage / PROGRAMS_FOLDER / program['program']
            folder.mkdir(parents=True)
            single.export_kit(Path(program['kit']), folder, program['serials'], scratch)
            configs[program['program']] = (folder / 'game.toml').read_text(encoding='utf-8-sig')
        stem = set_bios_stem(configs)
        write_set_toml(stage / SET_FILE, plan, set_exe_name(plan), stem, host_config(plan, configs, stem))
        write_set_version(stage, plan, {'warnings': []})
        if env.get('PSXRECOMP_TOOLCHAIN_DIR'):
            single.write_package_notices(env['PSXRECOMP_TOOLCHAIN_DIR'], stage, {})
        run([sys.executable, fw / 'tools/program_set.py', 'check', '--set', stage / SET_FILE], stage, env, work, record, 'set-check-set-file')
        check_env = env | {'PATH': str(single.BASH.parent) + os.pathsep + env['PATH']}
        run([single.BASH, fw / 'tools/stage_framework_tree.sh', '--framework', fw, '--dest', stage / 'psxrecomp'], work, check_env, work, record, 'set-admission-stage')
        single.private_paths(fw, stage, check_env, work, record, name='set-private-paths')
        record['early_admission'] = {'status': 'passed', 'programs': [p['program'] for p in plan['programs']], 'checked': ["set.toml by the pin's reader", 'kit exports', 'private-path gate on the staged framework tree and the kit exports']}
    finally:
        shutil.rmtree(stage, ignore_errors=True)

def make_package(work, fw, plan, sources, toolchain, jobs, env, record, overlay_archives=None, run=None):
    try:
        from . import workbench_package as single
    except ImportError:
        import workbench_package as single
    run = run or pb.run_command
    work, fw, toolchain = (Path(work), Path(fw), Path(toolchain))
    emitters = pb.build_emitters(fw, toolchain, jobs, work, env, record, run=run)
    source = work / 'setup-source'
    source.mkdir()
    configs = {}
    for program in plan['programs']:
        folder = source / PROGRAMS_FOLDER / program['program']
        folder.mkdir(parents=True)
        single.export_kit(Path(program['kit']), folder, program['serials'], record)
        configs[program['program']] = (folder / 'game.toml').read_text(encoding='utf-8-sig')
    package_sources = []
    for name in SHARED_TREES:
        pb.snapshot(Path(sources[name]['repo']), sources[name]['commit'], source / name, package_sources)
    record['package_sources'] = package_sources
    record['package_mode'] = 'no-inputs'
    exe_name = set_exe_name(plan)
    stem = set_bios_stem(configs)
    host = host_config(plan, configs, stem)
    set_toml = write_set_toml(source / SET_FILE, plan, exe_name, stem, host)
    record['set_file'] = pb.identity(source / SET_FILE)
    record['set_host_keys'] = {'from_program': host['program'], 'game': sorted(host['game']), 'recompiler': sorted(host['recompiler'])}
    record['set_version'] = write_set_version(source, plan, record)
    notices = single.write_package_notices(toolchain, source, record)
    package_fw = source / 'psxrecomp'
    run([sys.executable, package_fw / 'tools/program_set.py', 'init-host', '--root', source], source, env, work, record, 'set-init-host')
    missing = [name for name in HOST_FILES if not (source / name).is_file()]
    if missing:
        raise pb.BuildStop("The pin's program_set.py init-host did not write " + ', '.join(missing) + ' at the package root. Read set-init-host.log.')
    if (source / SET_FILE).read_bytes() != set_toml:
        raise pb.BuildStop("The pin's program_set.py init-host changed set.toml. Studio's copy is the one the package must carry.")
    pb.stage_overlay_archives(package_fw, source, record, overlay_archives)
    cmake = toolchain / 'bin/cmake.exe'
    setup = source / 'build-setup'
    run([cmake, '-S', source, '-B', setup, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_COMPILER=clang', '-DCMAKE_CXX_COMPILER=clang++', '-DPSXRECOMP_FORCE_SETUP_HOST=ON', '-DPSXRECOMP_ALLOW_NO_BIOS=ON', '-DPSX_DEPS_OFFLINE=ON'], source, env, work, record, 'configure-set-setup-host')
    run([cmake, '--build', setup, '--target', 'psx-runtime', '-j2'], source, env, work, record, 'build-set-setup-host')
    name_file = setup / 'psxrecomp_exe_name-psx-runtime.txt'
    actual = name_file.read_text().strip() if name_file.is_file() else ''
    if actual != exe_name:
        raise pb.BuildStop(f"The set's setup host was built as {actual or 'an unnamed program'}, not {exe_name}, the [set] exe_name Studio wrote.")
    record['setup_exe_name'] = exe_name
    record['setup_package_selfcheck'] = single.setup_check(setup / (exe_name + '.exe'), setup, env, work, record, 'set-setup-package-selfcheck', True)
    package_env = single.packager_environment(env, toolchain, record, Path(work) / 'package-set-windows-host')
    run(packager_command(single.BASH, source, setup, plan, exe_name, emitters), source, package_env, work, record, 'package-set-windows')
    single.packager_wrote(record)
    packages = list((source / 'dist').glob('*.zip'))
    if len(packages) != 1:
        raise pb.BuildStop('The pin packager must produce exactly one local Windows package for the set.')
    record['windows_package'] = pb.identity(packages[0])
    record['package_inventory'] = check_package(packages[0], set_toml, plan, exe_name, notices)
    record['package_notices']['in_package'] = 'unchanged'
    single.setup_start_check(packages[0], exe_name, env, work, record, name='set-setup-start')
    return (packages[0], set_toml, exe_name)

def join_with_pinned_tool(fw, set_file, programs, target, work, env, record, run=None):
    run = run or pb.run_command
    for program in programs:
        if not program.get('executable') or not program.get('exe_name'):
            continue
        built = Path(program['executable']).stem
        if built != program['exe_name']:
            raise pb.BuildStop(f"Program {program['program']} was built as {built}, but the set declares exe_name {program['exe_name']}. Make the kit's EXE_NAME (CMakeLists.txt) and its [retail_identity.set] exe_name agree.")
    result = Path(work) / 'program-set-join.json'
    command = [sys.executable, Path(fw) / 'tools/program_set.py', 'join', '--set', set_file]
    for program in programs:
        command += ['--program', f"{program['program']}={program['product']}"]
    command += ['--out', target, '--record', result]
    run(command, work, env, work, record, 'set-join')
    try:
        merge = json.loads(result.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise pb.BuildStop(f"The pin's program_set.py join left no readable record at {result}: {error}")
    lacking = [key for key in JOIN_RECORD_KEYS if key not in merge]
    if lacking:
        raise pb.BuildStop("The pin's program_set.py join record lacks " + ', '.join(lacking) + '.')
    left = {program: names for program, names in (merge['not_taken'] or {}).items() if names}
    if left:
        raise pb.BuildStop("The pin's join left files of a program's product out of the joined folder: " + '; '.join((f"{program}: {', '.join(names)}" for program, names in left.items())) + '. A product file must be taken or rewritten. Report the names to the framework side.')
    return merge
INSTALL_RESULT = 'program-set-install.json'
GEN_MARKER = 'program_set.generated'
INSTALLED_ONLY = ('disc.cfg', 'bios.cfg', 'recomp_deferred_rebuild.cmd', 'overlay_toolchain/compiler.txt')
PRIVATE_ONLY_REASONS = {'BUILDINFO.json': "Studio's provenance", 'SHA256SUMS.txt': "Studio's provenance", 'settings.toml': 'written by the launcher on its first save', 'bios.cfg': "Studio writes it at build; on a player's machine the wizard does, and the setup pass run as commands has no wizard", 'overlay_codegen_hash.h': "Studio's own copy at the product root; the pin stages overlay_toolchain/include/overlay_codegen_hash.h, which both folders have", 'saves/disc_digests.tsv': 'a program writes it at a start; the gate started the private product', 'overlay_toolchain/bios/OpenBIOS.toml': 'the package is made with --omit-openbios, so the profile is not there for the setup to stage', 'overlay_toolchain/compiler-store.sha256': "Studio's shared compiler store (thin input mode)", 'licenses/framework/tools/tasreplays/external/COPYING': "a private product takes its licence files from the whole framework tree; no setup package carries tools/tasreplays (the pin's stage_framework_tree.sh leaves that developer tree out), so no setup can write it. Kept in the private product: nothing of that folder is in either product, and the file does no harm there"}
PRIVATE_ONLY = tuple(PRIVATE_ONLY_REASONS)
IGNORED_FOLDERS = ('cache',)
SAME_BYTES_FOLDER = 'licenses'

def _text_difference(name, ours, theirs):

    def digest(data):
        return hashlib.sha256(data).hexdigest()[:12]
    same_text = ours.replace(b'\r\n', b'\n') == theirs.replace(b'\r\n', b'\n')
    return f'{name}: not the same bytes in the two folders (private {len(ours)} bytes, sha256 {digest(ours)}; installed {len(theirs)} bytes, sha256 {digest(theirs)}; ' + ('the same text with other line endings' if same_text else 'the text differs') + ')'

def setup_commands(root, plan, exe_name, discs, bios, stem):
    root = Path(root)
    cli = root / 'psxrecomp/psxrecomp_cli.py'
    config = root / SET_FILE
    located = [part for number, path in enumerate(discs, start=1) for part in ('--set-disc', f'{number}={path}')]
    first = plan['programs'][0]['exe_name']
    return [('set-generate', [sys.executable, cli, 'generate', '--project-root', root, '--config', config, *located, '--bios', bios, '--bios-stem', stem, '--gen-marker', GEN_MARKER, '--no-toolchain-download', '--json-progress']), ('set-rebuild', [sys.executable, cli, 'rebuild', '--project-root', root, '--config', config, '--build-dir', root / 'build-release', '--target', 'psx-runtime', '--exe-basename', first, '--no-pgo', '--prune-after', 'build-intermediates', '--no-toolchain-download', '--cmake-extra=-DPSX_DEPS_OFFLINE=ON', '--json-progress']), ('set-setup-after', [root / (exe_name + '.exe'), '--setup-selfcheck'])]

def install(package, root, plan, exe_name, discs, bios, stem, env, work, record, overlay_archives=None, run=None):
    try:
        from . import workbench_package as single
    except ImportError:
        import workbench_package as single
    run = run or pb.run_command
    package, root, work = (Path(package), Path(root), Path(work))
    if pb.sha(package) != record['windows_package']['sha256']:
        raise pb.BuildStop("The set's Windows package changed before the setup pass.")
    root.mkdir()
    with zipfile.ZipFile(package) as archive:
        for info in archive.infolist():
            if not (root / info.filename.replace('\\', '/')).resolve().is_relative_to(root.resolve()):
                raise pb.BuildStop('The package contains a path outside its extraction directory.')
        archive.extractall(root)
    single.setup_check(root / (exe_name + '.exe'), root, env, work, record, 'set-setup-before', True)
    local = env.copy()
    local['CCACHE_DIR'] = str(root / '.cache/ccache')
    local['CCACHE_BASEDIR'] = str(root)
    for name, exe in (('PSXRECOMP_GAME', 'psxrecomp-game.exe'), ('PSXRECOMP_BIOS', 'psxrecomp-bios.exe')):
        local[name] = str(root / 'psxrecomp/recompiler/build' / exe)
    pb.stage_overlay_archives(root / 'psxrecomp', root, record, overlay_archives)
    staged_bios = pb.bios_stage_path(root / 'psxrecomp', stem)
    try:
        for name, argv in setup_commands(root, plan, exe_name, discs, bios, stem):
            checked = name == 'set-setup-after'
            step_env = pb.host_start_environment(local, work / (name + '-host')) if checked else local
            log = run(argv, root, step_env, work, record, name, keep_stdout=checked)
            if checked and single.setup_report(log)['sources_missing'] is not False:
                raise pb.BuildStop("The set's setup pass did not pass the setup self-check.")
    finally:
        pb.remove_owned_file(staged_bios, missing_ok=True)
    result = root / INSTALL_RESULT
    try:
        record['install_result'] = json.loads(result.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise pb.BuildStop(f'The setup pass left no readable {INSTALL_RESULT} at the package root: {error}')
    if pb.sha(package) != record['windows_package']['sha256']:
        raise pb.BuildStop("The set's Windows package changed during the setup pass.")
    installed = root / 'build-release'
    lacking = [program['exe_name'] + '.exe' for program in plan['programs'] if not (installed / (program['exe_name'] + '.exe')).is_file()]
    if lacking:
        raise pb.BuildStop('The setup pass did not leave every program in ' + str(installed) + ': ' + ', '.join(lacking) + ' missing.')
    return installed

def record_installed(root, installed, evidence):
    root, installed, evidence = (Path(root), Path(installed), Path(evidence))
    names = {'root': 'replay-root-listing.txt', 'installed': 'replay-installed-files.txt', 'result': INSTALL_RESULT}
    entries = []
    for entry in sorted(os.scandir(root), key=lambda e: e.name.lower()):
        kind = 'link' if entry.is_symlink() or entry.is_junction() else 'dir' if entry.is_dir() else 'file'
        entries.append(f'{kind}\t{entry.name}')
        if kind == 'dir' and entry.name == PROGRAMS_FOLDER:
            for program in sorted(os.scandir(entry.path), key=lambda e: e.name.lower()):
                for inner in sorted(os.scandir(program.path), key=lambda e: e.name.lower()):
                    inner_kind = 'link' if inner.is_symlink() or inner.is_junction() else 'dir' if inner.is_dir() else 'file'
                    entries.append(f'{inner_kind}\t{entry.name}/{program.name}/{inner.name}')
    (evidence / names['root']).write_text('\n'.join(entries) + '\n', encoding='utf-8')
    files = sorted((path.relative_to(installed).as_posix() for path in installed.rglob('*') if path.is_file()))
    (evidence / names['installed']).write_text('\n'.join(files) + '\n', encoding='utf-8')
    if (root / INSTALL_RESULT).is_file():
        shutil.copy2(root / INSTALL_RESULT, evidence / names['result'])
    return names

def _files(folder):
    folder = Path(folder)
    return {path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file() and path.relative_to(folder).parts[0] not in IGNORED_FOLDERS}

def _game_facts(config):
    sections = tomllib.loads(Path(config).read_text(encoding='utf-8-sig'))
    game = sections.get('game') or {}
    return {'discs': len(game.get('discs') or []), 'disc_serials': game.get('disc_serials'), 'program_discs': game.get('program_discs'), 'memcard_dir': (sections.get('runtime') or {}).get('memcard_dir')}

def compare_installed(private, installed, plan, installed_only=INSTALLED_ONLY, private_only=PRIVATE_ONLY):
    private, installed = (Path(private), Path(installed))
    left, right = (_files(private), _files(installed))
    differences = []
    for name in sorted(left - right):
        if name not in private_only:
            differences.append(f'{name}: in the private set product, not in the installed folder')
    for name in sorted(right - left):
        if name not in installed_only:
            differences.append(f'{name}: in the installed folder, not in the private set product')
    licences = sorted((name for name in left & right if name.split('/', 1)[0] == SAME_BYTES_FOLDER))
    for name in licences:
        ours, theirs = ((private / name).read_bytes(), (installed / name).read_bytes())
        if ours != theirs:
            differences.append(_text_difference(name, ours, theirs))
    for program in plan['programs']:
        config = program['exe_name'] + '.game.toml'
        if not (private / config).is_file() or not (installed / config).is_file():
            continue
        ours, theirs = (_game_facts(private / config), _game_facts(installed / config))
        for key in ours:
            if ours[key] != theirs[key]:
                differences.append(f'{config}: {key} is {theirs[key]!r} in the installed folder and {ours[key]!r} in the private set product')
    settings = installed / 'settings.toml'
    if settings.is_file() and 'disc' in tomllib.loads(settings.read_text(encoding='utf-8-sig')):
        differences.append('settings.toml: the installed folder remembers a disc before any program has started')
    return {'differences': differences, 'private_files': len(left), 'installed_files': len(right), 'shared_files': len(left & right), 'licence_files_compared': len(licences), 'installed_only': sorted((name for name in right - left if name in installed_only)), 'private_only': sorted((name for name in left - right if name in private_only))}

def require_equal(comparison):
    if comparison['differences']:
        shown = comparison['differences'][:12]
        more = len(comparison['differences']) - len(shown)
        raise pb.BuildStop("The folder the set's setup package installs is not the private set product: " + '; '.join(shown) + (f'; and {more} more' if more else '') + '. The package was not delivered. The full list is in the receipt.')
