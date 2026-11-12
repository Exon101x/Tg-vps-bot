"""Bot state that used to live in plain dicts and died on every restart.

Five machines held their working set only in RAM: confirmations, pending
passwords, /multi collections, /ask pickers, terminal mode. A deploy or crash
mid-flow lost them; worse, the flow silently continued as if nothing happened
(a /term user's next message ran as a shell command after a restart). Each
machine now keeps its data in one named kv blob that is written through to the
database and re-hydrated on boot, so the bot resumes instead of forgetting.

Values must be plain JSON (no message objects, no closures) and every entry
carries its own expiry; sweep() drops what is stale, so the blob cannot grow
without bound. Everything is encrypted at rest by store's Fernet key, so a
password waiting for its server is never plaintext in the database.
"""
import json
import logging
import time

import store

log = logging.getLogger("persist")

DEBOUNCE = 3.0  # s between blob writes; flush() forces one for shutdown / tests


def _now():
    return time.time()


class State:
    """One named JSON document, persisted to the kv table behind a debounce.

    A plain dict subclass would invite storing live objects; the deliberate
    friction of .get()/.pop()/.__setitem__() only, plus a serialisation check
    on every write, keeps the blob reloadable forever.
    """

    def __init__(self, name, ttl=None, preload=True):
        self.name, self.ttl = name, ttl
        self._data, self._dirty, self._last = {}, False, 0.0
        if preload:
            self.load()

    # ---- lifecycle
    def load(self):
        """Hydrate from the database (encrypted kv blob). A corrupt blob is logged and
        dropped, never fatal: the bot starts with empty state rather than not at all."""
        try:
            raw = store.load_blob(self.name)
            self._data = json.loads(raw) if raw else {}
        except Exception as e:
            log.warning("state %s unreadable (%s); starting empty", self.name, e)
            self._data = {}
        self._dirty, self._last = False, 0.0

    def flush(self, force=False):
        """Write the blob back if dirty. Debounced: a busy /multi session edits its
        entry on every part, and one write per DEBOUNCE seconds is plenty."""
        if not self._dirty:
            return
        if not force and _now() - self._last < DEBOUNCE:
            return
        try:
            store.save_blob(self.name, json.dumps(self._data).encode())  # fails here, not in the database
            self._dirty, self._last = False, _now()
        except Exception as e:
            log.error("state %s not saved (%s)", self.name, e)

    def touch(self):
        """Mark dirty after mutating a nested structure in place (State cannot see that)."""
        self._dirty = True
        self.flush()

    # ---- housekeeping
    def sweep(self, now=None):
        """Drop entries whose own 'ts' (or the entry itself, for bare timestamps like TERM)
        is older than ttl; called from the monitor loop so the blob shrinks as flows expire
        even if the user never returns."""
        if not self.ttl:
            return
        now = now or _now()
        stale = []
        for k, v in self._data.items():
            ts = v.get("ts", now) if isinstance(v, dict) else v if isinstance(v, (int, float)) else now
            if now - ts > self.ttl:
                stale.append(k)
        for k in stale:
            del self._data[k]
            self._dirty = True
        if stale:
            self.flush(force=True)

    # ---- dict-like access (the only surface the bot is allowed to use)
    def get(self, key, default=None):
        return self._data.get(self._k(key), default)

    def __contains__(self, key):
        return self._k(key) in self._data

    def __getitem__(self, key):
        return self._data[self._k(key)]

    def __setitem__(self, key, value):
        self._data[self._k(key)] = value
        self._dirty = True
        self.flush()

    def __delitem__(self, key):
        k = self._k(key)
        if k in self._data:
            del self._data[k]
            self._dirty = True
            self.flush()

    def pop(self, key, default=None):
        k = self._k(key)
        if k in self._data:
            self._dirty = True
            v = self._data.pop(k)
            self.flush()
            return v
        return default

    def setdefault(self, key, default):
        k = self._k(key)
        if k not in self._data:
            self._data[k] = default
            self._dirty = True
            self.flush()
        return self._data[k]

    def items(self):
        return self._data.items()

    def keys(self):
        return self._data.keys()

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    @staticmethod
    def _k(key):
        """JSON turns int keys into strings on reload; writing them as strings from the start
        keeps an in-session State and a reloaded one answering the same lookups."""
        return key if isinstance(key, str) else str(key)


def sweep_all(states):
    """One sweep over many States (the monitor loop calls it once per cycle)."""
    for s in states:
        try:
            s.sweep()
        except Exception:
            log.exception("sweeping %s failed", s.name)
