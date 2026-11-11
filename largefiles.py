"""Files bigger than Telegram's per-file limit (2 GB): volume planning for /get and the server-side join script."""
import re
import shlex

MIN_VOLUME = 1024 * 1024
MAX_VOLUMES = 999  # volume names have three digits: name.001 ... name.999


def choose_volume(tg_max, free_bytes, override=None):
    """Volume size in bytes: Telegram's limit, or the user's -v value, shrunk to fit 85% of the free disk."""
    size = min(tg_max, override) if override else tg_max
    size = min(size, int(free_bytes * 0.85))
    if size < MIN_VOLUME:
        raise ValueError("not enough free disk space in the temp directory to stage even a 1 MiB volume")
    return size


def plan_volumes(total, volume):
    """[(index, offset, length)] covering `total` bytes in volumes of at most `volume` bytes."""
    plan = [(i, off, min(volume, total - off)) for i, off in enumerate(range(0, total, volume))]
    if len(plan) > MAX_VOLUMES:
        raise ValueError(f"{len(plan)} volumes would be needed (limit {MAX_VOLUMES}): use a larger -v or free up disk space")
    return plan


def volume_name(name, index):
    """0 -> name.001: the naming 7-Zip, `split --numeric-suffixes=1 -a 3` and /join all understand."""
    return f"{name}.{index + 1:03d}"


def join_script(base, remove=False):
    """Shell script that concatenates base.001, base.002, ... into base (checks everything first, verifies the size)."""
    b = shlex.quote(base)
    script = f'''B={b}
vol() {{ printf '%s.%03d' "$B" "$1"; }}
i=1; sum=0
while [ -f "$(vol $i)" ]; do sum=$((sum + $(wc -c < "$(vol $i)"))); i=$((i + 1)); done
n=$((i - 1))
if [ "$n" -eq 0 ]; then echo "no volumes named $B.001, $B.002, ... in $(pwd)"; exit 2; fi
total=$(ls -1 "$B".[0-9][0-9][0-9] 2>/dev/null | wc -l)
if [ "$total" -ne "$n" ]; then echo "volumes are not consecutive: found $total files but only $n in a row from .001 (a volume is missing)"; exit 7; fi
if [ -e "$B" ]; then echo "$B already exists; move it away first"; exit 3; fi
avail=$(df -Pk . | awk 'NR==2{{print $4 * 1024}}')
if [ "$avail" -lt "$sum" ]; then echo "not enough free space: need $sum bytes, have $avail"; exit 4; fi
cat "$B".[0-9][0-9][0-9] > "$B.joining" || {{ rm -f "$B.joining"; echo "join failed"; exit 5; }}
got=$(wc -c < "$B.joining")
if [ "$got" -ne "$sum" ]; then rm -f "$B.joining"; echo "size mismatch ($got of $sum bytes); nothing was changed"; exit 6; fi
mv "$B.joining" "$B" && echo "joined $n volumes into $B ($got bytes)"
'''
    if remove:
        script += 'rm -f -- "$B".[0-9][0-9][0-9] && echo "volumes removed"\n'
    return script


_VOLUME = re.compile(r"^(.*)\.(\d{3})$")


def order_volumes(items):
    """items: [(message_id, filename, size)] collected by /multi. Returns (ordered items, base name or None, problem or None).
    Files named name.001, name.002, ... are ordered by number and checked for gaps; anything else keeps the order sent."""
    matches = [_VOLUME.match(fn) for _, fn, _ in items]
    if not all(matches):
        return sorted(items, key=lambda it: it[0]), None, None
    bases = {mt.group(1) for mt in matches}
    if len(bases) > 1:
        return items, None, "these volumes belong to different files: " + ", ".join(sorted(bases)) + ". Use /cancel and start again."
    numbers = sorted(int(mt.group(2)) for mt in matches)
    if len(set(numbers)) != len(numbers):
        dupes = sorted({n for n in numbers if numbers.count(n) > 1})
        return items, None, f"volume number(s) {', '.join(f'{n:03d}' for n in dupes)} were sent twice. /cancel and start again."
    missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
    if missing:
        return items, None, f"volume(s) {', '.join(f'{n:03d}' for n in missing[:10])} are missing: send them, then /done again."
    ordered = sorted(items, key=lambda it: int(_VOLUME.match(it[1]).group(2)))
    return ordered, bases.pop(), None
