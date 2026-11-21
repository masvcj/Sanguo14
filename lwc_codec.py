"""Pure-Python codec for Koei Tecmo LWC\\x1a (LS11) containers used by SAN14 .s14 files."""

from __future__ import annotations

from pathlib import Path
import struct

LWC_MAGIC = b"LWC\x1a"
_LWC_HEADER_SIZE = 12 + 256

# Precompute bit encodings for codes 0..4095 so compression runs quickly in pure Python.
_CODE_BITS: list[int] = []
_CODE_LEN: list[int] = []
for _c in range(4096):
    _k = (_c + 2).bit_length() - 1
    _ones = _k - 1
    _rem = _c - ((1 << _k) - 2)
    _CODE_BITS.append((((1 << _ones) - 1) << (_k + 1)) | _rem)
    _CODE_LEN.append(_ones + 1 + _k)


class LwcFormatError(ValueError):
    """Raised when an LWC\\x1a stream is missing or malformed."""


def lwc_decompress(blob: bytes) -> tuple[bytes, bytes]:
    """Split an .s14 file into (prefix_header, decompressed_payload)."""
    pos = blob.find(LWC_MAGIC)
    if pos < 0:
        raise LwcFormatError("未找到 LWC 数据头，可能不是有效的三国志14 .s14 文件。")
    if pos + _LWC_HEADER_SIZE > len(blob):
        raise LwcFormatError("LWC 数据头不完整。")

    header = blob[:pos]
    uncomp_size, comp_size = struct.unpack_from("<II", blob, pos + 4)
    table = blob[pos + 12 : pos + 12 + 256]
    stream_start = pos + _LWC_HEADER_SIZE
    stream_end = stream_start + comp_size
    if stream_end > len(blob):
        raise LwcFormatError(
            f"LWC 压缩流长度异常：需要 {comp_size} 字节，实际剩余 {len(blob) - stream_start} 字节。"
        )
    stream = blob[stream_start:stream_end]
    total_bits = comp_size * 8
    bit_pos = 0
    out = bytearray()

    while len(out) < uncomp_size:
        n = 0
        while True:
            if bit_pos >= total_bits:
                raise LwcFormatError("LWC 比特流提前结束。")
            bit = (stream[bit_pos >> 3] >> (7 - (bit_pos & 7))) & 1
            bit_pos += 1
            if bit == 0:
                break
            n += 1
            if n > 24:
                raise LwcFormatError("LWC 编码长度超出合法范围。")
        k = n + 1
        if bit_pos + k > total_bits:
            raise LwcFormatError("LWC 比特流在读取数值位时提前结束。")
        rem = 0
        for _ in range(k):
            rem = (rem << 1) | ((stream[bit_pos >> 3] >> (7 - (bit_pos & 7))) & 1)
            bit_pos += 1
        code = ((1 << k) - 2) + rem

        if code < 256:
            out.append(table[code])
        else:
            offset = code - 256
            if offset <= 0 or offset > len(out):
                raise LwcFormatError(f"LWC 回溯偏移无效: offset={offset}, out_len={len(out)}")
            n2 = 0
            while True:
                if bit_pos >= total_bits:
                    raise LwcFormatError("LWC 比特流在读取匹配长度时提前结束。")
                bit = (stream[bit_pos >> 3] >> (7 - (bit_pos & 7))) & 1
                bit_pos += 1
                if bit == 0:
                    break
                n2 += 1
                if n2 > 24:
                    raise LwcFormatError("LWC 长度编码超出合法范围。")
            k2 = n2 + 1
            if bit_pos + k2 > total_bits:
                raise LwcFormatError("LWC 比特流在读取长度数值位时提前结束。")
            rem2 = 0
            for _ in range(k2):
                rem2 = (rem2 << 1) | ((stream[bit_pos >> 3] >> (7 - (bit_pos & 7))) & 1)
                bit_pos += 1
            length = ((1 << k2) - 2) + rem2 + 3
            for _ in range(length):
                out.append(out[-offset])
                if len(out) >= uncomp_size:
                    break

    return header, bytes(out)


def lwc_compress(raw: bytes) -> bytes:
    """Compress raw bytes into a valid Koei LWC\\x1a (LS11) block."""
    n = len(raw)
    counts = [0] * 256
    for byte in raw:
        counts[byte] += 1
    table = bytes(sorted(range(256), key=lambda x: (-counts[x], x)))
    rank = [0] * 256
    for idx, val in enumerate(table):
        rank[val] = idx

    out_bytes = bytearray()
    bit_buf = 0
    bit_cnt = 0

    def emit_code(code: int) -> None:
        nonlocal bit_buf, bit_cnt
        if code < 4096:
            val = _CODE_BITS[code]
            blen = _CODE_LEN[code]
        else:
            k = (code + 2).bit_length() - 1
            ones = k - 1
            rem = code - ((1 << k) - 2)
            val = (((1 << ones) - 1) << (k + 1)) | rem
            blen = ones + 1 + k
        bit_buf = (bit_buf << blen) | val
        bit_cnt += blen
        while bit_cnt >= 8:
            bit_cnt -= 8
            out_bytes.append((bit_buf >> bit_cnt) & 0xFF)
            bit_buf &= (1 << bit_cnt) - 1

    max_dist = 4096
    ht: dict[bytes, int] = {}
    i = 0
    while i < n:
        best_len = 0
        best_dist = 0
        if i + 3 <= n:
            if i > 0 and raw[i] == raw[i - 1] and raw[i + 1] == raw[i] and raw[i + 2] == raw[i]:
                b0 = raw[i]
                ln = 3
                limit = min(n - i, 4096)
                while ln < limit and raw[i + ln] == b0:
                    ln += 1
                best_len = ln
                best_dist = 1
            else:
                key = raw[i : i + 3]
                prev = ht.get(key)
                ht[key] = i
                if prev is not None:
                    dist = i - prev
                    if 1 <= dist <= max_dist:
                        ln = 3
                        limit = min(n - i, 258)
                        while ln < limit and raw[prev + ln] == raw[i + ln]:
                            ln += 1
                        if ln >= 4 or (ln == 3 and dist <= 64):
                            best_len = ln
                            best_dist = dist

        if best_len >= 3:
            emit_code(256 + best_dist)
            emit_code(best_len - 3)
            if best_len <= 16:
                for j in range(i + 1, min(i + best_len, n - 2)):
                    ht[raw[j : j + 3]] = j
            i += best_len
        else:
            if i + 3 <= n:
                ht[raw[i : i + 3]] = i
            emit_code(rank[raw[i]])
            i += 1

    if bit_cnt > 0:
        out_bytes.append((bit_buf << (8 - bit_cnt)) & 0xFF)

    return LWC_MAGIC + struct.pack("<II", n, len(out_bytes)) + table + bytes(out_bytes)


def unpack_s14_file(path: Path) -> tuple[bytes, bytes]:
    """Read and decompress an .s14 file into (header, raw_payload)."""
    return lwc_decompress(path.read_bytes())


def pack_s14_bytes(header: bytes, raw: bytes) -> bytes:
    """Re-pack header + raw payload into a complete .s14 byte image."""
    return header + lwc_compress(raw)
