#!/usr/bin/env python3
"""Gateway setup - configure the payment gateways of this bot in one guided run.

The bot takes payments three ways (see payments.py); this tool sets up any of them,
live-tests what it can, and writes the result to your .env:

  UPI Gateway   your self-hosted multi-merchant UPI gateway (github.com/Exon101x/upi-gateway).
                Buyers get its checkout page (dynamic QR + one-tap UPI app links) and a
                verified webhook activates the plan the moment the payment confirms.

  UPI direct    your own UPI id: a upi://pay QR + app button. No bank feed, so the buyer
                sends /paid <UTR> and an admin confirms - the assisted flow, one tap away.

  Crypto        CryptAPI (cryptapi.io, 100+ coins, no signup): each order mints a payment
                address, on-chain confirmation auto-activates the plan.

Usage:
  python3 setup_gateway.py                     # the guided setup (what you usually want)
  python3 setup_gateway.py --check             # diagnose the current setup, change nothing
  python3 setup_gateway.py --gen-secret        # print a strong webhook/API secret
  python3 setup_gateway.py --qr "upi://..."    # render any text as a QR png (qr_test.png)
  python3 setup_gateway.py --write KEY=V ...   # validated batch write (scripted setups)
  options: --env PATH (default .env), --yes (skip confirmations), --no-probe (no live calls)

Everything it writes goes to the .env file only; secrets are masked in its own output.
The bot reads .env at startup (real environment variables win), so restart it afterwards.

Source: https://github.com/Exon101x/Tg-vps-bot - fork it, keep the link, sell your own plans.
"""
import argparse
import io
import json
import os
import re
import secrets
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = ".env"  # default target; the bot's load_dotenv() reads exactly this

# The keys this tool may write, by payment method. Anything else in --write is a typo.
METHODS = {
    "upigw": {
        "title": "UPI Gateway (auto-confirm UPI)",
        "docs": "github.com/Exon101x/upi-gateway",
        "required": ["UPIGW_URL", "UPIGW_API_KEY", "UPIGW_API_SECRET", "UPIGW_WEBHOOK_SECRET"],
        "optional": ["UPIGW_MAX_SKEW"],
        "needs_webapp": True,
    },
    "upi": {
        "title": "UPI direct (your UPI id, confirm via /paid)",
        "docs": "any UPI app: GPay / PhonePe / Paytm",
        "required": ["UPI_VPA"],
        "optional": ["UPI_NAME"],
        "needs_webapp": False,
    },
    "crypt": {
        "title": "Crypto via CryptAPI (auto-confirm, your own wallets)",
        "docs": "cryptapi.io",
        "required": ["CRYPTAPI_ADDR"],  # pseudo-key: at least one CRYPTAPI_<COIN>_ADDR
        "optional": ["CRYPTAPI_BASE", "CRYPTAPI_TOLERANCE", "CRYPTAPI_CONFIRMATIONS", "CRYPTAPI_API_KEY"],
        "needs_webapp": True,
    },
}
WRITABLE = {k for m in METHODS.values() for k in m["required"] + m["optional"]} - {"CRYPTAPI_ADDR"}
WRITABLE.add("WEBAPP_URL")

VPA_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-_]{1,63}@[a-zA-Z][a-zA-Z0-9.\-]{1,31}$")
URL_RE = re.compile(r"^https?://[a-zA-Z0-9.\-]+(:\d{1,5})?(/.*)?$")

C = {"g": "\033[92m", "y": "\033[93m", "r": "\033[91m", "b": "\033[96m", "d": "\033[2m", "0": "\033[0m"}


def paint(s, k):
    return f"{C[k]}{s}{C['0']}" if sys.stdout.isatty() else str(s)


# ---------------------------------------------------------------- env file i/o (preserves everything)
def read_env(path):
    """KEY=VALUE pairs of an env file (comments and blanks ignored, later lines win)."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(path, updates):
    """Update KEY=VALUE lines in place (comments and unknown lines untouched), append the
    rest under a 'payment gateways' banner. Values are written bare - the bot's dotenv
    loader takes them as-is, and everything this tool writes is space-free unless quoted."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    done = set()
    for i, line in enumerate(lines):  # replace existing values, keep position and comments
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k = s.partition("=")[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    missing = [k for k in updates if k not in done]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# ---- payment gateways (added by setup_gateway.py; real env vars win)")
        for k in missing:
            lines.append(f"{k}={updates[k]}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return sorted(done), missing


# ---------------------------------------------------------------- validation
def check_url(key, val):
    """Gateway/webapp URLs must be http(s); https unless the host is local (tests, dev)."""
    if not URL_RE.match(val or ""):
        return f"{key} must look like https://host[:port][/path] (got {val!r})"
    host = urllib.parse.urlsplit(val).hostname or ""
    if val.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        return f"{key} must be https:// - payment webhooks must not cross the internet in clear"
    return ""


def check_vpa(key, val):
    if not VPA_RE.match(val or ""):
        return f"{key} must be a UPI id like yourname@okicici or 9876543210@paytm (got {val!r})"
    return ""


def check_addr(key, val):
    """A wallet address sanity check per family: right prefix, right charset, right length.
    Unknown families get the generic rule - wrong is obvious, unusual is allowed."""
    v = (val or "").strip()
    fam = key[9:-5].lower() if key.startswith("CRYPTAPI_") and key.endswith("_ADDR") else ""
    base = fam.split("_")[-1]
    if " " in v or len(v) < 14 or len(v) > 120:
        return f"{key} does not look like a wallet address (got {v[:16]}...)"
    if base in ("btc", "ltc", "bch") and fam == base:  # plain coins: bc1..., 1..., 3..., L...
        if base == "btc" and not (v.startswith(("bc1", "1", "3")) and len(v) in (26, 34, 42, 62)):
            return f"{key} should be a Bitcoin address (bc1... / 1... / 3...; got {v[:16]}...)"
        if base == "ltc" and not v.startswith(("ltc1", "L", "M", "3")):
            return f"{key} should be a Litecoin address (ltc1... / L... / M...)"
        if base == "bch" and not v.startswith(("bitcoincash:", "q", "p", "1", "3")):
            return f"{key} should be a Bitcoin Cash address (q... / bitcoincash:...)"
    if base in ("eth", "polygon", "avax", "arb", "op") and fam.replace("_", "/") in (
            "eth", "polygon", "arb", "op", "polygon/eth"):
        if not (v.startswith("0x") and len(v) == 42 and re.fullmatch(r"0x[0-9a-fA-F]{40}", v)):
            return f"{key} should be an EVM address (0x + 40 hex chars; got {v[:16]}...)"
    if "usdt" in fam and fam.startswith("trc20") and not (v.startswith("T") and len(v) == 34
                                                           and re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", v)):
        return f"{key} should be a TRON address (T + 33 base58 chars; got {v[:16]}...)"
    return ""


def check_int(key, val, lo, hi):
    try:
        n = int(val)
    except (TypeError, ValueError):
        return f"{key} must be a whole number ({lo}-{hi})"
    if not lo <= n <= hi:
        return f"{key} must be between {lo} and {hi}"
    return ""


CHECKS = {
    "WEBAPP_URL": lambda k, v: check_url(k, v),
    "UPIGW_URL": lambda k, v: check_url(k, v),
    "UPIGW_WEBHOOK_SECRET": lambda k, v: "" if len(v or "") >= 16 else f"{k} should be 16+ chars (generate one: --gen-secret)",
    "UPIGW_MAX_SKEW": lambda k, v: check_int(k, v, 30, 3600),
    "UPIGW_API_KEY": lambda k, v: "" if len(v or "") >= 4 else f"{k} looks too short",
    "UPIGW_API_SECRET": lambda k, v: "" if len(v or "") >= 4 else f"{k} looks too short",
    "UPI_VPA": check_vpa,
    "UPI_NAME": lambda k, v: "" if 2 <= len(v or "") <= 64 else f"{k} should be 2-64 chars (the name UPI apps show)",
    "CRYPTAPI_BASE": lambda k, v: check_url(k, v),
    "CRYPTAPI_TOLERANCE": lambda k, v: check_int(k, v, 0, 10),
    "CRYPTAPI_CONFIRMATIONS": lambda k, v: check_int(k, v, 1, 64),
    "CRYPTAPI_API_KEY": lambda k, v: "" if not v or len(v) >= 8 else f"{k} looks too short for a BlockBee key",
}


def validate(key, val):
    """'' = fine. Anything else = the reason it is not. CRYPTAPI_<X>_ADDR keys are addresses."""
    if key.startswith("CRYPTAPI_") and key.endswith("_ADDR") and key != "CRYPTAPI_ADDR":
        return check_addr(key, val)
    fn = CHECKS.get(key)
    return fn(key, val) if fn else f"unknown key {key} (this tool writes: {', '.join(sorted(WRITABLE))}, CRYPTAPI_<COIN>_ADDR)"


# ---------------------------------------------------------------- qr + upi link (same code path as payments.py)
# This is a setup tool: it must run before the bot exists - no database, no SECRET_KEY,
# no payments/store import. The QR and the intent link below are exactly what the bot
# renders at checkout (payments.qr_png / payments.upi_intent), inlined for independence.
def qr_png(text):
    """The text as a PNG - identical parameters to payments.qr_png."""
    import qrcode
    from qrcode.image.pure import PyPNGImage
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    q.add_data(text)
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image(image_factory=PyPNGImage).save(buf)
    return buf.getvalue()


def upi_link(vpa, name, amount_inr, ref):
    """The classic upi://pay deep link - identical fields to payments.upi_intent."""
    q = urllib.parse.urlencode({"pa": vpa, "pn": (name or "vpsbot")[:64], "am": f"{amount_inr:.2f}",
                                "cu": "INR", "tn": "vpsbot setup", "tr": str(ref)[:35]})
    return f"upi://pay?{q}"


# ---------------------------------------------------------------- live probes (read-only, safe)
def probe(url, headers=None, timeout=12):
    """One GET. Returns (status, body-snippet): 0 = cannot reach, else the HTTP status."""
    try:
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(300).decode(errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(300).decode(errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except (urllib.error.URLError, TimeoutError, OSError, socket.timeout):
        return 0, ""


def probe_webapp(url):
    """The webhook endpoint this bot serves must be publicly reachable: {url}/healthz."""
    code, _ = probe(url.rstrip("/") + "/healthz")
    if code == 0:
        return False, f"cannot reach {url}/healthz - gateways cannot deliver webhooks to it"
    return True, f"{url}/healthz answers (HTTP {code}) - webhooks can reach the bot"


def probe_upigw(url, key, secret):
    """Knock on the gateway with the merchant credentials: a lookup of an order that does
    not exist. 401/403 = the credentials are refused, 404/200 = they are accepted, 0 = the
    gateway is down. No state is created either way."""
    code, body = probe(f"{url.rstrip('/')}/api/v1/orders/vpsbot-setup-probe",
                       {"X-Api-Key": key, "X-Api-Secret": secret})
    if code == 0:
        return False, f"cannot reach the gateway at {url}"
    if code in (401, 403):
        return False, f"the gateway refused the API key/secret (HTTP {code}: {body[:120]})"
    return True, f"the gateway is reachable and accepted the credentials (HTTP {code})"


def probe_crypt(base, apikey=""):
    """CryptAPI/BlockBee sanity: the public key endpoint (what callbacks are signed with)
    and a live conversion rate. Both are public read-only endpoints."""
    url = f"{base.rstrip('/')}/pubkey/" + (f"?apikey={apikey}" if apikey else "")
    code, body = probe(url)
    if code != 200:
        return False, f"cannot reach {base}/pubkey/ (HTTP {code or 'no connection'})"
    try:
        if not json.loads(body or "{}").get("pubkey"):
            return False, "the pubkey endpoint answered without a pubkey"
    except json.JSONDecodeError:
        return False, "the pubkey endpoint returned non-JSON data"
    return True, "cryptapi.io answers (pubkey endpoint OK)"


# ---------------------------------------------------------------- the setup state
def method_state(env):
    """{method: 'off'|'partial'|'ready'} from a KEY=VALUE dict, plus the missing keys."""
    out = {}
    for mid, m in METHODS.items():
        if mid == "crypt":
            have = [k for k in env if k.startswith("CRYPTAPI_") and k.endswith("_ADDR") and env[k]]
            miss = [] if have else ["CRYPTAPI_<COIN>_ADDR"]
        else:
            miss = [k for k in m["required"] if not env.get(k)]
        if mid in ("upigw", "crypt") and m["needs_webapp"] and not env.get("WEBAPP_URL") and not miss:
            miss = ["WEBAPP_URL"]
        out[mid] = {"state": "off" if not any(env.get(k) for k in m["required"]) and
                    (mid != "crypt" or not any(k.startswith("CRYPTAPI_") and k.endswith("_ADDR")
                                               and env.get(k) for k in env)) else ("ready" if not miss else "partial"),
                    "missing": miss}
    return out


def mask(v):
    return v if len(v) <= 8 else f"{v[:4]}...{v[-4:]} ({len(v)} chars)"


def banner(env):
    print(paint("\n  Tg-vps-bot gateway setup", "b") + paint(f"  ({ENV_FILE})\n", "d"))
    st = method_state(env)
    for mid, m in METHODS.items():
        s = st[mid]["state"]
        mark = paint("ready  ", "g") if s == "ready" else (paint("partial", "y") if s == "partial" else paint("off    ", "d"))
        extra = ""
        if s == "partial":
            extra = paint("  missing: " + ", ".join(st[mid]["missing"]), "y")
        print(f"  [{mark}] {m['title']}{extra}")
    print(f"  [{'on ' if env.get('WEBAPP_URL') else paint('off', 'd')}] webhooks / Mini App (WEBAPP_URL)"
          + ("" if env.get("WEBAPP_URL") else paint("  - required by the two auto-confirm modes", "d")))
    print()


def ask(prompt, default="", secret=False):
    """One prompt line; Enter keeps the default. No TTY (piped/cron) behaves like --yes."""
    try:
        if secret:
            import getpass
            v = getpass.getpass(f"{prompt} [{default if default else 'empty'}]: ") if sys.stdin.isatty() \
                else input(f"{prompt} [{default if default else 'empty'}]: ")
        else:
            v = input(f"{prompt} [{default if default else 'empty'}]: ")
    except EOFError:
        v = ""
    except KeyboardInterrupt:
        print()
        raise SystemExit(1)
    return v.strip() or default


def ask_yes(prompt, default=False):
    try:
        v = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        v = ""
    except KeyboardInterrupt:
        print()
        raise SystemExit(1)
    if not v:
        return default
    return v in ("y", "yes")


# ---------------------------------------------------------------- the guided setup
def wizard(env, args, updates):
    banner(env)
    print("  Values go to .env only. Press Enter to keep the current value, Ctrl+C to abort.\n")

    webapp = env.get("WEBAPP_URL", "")
    webapp = ask("  Public https address of this bot (WEBAPP_URL)", webapp)
    if webapp:
        err = validate("WEBAPP_URL", webapp)
        if err:
            print(paint("  ! " + err, "r"))
        else:
            updates["WEBAPP_URL"] = webapp
            if not args.no_probe:
                ok, msg = probe_webapp(webapp)
                print(paint("  * " + msg, "g" if ok else "y"))
    print()

    # ---- UPI Gateway
    if ask_yes("  Set up the UPI Gateway (github.com/Exon101x/upi-gateway)?", False):
        print(paint("\n  Credentials come from the gateway's Dashboard -> API & Webhooks.", "d"))
        url = ask("    Gateway URL (UPIGW_URL)", env.get("UPIGW_URL", ""))
        key = ask("    API key (UPIGW_API_KEY)", env.get("UPIGW_API_KEY", ""))
        sec = ask("    API secret (UPIGW_API_SECRET)", env.get("UPIGW_API_SECRET", ""), secret=True)
        wh = env.get("UPIGW_WEBHOOK_SECRET", "")
        if not wh and ask_yes("    Generate a webhook signing secret?", True):
            wh = secrets.token_urlsafe(32)
            print(f"    generated: {mask(wh)} (the full value goes to .env and must be pasted\n"
                  "    into the gateway's Dashboard -> API & Webhooks too)")
        else:
            wh = ask("    Webhook signing secret (UPIGW_WEBHOOK_SECRET)", wh, secret=True)
        vals = {"UPIGW_URL": url, "UPIGW_API_KEY": key, "UPIGW_API_SECRET": sec, "UPIGW_WEBHOOK_SECRET": wh}
        bad = [(k, validate(k, v)) for k, v in vals.items() if v]
        bad = [(k, e) for k, e in bad if e]
        if bad:
            for k, e in bad:
                print(paint(f"    ! {e}", "r"))
        else:
            updates.update(vals)
            if not args.no_probe and url and key and sec:
                ok, msg = probe_upigw(url, key, sec)
                print(paint("    * " + msg, "g" if ok else "y"))
            if webapp:
                print(paint(f"    * register this webhook URL in the gateway's dashboard:\n"
                            f"        {webapp.rstrip('/')}/pay/upigw/cb", "b"))
        print()

    # ---- UPI direct
    if ask_yes("  Set up direct UPI (your own UPI id, admin confirms via /paid)?", False):
        vpa = ask("    Your UPI id (UPI_VPA)", env.get("UPI_VPA", ""))
        name = ask("    Name UPI apps should show (UPI_NAME)", env.get("UPI_NAME", "vpsbot"))
        err = validate("UPI_VPA", vpa) if vpa else ""
        if err:
            print(paint("    ! " + err, "r"))
        else:
            updates["UPI_VPA"] = vpa
            if name:
                updates["UPI_NAME"] = name
            try:
                png = qr_png(upi_link(vpa, name, 1.0, "vpsbotsetup"))
                print(paint(f"    * a 1.00 INR test QR renders fine ({len(png)} bytes) - buyers will see this", "g"))
            except Exception as e:  # qrcode not installed, etc.
                print(paint(f"    ! QR rendering failed ({e}) - pip install -r requirements.txt", "y"))
        print()

    # ---- CryptAPI
    if ask_yes("  Set up crypto payments via CryptAPI (paid out to your own wallets)?", False):
        while True:
            coin = ask("    Coin (e.g. BTC, TRC20_USDT; empty to stop)", "").upper().replace(" ", "_")
            if not coin:
                break
            k = f"CRYPTAPI_{coin}_ADDR"
            addr = ask(f"    Your {coin} wallet address ({k})", env.get(k, ""))
            err = validate(k, addr) if addr else f"{k} is required for the {coin} option"
            if err:
                print(paint("    ! " + err, "r"))
                continue
            updates[k] = addr
            print(paint(f"    + buyers can now pay with {coin.replace('_', ' ')}", "g"))
        tol = ask("    Underpayment tolerance % (CRYPTAPI_TOLERANCE)", env.get("CRYPTAPI_TOLERANCE", "1"))
        if not validate("CRYPTAPI_TOLERANCE", tol):
            updates["CRYPTAPI_TOLERANCE"] = tol
        conf = ask("    Confirmations to wait (CRYPTAPI_CONFIRMATIONS)", env.get("CRYPTAPI_CONFIRMATIONS", "1"))
        if not validate("CRYPTAPI_CONFIRMATIONS", conf):
            updates["CRYPTAPI_CONFIRMATIONS"] = conf
        if not args.no_probe:
            base = updates.get("CRYPTAPI_BASE") or env.get("CRYPTAPI_BASE") or (
                "https://api.blockbee.io" if updates.get("CRYPTAPI_API_KEY") or env.get("CRYPTAPI_API_KEY")
                else "https://api.cryptapi.io")
            ok, msg = probe_crypt(base, updates.get("CRYPTAPI_API_KEY") or env.get("CRYPTAPI_API_KEY", ""))
            print(paint("    * " + msg, "g" if ok else "y"))
            if webapp:
                print(paint(f"    * verify the domain {urllib.parse.urlsplit(webapp).hostname or webapp} once at\n"
                            "        https://cryptapi.io (Dashboard -> domains) - CryptAPI only creates\n"
                            "        payment addresses for verified callback domains", "b"))
        print()

    if not updates:
        print("  Nothing to write. Bye.")
        return 0
    print("  Summary of what will be written to " + ENV_FILE + ":")
    for k in sorted(updates):
        shown = mask(updates[k]) if any(t in k for t in ("SECRET", "KEY", "TOKEN")) and k != "UPIGW_URL" else updates[k]
        print(f"    {k} = {shown}")
    if not args.yes and not ask_yes(f"\n  Write these to {ENV_FILE}?", True):
        print("  Aborted, nothing written.")
        return 1
    done, added = write_env(ENV_FILE, updates)
    print(paint(f"\n  written: {len(done)} updated, {len(added)} added -> {ENV_FILE}", "g"))
    print("\n  Next steps:")
    print("    1. restart the bot so it picks up the new .env")
    if "UPIGW_URL" in updates:
        print("    2. paste the webhook signing secret + webhook URL into the gateway's Dashboard")
    if any(k.startswith("CRYPTAPI_") and k.endswith("_ADDR") for k in updates):
        print("    2. verify your domain at https://cryptapi.io so addresses can be created")
    print(paint("    3. check everything: python3 setup_gateway.py --check", "b"))
    return 0


# ---------------------------------------------------------------- the checker
def check(env, args):
    """Diagnose the current setup: what is configured, what is missing, what answers.
    Exit 0 = consistent (nothing or fully configured), 1 = something needs attention."""
    print(paint("\n  gateway setup check\n", "b"))
    st = method_state(env)
    problems = 0

    for mid, m in METHODS.items():
        s = st[mid]
        mark = {"ready": paint("READY  ", "g"), "partial": paint("PARTIAL", "y"), "off": paint("off    ", "d")}[s["state"]]
        print(f"  [{mark}] {m['title']}")
        if s["state"] == "partial":
            print(paint(f"          missing: {', '.join(s['missing'])}  (run: python3 setup_gateway.py)", "y"))
            problems += 1
        elif s["state"] == "off":
            print(paint(f"          not configured ({m['docs']}) - buyers use /paid + admin approval", "d"))
        if s["state"] == "ready" and not args.no_probe:
            if mid == "upigw":
                ok, msg = probe_upigw(env["UPIGW_URL"], env["UPIGW_API_KEY"], env["UPIGW_API_SECRET"])
                print(paint(f"          {'ok ' if ok else '!! '}{msg}", "g" if ok else "y"))
                problems += 0 if ok else 1
            if mid == "crypt":
                base = env.get("CRYPTAPI_BASE") or ("https://api.blockbee.io" if env.get("CRYPTAPI_API_KEY")
                                                    else "https://api.cryptapi.io")
                ok, msg = probe_crypt(base, env.get("CRYPTAPI_API_KEY", ""))
                print(paint(f"          {'ok ' if ok else '!! '}{msg}", "g" if ok else "y"))
                problems += 0 if ok else 1
    wa = env.get("WEBAPP_URL", "")
    if wa:
        if not args.no_probe:
            ok, msg = probe_webapp(wa)
            print(paint(f"  [{'ok ' if ok else '!! '}] webhooks: {msg}", "g" if ok else "y"))
            problems += 0 if ok else 1
    else:
        print(paint("  [off] webhooks / Mini App (WEBAPP_URL) - the auto-confirm modes need it", "d"))

    if all(v["state"] == "off" for v in st.values()) and not env.get("WEBAPP_URL"):
        print(paint("  no gateway configured yet - run: python3 setup_gateway.py\n", "d"))
        return 0

    try:  # the QR pipeline, exactly what the bot renders at checkout
        png = qr_png(upi_link(env.get("UPI_VPA") or "setup@okicici", env.get("UPI_NAME"), 1.0, "vpsbotsetup"))
        print(paint(f"  [ok ] QR pipeline ({len(png)} bytes per code)", "g"))
    except Exception as e:
        print(paint(f"  [!!] QR pipeline failed: {e} - pip install -r requirements.txt", "y"))
        problems += 1

    if problems:
        print(paint(f"\n  {problems} issue(s) need attention.\n", "r"))
        return 1
    print(paint("\n  all configured gateways are consistent and reachable.\n", "g"))
    return 0


# ---------------------------------------------------------------- cli
def main():
    global ENV_FILE
    p = argparse.ArgumentParser(description="Set up the payment gateways of Tg-vps-bot")
    p.add_argument("--check", action="store_true", help="diagnose the current setup, change nothing")
    p.add_argument("--write", nargs="+", metavar="KEY=VALUE", help="validated batch write to the env file")
    p.add_argument("--gen-secret", action="store_true", help="print a strong secret for webhook signing")
    p.add_argument("--qr", nargs="+", metavar="TEXT", help="render TEXT as qr_test.png (QR pipeline test)")
    p.add_argument("--env", default=ENV_FILE, help=f"target env file (default {ENV_FILE})")
    p.add_argument("--yes", action="store_true", help="no confirmations (non-interactive)")
    p.add_argument("--no-probe", action="store_true", help="skip live network probes")
    args = p.parse_args()
    ENV_FILE = args.env

    if args.gen_secret:
        print(secrets.token_urlsafe(32))
        return 0
    if args.qr:
        with open("qr_test.png", "wb") as f:
            f.write(qr_png(" ".join(args.qr)))
        print(f"wrote qr_test.png ({os.path.getsize('qr_test.png')} bytes)")
        return 0
    if args.write:
        updates, errs = {}, []
        for item in args.write:
            k, sep, v = item.partition("=")
            k, v = k.strip(), v.strip() if sep else ""
            if not k:
                errs.append(f"empty key in {item!r}")
                continue
            err = validate(k, v)
            if err:
                errs.append(err)
            else:
                updates[k] = v
        if errs:
            for e in errs:
                print(paint("! " + e, "r"))
            return 2
        done, added = write_env(ENV_FILE, updates)
        print(f"{ENV_FILE}: {len(done)} updated, {len(added)} added")
        return 0

    env = read_env(ENV_FILE)
    if args.check:
        return check(env, args)
    return wizard(env, args, {})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(1)
