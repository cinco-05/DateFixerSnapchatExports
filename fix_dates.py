"""
Snapchat/Immich date fixer.
Reads the date from each filename (YYYY-MM-DD_...), then writes that date into
the file's modified time AND inside the file's own metadata (MP4 video headers,
JPEG EXIF). Originals in input/ are never touched; fixed copies go to output/.
Uses only the Python standard library.
"""
import os, re, sys, shutil, struct, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(BASE, "input")
OUTPUT = os.path.join(BASE, "output")
UNMATCHED = os.path.join(OUTPUT, "_no_date_in_filename")
LOG = os.path.join(BASE, "log.txt")
NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[_\- ]")
UTC = dt.timezone.utc


def mp4_secs(d):
    return int((dt.datetime(d.year, d.month, d.day, 12, tzinfo=UTC)
                - dt.datetime(1904, 1, 1, tzinfo=UTC)).total_seconds())


def patch_mp4(path, d):
    """Rewrite creation/modification times in mvhd, tkhd and mdhd boxes."""
    secs = mp4_secs(d)
    n = 0
    with open(path, "r+b") as f:
        def patch(p):
            nonlocal n
            f.seek(p)
            ver = f.read(1)[0]
            f.seek(p + 4)
            f.write(struct.pack(">QQ", secs, secs) if ver == 1 else struct.pack(">II", secs, secs))
            n += 1

        def walk(start, end):
            pos = start
            while pos + 8 <= end:
                f.seek(pos)
                sz, typ = struct.unpack(">I4s", f.read(8))
                hl = 8
                if sz == 1:
                    sz = struct.unpack(">Q", f.read(8))[0]
                    hl = 16
                elif sz == 0:
                    sz = end - pos
                if sz < hl:
                    break
                if typ in (b"mvhd", b"tkhd", b"mdhd"):
                    patch(pos + hl)
                elif typ in (b"moov", b"trak", b"mdia"):
                    walk(pos + hl, min(pos + sz, end))
                pos += sz

        walk(0, os.path.getsize(path))
    return n > 0


def build_exif(d):
    s = d.strftime("%Y:%m:%d 12:00:00").encode() + b"\0"  # 20 bytes
    ifd0 = 8
    exif_ifd = ifd0 + 2 + 2 * 12 + 4
    s0 = exif_ifd + 2 + 2 * 12 + 4  # after Exif IFD
    # IFD0 data string is placed right after IFD0 header block, so compute carefully
    ifd0_end = ifd0 + 2 + 2 * 12 + 4
    dt_off = ifd0_end                      # DateTime string
    exif_ifd = dt_off + 20
    ex_end = exif_ifd + 2 + 2 * 12 + 4
    o1, o2 = ex_end, ex_end + 20
    b = b"II*\x00" + struct.pack("<I", 8)
    b += struct.pack("<H", 2)
    b += struct.pack("<HHII", 0x0132, 2, 20, dt_off)
    b += struct.pack("<HHII", 0x8769, 4, 1, exif_ifd)
    b += struct.pack("<I", 0)
    b += s
    b += struct.pack("<H", 2)
    b += struct.pack("<HHII", 0x9003, 2, 20, o1)
    b += struct.pack("<HHII", 0x9004, 2, 20, o2)
    b += struct.pack("<I", 0)
    b += s + s
    seg = b"Exif\0\0" + b
    return b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg


def patch_jpeg(path, d):
    """Insert EXIF date tags if the JPEG has no EXIF block. Returns True if added."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return False
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA:
            break
        seglen = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\0\0":
            return False  # already has EXIF; keep it
        pos += 2 + seglen
    ins = 2
    if data[2:4] == b"\xff\xe0":  # place after JFIF header
        ins = 4 + struct.unpack(">H", data[4:6])[0]
    with open(path, "wb") as f:
        f.write(data[:ins] + build_exif(d) + data[ins:])
    return True


def set_win_ctime(path, ts):
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.CreateFileW.restype = wintypes.HANDLE
        h = k.CreateFileW(path, 0x100, 0, None, 3, 0x80, None)
        if h in (-1, None, wintypes.HANDLE(-1).value):
            return
        t = ctypes.c_ulonglong(int((ts + 11644473600) * 10_000_000))
        k.SetFileTime(h, ctypes.byref(t), ctypes.byref(t), ctypes.byref(t))
        k.CloseHandle(h)
    except Exception:
        pass


def main():
    os.makedirs(INPUT, exist_ok=True)
    os.makedirs(OUTPUT, exist_ok=True)
    files = []
    for root, _, names in os.walk(INPUT):
        for n in names:
            if n.startswith(".") or n == "PUT_FILES_HERE.txt":
                continue
            files.append(os.path.join(root, n))
    if not files:
        print("No files found in the 'input' folder. Put your files there and run again.")
        return
    ok = bad = 0
    lines = []
    for i, src in enumerate(files, 1):
        name = os.path.basename(src)
        m = NAME_RE.match(name)
        if not m:
            os.makedirs(UNMATCHED, exist_ok=True)
            shutil.copy2(src, os.path.join(UNMATCHED, name))
            lines.append(f"NO DATE   {name}")
            bad += 1
            continue
        try:
            d = dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            os.makedirs(UNMATCHED, exist_ok=True)
            shutil.copy2(src, os.path.join(UNMATCHED, name))
            lines.append(f"BAD DATE  {name}")
            bad += 1
            continue
        dst = os.path.join(OUTPUT, name)
        shutil.copyfile(src, dst)
        note = []
        ext = name.lower().rsplit(".", 1)[-1]
        try:
            if ext in ("mp4", "mov", "m4v") and patch_mp4(dst, d):
                note.append("video-metadata")
            elif ext in ("jpg", "jpeg") and patch_jpeg(dst, d):
                note.append("exif")
        except Exception as e:
            note.append(f"metadata-skipped({e})")
        ts = dt.datetime(d.year, d.month, d.day, 12).timestamp()
        os.utime(dst, (ts, ts))
        set_win_ctime(dst, ts)
        note.append("file-date")
        lines.append(f"OK {d}  {name}  [{', '.join(note)}]")
        ok += 1
        if i % 25 == 0 or i == len(files):
            print(f"  {i}/{len(files)} done")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nFinished. Fixed: {ok}   No usable date: {bad}")
    print("Fixed files are in the 'output' folder. Details in log.txt.")
    if bad:
        print("Files without a date in the name were copied to output\\_no_date_in_filename.")


if __name__ == "__main__":
    main()
