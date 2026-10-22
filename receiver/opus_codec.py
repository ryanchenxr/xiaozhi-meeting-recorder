"""Minimal ctypes binding to the local libopus (Homebrew). No pip install needed.

Decoding is used at finish to turn the device's framed Opus stream into the WAV
handed to the existing pipeline; encoding is only for local synthetic tests.
"""
import ctypes, ctypes.util, os, struct

RATE, CHANNELS, FRAME = 16000, 1, 960   # 60 ms packets from meeting.16
MAX_PACKET = 1275
APPLICATION_VOIP = 2048
_lib = None

def _load():
    global _lib
    if _lib is not None: return _lib
    candidates = [os.environ.get('MEETING_LIBOPUS'), ctypes.util.find_library('opus'),
                  '/opt/homebrew/lib/libopus.dylib', '/usr/local/lib/libopus.dylib']
    for path in filter(None, candidates):
        try: lib = ctypes.CDLL(path)
        except OSError: continue
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_encoder_ctl.restype = ctypes.c_int
        lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]   # one variadic int follows
        _lib = lib
        return lib
    raise OSError('libopus not available')

def split_frames(data):
    """Framed stream [u16 LE length][packet]... -> list of packets. ValueError if malformed."""
    packets, i = [], 0
    while i < len(data):
        if i + 2 > len(data): raise ValueError('truncated_header')
        n = data[i] | (data[i + 1] << 8); i += 2
        if not 1 <= n <= MAX_PACKET or i + n > len(data): raise ValueError('bad_packet_length')
        packets.append(data[i:i + n]); i += n
    if not packets: raise ValueError('empty')
    return packets

def frame(packets):
    return b''.join(struct.pack('<H', len(p)) + p for p in packets)

def decode(packets):
    """Decode each packet to exactly FRAME samples; returns little-endian int16 PCM bytes."""
    lib = _load(); err = ctypes.c_int(0)
    st = lib.opus_decoder_create(RATE, CHANNELS, ctypes.byref(err))
    if not st or err.value != 0: raise OSError('decoder_create_failed')
    try:
        out = bytearray(); buf = (ctypes.c_int16 * FRAME)()
        for p in packets:
            n = lib.opus_decode(st, bytes(p), len(p), buf, FRAME, 0)
            if n != FRAME: raise ValueError('decode_failed')
            out += bytes(buf)
        return bytes(out)
    finally:
        lib.opus_decoder_destroy(st)

def encode(pcm, bitrate=None, cbr=False):
    """Test helper: int16 LE mono PCM (multiple of FRAME samples) -> list of packets.
    bitrate/cbr let tests match the firmware's 32 kbit/s packet sizes."""
    lib = _load(); err = ctypes.c_int(0)
    st = lib.opus_encoder_create(RATE, CHANNELS, APPLICATION_VOIP, ctypes.byref(err))
    if not st or err.value != 0: raise OSError('encoder_create_failed')
    try:
        if bitrate and lib.opus_encoder_ctl(st, 4002, ctypes.c_int32(bitrate)) != 0: raise OSError('set_bitrate_failed')
        if cbr and lib.opus_encoder_ctl(st, 4006, ctypes.c_int32(0)) != 0: raise OSError('set_vbr_failed')
        packets, step = [], FRAME * 2; out = ctypes.create_string_buffer(1500)
        for i in range(0, len(pcm), step):
            block = pcm[i:i + step]
            samples = (ctypes.c_int16 * FRAME).from_buffer_copy(block.ljust(step, b'\0'))
            n = lib.opus_encode(st, samples, FRAME, out, 1500)
            if n <= 0: raise ValueError('encode_failed')
            packets.append(out.raw[:n])
        return packets
    finally:
        lib.opus_encoder_destroy(st)
