"""VPS instances for subscribers: provision, renew, deprovision - over SSH on the owner's host node.

The node is configured by the OWNER with env vars (VPS_NODE_HOST and friends) - it is the bot's own
infrastructure, never a user-supplied address. By default each instance is a locked-down Linux user
with a disk quota; override the templates (VPS_PROVISION_CMD / VPS_DEPROVISION_CMD) to create LXC
containers, Docker sandboxes or Proxmox CTs instead. Whatever the templates do, credentials are
alnum-only and Fernet-encrypted at rest, so nothing here can inject into a shell.

Source: https://github.com/Exon101x/Tg-vps-bot - fork it, run it on your own node.
"""
import os
import secrets
import string
import time

import store
from remote import Remote
from util import env_int

NODE_HOST = os.getenv("VPS_NODE_HOST", "").strip()
NODE_PORT = env_int("VPS_NODE_PORT", 22, lo=1, hi=65535)
NODE_USER = (os.getenv("VPS_NODE_USER", "root").strip() or "root")[:32]
NODE_PASS = os.getenv("VPS_NODE_PASS", "")
NODE_KEY = os.getenv("VPS_NODE_KEY", "").strip()          # path to a private key file
PUBLIC_HOST = (os.getenv("VPS_PUBLIC_HOST", "").strip() or NODE_HOST)[:255]  # what users connect to (NAT)
SSH_PORT = env_int("VPS_SSH_PORT", 22, lo=1, hi=65535)    # the SSH port of the instances themselves
PROVISION_CMD = os.getenv("VPS_PROVISION_CMD",
                          "useradd -m -s /bin/bash {user} && echo '{user}:{pass}' | chpasswd"
                          " && (setquota -u {user} {disk}G {disk}G 0 0 / 2>/dev/null || true)")
DEPROVISION_CMD = os.getenv("VPS_DEPROVISION_CMD", "userdel -rf {user}")
SAFE_USER = "abcdefghijklmnopqrstuvwxyz0123456789"        # usernames: lowercase alnum only
PASS_ALPHABET = string.ascii_letters + string.digits      # passwords: alnum only -> no shell quoting issues


def configured():
    return bool(NODE_HOST and (NODE_PASS or NODE_KEY))


def node_config():
    """Connection dict for Remote (remote.py), or None when the node is not configured."""
    if not configured():
        return None
    secret = NODE_PASS
    if NODE_KEY:
        with open(NODE_KEY) as f:  # read lazily: the file may appear after the bot started
            secret = f.read()
    return dict(uid=0, name="host-node", scheme="ssh", host=NODE_HOST, port=NODE_PORT, user=NODE_USER,
                auth="key" if NODE_KEY else "password", secret=secret, cwd="", hostkey="")


def _sudo(cmd):
    return cmd if NODE_USER == "root" else "sudo -n " + cmd


def _render(tpl, user, pwd, cpu, ram, disk):
    """Format a template with strictly-typed values: username/password are alnum, specs are ints,
    so the rendered command can never smuggle in shell metacharacters from user land. (`pass` is a
    reserved word, hence the dict instead of keyword arguments.)"""
    vals = {"user": user, "pass": pwd, "cpu": int(cpu), "ram": int(ram), "disk": int(disk)}
    return _sudo(tpl.format(**vals))


def new_credentials(uid):
    """(username, password) for a new instance. Lowercase alnum, starts with a letter, 6-16 chars;
    password: 16 chars of letters+digits (~95 bits)."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    user = ("tg" + str(uid)[-4:].lower() + "".join(secrets.choice(SAFE_USER) for _ in range(6)))[:15]
    user += secrets.choice(letters)  # end with a letter: nicer usernames, no numeric-suffix tricks
    pwd = "".join(secrets.choice(PASS_ALPHABET) for _ in range(16))
    return user, pwd


def provision(uid, plan, expires):
    """Create the instance on the node and record it. Returns the allocation (password included) or
    raises with the node's output."""
    c = node_config()
    if not c:
        raise RuntimeError("the host node is not configured (VPS_NODE_HOST is unset)")
    user, pwd = new_credentials(uid)
    cmd = _render(PROVISION_CMD, user, pwd, plan.vps_cpu, plan.vps_ram, plan.vps_disk)
    with Remote(c) as r:
        out, rc = r.exec(cmd, 60)
    if rc:
        raise RuntimeError(f"provisioning failed (rc={rc}): {out.strip()[:400]}")
    aid = store.alloc_add(uid, plan.key, PUBLIC_HOST, SSH_PORT, user, pwd,
                          plan.vps_cpu, plan.vps_ram, plan.vps_disk, expires)
    store.log(uid, "host-node", f"provisioned {user} ({plan.vps_cpu}c/{plan.vps_ram}M/{plan.vps_disk}G) for {plan.name}")
    return store.alloc_get(aid)


def deprovision(alloc, reason="expired"):
    """Remove the instance from the node. The allocation row stays (status expired/revoked) so the
    username is never handed out again soon and the buyer keeps the receipt."""
    note = ""
    try:
        c = node_config()
        if c:
            with Remote(c) as r:
                out, rc = r.exec(_render(DEPROVISION_CMD, alloc["user"], alloc["secret"],
                                         alloc["cpu"], alloc["ram"], alloc["disk"]), 60)
            if rc:
                note = f" (node said: {out.strip()[:200]})"
    except Exception as e:
        note = f" (node unreachable: {type(e).__name__})"
    store.alloc_set(alloc["id"], status="revoked" if reason == "revoked" else "expired")
    store.log(alloc["uid"], "host-node", f"deprovisioned {alloc['user']} ({reason}){note}")
    return note


def sweep():
    """Called from the monitor loop: instances whose subscription ran out are removed from the node.
    Returns [(uid, text)] - the caller (async side) delivers the notes."""
    out = []
    for alloc in store.allocs_expiring(int(time.time())):
        note = deprovision(alloc, "expired")
        out.append((alloc["uid"],
                    f"⏰ Your VPS {alloc['user']}@{alloc['host']} was removed - the subscription ran out.\n"
                    f"Want it back? /plans - a new payment brings a fresh one back in minutes.{note}"))
    return out
