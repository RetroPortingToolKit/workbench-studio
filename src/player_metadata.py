"""Resolve controller slots from the disc serial, with known kit declarations first."""
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import urllib.request

from selection import validate_players

CATALOG = Path(__file__).parent / 'compatibility' / 'players.json'
CACHE_SCHEMA = 2


class Cells(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells, self.parts = [], None

    def handle_starttag(self, tag, attrs):
        if tag == 'td':
            self.parts = []

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.parts is not None:
            self.cells.append(' '.join(' '.join(self.parts).split()))
            self.parts = None


def parse_page(serial, text):
    parser = Cells()
    parser.feed(text)
    cells = parser.cells
    def value(label, default=None):
        try:
            return cells[cells.index(label) + 1]
        except (ValueError, IndexError):
            if default is not None:
                return default
            raise ValueError(f'Player metadata for {serial} has no {label} field') from None
    if serial not in re.findall(r'\b[A-Z]{4}-\d{5}\b', value('Serial Number(s)')):
        raise ValueError(f'Player metadata does not match disc serial {serial}')
    match = re.fullmatch(r'(\d)(?:\s*(?:-|to|or)\s*(\d))?\s+Players?', value('Number Of Players'), re.I)
    if not match:
        raise ValueError(f'Player count for {serial} needs review')
    count = validate_players(int(match[2] or match[1]))
    multitap = value('Multi-Tap Function Compatible', 'No')
    if multitap.lower().startswith('yes'):
        tap = re.search(r'(\d)(?:\s*(?:-|to|or)\s*(\d))?\s+Players?', multitap, re.I)
        if tap:
            count = max(count, validate_players(int(tap[2] or tap[1])))
    return count


def resolve(serial, data_root):
    if not re.fullmatch(r'[A-Z]{4}-\d{5}', serial):
        raise ValueError('Could not identify the disc boot serial for player metadata')
    from pin_builder import pin_build as pb
    cache_root = Path(os.environ.get('WORKBENCH_PUBLIC_CACHE_ROOT', str(Path(data_root) / 'cache')))
    kit = pb.find_kit([serial], [cache_root / 'pins' / 'recipes'])
    if kit:
        count = pb.declared_players(kit)
        if count is not None:
            return dict(serial=serial, players=validate_players(count), source='matched kit project-manifest.toml')
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
    if serial in catalog:
        record = catalog[serial]
        return dict(serial=serial, players=validate_players(record['players']), source=record['source'])
    cache = Path(data_root) / 'player-metadata' / (serial + '.json')
    if cache.is_file():
        try:
            record = json.loads(cache.read_text(encoding='utf-8'))
            if isinstance(record, dict) and record.get('schema') == CACHE_SCHEMA and record['serial'] == serial and record['source'] == f'https://psxdatacenter.com/games/{serial}.html':
                validate_players(record['players'])
                return record
        except (ValueError, KeyError, TypeError):
            pass
    url = f'https://psxdatacenter.com/games/{serial}.html'
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = response.read(2 * 1024 * 1024)
            # Some pages contain malformed UTF-16 with misaligned line endings.
            # The serial/player table labels are ASCII; remove zero padding.
            text = data.replace(b'\x00', b'').decode('windows-1252', errors='replace')
        record = dict(schema=CACHE_SCHEMA, serial=serial, players=parse_page(serial, text), source=url)
    except (OSError, ValueError) as error:
        raise ValueError(f'Could not identify player count for {serial}: {error}. Choose a player override or retry Auto; no player count was guessed.') from error
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


def identify(jobs, data_root, log=lambda line: None, *, allow_unknown=False):
    import artwork
    Path(data_root).mkdir(parents=True, exist_ok=True)
    for game in jobs:
        serial = artwork.detect(game.discs[0], data_root)
        try:
            if game.player_override is not None:
                record = dict(serial=serial, players=validate_players(game.player_override), source='manual override')
            else:
                record = resolve(serial, data_root)
        except ValueError as error:
            if not allow_unknown:
                raise
            game.players, game.player_metadata, game.player_error = None, dict(serial=serial), str(error)
            continue
        game.players, game.player_metadata = record['players'], record
        game.player_error = ''
        log(f"{game.name}: {game.players} player(s), serial {serial}; metadata: {record['source']}")
