from copy import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import time
import tomllib
try:
    from . import pin_build as pb
    from . import lean_boot_gate
    from . import workbench_runs as runs
    from . import workbench_output as wo
    from . import workbench_package as single
    from . import workbench_set_package as set_package
except ImportError:
    import pin_build as pb
    import lean_boot_gate
    import workbench_runs as runs
    import workbench_output as wo
    import workbench_package as single
    import workbench_set_package as set_package
PER_PROGRAM = {'game.toml', 'BUILDINFO.json', 'SHA256SUMS.txt', 'disc.cfg', 'settings.toml'}
KNOWN_VARIANTS = {'overlay_toolchain/psxrecomp-game.exe', 'mods/README.md'}
LINE_UNIONS = {'saves/disc_digests.tsv'}
RUNTIME_MARKERS = (('runtime/include/disc_roster.h', 'disc_roster_program_subset'), ('runtime/src/main.cpp', '.game.toml'))

def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()

def require_runtime_support(repo, commit):
    for path, marker in RUNTIME_MARKERS:
        try:
            text = pb.git(repo, 'show', f'{commit}:{path}')
        except Exception as error:
            raise pb.BuildStop(f'Cannot read {path} at the pin commit to check program-set support: {error}')
        if marker not in text:
            raise pb.BuildStop("The selected discs are the programs of one set, but this pin's runtime cannot share one folder between programs: it has no [game] program_discs and no per-exe config (pin H13). Build each program on its own (one disc per build), or choose a pin with program-set support.")

def set_program_discs(config, set_discs, set_serials, positions):
    pb.set_game_media(config, set_discs, set_serials)
    text = config.read_text(encoding='utf-8-sig')
    text = re.sub('(?m)^\\s*program_discs\\s*=.*\\n', '', text)
    updated, count = re.subn('(?m)^(disc_serials\\s*=.*\\n)', lambda m: m.group(1) + 'program_discs = ' + json.dumps(positions) + '\n', text, count=1)
    parsed = tomllib.loads(updated)['game'] if count else {}
    if parsed.get('program_discs') != positions or parsed.get('discs') != set_discs:
        raise pb.BuildStop(f"Cannot write program_discs into {config.name}. Review the program's game.toml.")
    config.write_text(updated, encoding='utf-8')

def shared_settings(text):
    out, skipping = ([], False)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            skipping = stripped == '[disc]'
        if not skipping:
            out.append(line)
    return '\n'.join(out) + '\n'

def merge_products(programs, target, plan):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    record = {'known_variants': {}, 'line_unions': [], 'per_program': [], 'shared_files': 0}
    set_discs = [None] * len(plan['serials'])
    for program in programs:
        game = tomllib.loads((Path(program['product']) / 'game.toml').read_text(encoding='utf-8-sig'))['game']
        own_paths = game.get('discs') or ([game['disc']] if game.get('disc') else [])
        own_serials = game.get('disc_serials') or []
        for path, serial in zip(own_paths, own_serials):
            if serial in plan['serials']:
                set_discs[plan['serials'].index(serial)] = path
    if any((path is None for path in set_discs)):
        missing = [s for s, p in zip(plan['serials'], set_discs) if p is None]
        raise pb.BuildStop("The programs' products do not name a disc for every serial of the set: " + ', '.join(missing) + '.')
    settings_text = None
    for order, program in enumerate(programs):
        product = Path(program['product'])
        built_exe = program['executable']
        exe_name = program['exe_name'] + Path(built_exe).suffix
        for source in sorted((p for p in product.rglob('*') if p.is_file())):
            relative = source.relative_to(product).as_posix()
            if relative in PER_PROGRAM or relative == built_exe:
                continue
            destination = target / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                record['shared_files'] += 1
                continue
            if relative in LINE_UNIONS:
                lines = destination.read_text(encoding='utf-8').splitlines()
                extra = [line for line in source.read_text(encoding='utf-8').splitlines() if line not in lines]
                if extra:
                    destination.write_text('\n'.join(lines + extra) + '\n', encoding='utf-8', newline='\n')
                if relative not in record['line_unions']:
                    record['line_unions'].append(relative)
                continue
            if sha(source) == sha(destination):
                continue
            if relative in KNOWN_VARIANTS:
                entry = record['known_variants'].setdefault(relative, {'kept': programs[0]['program'], 'sha256': {programs[0]['program']: sha(destination)}})
                entry['sha256'][program['program']] = sha(source)
                continue
            raise pb.BuildStop(f"The programs of this set disagree about {relative} ({programs[0]['program']} and {program['program']}). One folder can hold one copy. Make the kits agree, or add the file to the known per-build list if the difference is harmless.")
        shutil.copy2(product / built_exe, target / exe_name)
        config = target / (program['exe_name'] + '.game.toml')
        shutil.copy2(product / 'game.toml', config)
        set_program_discs(config, set_discs, plan['serials'], program['positions'])
        if order == 0 and (product / 'settings.toml').is_file():
            settings_text = shared_settings((product / 'settings.toml').read_text(encoding='utf-8-sig'))
        record['per_program'].append({'program': program['program'], 'executable': exe_name, 'executable_sha256': sha(target / exe_name), 'config': config.name, 'program_discs': program['positions']})
    if settings_text is not None:
        (target / 'settings.toml').write_text(settings_text, encoding='utf-8', newline='\n')
    (target / 'saves').mkdir(exist_ok=True)
    record['set_discs'] = set_discs
    return record

def write_set_provenance(target, plan, programs, merge, boots):
    wo.assert_no_netplay_state(target)
    wo.assert_no_stray_sources(target)
    entries = []
    for program in programs:
        info = dict(program.get('buildinfo') or {})
        info.pop('payload', None)
        merged = next((row for row in merge['per_program'] if row['program'] == program['program']))
        entries.append({'program': program['program'], 'executable': merged['executable'], 'executable_sha256': merged['executable_sha256'], 'config': merged['config'], 'serials': program['serials'], 'program_discs': program['positions'], 'shortcut': program['shortcut'], 'kit': program['kit'], 'netplay': info.get('netplay'), 'framework_pin_stamped': info.get('framework_pin_stamped'), 'lobby_version': info.get('lobby_version'), 'build': info})
    first = entries[0]['build'] if entries else {}
    info = {'schema': 3, 'build_route': 'workbench_private_program_set', 'platform': 'windows-x64', 'set': {'name': plan['name'], 'title': plan['title'], 'serials': plan['serials']}, 'runtime_commit': first.get('runtime_commit'), 'runtime_tree': first.get('runtime_tree'), 'ui_commit': first.get('ui_commit'), 'toolchain': first.get('toolchain'), 'programs': entries, 'shortcuts': [{'name': e['shortcut'], 'executable': e['executable']} for e in entries], 'merge': {k: merge[k] for k in ('known_variants', 'line_unions', 'shared_files')}, 'set_boot_check': boots, 'full_game_correctness': 'NOT_CLAIMED', 'payload': pb.inventory(target)}
    pb.write_json(target / 'BUILDINFO.json', info)
    sums = pb.inventory(target)
    (target / 'SHA256SUMS.txt').write_text(''.join((f"{item['sha256']}  {item['path']}\n" for item in sums)), encoding='utf-8')
    return info

def set_boot_check(product, programs, plan, evidence, unrelated_cwd, env, on_run=None):
    order = list(programs) + ([programs[0]] if len(programs) > 1 else [])
    rows = []
    for number, program in enumerate(order, start=1):
        merged_exe = Path(product) / (program['exe_name'] + Path(program['executable']).suffix)
        expected = plan['serials'][program['positions'][0] - 1]
        label = program['program'] + ('-again' if number > len(programs) else '')
        lean_boot_gate.start_other_discs(Path(product), merged_exe, Path(evidence), Path(unrelated_cwd), env, [{'disc': label, 'serial': expected, 'select': None}], on_run=on_run, first_number=number, rows=rows, kind='program')
        rows[-1]['program'] = program['program']
    return rows

def payload_difference(before, folder):
    then = {item['path']: item['sha256'] for item in before}
    now = {item['path']: item['sha256'] for item in pb.inventory(Path(folder))}
    return (sorted(set(now) - set(then)), sorted(set(then) - set(now)), sorted((path for path in set(now) & set(then) if now[path] != then[path])))

def require_unchanged_payload(before, folder):
    added, missing, changed = payload_difference(before, folder)
    if not (added or missing or changed):
        return

    def some(paths):
        return ', '.join(paths[:6]) + (f' and {len(paths) - 6} more' if len(paths) > 6 else '')
    parts = [f'{label}: {some(paths)}' for label, paths in (("not part of the programs' payloads", added), ('missing', missing), ('changed', changed)) if paths]
    raise pb.BuildStop("The set's folder is not the merged payload any more (" + '; '.join(parts) + '). Files a start leaves behind must never be delivered; the set check runs in a copy.')

def leftovers(before, folder):
    folder = Path(folder)
    added, missing, changed = payload_difference(before, folder)
    return {'files': len(added), 'bytes': sum(((folder / path).stat().st_size for path in added)), 'examples': added[:8], 'changed': changed, 'removed': missing}

def remove_folder(folder, attempts=10, delay=1.0, sleep=time.sleep):
    error = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(folder)
            return None
        except FileNotFoundError:
            return None
        except OSError as failure:
            error = failure
            if attempt + 1 < attempts:
                sleep(delay)
    return str(error)

def mark_plan_run(plan_receipt, cache, receipt):
    path = (plan_receipt or {}).get('receipt')
    if not path or not Path(path).is_file():
        return
    try:
        path = Path(path)
        run = path.parent.parent
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('status') != 'human-stop' or not saved.get('program_set'):
            return
        work = run / 'work'
        if work.is_dir() and run.resolve().parent == Path(cache).resolve():
            error = remove_folder(work)
            if error:
                receipt['warnings'].append(f"The planning run's work folder could not be removed: {work} ({error}).")
        saved.update(status=runs.PLAN_STATUS, work_retained=work.exists(), plan_note="This run found that the selected discs are the programs of one set and handed over to the set build. It is not a failed build; the set build's own receipt says how that build ended.")
        pb.write_json(path, saved)
        receipt['plan_run']['status'] = runs.PLAN_STATUS
    except (OSError, ValueError) as error:
        receipt['warnings'].append(f'The planning run could not be relabelled: {error}')

def set_steps(cache, pin, kind):
    return {'commands': [], 'inputs': [], 'sources': [], 'warnings': [], 'pin': pin, 'output_kind': kind, 'stage_history': str(Path(cache) / 'workbench-stage-history.json')}

def kit_configs(plan):
    return {program['program']: (Path(program['kit']) / 'game.toml').read_text(encoding='utf-8-sig') for program in plan['programs']}
PROGRAM_FINDINGS = ('game_code_interpreted', 'overlay_code_interpreted', 'cdrom_reads', 'bundled_notice_gaps', 'start_memory')

def overlay_rank(record):
    if not record:
        return (-1, -1)
    warned = {'fail': 2, 'refused': 1}.get(record.get('verdict'), 0)
    rate = record.get('per_frame')
    return (warned, rate if type(rate) in (int, float) else -1)

def carry_program_findings(receipt, name, one):
    interpreted = one.get('game_code_interpreted')
    if interpreted and (interpreted.get('per_frame') or 0) >= (receipt.get('game_code_interpreted') or {}).get('per_frame', -1):
        receipt['game_code_interpreted'] = {**interpreted, 'program': name}
    overlay = one.get('overlay_code_interpreted')
    if overlay and overlay_rank(overlay) >= overlay_rank(receipt.get('overlay_code_interpreted')):
        receipt['overlay_code_interpreted'] = {**overlay, 'program': name}
    reads = one.get('cdrom_reads')
    if reads and reads.get('silent_run_max', 0) >= (receipt.get('cdrom_reads') or {}).get('silent_run_max', -1):
        receipt['cdrom_reads'] = {**reads, 'program': name}
    gaps = one.get('bundled_notice_gaps')
    if gaps:
        receipt['bundled_notice_gaps'] = [*(receipt.get('bundled_notice_gaps') or []), *(f'{name}: {gap}' for gap in gaps)]
    memory = one.get('start_memory')
    if memory and memory.get('peak_process_mb') is not None and (memory['peak_process_mb'] >= (receipt.get('start_memory') or {}).get('peak_process_mb', -1)):
        receipt['start_memory'] = {**memory, 'heaviest_start': f"{name} build, start {memory.get('heaviest_start')}"}

def keep_heavier(receipt, earlier):
    now = receipt.get('start_memory') or {}
    if earlier and earlier.get('peak_process_mb') is not None and (earlier['peak_process_mb'] > now.get('peak_process_mb', -1)):
        receipt['start_memory'] = {**earlier, 'starts': (earlier.get('starts') or 0) + (now.get('starts') or 0)}

def start_summary(boots):
    return [{'program': row['program'], 'start': row['disc'], 'serial': row.get('mounted_serial'), 'frames': row.get('frames'), 'passed': bool(row.get('passed')), 'peak_process_mb': (row.get('memory') or {}).get('peak_process_mb')} for row in boots]

def check_installed(installed, plan, check, evidence, steps, bios=None):
    wo.assert_no_netplay_state(installed)
    wo.assert_no_stray_sources(installed)
    wo.record_bundled_notice_gaps(installed, steps)
    payload = pb.inventory(installed)
    shutil.copytree(installed, check)
    if bios is not None and (not (check / 'bios.cfg').exists()):
        (check / 'bios.cfg').write_text(str(bios) + '\n', encoding='utf-8')
        steps['bios_cfg_for_starts'] = 'written into the copy the programs start from, as the wizard records it'
    launch_cwd = evidence / 'replay-unrelated-cwd'
    launch_cwd.mkdir()
    boots_dir = evidence / 'replay-set-boots'
    boots_dir.mkdir()
    programs = [{**program, 'executable': program['exe_name'] + '.exe'} for program in plan['programs']]
    boots = set_boot_check(check, programs, plan, boots_dir, launch_cwd, os.environ.copy())
    require_unchanged_payload(payload, installed)
    lean_boot_gate.record_start_memory(boots, steps)
    return start_summary(boots)

def build(args, plan, build_one, plan_receipt=None):
    destination = args.output.resolve()
    cache = runs.run_root(getattr(args, 'run_root', None))
    cache.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='run-', dir=cache))
    runs.claim(run, destination)
    evidence = run / 'evidence'
    evidence.mkdir()
    work = run / 'work'
    kind = getattr(args, 'output_kind', 'private')
    receipt = {'status': 'started', 'output': str(destination), 'output_kind': kind, 'build_route': 'workbench_setup_program_set' if kind == 'setup' else 'workbench_private_program_set', 'program_set': plan, 'programs': [], 'warnings': [], 'logs': str(evidence), 'receipt': str(evidence / 'build-receipt.json'), 'host': socket.gethostname(), 'run_root': {'path': str(cache), 'free_gb_at_start': None}, 'plan_run': {'receipt': (plan_receipt or {}).get('receipt'), 'logs': (plan_receipt or {}).get('logs')}}
    pending = {}
    reservation = destination / '.workbench-reservation'
    reserved = made = False
    steps = None
    try:
        held = 'Another build holds this output directory. Select a new empty directory.'
        if reservation.exists():
            raise pb.BuildStop(held)
        if destination.exists() and any(destination.iterdir()):
            raise pb.BuildStop('The output directory is occupied. Select a new empty directory.')
        made = not destination.exists()
        destination.mkdir(parents=True, exist_ok=True)
        try:
            with reservation.open('x', encoding='utf-8'):
                pass
        except FileExistsError:
            raise pb.BuildStop(held) from None
        reserved = True
        mark_plan_run(plan_receipt, cache, receipt)
        receipt['run_root']['free_gb_at_start'] = runs.require_free_space(cache)
        if kind not in ('private', 'setup'):
            raise pb.BuildStop(f'A program set builds as a private product or as a setup package, not as {kind}.')
        if set(getattr(args, 'platform', None) or ['windows-x64']) != {'windows-x64'}:
            raise pb.BuildStop('A program set builds for Windows x64 only for now.')
        tests = set(getattr(args, 'test', None) or [])
        if tests - {'windows-x64'}:
            raise pb.BuildStop('Only the Windows package test is available for a program set.')
        if kind == 'setup' and getattr(args, 'optimize_pgo', False):
            raise pb.BuildStop('PGO is a private product option; it cannot be applied to an inputs-free setup package.')
        testing = bool(tests)
        packaging = kind == 'setup' or testing
        receipt['automated_tests'] = {'windows-x64': {'selected': testing, 'status': 'requested' if testing else 'not run (operator choice)'}}
        pin = pb.load_pin(args.pin)
        repo = args.source_repo.resolve()
        require_runtime_support(repo, pin['commit'])
        if packaging:
            set_package.require_setup_support(repo, pin)
        pinned_tools = set_package.has_setup_support(repo, pin['commit'])
        if any((not program.get('discs') for program in plan['programs'])):
            raise pb.BuildStop('The set plan does not name a disc for every program.')
        env = fw = None
        if packaging or pinned_tools:
            work.mkdir()
            steps = receipt['set_steps'] = set_steps(cache, pin, kind)
            toolchain = args.toolchain.resolve()
            jobs = pb.resolve_build_jobs(getattr(args, 'build_jobs', None))
            env = pb.build_environment(toolchain, work, jobs)
            fw = work / 'psxrecomp'
            pb.snapshot(repo, pin['commit'], fw, steps['sources'])
        if pinned_tools and set_package.has_kit_check(fw):
            set_package.kit_check(fw, plan, work, env, steps)
        if packaging:
            set_package.early_admission(work, fw, plan, env, steps)
        built, ones = ([], [])
        for program in plan['programs']:
            sub = copy(args)
            sub.disc = list(program['discs'])
            sub.kit = Path(program['kit'])
            sub.test = []
            sub.output = Path(tempfile.mkdtemp(prefix=f".{destination.name} program {program['program']} ", dir=destination.parent))
            sub.output.rmdir()
            pending[f"program-{program['program']}"] = sub.output
            print(f"note: building program {program['program']} of {plan['title']}", flush=True)
            one = build_one(sub)
            ones.append(one)
            receipt['programs'].append({'program': program['program'], 'status': one.get('status'), 'receipt': one.get('receipt'), 'logs': one.get('logs'), 'output': str(sub.output), 'kit': program['kit'], 'next_action': one.get('next_action'), **{name: one[name] for name in PROGRAM_FINDINGS if one.get(name) is not None}})
            receipt['warnings'] += [f"{program['program']}: {warning}" for warning in one.get('warnings') or []]
            carry_program_findings(receipt, program['program'], one)
            if one.get('status') == 'human-stop':
                raise pb.BuildStop(f"Program {program['program']} did not build: {one.get('next_action')}")
            if kind == 'private':
                product = sub.output / 'windows-x64'
                buildinfo = json.loads((product / 'BUILDINFO.json').read_text(encoding='utf-8'))
                built.append({**program, 'product': str(product), 'executable': Path(one['executable']['path']).name, 'buildinfo': buildinfo})
        if packaging or pinned_tools:
            sources = set_package.framework_sources(ones)
            exported = steps['sources'][0]['commit']
            if sources['psxrecomp']['commit'] != exported:
                raise pb.BuildStop(f"The programs were built from framework commit {sources['psxrecomp']['commit'][:9]}, not from the pin's {exported[:9]} that the set's own steps use. Build the set again.")
        merged = None
        if kind == 'private':
            staged = Path(tempfile.mkdtemp(prefix=f'.{destination.name} set ', dir=destination.parent))
            pending['set'] = staged
            merged = staged / 'windows-x64'
            if pinned_tools:
                set_file = work / set_package.SET_FILE
                set_package.write_set_toml(set_file, plan, set_package.set_exe_name(plan), set_package.set_bios_stem(kit_configs(plan)))
                receipt['join'] = "the pin's tools/program_set.py"
                try:
                    merge = set_package.join_with_pinned_tool(fw, set_file, built, merged, work, env, steps)
                finally:
                    if (work / 'program-set-join.json').is_file():
                        shutil.copy2(work / 'program-set-join.json', evidence / 'program-set-join.json')
                    differing = set_package.join_disagreements(work)
                    if differing:
                        receipt['join_disagreements'] = differing
            else:
                merge = merge_products(built, merged, plan)
                receipt['join'] = 'workbench_set.merge_products (the pin has no tools/program_set.py)'
            payload = pb.inventory(merged)
            check = Path(tempfile.mkdtemp(prefix=f'.{destination.name} set check ', dir=destination.parent))
            pending['set-check'] = check
            shutil.copytree(merged, check / 'windows-x64')
            launch_cwd = evidence / 'unrelated-cwd'
            launch_cwd.mkdir()
            boots_dir = evidence / 'set-boots'
            boots_dir.mkdir()
            boots = set_boot_check(check / 'windows-x64', built, plan, boots_dir, launch_cwd, os.environ.copy())
            summary = start_summary(boots)
            receipt['set_boot_check'] = summary
            from_programs = receipt.pop('start_memory', None)
            lean_boot_gate.record_start_memory(boots, receipt)
            keep_heavier(receipt, from_programs)
            receipt['set_check_leftovers'] = leftovers(payload, check / 'windows-x64')
            require_unchanged_payload(payload, merged)
            wo.record_bundled_notice_gaps(merged, receipt)
            info = write_set_provenance(merged, plan, built, merge, summary)
            receipt['merge'] = info['merge']
        package = None
        if packaging:
            package, _set_toml, host = set_package.make_package(work, fw, plan, sources, toolchain, jobs, env, steps, getattr(args, 'overlay_archives', None))
            receipt['windows_package'] = steps['windows_package']
            receipt['setup_exe_name'] = host
        if testing:
            test = receipt['automated_tests']['windows-x64']
            test['status'] = 'running'
            discs = [next((Path(program['discs'][program['positions'].index(position)]) for program in plan['programs'] if position in program['positions'])) for position in range(1, len(plan['serials']) + 1)]
            bios = Path(args.bios) if Path(args.bios).is_absolute() else Path(args.bios).absolute()
            stem = set_package.set_bios_stem(kit_configs(plan))
            installed = set_package.install(package, work / 'set-kit', plan, host, discs, bios, stem, env, work, steps, getattr(args, 'overlay_archives', None))
            test['evidence'] = set_package.record_installed(installed.parent, installed, evidence)
            if merged is not None:
                comparison = set_package.compare_installed(merged, installed, plan)
                test['comparison'] = comparison
            else:
                test['comparison'] = 'not made: a setup build has no private set product to compare with'
                test['licences'] = single.installed_notices(installed.parent, installed)
            test['starts'] = check_installed(installed, plan, work / 'set-kit-check', evidence, steps, bios=bios)
            if steps.get('start_memory'):
                test['start_memory'] = steps.pop('start_memory')
            if merged is not None:
                set_package.require_equal(comparison)
            else:
                single.require_installed_notices(test['licences'])
            test.update(status='passed', package_sha256=receipt['windows_package']['sha256'], scope="The set's package unpacked into a clean folder, the setup pass run as commands, the installed folder checked by the product rules, and every program started from a copy; no TAS or whole-game qualification.")
        if kind == 'private':
            reservation.unlink()
            reserved = False
            destination.rmdir()
            staged.rename(destination)
            pending.pop('set')
            if package is not None:
                shutil.copy2(package, evidence / package.name)
                receipt['windows_package'] = pb.identity(evidence / package.name)
        else:
            staged = Path(tempfile.mkdtemp(prefix=f'.{destination.name} set package ', dir=destination.parent))
            pending['set-package'] = staged
            shutil.copy2(package, staged / package.name)
            if pb.sha(staged / package.name) != receipt['windows_package']['sha256']:
                raise pb.BuildStop("The set's package changed while it was copied to the output.")
            reservation.unlink()
            reserved = False
            destination.rmdir()
            staged.rename(destination)
            pending.pop('set-package')
            receipt['windows_package'] = pb.identity(destination / package.name)
        for role in [r for r in pending]:
            error = remove_folder(pending[role])
            if error is None:
                pending.pop(role)
            else:
                receipt['warnings'].append(f'A folder this build staged beside the output could not be removed: {pending[role]} ({error}). Remove it by hand; it is not part of the product.')
        if kind == 'private':
            receipt.update(status='build-pass', set_boot_check=summary, merge=info['merge'], executables=[pb.identity(destination / 'windows-x64' / row['executable']) for row in merge['per_program']], shortcuts=info['shortcuts'], qualification='One build per program; each passed its own startup gate, and every program started from a copy of the merged folder. No TAS or whole-game qualification.')
            receipt['executable'] = receipt['executables'][0]
        else:
            receipt.update(status='setup-package-pass', qualification='Inputs-free setup package for the whole set; no private product or gameplay qualification.' + (' The setup pass was run on a clean folder and every program started.' if testing else ''))
        if pending:
            receipt['staging_left'] = [str(folder) for folder in pending.values()]
        receipt['outputs'] = pb.inventory(destination)
    except Exception as error:
        receipt.update(status='human-stop', next_action=str(error))
        for test in (receipt.get('automated_tests') or {}).values():
            if test.get('status') == 'running':
                test['status'] = 'failed'
            elif test.get('status') == 'requested':
                test['status'] = 'not run (the build stopped before the test)'
        (evidence / 'HUMAN-STOP.txt').write_text(str(error) + '\n', encoding='utf-8')
        receipt['stop_file'] = str(evidence / 'HUMAN-STOP.txt')
        for role in [r for r in pending]:
            folder = pending[role]
            if folder.is_dir() and (not any(folder.iterdir())):
                folder.rmdir()
                pending.pop(role)
        wo.retain_failed_payloads(pending, evidence, receipt)
    finally:
        if reserved:
            reservation.unlink(missing_ok=True)
            if made and destination.is_dir() and (not any(destination.iterdir())):
                destination.rmdir()
        stopped = receipt['status'] == 'human-stop'
        if steps is not None:
            for path in work.glob('*.log') if work.is_dir() else []:
                shutil.copy2(path, evidence / path.name)
            for row in [*steps.get('commands', []), *steps.get('stages', [])]:
                if row.get('log') and (evidence / Path(row['log']).name).is_file():
                    row['log'] = str(evidence / Path(row['log']).name)
            pb.progress.finish(steps, evidence, stopped, receipt.get('next_action'))
            if stopped and steps.get('failed_stage'):
                receipt.setdefault('failed_stage', steps['failed_stage'])
            receipt['warnings'] += steps.pop('warnings', [])
            if steps.get('bundled_notice_gaps'):
                receipt['installed_bundled_notice_gaps'] = steps['bundled_notice_gaps']
        receipt['work_directory'] = str(work)
        if work.is_dir() and (not stopped) and (not getattr(args, 'keep_development_files', False)):
            error = remove_folder(work)
            if error:
                receipt['warnings'].append(f"The set build's work folder could not be removed: {work} ({error}).")
        receipt['work_retained'] = work.is_dir()
        pb.write_json(evidence / 'build-receipt.json', receipt)
        if steps is not None:
            runs.after_build(cache, run, evidence, receipt)
            pb.write_json(evidence / 'build-receipt.json', receipt)
    return receipt
