import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
try:
    from . import start_memory
except ImportError:
    try:
        import start_memory
    except ImportError:
        start_memory = None
try:
    from . import overlay_only_gate
except ImportError:
    try:
        import overlay_only_gate
    except ImportError:
        overlay_only_gate = None

def identity(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}

def overlay_split(loader):
    pair = (loader.get('disp_native_overlay'), loader.get('disp_interp_overlay'))
    return pair if all((type(value) is int for value in pair)) else None

def kernel_dispatch(report):
    loader = report.get('overlay_loader') or {}
    if overlay_split(loader) is None:
        return None
    bless = report.get('kbless') or {}
    return {'native': loader.get('disp_native_kernel'), 'interpreted': loader.get('disp_interp_kernel'), 'kbless_state': bless.get('state'), 'kbless_native_hits': bless.get('native_hits'), 'kbless_disabled_by_overflow': bless.get('disabled_by_overflow')}

def above_kernel_dispatch(report):
    split = overlay_split(report.get('overlay_loader') or {})
    return None if split is None else {'native': split[0], 'interpreted': split[1]}

def overlay_evidence(report):
    loader = report.get('overlay_loader') or {}
    split = overlay_split(loader)
    counts = [loader.get(k, 0) for k in ('disp_native', 'disp_interp', 'loads')]
    counts = [c if type(c) is int else 0 for c in counts]
    if split is not None:
        counts[1] = split[1]
    return (counts[0], any(counts))

def overlay_captured(report, *roots):
    loader = report.get('overlay_loader') or {}
    if any((type(loader.get(k)) is int and loader.get(k) > 0 for k in ('loads', 'registered', 'disp_native'))):
        return True
    return any(((Path(root) / 'overlay_captures.json').exists() for root in roots))

def frame_count(report):
    return report.get('frame', report.get('frames', report.get('frame_count', 0)))

def game_code_interpreted(report):
    split = overlay_split(report.get('overlay_loader') or {})
    if split is None or split[1] <= 0:
        return None
    frames = frame_count(report)
    usable = type(frames) is int and frames > 0
    return {'dispatches': split[1], 'frames': frames if usable else None, 'per_frame': round(split[1] / frames, 2) if usable else None, 'last_epc': (report.get('cpu') or {}).get('epc')}
CDROM_FIELDS = ('read_starts', 'silent_reads', 'silent_run_max')
CDROM_SILENT_RUN_WARN = 3

def cdrom_reads(report):
    cdrom = report.get('cdrom')
    if not isinstance(cdrom, dict):
        return None
    values = {name: cdrom.get(name) for name in CDROM_FIELDS}
    if any((type(value) is not int for value in values.values())):
        return None
    return values

def cdrom_warning(record):
    if not record or record['silent_run_max'] < CDROM_SILENT_RUN_WARN:
        return None
    return f"The second start had {record['silent_run_max']} CD reads in a row that delivered no sector ({record['silent_reads']} silent of {record['read_starts']} read starts). Up to 2 in a row is normal; 3 or more means a read does not deliver in time, and tens or hundreds mean the game is stuck retrying a read. Look at the title before it ships."
GAME_CODE_WARN_PER_FRAME = 1.0

def game_code_warning(record):
    if not record:
        return None
    if record.get('per_frame') is not None and record['per_frame'] < GAME_CODE_WARN_PER_FRAME:
        return None
    rate = f"{record['per_frame']} per frame over {record['frames']} frames" if record.get('per_frame') is not None else 'frame count unknown'
    return f"The second start interpreted {record['dispatches']} dispatches of game code ({rate}; last exception return {record['last_epc']}). The title runs; that code runs slower than compiled code. The remedy is kit work: compile those return points (build)."
OVERLAY_WARN_PER_FRAME = 100

def overlay_code_interpreted(report):
    if overlay_only_gate is None or not isinstance(report, dict):
        return None
    detail = report.get('interp_detail')
    table = detail.get('per_address') if isinstance(detail, dict) else None
    if not isinstance(table, dict) or 'by_place' not in table:
        return None
    result = overlay_only_gate.judge(report, OVERLAY_WARN_PER_FRAME, per_frame=True)
    record = {key: result[key] for key in ('verdict', 'overlay', 'frames', 'per_frame', 'limit', 'generation', 'lower_bound', 'text', 'kernel')}
    record['reasons'] = overlay_only_gate.miss_reasons(report)
    if result['verdict'] == overlay_only_gate.REFUSED:
        record['reason'] = result['reason']
    record['line'] = result['line']
    return record

def overlay_code_warning(record):
    if not record or record.get('verdict') not in ('fail', 'refused'):
        return None
    if record['verdict'] == 'refused':
        return f"The overlay-only gate could not judge the second start: {record.get('reason')}. Its overlay interpretation is not known; the build does not stop for it (build, D29). {record['line']}"
    return f"The second start ran overlay code in the interpreter more than {record['limit']} times a frame. The title runs; that code runs slower than compiled code. A warning, not a stop (D29): read the loader-miss reasons before a stop is decided (build). {record['line']}"

def overlay_compile(report):
    data = report.get('overlay_compile')
    if not isinstance(data, dict):
        return None

    def number(key):
        value = data.get(key)
        return value if type(value) is int and value > 0 else 0
    classes = []
    for item in data.get('failure_classes') or []:
        if isinstance(item, dict):
            classes.append({'class': str(item.get('class')), 'count': item.get('count'), 'first': str(item.get('first_error') or item.get('first') or '')[:300]})
    record = {'runs': number('runs'), 'runs_failed': number('runs_failed'), 'runs_with_result': number('runs_with_result'), 'attempted': number('units_attempted'), 'compiled': number('units_compiled'), 'failed': max(number('units_failed'), number('fail_lines')), 'skipped': number('units_skipped'), 'classes': classes, 'failures_in_unnamed_classes': number('fail_lines_in_unnamed_classes'), 'state': data.get('state'), 'consistent': data.get('consistent')}
    if record['runs'] and (not record['runs_with_result']) and (data.get('state') == 'idle'):
        record['no_result'] = True
        record['output_tail'] = str(data.get('output_tail') or '')[-300:]
    if record['runs_failed'] > record['runs']:
        record['driver_not_started'] = record['runs_failed'] - record['runs']
    return record

def overlay_compile_warning(record, start):
    if not record:
        return None
    if record.get('failed'):
        classes = ', '.join((f"{item.get('count')} {item.get('class')}" for item in record.get('classes') or []))
        first = next((item['first'] for item in record.get('classes') or [] if item.get('first')), '')
        more = f" {record['failures_in_unnamed_classes']} more failure(s) are in classes the runtime did not name." if record.get('failures_in_unnamed_classes') else ''
        return f"{start} reported {record['failed']} failed overlay compile(s)" + (f' ({classes})' if classes else '') + '. Those units run interpreted.' + (f' First: {first}' if first else '') + more + ' A unit that fails in two compile runs is counted twice (build).'
    if record.get('no_result'):
        return f"{start} ran its overlay compile driver {record['runs']} time(s) and it never printed a result. The end of its output: {record.get('output_tail')!r} (build)."
    if record.get('driver_not_started'):
        return f"{start} could not start its overlay compile driver {record['driver_not_started']} time(s); what it captured then was not compiled (build)."
    return None

def start_label(row):
    return f"The start from disc {row['disc']}" if row.get('disc') is not None else f"Start {row.get('number')}"

def record_overlay_compile(rows, receipt):
    starts = [{'start': start_label(row), **row['overlay_compile']} for row in rows if row.get('overlay_compile')]
    if not starts:
        return
    receipt['overlay_compile_starts'] = starts
    worst = max(starts, key=lambda item: (item.get('failed') or 0, bool(item.get('no_result')), item.get('driver_not_started') or 0))
    line = overlay_compile_warning(worst, worst['start'])
    if line:
        others = [item['start'] for item in starts if item is not worst and item.get('failed')]
        receipt.setdefault('warnings', []).append(line + (f" Also failing: {', '.join(others)}." if others else ''))

def start_findings(report):
    found = {'game_code_interpreted': game_code_interpreted(report), 'overlay_code_interpreted': overlay_code_interpreted(report), 'cdrom_reads': cdrom_reads(report), 'overlay_compile': overlay_compile(report)}
    return {name: value for name, value in found.items() if value is not None}

def record_start_findings(row, receipt):
    interpreted = row.get('game_code_interpreted')
    if interpreted is not None:
        receipt['game_code_interpreted'] = interpreted
        warning = game_code_warning(interpreted)
        if warning:
            receipt.setdefault('warnings', []).append(warning)
    overlay = row.get('overlay_code_interpreted')
    if overlay is not None:
        receipt['overlay_code_interpreted'] = overlay
        warning = overlay_code_warning(overlay)
        if warning:
            receipt.setdefault('warnings', []).append(warning)
    reads = row.get('cdrom_reads')
    if reads is not None:
        receipt['cdrom_reads'] = reads
        warning = cdrom_warning(reads)
        if warning:
            receipt.setdefault('warnings', []).append(warning)
COMPILER_STOP = 'Second start must report frames > 0, and overlay_loader.disp_native > 0 whenever an overlay was loaded or interpreted.'

def judge_second_start(report, captured):
    frames = frame_count(report)
    if type(frames) is not int or frames <= 0:
        return COMPILER_STOP
    native, exercised = overlay_evidence(report)
    if not exercised or native > 0:
        return None
    if captured:
        return COMPILER_STOP
    if overlay_split(report.get('overlay_loader') or {}) is not None:
        return None
    return outside_overlay_message(report)

def outside_overlay_message(report):
    loader = report.get('overlay_loader') or {}
    epc = (report.get('cpu') or {}).get('epc')
    interpreted = loader.get('disp_interp')
    return f'Second start interpreted {interpreted} dispatches, but neither start captured, registered or loaded an overlay. The interpreted code is outside any overlay: boot-executable or kernel code the kit does not compile (last exception return {epc}), or the game never reached the point where it loads overlays. This is not an overlay compiler failure; review the kit.'

def overlay_cache_progress(*roots):
    found = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for pattern in ('**/cache/**/*.dll', '**/overlay*/**/*.dll', '**/cache/**/*.so', '**/overlay*/**/*.so'):
            for path in root.glob(pattern):
                try:
                    found[str(path)] = path.stat().st_mtime
                except OSError:
                    continue
    return (len(found), max(found.values()) if found else 0.0)

class MemoryLimitReached(Exception):
    pass

def start_guard(child):
    return start_memory.guard(child) if start_memory is not None else None

def finish_guard(memory, row):
    if memory is None:
        row['memory'] = {'limit_mb': None, 'not_limited': 'this host runs the gate without its memory module; nothing limits the start'}
        return
    memory.close()
    record = memory.record()
    if record is not None:
        row['memory'] = record

def memory_stop(row, what):
    record = row.get('memory') or {}
    if record.get('exceeded'):
        raise RuntimeError(f'{what} was stopped: ' + start_memory.stop_text(record))
START_MEMORY_LOOK_FRACTION = 0.5

def start_memory_summary(rows):
    measured = [(row, row['memory']) for row in rows if isinstance(row.get('memory'), dict)]
    if not measured:
        return None
    summary = {'limit_mb': measured[0][1].get('limit_mb'), 'starts': len(measured)}
    peaks = [(memory['peak_process_mb'], row) for row, memory in measured if memory.get('peak_process_mb') is not None]
    if peaks:
        heaviest, row = max(peaks, key=lambda item: item[0])
        summary.update(peak_process_mb=heaviest, peak_job_mb=max((memory.get('peak_job_mb') or 0 for _row, memory in measured)), heaviest_start=row.get('program') or row.get('disc') or row.get('number'))
    reason = next((memory['not_limited'] for _row, memory in measured if memory.get('not_limited')), None)
    if reason:
        summary['not_limited'] = reason
    return summary

def record_start_memory(rows, receipt):
    summary = start_memory_summary(rows)
    if summary is None:
        return
    receipt['start_memory'] = summary
    warnings = receipt.setdefault('warnings', [])
    if summary.get('not_limited'):
        warnings.append(f"The headless starts ran without a memory limit: {summary['not_limited']}.")
    limit, peak = (summary.get('limit_mb'), summary.get('peak_process_mb'))
    if limit and peak is not None and (peak >= limit * START_MEMORY_LOOK_FRACTION):
        warnings.append(f"A process of start {summary['heaviest_start']} used {peak:,.0f} MB, more than half of the {limit:,} MB a start may use. Look at the title before it reaches the limit (build).")

def wait_with_overlay_progress(child, budget, product, unrelated_cwd, row, idle_grace=120.0, poll=2.0, margin=90.0, memory=None):
    deadline = time.monotonic() + budget + margin
    last_count, last_newest = overlay_cache_progress(product, unrelated_cwd)
    idle_since = time.monotonic()
    extensions = 0
    while True:
        try:
            if child.wait(timeout=poll) is not None:
                row['overlay_cache'] = {'artifacts': last_count, 'extensions': extensions}
                return child.returncode
        except subprocess.TimeoutExpired:
            pass
        if memory is not None and memory.exceeded():
            raise MemoryLimitReached()
        count, newest = overlay_cache_progress(product, unrelated_cwd)
        if count > last_count or newest > last_newest:
            last_count, last_newest = (max(count, last_count), max(newest, last_newest))
            idle_since = time.monotonic()
            if time.monotonic() >= deadline:
                extensions += 1
        if time.monotonic() >= deadline and time.monotonic() - idle_since > idle_grace:
            raise subprocess.TimeoutExpired(child.args if hasattr(child, 'args') else 'boot', budget)

def processes_under(folder):
    root = os.path.normcase(str(Path(folder).resolve())) + os.sep
    found = []
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class Entry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot in (None, wintypes.HANDLE(-1).value):
            return found
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(Entry)
            more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                handle = kernel.OpenProcess(4096, False, entry.th32ProcessID)
                if handle:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            if os.path.normcase(buffer.value).startswith(root):
                                found.append((int(entry.th32ProcessID), buffer.value))
                    finally:
                        kernel.CloseHandle(handle)
                more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
    else:
        for entry in Path('/proc').glob('[0-9]*'):
            try:
                image = os.readlink(str(entry / 'exe'))
            except OSError:
                continue
            if os.path.normcase(image).startswith(root):
                found.append((int(entry.name), image))
    return found

def settle_product_processes(product, timeout=30.0, poll=0.5, lister=processes_under, clock=time.monotonic, sleep=time.sleep, kill=None):
    kill = kill or (lambda pid: os.kill(pid, signal.SIGTERM))
    started = clock()
    seen = {}
    left = lister(product)
    while left and clock() - started < timeout:
        seen.update(left)
        sleep(poll)
        left = lister(product)
    killed = []
    for pid, image in left:
        seen[pid] = image
        try:
            kill(pid)
            killed.append(pid)
        except OSError:
            pass
    if not seen:
        return None
    return {'waited_seconds': round(clock() - started, 1), 'processes': [{'pid': pid, 'image': image} for pid, image in sorted(seen.items())], 'ended_by_gate': killed}
FORCED_CONTROLS = ('PSX_EXIT_HALT', 'PSX_EXIT_AFTER_FRAMES', 'PSX_STOP_AFTER_FRAME', 'PSX_STOP_AFTER_FRAMES', 'PSX_MAX_FRAMES', 'PSX_FRAME_LIMIT', 'PSX_OVERLAY_AUTOCOMPILE_CMD', 'PSX_OVERLAY_BACKEND')
DISC_START_MILLISECONDS = 60000

def select_disc(settings_text, index):
    out, table, placed = ([], '', False)
    for line in settings_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            table = stripped
            out.append(line)
            if table == '[disc]' and (not placed):
                out.append(f'selected = {index}')
                placed = True
            continue
        if table == '[disc]' and re.match('\\s*selected\\s*=', line):
            continue
        out.append(line)
    if not placed:
        out += ['[disc]', f'selected = {index}']
    return '\n'.join(out) + '\n'

def boot_name_serial(name):
    stem = name.replace('\\', '/').split('/')[-1].split(';')[0].strip()
    found = re.match('([A-Za-z]{4})_(\\d{3})\\.(\\d{2})$', stem)
    return f'{found.group(1)}-{found.group(2)}{found.group(3)}'.upper() if found else stem.upper()

def mounted_serial(log_text):
    found = re.search('disc region \\S+ \\(serial ([^)]+)\\)', log_text)
    if found:
        return found.group(1)
    found = re.search('text image guard armed \\(0x[0-9A-Fa-f]+\\.\\.0x[0-9A-Fa-f]+, disc ([^)]+)\\)', log_text)
    return boot_name_serial(found.group(1)) if found else None

def start_other_discs(product, executable, evidence, unrelated_cwd, env, discs, on_run=None, first_number=3, rows=None, kind='disc'):
    rows = [] if rows is None else rows
    settings = product / 'settings.toml'
    original = settings.read_bytes() if settings.is_file() else None
    touched = False
    try:
        for offset, disc in enumerate(discs):
            number, index, expected = (first_number + offset, disc['disc'], disc.get('serial'))
            select = disc.get('select', index)
            for directory in (unrelated_cwd, product):
                for name in ('psx_last_run_report.json', 'psx_crash.txt', 'psx_freeze_heartbeat.json'):
                    (directory / name).unlink(missing_ok=True)
            if select is not None:
                touched = True
                settings.write_bytes(select_disc(original.decode('utf-8-sig') if original else '', int(select)).encode('utf-8'))
            launch_env = env.copy()
            removed = {key: launch_env.pop(key) for key in FORCED_CONTROLS if key in launch_env}
            forced = {'PSX_HEADLESS': '1', 'PSX_NO_LAUNCHER': '1', 'PSX_HOST_MUTE': '1', 'PSX_EXIT_AFTER_MS': str(DISC_START_MILLISECONDS), 'SDL_AUDIODRIVER': 'dummy', 'SDL_VIDEODRIVER': 'windows' if os.name == 'nt' else 'dummy'}
            launch_env.update(forced)
            row = {'number': number, 'disc': index, 'expected_serial': expected, 'environment': forced, 'removed_controls': removed, 'stdout': str(evidence / f'run-{kind}-{index}.out'), 'stderr': str(evidence / f'run-{kind}-{index}.err')}
            rows.append(row)
            if on_run:
                on_run(row, 'running')
            start = time.monotonic()
            with open(row['stdout'], 'wb') as stdout, open(row['stderr'], 'wb') as stderr:
                child = subprocess.Popen([str(executable)], cwd=unrelated_cwd, env=launch_env, stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                row['pid'] = child.pid
                memory = start_guard(child)
                try:
                    row['exit_code'] = wait_with_overlay_progress(child, DISC_START_MILLISECONDS / 1000, product, unrelated_cwd, row, memory=memory)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise RuntimeError(f'Headless start of {kind} {index} timed out; the owned process was stopped.')
                except MemoryLimitReached:
                    memory.end()
                    row['exit_code'] = child.wait()
                finally:
                    row['elapsed_seconds'] = round(time.monotonic() - start, 3)
                    settled = settle_product_processes(product)
                    if settled:
                        row['settled_processes'] = settled
                    finish_guard(memory, row)
            memory_stop(row, f'Headless start of {kind} {index}')
            log_text = ''.join((Path(row[name]).read_text(encoding='utf-8', errors='replace') for name in ('stdout', 'stderr')))
            row['mounted_serial'] = mounted_serial(log_text)
            report = unrelated_cwd / 'psx_last_run_report.json'
            if report.is_file():
                saved = evidence / f'last-run-{kind}-{index}.json'
                shutil.copy2(report, saved)
                row['report'] = identity(saved)
            if row['exit_code'] != 0 or any(((d / 'psx_crash.txt').exists() for d in (unrelated_cwd, product))):
                raise RuntimeError(f'Headless start of {kind} {index} did not exit cleanly; read its external logs.')
            if 'report' not in row:
                raise RuntimeError(f'Headless start of {kind} {index} produced no fresh last-run report.')
            data = json.loads(saved.read_text(encoding='utf-8-sig'))
            if data.get('exit_origin') != 'exit_after_ms' or row['elapsed_seconds'] < DISC_START_MILLISECONDS / 1000:
                raise RuntimeError(f'Headless start of {kind} {index} exited before its requested clean timer.')
            frames = data.get('frame', data.get('frames', data.get('frame_count', 0)))
            native, exercised = overlay_evidence(data)
            row.update(frames=frames, disp_native=native, overlays_exercised=exercised, overlays_captured=overlay_captured(data, product, unrelated_cwd))
            kernel = kernel_dispatch(data)
            if kernel is not None:
                row['kernel_dispatch'] = kernel
                row['above_kernel_dispatch'] = above_kernel_dispatch(data)
            row.update(start_findings(data))
            if type(frames) is not int or frames <= 0:
                raise RuntimeError(f'Headless start of {kind} {index} ran no frames.')
            if expected and row['mounted_serial'] != expected:
                raise RuntimeError(f"Headless start of {kind} {index} mounted {row['mounted_serial'] or 'no named disc'}, not {expected}. The product does not start from that disc of its roster.")
            row['passed'] = True
            if on_run:
                on_run(row, 'done')
    finally:
        if touched and original is None:
            settings.unlink(missing_ok=True)
        elif touched:
            settings.write_bytes(original)
    return rows

def run_lean_boots(product, exe_name, evidence, unrelated_cwd, env, on_run=None, discs=None):
    product, evidence, unrelated_cwd = map(lambda p: Path(p).resolve(), (product, evidence, unrelated_cwd))
    if evidence.is_relative_to(product) or unrelated_cwd.is_relative_to(product):
        raise ValueError('Evidence and launch working directory must be outside the staged product.')
    if Path(exe_name).name != exe_name:
        raise ValueError('Executable must be a filename within the staged product.')
    executable = product / exe_name
    if not executable.is_file() or not unrelated_cwd.is_dir():
        raise ValueError('Staged executable and unrelated working directory must exist.')
    evidence.mkdir(parents=True, exist_ok=False)
    result = {'schema': 1, 'status': 'running', 'executable': identity(executable), 'cwd': str(unrelated_cwd), 'argv': [str(executable)], 'runs': []}
    receipt = evidence / 'lean-boot-receipt.json'
    try:
        for number, milliseconds in ((1, 180000), (2, 60000)):
            for directory in (unrelated_cwd, product):
                for name in ('psx_last_run_report.json', 'psx_crash.txt', 'psx_freeze_heartbeat.json'):
                    (directory / name).unlink(missing_ok=True)
            launch_env = env.copy()
            removed = {}
            for key in ('PSX_EXIT_HALT', 'PSX_EXIT_AFTER_FRAMES', 'PSX_STOP_AFTER_FRAME', 'PSX_STOP_AFTER_FRAMES', 'PSX_MAX_FRAMES', 'PSX_FRAME_LIMIT', 'PSX_OVERLAY_AUTOCOMPILE_CMD', 'PSX_OVERLAY_BACKEND'):
                if key in launch_env:
                    removed[key] = launch_env.pop(key)
            forced = {'PSX_HEADLESS': '1', 'PSX_NO_LAUNCHER': '1', 'PSX_HOST_MUTE': '1', 'PSX_EXIT_AFTER_MS': str(milliseconds), 'SDL_AUDIODRIVER': 'dummy', 'SDL_VIDEODRIVER': 'windows' if os.name == 'nt' else 'dummy'}
            launch_env.update(forced)
            row = {'number': number, 'environment': forced, 'removed_controls': removed, 'stdout': str(evidence / f'run-{number}.out'), 'stderr': str(evidence / f'run-{number}.err')}
            result['runs'].append(row)
            if on_run:
                on_run(row, 'running')
            start = time.monotonic()
            with open(row['stdout'], 'wb') as stdout, open(row['stderr'], 'wb') as stderr:
                child = subprocess.Popen([str(executable)], cwd=unrelated_cwd, env=launch_env, stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                row['pid'] = child.pid
                memory = start_guard(child)
                try:
                    row['exit_code'] = wait_with_overlay_progress(child, milliseconds / 1000, product, unrelated_cwd, row, memory=memory)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise RuntimeError(f'Headless run {number} timed out; the owned process was stopped.')
                except MemoryLimitReached:
                    memory.end()
                    row['exit_code'] = child.wait()
                finally:
                    row['elapsed_seconds'] = round(time.monotonic() - start, 3)
                    settled = settle_product_processes(product)
                    if settled:
                        row['settled_processes'] = settled
                    finish_guard(memory, row)
            memory_stop(row, f'Headless run {number}')
            report = unrelated_cwd / 'psx_last_run_report.json'
            if report.is_file():
                saved = evidence / f'last-run-{number}.json'
                shutil.copy2(report, saved)
                row['report'] = identity(saved)
            if row['exit_code'] != 0 or any(((d / 'psx_crash.txt').exists() for d in (unrelated_cwd, product))):
                raise RuntimeError(f'Headless run {number} did not exit cleanly; read its external logs.')
            if 'report' not in row:
                raise RuntimeError(f'Headless run {number} produced no fresh last-run report.')
            data = json.loads(saved.read_text(encoding='utf-8-sig'))
            if data.get('exit_origin') != 'exit_after_ms' or row['elapsed_seconds'] < milliseconds / 1000:
                raise RuntimeError(f'Headless run {number} exited before its requested clean timer.')
            frames = frame_count(data)
            native, exercised = overlay_evidence(data)
            captured = overlay_captured(data, product, unrelated_cwd)
            row.update(frames=frames, disp_native=native, overlays_exercised=exercised, overlays_captured=captured)
            kernel = kernel_dispatch(data)
            if kernel is not None:
                row['kernel_dispatch'] = kernel
                row['above_kernel_dispatch'] = above_kernel_dispatch(data)
            row.update(start_findings(data))
            if number == 2:
                stop = judge_second_start(data, captured or any((r.get('overlays_captured') for r in result['runs'])))
                if stop is not None:
                    raise RuntimeError(stop)
            if on_run:
                on_run(row, 'done')
        if discs:
            result['disc_runs'] = []
            start_other_discs(product, executable, evidence, unrelated_cwd, env, discs, on_run=on_run, rows=result['disc_runs'])
        if identity(executable) != result['executable']:
            raise RuntimeError('Staged executable changed during the boot gate.')
        result['status'] = 'passed'
        return result
    except Exception as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        receipt.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
