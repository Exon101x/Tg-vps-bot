"""Small shared helpers."""
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


def eta(seconds):
    s = int(seconds)
    if s < 0 or s > 99 * 3600:
        return "?"
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")
