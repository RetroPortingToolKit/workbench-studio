import json
import sys
PASS = 'pass'
FAIL = 'fail'
REFUSED = 'refused'
EXIT_CODE = {PASS: 0, FAIL: 1, REFUSED: 2}
OVERLAY_INTERP_LIMIT = 0
PLACES = ('kernel', 'text', 'overlay')
ABSOLUTE = 'absolute'
PER_FRAME = 'per-frame'
TABLE_FULL_PERCENT = 85
OLD_MEANING = 'old-meaning'
AFTER_ENTRY = 'after-entry'
NOT_ENTERED = 'not-entered'
GENERATION_TEXT = {OLD_MEANING: 'report old meaning: no per_address.preload, counts from power-on, BIOS shell included', AFTER_ENTRY: 'report after game entry: per_address.preload present, entry at frame {entry}', NOT_ENTERED: 'report never entered the game: preload.game_started 0'}

def _count(value):
    return type(value) is int and value >= 0

def _address(value):
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None

def _step(node, *path):
    walked = []
    for key in path:
        walked.append(key)
        if not isinstance(node, dict) or key not in node:
            return (None, '.'.join(walked))
        node = node[key]
    return (node, None)

def place_counts(report):
    if not isinstance(report, dict):
        return (None, 'the report is not a JSON object')
    by_place, missing = _step(report, 'interp_detail', 'per_address', 'by_place')
    if missing:
        return (None, f'it has no {missing}')
    if not isinstance(by_place, dict):
        return (None, 'interp_detail.per_address.by_place is not an object')
    counts = {}
    for place in PLACES:
        entry = by_place.get(place)
        if not isinstance(entry, dict):
            return (None, f'interp_detail.per_address.by_place has no {place} object')
        if not _count(entry.get('entries')):
            return (None, f'interp_detail.per_address.by_place.{place}.entries is not a count')
        counts[place] = entry['entries']
    return (counts, None)

def text_range(report):
    ranges, missing = _step(report, 'interp_detail', 'ranges')
    if missing or not isinstance(ranges, dict):
        return (None, 'it has no interp_detail.ranges')
    low, high = (_address(ranges.get('text_lo')), _address(ranges.get('text_hi')))
    if low is None or high is None:
        return (None, 'interp_detail.ranges.text_lo or text_hi is not an address')
    if high <= low:
        return (None, f"its boot-executable range is empty (text_lo {ranges.get('text_lo')}, text_hi {ranges.get('text_hi')}), so the overlay count would include the boot text")
    return ((low, high), None)

def miss_reasons(report):
    miss, missing = _step(report, 'interp_detail', 'loader_miss', 'above_kernel')
    if missing or not isinstance(miss, dict):
        return None
    return {name: value for name, value in miss.items() if _count(value) and value > 0}

def _reasons_text(reasons, top=3):
    if reasons is None:
        return 'loader_miss not reported'
    if not reasons:
        return 'no loader misses above the kernel window'
    ranked = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    return 'above-kernel loader misses ' + ', '.join((f'{name}={count}' for name, count in ranked[:top]))

def frame_count(report):
    return report.get('frame', report.get('frames', report.get('frame_count', 0)))

def generation(report):
    table, missing = _step(report, 'interp_detail', 'per_address')
    if missing or not isinstance(table, dict):
        return (None, 'it has no interp_detail.per_address')
    if 'preload' not in table:
        return ((OLD_MEANING, None, 0), None)
    preload = table['preload']
    if not isinstance(preload, dict):
        return (None, 'interp_detail.per_address.preload is not an object')
    started = preload.get('game_started')
    if type(started) is not int or started not in (0, 1):
        return (None, 'interp_detail.per_address.preload.game_started is not 0 or 1')
    if started == 0:
        return ((NOT_ENTERED, None, 0), None)
    if not _count(preload.get('frame')):
        return (None, 'interp_detail.per_address.preload.frame is not a count')
    if not _count(preload.get('addresses')):
        return (None, 'interp_detail.per_address.preload.addresses is not a count')
    return ((AFTER_ENTRY, preload['frame'], preload['addresses']), None)

def table_fill(report, moved=0):
    table, missing = _step(report, 'interp_detail', 'per_address')
    if missing or not isinstance(table, dict):
        return (None, 'it has no interp_detail.per_address')
    addresses, size = (table.get('addresses'), table.get('table_size'))
    if not _count(addresses) or not _count(size) or size == 0:
        return (None, 'interp_detail.per_address.addresses or table_size is not a count, so the report cannot say whether its place counts are complete')
    return ((addresses + moved, size), None)

def _refused(why, limit, mode, kind=None, entry=None):
    tag = f' ({GENERATION_TEXT[kind].format(entry=entry)})' if kind else ''
    line = f'Overlay-only gate REFUSED{tag}: {why}. A report that cannot tell overlay code from boot text and kernel code is not a pass. Use a runtime from pin H or later (build).'
    return {'verdict': REFUSED, 'mode': mode, 'overlay': None, 'limit': limit, 'frames': None, 'per_frame': None, 'floor': None, 'text': None, 'kernel': None, 'lower_bound': None, 'generation': kind, 'line': line, 'reason': why}

def _number(value):
    return type(value) in (int, float) and value >= 0 and (value == value) and (value != float('inf'))

def decide(counts, limit=OVERLAY_INTERP_LIMIT, text_range=None, reasons=None, frames=None, per_frame=False, table=None, kind=OLD_MEANING, entry=None):
    mode = PER_FRAME if per_frame else ABSOLUTE
    usable = type(frames) is int and frames > 0
    if per_frame and (not usable):
        since = ' after game entry' if kind == AFTER_ENTRY else ''
        return _refused(f'the start reports no usable frame count{since} ({frames!r}), so a per-frame limit cannot be judged', limit, mode, kind, entry)
    overlay = counts['overlay']
    if text_range is not None:
        where = f'outside the kernel window and outside the boot executable (text_lo..text_hi 0x{text_range[0]:08X}..0x{text_range[1]:08X})'
    else:
        where = 'outside the kernel window and outside the boot executable'
    since = ' after game entry' if kind == AFTER_ENTRY else ''
    rate = round(overlay / frames, 2) if usable else None
    if per_frame:
        breach = overlay > limit * frames
        measure = f'judged overlay={overlay} interpreter entries {where}, {rate} a frame over {frames} frames{since}'
        limit_text = f'the limit {limit} entries a frame'
    else:
        breach = overlay > limit
        frame_text = f'{frames} frames{since}' if usable else 'frames not reported'
        measure = f'judged overlay={overlay} interpreter entries {where}, over {frame_text}'
        limit_text = f'the limit {limit} entries'
    head = 'FAIL' if breach else 'PASS'
    relation = 'over' if breach else 'within'
    lower_bound = None
    note = ''
    if table is not None:
        lower_bound = table[0] * 100 >= table[1] * TABLE_FULL_PERCENT
        if lower_bound:
            note = f' The per-address table holds {table[0]} of {table[1]} addresses (85 % or more): the counts are a lower bound' + ('.' if breach else ', so a pass here is not proof.')
    other = f"not judged: boot text {counts['text']}, kernel {counts['kernel']}"
    gen = GENERATION_TEXT[kind].format(entry=entry)
    line = f'Overlay-only gate {head} (mode {mode}, {gen}): {measure}, {relation} {limit_text}; {other}; {_reasons_text(reasons)}.{note}'
    return {'verdict': FAIL if breach else PASS, 'mode': mode, 'overlay': overlay, 'limit': limit, 'frames': frames if usable else None, 'per_frame': rate, 'floor': text_range[1] if text_range else None, 'text': counts['text'], 'kernel': counts['kernel'], 'lower_bound': lower_bound, 'generation': kind, 'line': line}

def judge(report, limit=OVERLAY_INTERP_LIMIT, per_frame=False):
    mode = PER_FRAME if per_frame else ABSOLUTE
    counts, why = place_counts(report)
    ranges = table = gen = None
    if counts is not None:
        gen, why = generation(report)
    if gen is not None and gen[0] == NOT_ENTERED:
        return _refused('the start never entered the game (preload.game_started 0), so by_place still holds the BIOS shell and nothing here is the game', limit, mode, NOT_ENTERED)
    if gen is not None:
        ranges, why = text_range(report)
    if gen is not None and why is None:
        table, why = table_fill(report, gen[2])
    if why is not None:
        return _refused(why, limit, mode, gen[0] if gen else None, gen[1] if gen else None)
    kind, entry, _ = gen
    frames = frame_count(report)
    if kind == AFTER_ENTRY and type(frames) is int and (frames > 0):
        frames -= entry
    return decide(counts, limit, ranges, miss_reasons(report), frames, per_frame, table, kind, entry)
USAGE = 'usage: overlay_only_gate.py REPORT.json [--limit N | --per-frame N]   (N: a count, 0 or more; --per-frame judges overlay entries per frame of the start)'

def _limit_value(text, whole):
    try:
        value = int(text) if whole else int(text) if text.lstrip('-').isdigit() else float(text)
    except ValueError:
        return None
    return value if _number(value) else None

def main(argv):
    args = list(argv)
    limit, per_frame = (OVERLAY_INTERP_LIMIT, False)
    flags = [flag for flag in ('--limit', '--per-frame') if flag in args]
    if len(flags) > 1:
        print(USAGE)
        return 64
    if flags:
        at = args.index(flags[0])
        per_frame = flags[0] == '--per-frame'
        value = _limit_value(args[at + 1], whole=not per_frame) if at + 1 < len(args) else None
        if value is None:
            print(USAGE)
            return 64
        limit = value
        del args[at:at + 2]
    if len(args) != 1:
        print(USAGE)
        return 64
    try:
        with open(args[0], encoding='utf-8') as stream:
            report = json.load(stream)
    except (OSError, ValueError) as error:
        result = _refused(f'the report {args[0]} cannot be read as JSON ({error})', limit, PER_FRAME if per_frame else ABSOLUTE)
    else:
        result = judge(report, limit, per_frame)
    print(result['line'])
    return EXIT_CODE[result['verdict']]
if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
