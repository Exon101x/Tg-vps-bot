"""Subscription plans: what users can buy (paid tiers + VPS instances to host their own bots).

A small catalog the bot owner configures (SUBSCRIPTION_PLANS, or the defaults below). Buying is a
classic assisted flow: /plans -> Buy -> the user pays however the owner accepts (PAYMENT_INFO holds
the links) -> /paid <reference> -> the owner (or an admin) approves -> the plan activates instantly
and VPS plans get their instances provisioned (hosting.py) with SSH credentials delivered to the buyer.

Source: https://github.com/Exon101x/Tg-vps-bot - fork it, sell your own plans, keep the link.
"""
import json
import logging
import os
import time
from dataclasses import dataclass

import store

REPO_URL = "https://github.com/Exon101x/Tg-vps-bot"
CURRENCY = (os.getenv("SUBSCRIPTION_CURRENCY", "USD").strip() or "USD")[:8]
PAYMENT_INFO = os.getenv("PAYMENT_INFO", "").strip()

# The default catalog: free forever, then paid tiers. Override everything with SUBSCRIPTION_PLANS
# (a JSON array of the same fields, e.g. [{"key": "pro", "name": "PRO", "price": 2.5, "days": 30,
# "max_servers": 15, "vps_slots": 0}, ...]). The free plan always exists - it is what users get
# without a subscription, and the fallback when a paid one expires.
FREE_KEY = "free"


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price: float = 0.0          # per period, in CURRENCY
    days: int = 30              # how long one payment lasts
    max_servers: int = 3        # saved servers (-1 = unlimited)
    vps_slots: int = 0          # included VPS instances
    vps_cpu: int = 1            # cores per instance
    vps_ram: int = 1024         # MB per instance
    vps_disk: int = 15          # GB per instance
    tag: str = ""               # little badge, e.g. "Most popular"
    desc: str = ""
    order: int = 0              # display order in /plans

    @property
    def cents(self):
        return round(self.price * 100)

    @property
    def free(self):
        return self.key == FREE_KEY or self.cents <= 0


def _default_plans():
    return [
        Plan(FREE_KEY, "Free", price=0, days=0, max_servers=3, vps_slots=0,
             desc="The bot as it is: servers, transfers, the Mini App.", order=0),
        Plan("pro", "PRO", price=2.5, days=30, max_servers=15, vps_slots=0, tag="Power users",
             desc="15 saved servers + everything in Free.", order=1),
        Plan("vps1", "VPS Starter", price=5, days=30, max_servers=25, vps_slots=1, vps_cpu=1, vps_ram=1024,
             vps_disk=15, tag="Host 1 bot", desc="1 included VPS (1 vCPU / 1 GB / 15 GB) to host your own bots 24/7.", order=2),
        Plan("vps2", "VPS Pro", price=10, days=30, max_servers=50, vps_slots=2, vps_cpu=2, vps_ram=2048,
             vps_disk=40, tag="Best value", desc="2 included VPSes (2 vCPU / 2 GB / 40 GB each) - host a whole bot farm.", order=3),
    ]


def _parse_plans(raw):
    """SUBSCRIPTION_PLANS (JSON array of dicts) -> Plan list. Bad input keeps the defaults:
    a typo must never take the bot down or empty the catalog."""
    try:
        items = json.loads(raw)
        if not isinstance(items, list) or not items:
            raise ValueError("not a non-empty list")
        plans = {}
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not isinstance(it.get("key"), str) or not it.get("name"):
                raise ValueError("every plan needs at least key and name")
            key = it["key"].strip().lower()[:32]
            if not key.isalnum():
                raise ValueError(f"bad plan key {it['key']!r} (letters/digits only)")
            days = max(0, int(it.get("days", 30)))
            plans[key] = Plan(
                key=key, name=str(it["name"])[:40],
                price=max(0.0, float(it.get("price", 0))), days=days,
                max_servers=int(it.get("max_servers", -1)), vps_slots=max(0, int(it.get("vps_slots", 0))),
                vps_cpu=max(1, int(it.get("vps_cpu", 1))), vps_ram=max(128, int(it.get("vps_ram", 1024))),
                vps_disk=max(1, int(it.get("vps_disk", 15))),
                tag=str(it.get("tag", ""))[:24], desc=str(it.get("desc", ""))[:200],
                order=int(it.get("order", i + 1)))
        if FREE_KEY not in plans:  # free is what everyone falls back to: always present
            plans[FREE_KEY] = _default_plans()[0]
        return sorted(plans.values(), key=lambda p: (p.free is False, p.order, p.key))
    except Exception as e:
        logging.warning("SUBSCRIPTION_PLANS is invalid (%s); using the default catalog", e)
        return _default_plans()


_RAW_PLANS = os.getenv("SUBSCRIPTION_PLANS", "").strip()
PLANS = _parse_plans(_RAW_PLANS) if _RAW_PLANS else _default_plans()
_PLAN_MAP = {p.key: p for p in PLANS}
FREE = _PLAN_MAP[FREE_KEY]


def plans(paid_only=False):
    return [p for p in PLANS if not paid_only or not p.free]


def plan(key):
    return _PLAN_MAP.get((key or "").strip().lower())


def payment_info(plan_):
    """What the buyer sees at checkout. PAYMENT_INFO can carry the owner's real links; the
    placeholders {amount}, {currency}, {plan} and {days} are replaced."""
    default = ("Pay {amount} {currency} however the owner accepts (UPI / PayPal / crypto - the links "
               "are in the channel bio or the pinned message), then send /paid <transaction reference> "
               "right here. The owner approves, your {plan} plan activates instantly.\n"
               f"Keep the receipt - support is in the channel or {REPO_URL}.")
    text = PAYMENT_INFO or default
    try:
        return text.format(amount=f"{plan_.price:g}", currency=CURRENCY, plan=plan_.name, days=plan_.days)
    except (KeyError, IndexError, ValueError):
        return text


# ---------------------------------------------------------------- subscription state
_cache = {}  # uid -> (plan_key, expires, checked_ts): expiry lookups stay cheap on hot paths


def effective(uid):
    """The plan that applies right now: the paid subscription while it lasts, Free afterwards."""
    now = int(time.time())
    hit = _cache.get(uid)
    if hit and now - hit[2] < 60:
        key, expires, _ = hit
        return _PLAN_MAP.get(key) or FREE if expires > now else FREE
    row = store.sub_get(uid)
    p = FREE
    if row and row["expires"] > now:
        p = _PLAN_MAP.get(row["plan"]) or FREE
    _cache[uid] = (p.key, row["expires"] if row else 0, now)
    return p


def sub_row(uid):
    """(plan, days_left) of the active subscription, or (Free, 0)."""
    row = store.sub_get(uid)
    if not row or row["expires"] <= int(time.time()):
        return FREE, 0
    return _PLAN_MAP.get(row["plan"]) or FREE, (row["expires"] - int(time.time())) // 86400


def activate(uid, plan_):
    """Approve a payment: start the plan now, or extend it from where it left off."""
    row = store.sub_get(uid)
    now = int(time.time())
    base = row["expires"] if row and row["expires"] > now else now
    expires = base + max(1, plan_.days) * 86400
    store.sub_set(uid, plan_.key, expires)
    _cache.pop(uid, None)
    return expires


def max_servers(uid):
    p = effective(uid)
    return p.max_servers, p


def server_limit_reached(uid):
    """True when the user's plan cannot take another saved server."""
    p = effective(uid)
    if p.max_servers < 0:
        return False, p
    return len(store.list_servers(uid)) >= p.max_servers, p


def vps_slots_left(uid):
    p = effective(uid)
    if p.vps_slots <= 0:
        return 0, p
    return p.vps_slots - store.alloc_active_count(uid), p


# ---------------------------------------------------------------- order lifecycle
# pending -> awaiting (reference submitted, admin decides) -> approved / rejected
def order_create(uid, plan_):
    for o in store.order_list(uid, 5):
        if o["status"] == "pending":  # one open basket at a time keeps /paid unambiguous
            if o["plan"] == plan_.key:
                return o, False
            store.order_set(o["id"], status="cancelled", decided=int(time.time()))
    return store.order_get(store.order_add(uid, plan_.key, plan_.cents, CURRENCY)), True


def order_submit_ref(uid, ref):
    """Attach a payment reference to the newest pending order -> awaiting the owner's approval."""
    ref = " ".join((ref or "").split())[:128]
    if not ref:
        return None, "Send the reference after the command: /paid <transaction id or UTR>"
    for o in store.order_list(uid, 5):
        if o["status"] == "pending":
            store.order_set(o["id"], ref=ref, status="awaiting")
            return store.order_get(o["id"]), None
        if o["status"] == "awaiting":
            return o, "Your previous order is already waiting for approval - the owner will see it."
    return None, "No pending order. Pick a plan first: /plans"


def order_decide(oid, approve, admin_uid):
    """Admin decision. Returns (order, plan, error): the caller provisions VPS plans after approve."""
    o = store.order_get(oid)
    if not o:
        return None, None, "no such order"
    if o["status"] != "awaiting":
        return o, None, f"order #{oid} is already {o['status']}"
    p = plan(o["plan"])
    if not p or p.free:
        return o, None, f"unknown plan {o['plan']!r}"
    store.order_set(oid, status="approved" if approve else "rejected", decided=int(time.time()))
    store.log(admin_uid, "admin", f"order #{oid} ({p.name}, {o['cents'] / 100:g} {o['currency']}) "
                                  f"{'approved' if approve else 'rejected'} for user {o['uid']}")
    return store.order_get(oid), p, None


# ---------------------------------------------------------------- admin stats
def stats():
    counts, (approved, cents) = store.order_stats()
    subs = {k: 0 for k in _PLAN_MAP}
    expired = 0
    now = int(time.time())
    # one sub row per user: count the active ones per plan
    for row in store.users(5000):
        s = store.sub_get(row["uid"])
        if s and s["expires"] > now:
            subs[s["plan"]] = subs.get(s["plan"], 0) + 1
        elif s:
            expired += 1
    total_allocs, active_allocs = store.alloc_counts()
    return {
        "orders": counts, "approved": approved,
        "revenue": f"{cents / 100:.2f} {CURRENCY}" if cents else f"0 {CURRENCY}",
        "subs_active": sum(subs.values()), "subs_expired": expired, "per_plan": subs,
        "vps_total": total_allocs, "vps_active": active_allocs,
    }


def plan_json(p, current=False):
    """A plan as the Mini App sees it."""
    return {"key": p.key, "name": p.name, "price": p.price, "cents": p.cents, "currency": CURRENCY,
            "days": p.days, "max_servers": p.max_servers, "vps_slots": p.vps_slots, "vps_cpu": p.vps_cpu,
            "vps_ram": p.vps_ram, "vps_disk": p.vps_disk, "tag": p.tag, "desc": p.desc,
            "free": p.free, "current": current}
