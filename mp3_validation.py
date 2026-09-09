"""Incremental validation of the exact MPEG-1 Layer III / 44.1 kHz output contract."""
from media_jobs import JobError


class MP3Validator:
    def __init__(self):
        self.pending = bytearray()
        self.frames = 0

    def feed(self, chunk):
        self.pending.extend(chunk)
        pos = 0
        rates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
        while len(self.pending) - pos >= 4:
            a, b, c, _ = self.pending[pos:pos+4]
            index = c >> 4
            if a != 255 or b & 0xFE != 0xFA or c & 0x0C or not 0 < index < 15:
                raise JobError('INVALID_MP3', 'Invalid MP3 frame inside the completed audio.')
            length = 144000 * rates[index] // 44100 + ((c >> 1) & 1)
            if len(self.pending) - pos < length:
                break
            pos += length
            self.frames += 1
        del self.pending[:pos]

    def finish(self):
        if self.pending or self.frames < 2:
            raise JobError('TRUNCATED_MP3', 'The MP3 ended inside a frame or contains too little audio.')
        return self.frames * 1152 / 44100
