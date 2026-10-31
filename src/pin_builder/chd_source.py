from __future__ import annotations
from pathlib import Path
from typing import Any, Optional
RAW = 2352
USER = 2048
USER_OFFSETS = (24, 16)

class ChdTrackSource:

    def __init__(self, chd: Path, psx_chd: Any, lib_path: Path, track_number: Optional[int]=None):
        self.chd = Path(chd)
        self.psx_chd = psx_chd
        self._disc = psx_chd.ChdDisc(self.chd, psx_chd.LibChdr(lib_path))
        tracks = [t for t in self._disc.tracks if t.type in ('MODE1_RAW', 'MODE2_RAW')]
        if not tracks:
            self._disc.close()
            raise ValueError(f'{self.chd.name} has no data track')
        self.track = next((t for t in tracks if t.number == track_number), tracks[0])
        self.frames = self.track.frames
        self._user_offset = self._detect_user_offset()

    def close(self) -> None:
        if self._disc is not None:
            self._disc.close()
            self._disc = None

    def __enter__(self) -> 'ChdTrackSource':
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _raw_sector(self, lba: int) -> bytes:
        if lba < 0 or lba >= self.frames:
            raise KeyError(lba)
        frame = self.track.chd_frame_start + lba
        hunk_index = frame // self._disc.frames_per_hunk
        within = frame % self._disc.frames_per_hunk
        hunk = self._disc._hunk(hunk_index)
        start = within * self.psx_chd.CD_FRAME_SIZE
        return hunk[start:start + RAW]

    def _detect_user_offset(self) -> int:
        raw = self._raw_sector(16)
        for offset in USER_OFFSETS:
            if len(raw) >= offset + USER and raw[offset + 1:offset + 6] == b'CD001':
                return offset
        return USER_OFFSETS[0]

    def read_user(self, _data: Any, lba: int) -> bytes:
        raw = self._raw_sector(lba)
        return raw[self._user_offset:self._user_offset + USER]

    def read_data_track(self) -> bytes:
        out = bytearray()
        for chunk in self._disc.iter_track_chunks(self.track):
            out += chunk
        return bytes(out)
