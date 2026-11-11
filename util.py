"""Small shared helpers."""
import logging
import os
import re


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def bar(done, total):
    if not total:
        return human(done)
    pct = min(100, done * 100 // total)
    return f"{'█' * (pct // 10)}{'░' * (10 - pct // 10)} {pct}%  ({human(done)}/{human(total)})"


def clean_name(s):
    """Safe single path segment: no directories, no odd characters, no leading dots."""
    s = os.path.basename(s.replace("\\", "/"))
    return re.sub(r"[^\w.\- ()\[\]]", "_", s).strip(". ")


def clean_path(s):
    """Safe multi-segment relative path (for caption destinations): every segment goes through
    clean_name, so no absolute paths, drive letters, backslash tricks or ../ traversal survive.
    Returns "" when nothing usable is left."""
    segs = [p for p in (clean_name(x) for x in s.replace("\\", "/").split("/")) if p and p not in (".", "..")]
    return "/".join(segs)


def env_int(name, default=0, lo=None, hi=None):
    """int(os.getenv(...)) that never crashes the bot over a typo in an optional variable."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logging.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        logging.warning("%s=%r is outside [%s, %s]; using %s", name, raw, lo, hi, default)
        return default
    return v


def eta(seconds):
    s = int(seconds)
    if s < 0 or s > 99 * 3600:
        return "?"
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")
