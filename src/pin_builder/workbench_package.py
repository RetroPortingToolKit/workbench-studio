import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
try:
    from . import pin_build as pb
    from . import lean_boot_gate
    from . import workbench_output as wo
    from . import zip_step
except ImportError:
    import pin_build as pb
    import lean_boot_gate
    import workbench_output as wo
    import zip_step
BASH = Path('C:/msys64/usr/bin/bash.exe')
ZIP_BIN = Path(__file__).resolve().parent / 'zip_step_bin'
NEEDS_BASH = 'Offline packaging needs MSYS2 bash at C:/msys64/usr/bin/bash.exe.'
PACKAGE_NOTICES_FOLDER = 'licenses/toolchain'
PACKAGE_NOTICES = tuple((f'{PACKAGE_NOTICES_FOLDER}/{name}' for name in wo.TOOLCHAIN_NOTICES))

def write_package_notices(toolchain, source, receipt):
    folder = Path(source) / PACKAGE_NOTICES_FOLDER
    written = wo.stage_toolchain_notices(toolchain, folder)
    missing = [name for name in wo.TOOLCHAIN_NOTICES if name not in written]
    if missing:
        raise pb.BuildStop(f'The toolchain pack at {toolchain} has no licence text for ' + ', '.join(missing) + " (expected deps/share/licenses/SDL3/LICENSE.txt, the opening comment of deps/include/zlib.h, and LICENSE.TXT). A setup package ships programs that link SDL3, zlib and the compiler's runtime, so it must carry their licence texts. Restore the pinned toolchain pack.")
    texts, files = ({}, [])
    for member, name in zip(PACKAGE_NOTICES, wo.TOOLCHAIN_NOTICES):
        texts[member] = (folder / name).read_bytes()
        files.append({**pb.identity(folder / name), 'path': member})
    receipt['package_notices'] = {'files': files, 'source': 'the pinned toolchain pack'}
    return texts

def package_notice_problems(archive, texts):
    names = set(archive.namelist())
    problems = []
    for member, data in texts.items():
        if member not in names:
            problems.append(f'{member}: missing')
        elif archive.read(member) != data:
            problems.append(f'{member}: not the bytes Studio wrote')
    return problems

def check_package_notices(package, texts, receipt):
    with zipfile.ZipFile(package) as archive:
        problems = package_notice_problems(archive, texts)
    if problems:
        raise pb.BuildStop('The setup package does not carry the licence texts of the libraries its programs link: ' + '; '.join(problems) + ". Studio wrote them into the package source. The pin's packager must carry a root licenses/ folder into the zip (tools/package_setup_host.sh, 'staged licenses/'); an older pin's packager does not. Choose a pin whose packager does. Nothing was delivered.")
    receipt['package_notices']['in_package'] = 'unchanged'

def installed_notices(package_root, product):
    package_root, product = (Path(package_root), Path(product))
    problems = []
    for member in PACKAGE_NOTICES:
        carried, installed = (package_root / member, product / member)
        if not carried.is_file():
            problems.append(f'{member}: not in the unpacked package')
        elif not installed.is_file():
            problems.append(f'{member}: in the package, not beside the game')
        elif installed.read_bytes() != carried.read_bytes():
            problems.append(f'{member}: beside the game with other bytes than the package carries')
    folder = product / 'licenses'
    files = []
    if folder.is_dir():
        files = sorted((path.relative_to(product).as_posix() for path in folder.rglob('*') if path.is_file()))
    return {'files': files, 'problems': problems}

def require_installed_notices(record):
    if record['problems']:
        raise pb.BuildStop('The folder the setup package installs lacks licence texts of the libraries the game links: ' + '; '.join(record['problems']) + ". The pin's setup must copy the package's licenses/toolchain/ beside the game it builds (tools/release_stage.py, stage_product_notices). The test did not pass.")
SETUP_START_ENV = {'PSX_HEADLESS': '1', 'PSX_HOST_MUTE': '1', 'SDL_VIDEODRIVER': 'dummy', 'SDL_AUDIODRIVER': 'dummy'}
SETUP_START_SECONDS = 30.0
CONFIG_LOADED = 'psxrecomp: loaded game config'
CONFIG_REFUSED = 'psxrecomp: failed to load --game'

def start_setup_program(executable, cwd, env, log, seconds=SETUP_START_SECONDS, arguments=()):
    with open(log, 'wb') as stream:
        child = subprocess.Popen([str(executable), *map(str, arguments)], cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            code = child.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            code = None
    return (code, Path(log).read_text(encoding='utf-8', errors='replace'))

def setup_start_check(package, exe_name, env, output, receipt, name='setup-package-start'):
    with pb.progress.measured(receipt, output, name, 'Start the setup program plainly'):
        log = Path(output) / (name + '.log')
        with tempfile.TemporaryDirectory(prefix='setup-start-', dir=output, ignore_cleanup_errors=True) as temp:
            unpacked = Path(temp)
            with zipfile.ZipFile(package) as archive:
                for info in archive.infolist():
                    if not (unpacked / info.filename.replace('\\', '/')).resolve().is_relative_to(unpacked.resolve()):
                        raise pb.BuildStop('The package contains a path outside its extraction directory.')
                archive.extractall(unpacked)
            executable = unpacked / (exe_name + '.exe')
            start_env = pb.host_start_environment({key: value for key, value in env.items() if not key.startswith('PSX_')}, Path(output) / (name + '-host')) | SETUP_START_ENV
            command = {'stage': name, 'argv': [str(executable)], 'cwd': str(unpacked), 'log': str(log)}
            receipt.setdefault('commands', []).append(command)
            code, text = start_setup_program(executable, unpacked, start_env, log)
            command['exit_code'] = code
            text = text.replace(str(unpacked), '<package>')
        lines = [line.strip() for line in text.splitlines()]
        refused = next((line for line in lines if CONFIG_REFUSED in line), None)
        loaded = next((line for line in lines if CONFIG_LOADED in line), None)
        receipt['setup_start'] = {'status': 'passed' if loaded and (not refused) else 'failed', 'exit_code': code, 'loaded': loaded, 'refused': refused, 'environment': dict(SETUP_START_ENV), 'how': "the unpacked package's setup program, no argument, headless; judged by the runtime's own configuration lines"}
        if refused or not loaded:
            raise pb.BuildStop('The setup program of this package cannot load its configuration and ends before any window: ' + (refused or f"its output has no '{CONFIG_LOADED}' line (exit code {code})") + f'. A player who starts it sees nothing (build). The package was not delivered. Read {log.name}.')
        return receipt['setup_start']
HANDOVER_LINE = 'setup host forwarding to product build'
KIT_START_SECONDS = 60
KIT_START_WAIT = 20.0
KIT_START_ENV = {'PSX_HEADLESS': '1', 'PSX_HOST_MUTE': '1', 'SDL_VIDEODRIVER': 'dummy', 'SDL_AUDIODRIVER': 'dummy'}
KIT_START_STAGE = 'stage 1: the hand-over line, exit code 0, and the build-release process with the BIOS and the disc on its command line; NO FRAMES CHECKED'
KIT_START_STAGE_2 = 'stage 2: the hand-over line, exit code 0, the build-release process with the BIOS and the disc on its command line, and frames from that start, ended by its own timer'
KIT_FRAMES_MILLISECONDS = 20000
KIT_FRAMES_WAIT = 90.0
HANDOVER_HEADLESS_MARK = 'handover_adds_launcher'
RUN_REPORT = 'psx_last_run_report.json'
KIT_START_SAID = re.compile('forwarding|Setup host|BIOS Mismatch|no BIOS selected|no disc image|build-release', re.I)

def handover_honours_headless(kit):
    try:
        text = (Path(kit) / 'psxrecomp' / 'host' / 'psxrecomp_codegen_host.c').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False
    return HANDOVER_HEADLESS_MARK in text

def fresh_run_report(folders, since):
    reports = [Path(folder) / RUN_REPORT for folder in folders]
    fresh = [path for path in reports if path.is_file() and path.stat().st_mtime >= since - 2]
    return max(fresh, key=lambda path: path.stat().st_mtime) if fresh else None

def product_processes(executable):
    name = Path(executable).name.replace("'", "''")
    script = 'Get-CimInstance Win32_Process -Filter "Name=\'' + name + '\'" | ForEach-Object { $w = (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue).MainWindowHandle; [pscustomobject]@{ pid = [int]$_.ProcessId; path = [string]$_.ExecutablePath; command_line = [string]$_.CommandLine; window = [int64]$w } } | ConvertTo-Json -Compress'
    done = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = done.stdout.strip()
    if not text:
        return []
    rows = json.loads(text)
    rows = rows if isinstance(rows, list) else [rows]
    wanted = os.path.normcase(os.path.realpath(executable))
    return [{'pid': row['pid'], 'command_line': row['command_line'], 'window': row['window']} for row in rows if row.get('path') and os.path.normcase(os.path.realpath(row['path'])) == wanted]

def stop_process(pid):
    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

def kit_exe_start(kit, exe_name, disc, bios, env, output, receipt, name='b4-kit-exe-start', wait=KIT_START_WAIT, poll=1.0, stage=None, frames_wait=KIT_FRAMES_WAIT):
    kit, output = (Path(kit), Path(output))
    executable, product = (kit / (exe_name + '.exe'), kit / 'build-release' / (exe_name + '.exe'))
    log = output / (name + '.log')
    stage = stage or (2 if handover_honours_headless(kit) else 1)
    forced = dict(KIT_START_ENV) | ({'PSX_EXIT_AFTER_MS': str(KIT_FRAMES_MILLISECONDS)} if stage == 2 else {})
    with pb.progress.measured(receipt, output, name, "Start the kit's own program as a player does"):
        for stale in product_processes(product):
            raise pb.BuildStop(f"A game of this test folder is already running (process {stale['pid']}), so the kit's own start cannot be judged.")
        argv = [str(executable), '--bios', str(bios), '--disc', str(disc)]
        command = {'stage': name, 'argv': argv, 'cwd': str(kit), 'log': str(log)}
        receipt.setdefault('commands', []).append(command)
        own = output / (name + '-host')
        start_env = pb.host_start_environment({key: value for key, value in env.items() if not key.startswith('PSX')}, own) | forced
        for folder in (kit, product.parent):
            (folder / RUN_REPORT).unlink(missing_ok=True)
        began = time.time()
        code, text = start_setup_program(executable, kit, start_env, log, KIT_START_SECONDS, arguments=argv[1:])
        command['exit_code'] = code
        lines = [line.strip() for line in text.splitlines()]
        handed = any((HANDOVER_LINE in line for line in lines))
        said = [line[:220] for line in lines if KIT_START_SAID.search(line)][:6]
        found = []
        if handed and code == 0:
            deadline = time.monotonic() + wait
            while not found and time.monotonic() < deadline:
                found = product_processes(product)
                if not found:
                    time.sleep(poll)
        problems = []
        frames = origin = report = None
        if stage == 2 and found:
            running, deadline = (found, time.monotonic() + frames_wait)
            while running and time.monotonic() < deadline:
                time.sleep(poll)
                running = product_processes(product)
            for process in running:
                stop_process(process['pid'])
            if running:
                problems.append(f'the game it started was still running {frames_wait:g} s after the hand-over, past its {KIT_FRAMES_MILLISECONDS / 1000:g} s timer, and was stopped')
            else:
                fresh = fresh_run_report((kit, product.parent), began)
                if fresh is None:
                    problems.append(f'the game it started left no fresh {RUN_REPORT} in the kit or in build-release')
                else:
                    saved = output / (name + '-last-run.json')
                    shutil.copy2(fresh, saved)
                    report = pb.identity(saved)
                    data = json.loads(saved.read_text(encoding='utf-8-sig'))
                    origin, frames = (data.get('exit_origin'), lean_boot_gate.frame_count(data))
                    if origin != 'exit_after_ms':
                        problems.append(f'the game it started ended by {origin!r}, not by its timer')
                    if type(frames) is not int or frames <= 0:
                        problems.append('the game it started ran no frame')
        else:
            for process in found:
                stop_process(process['pid'])
        if code is None:
            problems.append(f'the program was still running after {KIT_START_SECONDS} s and was stopped')
        elif code != 0:
            problems.append(f'the program ended with exit code {code}; a hand-over ends with 0')
        if not handed:
            problems.append(f'it did not print "{HANDOVER_LINE}"')
        if handed and code == 0 and (not found):
            problems.append(f'no process of build-release\\{product.name} was running within {wait:g} s of the hand-over')
        wrote = pb.host_start_wrote(own)
        if wrote['toolchain']:
            problems.append('it tried to install a toolchain during a plain start (' + ', '.join(wrote['toolchain'][:3]) + ')')
        if found:
            line = found[0]['command_line'].replace('/', '\\').lower()
            if not all((str(part).replace('/', '\\').lower() in line for part in ('--bios', bios, '--disc', disc))):
                problems.append('the game it started does not have the BIOS and the disc on its command line')
            if any((process['window'] for process in found)):
                problems.append("the game it started opened a window although SDL's dummy video driver was asked for")
        passed = KIT_START_STAGE_2 + f' ({frames} frames)' if stage == 2 else KIT_START_STAGE
        receipt['kit_exe_start'] = {'status': 'failed' if problems else 'passed ' + passed, 'stage': stage, 'frames_checked': stage == 2, 'exit_code': code, 'handover_line': handed, 'said': said, 'process': found[0] if found else None, 'environment': forced, 'own_folders': str(own), 'wrote_in_own_folders': wrote, 'how': "the installed kit's own program with --bios and --disc, no window; the game it starts is found by its image path and " + ('left to end by its own timer, and its run report is read' if stage == 2 else 'stopped')}
        if stage == 2:
            receipt['kit_exe_start'].update(frames=frames, exit_origin=origin, run_report=report, timer_milliseconds=KIT_FRAMES_MILLISECONDS)
        if problems:
            raise pb.BuildStop("The kit's own program does not start the game after the build: " + '; '.join(problems) + '. ' + ('It said: ' + ' | '.join(said) + '. ' if said else '') + 'A player who starts it gets setup again (build). The package was not delivered. ' + f'Read {log.name}.')
        print(f'[{name}] passed {passed}', flush=True)
        return receipt['kit_exe_start']

def packager_environment(env, toolchain, receipt, own):
    script, launcher = (Path(zip_step.__file__).resolve(), ZIP_BIN / 'zip')
    if not BASH.is_file():
        raise pb.BuildStop(NEEDS_BASH)
    if not launcher.is_file():
        raise pb.BuildStop(f"Workbench's zip launcher is missing: {launcher}. Restore the Studio checkout.")
    if env.get('PSXRECOMP_EMBED_TOOLCHAIN') == '1':
        raise pb.BuildStop('PSXRECOMP_EMBED_TOOLCHAIN=1 is set. A Workbench setup package never embeds a toolchain, and its packager runs with an empty toolchain root of its own. Unset the variable.')
    pb.record_inputs([BASH, script, launcher], receipt)
    receipt['package_zip'] = {**zip_step.describe(), 'script': pb.identity(script), 'launcher': pb.identity(launcher)}
    package_env = pb.host_start_environment(env, own)
    receipt['packager_host'] = {'own_folders': str(own)}
    package_env['PATH'] = str(ZIP_BIN) + os.pathsep + str(BASH.parent) + os.pathsep + env['PATH']
    for name in list(package_env):
        if name.startswith('WINDOWS_SIGN_'):
            package_env.pop(name)
    package_env['PSXRECOMP_RUNTIME_BIN_DIR'] = str(Path(toolchain) / 'bin')
    package_env['WORKBENCH_ZIP_PYTHON'] = sys.executable
    package_env['WORKBENCH_ZIP_SCRIPT'] = str(script)
    return package_env

def packager_wrote(receipt):
    wrote = pb.host_start_wrote(receipt['packager_host']['own_folders'])
    receipt['packager_host']['wrote_in_own_folders'] = wrote
    if wrote['toolchain']:
        raise pb.BuildStop("The packager's start of the setup program tried to install a toolchain (" + ', '.join(wrote['toolchain'][:3]) + '). A plain start must not. The package was not delivered. The files are in ' + receipt['packager_host']['own_folders'] + '.')
    return wrote

def preflight(repo, pin, receipt):
    if not BASH.is_file():
        raise pb.BuildStop(NEEDS_BASH)
    tools = [BASH, ZIP_BIN / 'zip']
    for path in tools:
        if not path.is_file():
            raise pb.BuildStop(f'Package prerequisite missing: {path}. Restore it before starting the build.')
    script = pb.git(repo, 'show', f"{pin['commit']}:tools/package_setup_host.sh")
    for option in ('--no-mods)', '--runtime-dir)', '--project-dir)', '--omit-openbios)'):
        if option not in script:
            raise pb.BuildStop(f'The selected pin packager lacks {option[:-1]}. Choose a compatible pin before building.')
    required = ('tools/stage_setup_sdk.sh', 'tools/stage_framework_tree.sh', 'tools/check_private_paths.sh', 'tools/mod_channel_filter.py')
    for path in required:
        pb.git(repo, 'cat-file', '-e', f"{pin['commit']}:{path}")
    pb.record_inputs(tools, receipt)
    receipt['package_preflight'] = {'status': 'passed', 'pin_commit': pin['commit'], 'required_pin_files': list(required), 'tools': [pb.identity(p) for p in tools]}

def catalog_options(source):
    return ['--runtime-dir', 'mods'] + ([] if (source / 'mods').is_dir() else ['--no-mods'])

def catalog_recipe(source):
    path = source / 'CMakeLists.txt'
    text = path.read_text(encoding='utf-8-sig')
    if (source / 'mods/preloaded').is_dir():
        return text
    pattern = '(?m)^set\\(PSX_PRELOADED_MODS "\\$\\{CMAKE_CURRENT_SOURCE_DIR\\}/mods/preloaded"\\)\\s*if\\(NOT IS_DIRECTORY "\\$\\{PSX_PRELOADED_MODS\\}"\\)\\s*set\\(PSX_PRELOADED_MODS "NONE"\\)\\s*endif\\(\\)'
    changed = re.sub(pattern, 'set(PSX_PRELOADED_MODS "NONE")', text)
    if '${CMAKE_CURRENT_SOURCE_DIR}/mods/preloaded' in changed:
        raise pb.BuildStop(f'Setup recipe references absent mods/preloaded without the supported NONE fallback: {path}. Restore the catalog or correct the source recipe before building.')
    return changed

def prepare_catalog(source, receipt):
    path = source / 'CMakeLists.txt'
    before = pb.identity(path)
    text = catalog_recipe(source)
    if text != path.read_text(encoding='utf-8-sig'):
        path.write_bytes(text.encode('utf-8'))
        receipt['setup_catalog_recipe'] = {'reason': 'Absent optional catalog: canonical IS_DIRECTORY fallback resolves to NONE', 'before': before, 'after': pb.identity(path)}

def project_options(source):
    options = []
    for path in sorted(source.iterdir()):
        if path.is_file() and (path.name in ('CMakeLists.txt', 'VERSION', 'game.toml', 'game_options.toml', 'symbols.toml', 'psx_symbols.h', 'psx_symbols_overlays.h', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'project-manifest.toml') or path.name.startswith('codegen_setup.')):
            options.extend(('--project-file', path.name))
    for folder in ('seeds', 'src', 'assets', 'launcher_assets', 'mods', 'cmake', 'tools', 'scripts'):
        if (source / folder).is_dir():
            options.extend(('--project-dir', folder))
    return options

def private_paths(fw, stage, env, output, receipt, name='package-private-paths'):
    gate = Path(fw) / 'tools/check_private_paths.sh'
    if not gate.is_file():
        raise pb.BuildStop("The pin lacks tools/check_private_paths.sh, the packager's private-path gate.")
    pb.run_command([BASH, gate, stage], output, env, output, receipt, name)

def source_admission(output, env, receipt):
    packager = output / 'psxrecomp/tools/package_setup_host.sh'
    text = packager.read_text(encoding='utf-8')
    start, end = ('# --- Gate: the staged tree', '# --- Windows: Authenticode-sign')
    if start not in text or end not in text:
        raise pb.BuildStop('The pin lacks the supported static setup-package admission gate.')
    gate = text[text.index(start):text.index(end)]
    with tempfile.TemporaryDirectory(prefix='package-source-check-', dir=output) as temp:
        stage = Path(temp)
        pb.copy_kit(output, stage, {'inputs': []})
        prepare_catalog(stage, receipt)
        check_env = env | {'PATH': str(BASH.parent) + os.pathsep + env['PATH']}
        pb.run_command([BASH, output / 'psxrecomp/tools/stage_framework_tree.sh', '--framework', output / 'psxrecomp', '--dest', stage / 'psxrecomp'], output, check_env, output, receipt, 'package-source-stage')
        shutil.copytree(output / 'recomp-ui', stage / 'recomp-ui', ignore=shutil.ignore_patterns('.git', '__pycache__', 'build'))
        options = project_options(stage)
        files = [options[i + 1] for i in range(0, len(options), 2) if options[i] == '--project-file']
        dirs = [options[i + 1] for i in range(0, len(options), 2) if options[i] == '--project-dir']
        probe = output / 'package-source-gate.sh'
        probe.write_bytes(('set -euo pipefail\nSTAGE=' + shlex.quote(stage.as_posix()) + '\nPROJECT_FILES=(' + ' '.join(map(shlex.quote, files)) + ')\nPROJECT_DIRS=(' + ' '.join(map(shlex.quote, dirs)) + ')\n' + gate).encode())
        pb.run_command([BASH, probe], output, check_env, output, receipt, 'package-source-admission')
        receipt['package_source_admission'] = {'status': 'passed', 'packager': pb.identity(packager), 'gate': pb.identity(probe), 'recipe': pb.identity(stage / 'CMakeLists.txt')}
        for entry in stage.iterdir():
            if entry.name != 'psxrecomp':
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        private_paths(output / 'psxrecomp', stage, check_env, output, receipt)
        receipt['package_source_admission']['private_paths'] = 'passed'

def setup_report(path):
    return pb.selfcheck_report(path)

def setup_check(executable, cwd, env, output, receipt, name, missing):
    with pb.progress.measured(receipt, output, name + ':contract', 'Validate setup self-check') as stage:
        log = output / (name + '.log')
        stage['log'] = str(log.resolve())
        try:
            pb.run_command([executable, '--setup-selfcheck'], cwd, pb.host_start_environment(env, Path(output) / (name + '-host')), output, receipt, name, success_codes=(0, 2) if missing else (0,), keep_stdout=True)
        except pb.BuildStop:
            if not missing or receipt['commands'][-1].get('exit_code') != 2 or setup_report(log)['sources_missing'] is not True:
                raise
        report = setup_report(log)
        if report['sources_missing'] is not missing:
            raise pb.BuildStop('The setup self-check reported the wrong generated-source state.')
        return report

def export_kit(kit, destination, serials, receipt):
    pb.copy_kit(Path(kit), destination, receipt)
    prepare_catalog(destination, receipt)
    pb.set_game_media(destination / 'game.toml', [f'disc/disc-{n + 1}.cue' for n in range(len(serials))], serials)

def make_package(output, exe_name, env, receipt):
    with pb.progress.measured(receipt, output, 'package', 'Source-only setup package'):
        return _make_package(output, exe_name, env, receipt)

def _make_package(output, exe_name, env, receipt):
    source = output / 'setup-source'
    source.mkdir()
    serials = [i['serial'] for i in receipt['disc_identities']]
    export_kit(Path(receipt['kit']), source, serials, receipt)
    records = []
    for name in ('psxrecomp', 'recomp-ui'):
        original = next((r for r in receipt['sources'] if Path(r['destination']) == output / name))
        pb.snapshot(Path(original['repo']), original['commit'], source / name, records)
    receipt['package_sources'] = records
    receipt['package_mode'] = 'no-inputs'
    fw = source / 'psxrecomp'
    pb.stage_overlay_archives(fw, source, receipt)
    toolchain = Path(env['PSXRECOMP_TOOLCHAIN_DIR'])
    notices = write_package_notices(toolchain, source, receipt)
    cmake = toolchain / 'bin/cmake.exe'
    setup = source / 'build-setup'
    pb.run_command([cmake, '-S', source, '-B', setup, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_COMPILER=clang', '-DCMAKE_CXX_COMPILER=clang++', '-DPSXRECOMP_FORCE_SETUP_HOST=ON', '-DPSXRECOMP_ALLOW_NO_BIOS=ON', '-DPSX_DEPS_OFFLINE=ON'], source, env, output, receipt, 'configure-setup-host')
    pb.run_command([cmake, '--build', setup, '--target', 'psx-runtime', '-j2'], source, env, output, receipt, 'build-setup-host')
    actual_name = (setup / 'psxrecomp_exe_name-psx-runtime.txt').read_text().strip()
    if not actual_name or Path(actual_name).name != actual_name or (exe_name and actual_name != exe_name):
        raise pb.BuildStop('The setup build did not publish the expected executable name.')
    exe_name = actual_name
    receipt['setup_exe_name'] = exe_name
    receipt['setup_package_selfcheck'] = setup_check(setup / (exe_name + '.exe'), setup, env, output, receipt, 'setup-package-selfcheck', True)
    bash = BASH
    package_env = packager_environment(env, toolchain, receipt, Path(output) / 'package-windows-host')
    command = [bash, fw / 'tools/package_setup_host.sh', '--root', source, '--build-dir', setup, '--artifact', 'windows-x64', '--zip-prefix', 'workbench', '--exe-name', exe_name, '--display-name', exe_name, '--recompiler-build', output / 'psxrecomp/recompiler/build', '--omit-openbios', *catalog_options(source)]
    command.extend(project_options(source))
    pb.run_command([str(p).replace('\\', '/') for p in command], source, package_env, output, receipt, 'package-windows')
    packager_wrote(receipt)
    packages = list((source / 'dist').glob('*.zip'))
    if len(packages) != 1:
        raise pb.BuildStop('The pin packager must produce exactly one local Windows package.')
    receipt['windows_package'] = pb.identity(packages[0])
    check_package_notices(packages[0], notices, receipt)
    setup_start_check(packages[0], exe_name, env, output, receipt)
    return packages[0]

def replay(output, package, exe_name, discs, bios, stem, serials, env, receipt):
    with pb.progress.measured(receipt, output, 'test-windows-x64', 'Windows package test'):
        return _replay_with_cleanup(output, package, exe_name, discs, bios, stem, serials, env, receipt)

def _replay_with_cleanup(output, package, exe_name, discs, bios, stem, serials, env, receipt):
    staged = []
    temporary = tempfile.TemporaryDirectory(prefix='b4-generation-', dir=output)
    with temporary:
        try:
            return _replay(output, package, exe_name, discs, bios, stem, serials, env, receipt, temporary, staged)
        finally:
            for path in staged:
                path.unlink(missing_ok=True)

def package_start(product, exe_name, disc, bios, env, output, receipt, number, milliseconds, grace=30.0, poll=1.0):
    name = f'b4-boot-{number}'
    for stale in ('psx_freeze_heartbeat.json', 'psx_crash.txt', 'psx_last_run_report.json'):
        (product / stale).unlink(missing_ok=True)
    launch_env = env | {'PSX_HEADLESS': '1', 'PSX_NO_LAUNCHER': '1', 'PSX_HOST_MUTE': '1', 'PSX_EXIT_AFTER_MS': str(milliseconds)}
    argv = [str(product / (exe_name + '.exe')), '--disc', str(disc), '--bios', str(bios)]
    row = {'number': number, 'argv': argv, 'stdout': str(output / f'{name}.out'), 'stderr': str(output / f'{name}.err')}
    receipt['commands'].append({'stage': name, 'argv': argv, 'cwd': str(product), 'log': row['stdout']})
    with pb.progress.measured(receipt, output, name, f'Package test start {number}'):
        with open(row['stdout'], 'wb') as stdout, open(row['stderr'], 'wb') as stderr:
            child = subprocess.Popen(argv, cwd=product, env=launch_env, stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            row['pid'] = child.pid
            memory = lean_boot_gate.start_guard(child)
            deadline = time.monotonic() + milliseconds / 1000 + grace
            verdict = None
            try:
                while verdict is None:
                    try:
                        row['exit_code'] = child.wait(timeout=poll)
                        verdict = 'exited'
                    except subprocess.TimeoutExpired:
                        if memory is not None and memory.exceeded():
                            verdict = 'memory limit'
                        elif (product / 'psx_crash.txt').exists():
                            verdict = 'crash'
                        elif time.monotonic() >= deadline:
                            verdict = 'timeout'
                if verdict != 'exited':
                    if memory is not None:
                        memory.end()
                    child.kill()
                    row['exit_code'] = child.wait()
            finally:
                lean_boot_gate.finish_guard(memory, row)
        row['verdict'] = verdict
        try:
            lean_boot_gate.memory_stop(row, f'B4 headless start {number}')
        except RuntimeError as stop:
            raise pb.BuildStop(str(stop)) from None
        if verdict != 'exited' or (product / 'psx_crash.txt').exists():
            raise pb.BuildStop(f'B4 headless start {number} did not exit cleanly within its time budget.')
    return row

def _replay(output, package, exe_name, discs, bios, stem, serials, env, receipt, temporary, staged):
    package_hash = pb.sha(package)
    if package_hash != receipt['windows_package']['sha256']:
        raise pb.BuildStop('The Windows package changed before B4 extraction.')
    kit = output / 'Windows replay kit'
    kit.mkdir()
    with zipfile.ZipFile(package) as archive:
        for info in archive.infolist():
            if not (kit / info.filename.replace('\\', '/')).resolve().is_relative_to(kit.resolve()):
                raise pb.BuildStop('The package contains a path outside its extraction directory.')
        archive.extractall(kit)
    setup_check(kit / (exe_name + '.exe'), kit, env, output, receipt, 'b4-setup-before', True)
    local_env = env.copy()
    local_env['CCACHE_DIR'] = str(kit / '.cache/ccache')
    local_env['CCACHE_BASEDIR'] = str(kit)
    for name, exe in (('PSXRECOMP_GAME', 'psxrecomp-game.exe'), ('PSXRECOMP_BIOS', 'psxrecomp-bios.exe')):
        local_env[name] = str(kit / 'psxrecomp/recompiler/build' / exe)
    pb.stage_overlay_archives(kit / 'psxrecomp', kit, receipt)
    config = kit / 'game.toml'
    cli = kit / 'psxrecomp/psxrecomp_cli.py'
    gen_disc = pb.generation_disc(discs[0], Path(temporary.name) / 'disc', receipt, pb.receipt_chdman(receipt))
    pb.stage_boot_executable(config, gen_disc, kit)
    staged.append(pb.bios_stage_path(kit / 'psxrecomp', stem))
    pb.run_command([sys.executable, cli, 'generate', '--project-root', kit, '--config', config, '--disc', gen_disc, '--bios', bios, '--bios-stem', stem, '--no-toolchain-download', '--json-progress'], kit, local_env, output, receipt, 'b4-generate')
    temporary.cleanup()
    pb.set_game_media(config, [p.as_posix() for p in discs], serials)
    product = kit / 'build-release'
    pb.run_command([sys.executable, cli, 'rebuild', '--project-root', kit, '--config', config, '--build-dir', product, '--target', 'psx-runtime', '--no-pgo', '--no-toolchain-download', '--cmake-extra=-DPSX_DEPS_OFFLINE=ON', '--json-progress'], kit, local_env, output, receipt, 'b4-rebuild')
    after = pb.run_command([kit / (exe_name + '.exe'), '--setup-selfcheck'], kit, pb.host_start_environment(local_env, output / 'b4-setup-after-host'), output, receipt, 'b4-setup-after', keep_stdout=True)
    if setup_report(after)['sources_missing'] is not False:
        raise pb.BuildStop('The exact-package rebuild did not pass setup self-check.')
    kit_stage = 2 if handover_honours_headless(kit) else 1
    if kit_stage == 1:
        kit_exe_start(kit, exe_name, discs[0], bios, local_env, output, receipt, stage=1)
    licences = installed_notices(kit, product)
    receipt['automated_tests']['windows-x64']['licences'] = licences
    reports = []
    receipt['game_launched'] = True
    starts = []
    for run, milliseconds in ((1, 180000), (2, 60000)):
        starts.append(package_start(product, exe_name, discs[0], bios, local_env, output, receipt, run, milliseconds))
        report = product / 'psx_last_run_report.json'
        saved = output / f'b4-last-run-{run}.json'
        shutil.copy2(report, saved)
        reports.append(pb.identity(saved))
    result = json.loads(Path(reports[-1]['path']).read_text(encoding='utf-8-sig'))
    frames = lean_boot_gate.frame_count(result)
    native, exercised = lean_boot_gate.overlay_evidence(result)
    first = json.loads(Path(reports[0]['path']).read_text(encoding='utf-8-sig'))
    captured = lean_boot_gate.overlay_captured(result, product) or lean_boot_gate.overlay_captured(first)
    stop = lean_boot_gate.judge_second_start(result, captured)
    if stop == lean_boot_gate.COMPILER_STOP:
        raise pb.BuildStop('B4 failed: the ' + stop[0].lower() + stop[1:])
    if stop is not None:
        raise pb.BuildStop('B4 failed: ' + stop)
    lean_boot_gate.record_start_findings(lean_boot_gate.start_findings(result), receipt)
    if kit_stage == 2:
        kit_exe_start(kit, exe_name, discs[0], bios, local_env, output, receipt, stage=2)
    if pb.sha(package) != package_hash:
        raise pb.BuildStop('The Windows package changed during B4.')
    require_installed_notices(licences)
    memory = {'warnings': []}
    lean_boot_gate.record_start_memory(starts, memory)
    if memory.get('start_memory'):
        receipt['automated_tests']['windows-x64']['start_memory'] = memory['start_memory']
        receipt.setdefault('warnings', []).extend(memory['warnings'])
    receipt['automated_tests']['windows-x64'].update(status='passed', package_sha256=package_hash, executable=pb.identity(product / (exe_name + '.exe')), reports=reports, frames=frames, disp_native=native, overlays_exercised=exercised, kernel_dispatch=lean_boot_gate.kernel_dispatch(result), above_kernel_dispatch=lean_boot_gate.above_kernel_dispatch(result), scope='B4 exact-package install and two boots; no TAS or whole-product qualification')
    receipt['disp_native'] = native
    receipt['native_dispatch_note'] = 'Measured on B4 second headless start only.'
