import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
LIMIT_ENV = 'WORKBENCH_OVERLAY_CHECK_SECONDS'
TIME_LIMIT_SECONDS = 600
LOG = 'overlay-compile-check.log'
UNITS_TABLE = 'overlay-compile-check-units.tsv'
SCOPE = "of the overlay units this build's starts captured; another start can observe other entry points in the same code and compile differently"
COMPILED, FAILED = ('captured units compiled', 'captured units failed')
KEEP_ENV = 'WORKBENCH_KEEP_FAILED_CAPTURES'
KEPT_FOLDER, KEPT_NAME = ('private', 'overlay-captures-of-failed-check.json')
FAILURES_KEPT = 20
FAIL = re.compile('SHARD FAIL \\[([^\\]]+)\\] ([^\\n]*)')
RESULT = re.compile('PSX_SHARD_RESULT ok=(\\d+) failed=(\\d+) skipped=(\\d+)')
ERROR_LINE = re.compile('^.*\\berror: .*$', re.M)
SOURCE_FOLDER = re.compile('^\\s*(?:[A-Za-z]:)?[^:\\n]*[\\\\/](?=[^\\\\/:\\n]+:\\d+:)')
FLAVOR = re.compile('_f(\\d+)$')
UNIT = re.compile('^Overlay  load=0x([0-9A-Fa-f]{8})\\s+size=(\\d+)\\s+crc32=0x([0-9A-Fa-f]{8})', re.M)
UNIT_NAMED = re.compile('0x([0-9A-Fa-f]{8}) crc ([0-9A-Fa-f]{8})')
COMPILE_LINE = re.compile('^\\s*compile: (.+?\\.exe|\\S+) -', re.M)

def time_limit(environ=None):
    text = (os.environ if environ is None else environ).get(LIMIT_ENV)
    if text is None or not str(text).strip():
        return TIME_LIMIT_SECONDS
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f'{LIMIT_ENV} must be a number of seconds, or 0 for no check; it is {text!r}.') from None
    if value < 0:
        raise ValueError(f'{LIMIT_ENV} must not be negative; it is {text!r}.')
    return value or None

def captured_overlays(product, target):
    product = Path(product)
    latest = product / 'overlay_captures.json'
    history = product / 'overlay_captures.json.d'
    files = sorted(history.glob('*.json'), key=lambda path: (path.stat().st_mtime, path.name)) if history.is_dir() else []
    files.append(latest)
    kept, read, unreadable, seen = ({}, 0, 0, 0)
    for file in files:
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            unreadable += 1
            continue
        read += 1
        captures = data.get('captures') if isinstance(data, dict) else data
        for capture in captures or []:
            if not isinstance(capture, dict):
                continue
            seen += 1
            key = (capture.get('load_addr'), capture.get('size'), hashlib.sha256(str(capture.get('bytes_b64')).encode('ascii', 'replace')).hexdigest())
            kept[key] = capture
    Path(target).write_text(json.dumps(list(kept.values())), encoding='utf-8')
    regions = Counter((str(capture.get('load_addr')) for capture in kept.values()))
    return {'snapshots': read, 'snapshots_unreadable': unreadable, 'captures_seen': seen, 'captures': len(kept), 'regions': len(regions), 'versions_of_the_commonest_region': max(regions.values()) if regions else 0}

def driver_command(product, jobs=None, captures=None):
    product = Path(product)
    toolchain = product / 'overlay_toolchain'
    python = toolchain / 'python' / ('python.exe' if os.name == 'nt' else 'bin/python3')
    recompiler = toolchain / ('psxrecomp-game.exe' if os.name == 'nt' else 'psxrecomp-game')
    driver, hint = (toolchain / 'compile_overlays.py', toolchain / 'compiler.txt')
    for needed in (python, driver, recompiler, hint):
        if not needed.is_file():
            raise ValueError(f'the product has no {needed.relative_to(product).as_posix()}')
    compiler = hint.read_text(encoding='utf-8').strip()
    if not compiler:
        raise ValueError('overlay_toolchain/compiler.txt names no compiler')
    compiler = compiler if Path(compiler).is_absolute() else str(toolchain / compiler)
    command = [str(python), '-u', '-B', str(driver), '--captures', str(captures or product / 'overlay_captures.json'), '--game-toml', str(product / 'game.toml'), '--recompiler', str(recompiler), '--runtime-include', str(toolchain / 'include'), '--project-root', str(toolchain), '--out-dir', str(product / 'cache'), '--compiler', 'gcc', '--gcc', compiler, '--check']
    if jobs:
        command += ['--jobs', str(int(jobs))]
    return command

def cache_flavor(product):
    for folder in sorted((Path(product) / 'cache').glob('*/gcc/*/*')) if (Path(product) / 'cache').is_dir() else []:
        found = FLAVOR.search(folder.name)
        if found and folder.is_dir():
            return found.group(1)
    return None

def units(text):
    found = []
    starts = list(UNIT.finditer(text))
    for index, match in enumerate(starts):
        block = text[match.end():starts[index + 1].start() if index + 1 < len(starts) else len(text)]

        def count(pattern):
            got = re.search(pattern, block, re.M)
            return int(got.group(1)) if got else None
        found.append({'load': match.group(1).upper(), 'crc': match.group(3).upper(), 'size': int(match.group(2)), 'seeds': count('^\\s*seeds: (\\d+)'), 'executed_pcs': count('^executed_pcs: (\\d+)'), 'function_entry_pcs': count('^function_entry_pcs: (\\d+)')})
    return found

def write_units(text, path):
    failed = {}
    for kind, rest in FAIL.findall(text):
        named = UNIT_NAMED.search(rest)
        if named:
            failed[named.group(1).upper(), named.group(2).upper()] = kind
    rows = ['unit\tsize\tseeds\texecuted_pcs\tfunction_entry_pcs\tfailed_class']
    listed = units(text)
    for unit in listed:
        rows.append('\t'.join(('' if value is None else str(value) for value in (f"0x{unit['load']} crc {unit['crc']}", unit['size'], unit['seeds'], unit['executed_pcs'], unit['function_entry_pcs'], failed.get((unit['load'], unit['crc']))))))
    Path(path).write_text('\n'.join(rows) + '\n', encoding='utf-8')
    return len(listed)

def read_log(text):
    fails = FAIL.findall(text)
    line = RESULT.search(text)
    listed = {(unit['load'], unit['crc']): unit for unit in units(text)}
    pieces = re.split('(?m)^.*SHARD FAIL \\[', text)
    failures = []
    for index, (kind, rest) in enumerate(fails[:FAILURES_KEPT]):
        found = ERROR_LINE.search(pieces[index]) if index < len(pieces) else None
        unit, _, detail = rest.partition(': ')
        error = found.group(0).strip() if found else detail.strip() or rest.strip()
        named = UNIT_NAMED.search(unit)
        about = listed.get((named.group(1).upper(), named.group(2).upper()), {}) if named else {}
        failures.append({'class': kind, 'unit': unit.strip()[:120], 'error': SOURCE_FOLDER.sub('', error)[:400], 'seeds': about.get('seeds'), 'executed_pcs': about.get('executed_pcs')})
    ran = COMPILE_LINE.search(text)
    record = {'shard_fail_lines': len(fails), 'fail_classes': dict(Counter((kind for kind, _ in fails))), 'failures': failures, 'result_line': line.group(0) if line else None, 'units_listed': len(listed), 'compiler_run': ran.group(1) if ran else None}
    if line:
        compiled, failed, skipped = (int(line.group(n)) for n in (1, 2, 3))
        record.update(attempted=compiled + failed, compiled=compiled, failed=failed, skipped=skipped)
    return record

def stop_tree(child):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True)
    else:
        child.kill()
    child.wait()

def run(product, evidence, jobs=None, environ=None):
    record = {'schema': 1}
    began = time.monotonic()
    try:
        limit = time_limit(environ)
        if limit is None:
            return {**record, 'status': 'not run', 'reason': f'{LIMIT_ENV} is 0'}
        record['time_limit_seconds'] = limit
        product, evidence = (Path(product), Path(evidence))
        if not (product / 'overlay_toolchain').is_dir():
            return {**record, 'status': 'no toolchain'}
        if not (product / 'overlay_captures.json').is_file():
            return {**record, 'status': 'no captures', 'attempted': 0, 'compiled': 0, 'failed': 0, 'skipped': 0}
        log = evidence / LOG
        scratch = Path(tempfile.mkdtemp(prefix='wb-overlay-check-'))
        env = dict(os.environ if environ is None else environ)
        env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
        for name in ('PSX_OVERLAY_CACHE_DIR', 'PSX_OVERLAY_CAPTURES'):
            env.pop(name, None)
        flavor = cache_flavor(product)
        if flavor is not None:
            env['PSX_OVERLAY_FLAVOR'] = flavor
        timed_out = False
        text = ''
        try:
            merged = scratch / 'captured-overlays.json'
            record['captured'] = captured_overlays(product, merged)
            command = driver_command(product, jobs, merged)
            record['compiler'] = command[command.index('--gcc') + 1] if '--gcc' in command else None
            with open(log, 'wb') as stream:
                child = subprocess.Popen(command, cwd=product, env=env, stdout=stream, stderr=subprocess.STDOUT)
                try:
                    child.wait(timeout=limit)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop_tree(child)
            text = log.read_text(encoding='utf-8', errors='replace')
            if FAIL.search(text) and (env.get(KEEP_ENV) or '1').strip() != '0':
                kept = evidence.parent / KEPT_FOLDER / KEPT_NAME
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(merged, kept)
                record['captures_kept'] = str(kept)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        record.update(read_log(text))
        record.update(log=str(log), exit_code=child.returncode, jobs=jobs, scope=SCOPE)
        write_units(text, evidence / UNITS_TABLE)
        record['units_table'] = str(evidence / UNITS_TABLE)
        if timed_out:
            record['status'] = 'incomplete'
            record['reason'] = f'the check reached its time limit of {limit:g} s'
        elif record.get('result_line') is None:
            record['status'] = 'no result'
            record['reason'] = f'the driver ended with exit {child.returncode} and printed no result line'
        else:
            record['status'] = FAILED if record['failed'] else COMPILED
    except Exception as error:
        record.update(status='not run', reason=f'{type(error).__name__}: {error}')
    finally:
        record['seconds'] = round(time.monotonic() - began, 1)
    return record

def warning(record):
    status = record.get('status')
    first = (record.get('failures') or [{}])[0]
    named = f" First: [{first.get('class')}] {first.get('unit')}: {first.get('error')}" if first else ''
    classes = ', '.join((f'{count} {name}' for name, count in sorted((record.get('fail_classes') or {}).items())))
    seeds = f" (its source was made from {first.get('seeds')} seeds)" if first and first.get('seeds') is not None else ''
    if status == FAILED:
        return f"{record['failed']} of the {record['attempted']} overlay units these starts captured do not compile ({classes}). They run interpreted.{named}{seeds} Another start can capture other entry points and compile differently. The whole compiler output is in {LOG}, every unit in {UNITS_TABLE} (build)."
    if status == 'incomplete':
        seen = record.get('shard_fail_lines') or 0
        return f"The overlay compile check did not finish: {record.get('reason')}. Until then {seen} unit(s) had failed{(' (' + classes + ')' if classes else '')}.{(named if seen else '')} A title with this many captured overlay versions wants a kit review; the output so far is in {LOG} (build)."
    if status == 'no result':
        return f"The overlay compile check gave no result: {record.get('reason')}. Read {LOG} (build)."
    if status == 'not run' and record.get('reason') != f'{LIMIT_ENV} is 0':
        return f"The overlay compile check did not run: {record.get('reason')} (build)."
    return None

def record_in(receipt, record):
    receipt['overlay_compile_check'] = record
    line = warning(record)
    if line:
        receipt.setdefault('warnings', []).append(line)
    return record
