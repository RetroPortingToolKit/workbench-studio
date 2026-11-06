from dataclasses import dataclass, field
from pathlib import Path
import re
DISC_SUFFIXES = {'.cue', '.iso', '.chd'}

@dataclass
class GameJob:
    name: str
    discs: list[Path]
    playlist: Path | None = None
    sbi: dict[Path, Path] = field(default_factory=dict)
    players: int | None = None
    player_metadata: dict = field(default_factory=dict)
    player_override: int | None = None
    player_error: str = ''

def validate_players(players):
    if type(players) is not int or not 1 <= players <= 8:
        raise ValueError('Players must be a whole number from 1 to 8')
    return players

def playlist_discs(path):
    discs = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        disc = Path(line.replace('\\', '/')).expanduser()
        if not disc.is_absolute():
            disc = path.parent / disc
        disc = disc.resolve()
        if disc.suffix.lower() not in DISC_SUFFIXES:
            raise ValueError(f'Playlist contains an unsupported image: {line}')
        if not disc.is_file():
            raise ValueError(f'Playlist disc not found: {disc}')
        if disc in discs:
            raise ValueError(f'Playlist repeats a disc: {disc.name}')
        discs.append(disc)
    if not discs:
        raise ValueError(f'Playlist contains no discs: {path.name}')
    return discs

def group_files(files, multiple_games=False, player_overrides=None):
    selected = list(dict.fromkeys((Path(p).expanduser().resolve() for p in files)))
    if not selected:
        return []
    for path in selected:
        if not path.is_file():
            raise ValueError(f'File not found: {path}')
        if path.suffix.lower() not in DISC_SUFFIXES | {'.m3u', '.sbi'}:
            raise ValueError(f'Choose CUE, ISO, CHD, M3U or SBI files: {path.name}')
    jobs = [GameJob(p.stem, playlist_discs(p), p) for p in selected if p.suffix.lower() == '.m3u']
    covered = {disc for job in jobs for disc in job.discs}
    singles = [p for p in selected if p.suffix.lower() in DISC_SUFFIXES and p not in covered]
    if multiple_games:
        jobs += [GameJob(p.stem, [p]) for p in singles]
    elif jobs:
        if len(jobs) > 1 or singles:
            raise ValueError('One Game accepts one playlist or the discs for one game. Choose Multiple Games for separate games.')
    elif singles:
        jobs = [GameJob(singles[0].stem, singles)]
    if not jobs:
        raise ValueError('Choose game images or a playlist alongside your SBI files')
    seen = set()
    for job in jobs:
        overlap = seen.intersection(job.discs)
        if overlap:
            raise ValueError('A disc belongs to more than one selected playlist: ' + next(iter(overlap)).name)
        seen.update(job.discs)
    for sbi in [p for p in selected if p.suffix.lower() == '.sbi']:
        matches = []
        for job in jobs:
            for disc in job.discs:
                stems = {disc.stem.casefold()}
                if disc.suffix.lower() == '.cue':
                    for track in re.findall('^\\s*FILE\\s+"([^"]+)"', disc.read_text(encoding='utf-8-sig'), re.M | re.I):
                        stems.add(Path(track).stem.casefold())
                if sbi.stem.casefold() in stems:
                    matches.append((job, disc))
        if len(matches) != 1:
            reason = 'matches more than one disc' if matches else 'has no matching disc filename'
            raise ValueError(f'SBI {sbi.name} {reason}')
        job, disc = matches[0]
        if disc in job.sbi:
            raise ValueError(f'More than one SBI selected for {disc.name}')
        job.sbi[disc] = sbi
    if player_overrides is not None:
        if not isinstance(player_overrides, list) or len(player_overrides) != len(jobs):
            raise ValueError('Choose one player override per game (Auto or 1-8)')
        for game, override in zip(jobs, player_overrides):
            game.player_override = None if override is None else validate_players(override)
    return jobs
