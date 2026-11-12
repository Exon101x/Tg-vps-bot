"""Payment gateways for the subscription system: UPI Gateway (self-hosted, auto-verified),
direct UPI intent links, and crypto (CryptAPI).

Three ways an owner can take payments, configured with env vars, all optional and independent:

  UPI GATEWAY (auto-verified    UPIGW_URL + UPIGW_API_KEY + UPIGW_API_SECRET + UPIGW_WEBHOOK_SECRET.
  UPI)                          An order is created on the owner's self-hosted UPI Gateway
                               (https://github.com/Exon101x/upi-gateway - a multi-merchant UPI
                               payment gateway with dynamic QR codes, payment verification and
                               account rotation) and the buyer gets its checkout page
                               (dynamic QR + one-tap UPI app deep links). When the payment
                               verifies, the gateway fires a signed webhook at this bot:
                               POST {WEBAPP_URL}/pay/upigw/cb, header X-Signature:
                               t=<unix-seconds>,v1=<hmac_sha256(webhook_secret, t + '.' + raw_body)>.
                               The timestamp must be fresh (UPIGW_MAX_SKEW seconds), the amount
                               must match the order, every event_id is remembered (delivery is
                               at-least-once and can arrive out of order) and event_sequence
                               keeps older events from reverting newer state - except
                               order.paid, which the gateway itself lets win over any earlier
                               state. Verified + matched + idempotent -> the plan (and its VPS)
                               activates instantly. INR only (it rides the NPCI UPI rails).

  UPI direct (manual)         UPI_VPA (+ UPI_NAME). A classic upi://pay intent link + QR the buyer
                              opens in GPay/PhonePe/Paytm, whatever. There is no bank feed in this
                              mode, so the buyer sends /paid <UTR reference> and the owner confirms -
                              the same assisted flow as before, now with one-tap payment.

  CRYPTAPI (auto-verified     CRYPTAPI_<COIN>_ADDR, e.g. CRYPTAPI_BTC_ADDR=bc1... or
  crypto, own wallets)        CRYPTAPI_TRC20_USDT_ADDR=T... (100+ coins, no signup, funds forward to
                              your own wallet; https://cryptapi.io). Each purchase mints a payment
                              address with a per-order nonce; callbacks arrive at
                              {WEBAPP_URL}/pay/cryptapi/cb signed with RSA-SHA256 (header
                              X-CA-Signature, verified against CryptAPI's pubkey endpoint - the same
                              scheme their official plugins use), are nonce-checked, accumulated per
                              uuid (partial payments count), and a confirmed total within
                              CRYPTAPI_TOLERANCE % of the expected amount auto-activates the plan.
                              Callbacks must answer exactly "*ok*".

Payments state lives in the kv store (key "pay:<order id>"): nonce, provider refs, addresses,
expected coin amounts, what has been received so far. No schema migration, all four databases.

Security: every webhook is verified before anything happens (timestamped HMAC-SHA256 for the UPI
Gateway, RSA-SHA256 for CryptAPI), signatures and nonces are compared with hmac.compare_digest,
amounts must match the order, event ids are remembered so replayed or retried webhooks can never
activate twice, and every decision is audit-logged. Gateway URLs come from the owner's env, never
from user input.

Source: https://github.com/Exon101x/Tg-vps-bot - fork it, keep the link, sell your own plans.
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import store
from util import env_int

log = logging.getLogger("payments")

# ---------------------------------------------------------------- config (all optional, never crashes)
UPI_VPA = os.getenv("UPI_VPA", "").strip()                 # the owner's UPI id, e.g. owner@okicici
UPI_NAME = (os.getenv("UPI_NAME", "").strip() or "vpsbot")[:64]

UPIGW_URL = os.getenv("UPIGW_URL", "").strip().rstrip("/")  # the self-hosted UPI Gateway base URL
UPIGW_API_KEY = os.getenv("UPIGW_API_KEY", "").strip()     # Dashboard -> API & Webhooks (X-Api-Key)
UPIGW_API_SECRET = os.getenv("UPIGW_API_SECRET", "").strip()  # same screen (X-Api-Secret)
UPIGW_WEBHOOK_SECRET = os.getenv("UPIGW_WEBHOOK_SECRET", "").strip()  # the Live webhook signing secret
UPIGW_MAX_SKEW = env_int("UPIGW_MAX_SKEW", 300, lo=30, hi=3600)  # how stale a webhook timestamp may be

CRYPTAPI_BASE = os.getenv("CRYPTAPI_BASE", "").strip().rstrip("/") or "https://api.cryptapi.io"
CRYPTAPI_API_KEY = os.getenv("CRYPTAPI_API_KEY", "").strip()  # BlockBee pro: swaps the base + signs in
if CRYPTAPI_API_KEY:
    CRYPTAPI_BASE = os.getenv("CRYPTAPI_BASE", "").strip().rstrip("/") or "https://api.blockbee.io"
CRYPTAPI_TOLERANCE = env_int("CRYPTAPI_TOLERANCE", 1, lo=0, hi=10)  # % underpayment accepted (crypto)
CRYPTAPI_MIN_CONFIRM = env_int("CRYPTAPI_CONFIRMATIONS", 1, lo=1, hi=64)  # confirmations to wait for

WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")  # public HTTPS base of this bot's port


def _coin_addrs():
    """CRYPTAPI_<TICKER>_ADDR env vars -> {api ticker: wallet address}. Ticker underscores map to
    slashes: CRYPTAPI_TRC20_USDT_ADDR -> 'trc20/usdt' (the API's path form)."""
    out = {}
    pre = "CRYPTAPI_"
    for k, v in os.environ.items():
        if k.startswith(pre) and k.endswith("_ADDR") and v.strip():
            coin = k[len(pre):-len("_ADDR")].strip().lower().replace("_", "/")
            if "/" in coin or coin.isalnum():  # 'btc', 'eth', 'trc20/usdt', ...
                out[coin] = v.strip()
    return out


CRYPT_ADDRS = _coin_addrs()  # {"btc": "bc1...", "trc20/usdt": "T..."}


def upigw_ready():
    return bool(UPIGW_URL and UPIGW_API_KEY and UPIGW_API_SECRET and UPIGW_WEBHOOK_SECRET and WEBAPP_URL)


def upigw_currency_ok(currency):
    return (currency or "").upper() == "INR"


def crypt_ready():
    return bool(CRYPT_ADDRS and WEBAPP_URL)


# ---------------------------------------------------------------- tiny HTTP client (stdlib, blocking)
def _http(url, data=None, headers=None, timeout=25):
    """One request, one shot. Returns parsed JSON (dict). Raises GatewayError on transport/API error."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:  # gateways answer 4xx/5xx with a JSON reason
        detail = ""
        try:
            d = json.loads(e.read().decode() or "{}")
            detail = str(d.get("message") or d.get("error") or "")[:200]
        except Exception:
            pass
        raise GatewayError(detail or f"gateway answered HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GatewayError(f"cannot reach the gateway ({type(e).__name__})") from None
    try:
        out = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayError("gateway returned non-JSON data")
    if out.get("status") == "error":
        raise GatewayError(str(out.get("error") or "gateway error")[:200])
    return out


class GatewayError(Exception):
    pass


# ---------------------------------------------------------------- QR codes (pure python, no Pillow)
def qr_png(text, border=4):
    """The text (a upi:// link, a coin: URI) as a PNG the chat and the Mini App can show directly."""
    import qrcode
    from qrcode.image.pure import PyPNGImage
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=border)
    q.add_data(text)
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image(image_factory=PyPNGImage).save(buf)
    return buf.getvalue()


def qr_data_url(text):
    return "data:image/png;base64," + base64.b64encode(qr_png(text)).decode()


# ---------------------------------------------------------------- order payment state (kv store)
def meta_get(oid):
    try:
        return json.loads(store.kv_get(f"pay:{oid}") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def meta_set(oid, **fields):
    m = meta_get(oid)
    m.update(fields)
    store.kv_set(f"pay:{oid}", json.dumps(m)[:6000])
    return m


# ---------------------------------------------------------------- UPI Gateway (self-hosted, auto-verified)
def upigw_headers():
    return {"X-Api-Key": UPIGW_API_KEY, "X-Api-Secret": UPIGW_API_SECRET}


def upigw_create(oid, plan, buyer_name=""):
    """Create an order on the owner's UPI Gateway (POST /api/v1/orders). Returns the gateway's
    checkout page, order number and UPI deep link. client_order_id ('vpsbot<oid>') is unique per
    merchant, so a retried order reuses the gateway's existing one instead of failing."""
    body = json.dumps({
        "amount": round(plan.cents / 100, 2),
        "client_order_id": f"vpsbot{oid}",
        "customer_name": (buyer_name or "")[:100],
        "note": f"{plan.name} plan ({plan.days} days)"[:50],
    }).encode()
    try:
        out = _http(UPIGW_URL + "/api/v1/orders", data=body,
                    headers={"Content-Type": "application/json", **upigw_headers()})
    except GatewayError as e:
        if "taken" not in str(e):  # not a duplicate client_order_id: a real failure
            raise
        out = _http(f"{UPIGW_URL}/api/v1/orders/vpsbot{oid}", headers=upigw_headers())  # already created
    d = out.get("data") or {}
    if not d.get("order_id") or not d.get("payment_url"):
        raise GatewayError("the gateway returned no payment URL")
    meta_set(oid, provider="upigw", gw_order=str(d["order_id"])[:64],
             gw_checkout=str(d["payment_url"])[:500], gw_intent=str(d.get("upi_intent") or "")[:500],
             amount_paise=plan.cents, gw_events={}, gw_last_seq=0)
    return {"order_id": str(d["order_id"]), "checkout_url": str(d["payment_url"]),
            "intent": str(d.get("upi_intent") or "") or None,
            "status": str(d.get("status") or "pending"), "expires_at": str(d.get("expires_at") or "")}


def upigw_status(oid):
    """Live status from the gateway (the Mini App polls this while the checkout is open)."""
    m = meta_get(oid)
    if not m.get("gw_order"):
        return None
    try:
        out = _http(f"{UPIGW_URL}/api/v1/orders/{urllib.parse.quote(str(m['gw_order']))}",
                    headers=upigw_headers())
        d = out.get("data") or {}
        return {k: d.get(k) for k in ("order_id", "status", "utr", "paid_at", "environment",
                                      "test", "expires_at")}
    except Exception as e:
        log.warning("UPI Gateway status lookup failed for order %s: %s", oid, e)
        return None


def upigw_verify(raw_body, sig_header):
    """X-Signature: t=<unix-seconds>,v1=<hex> - HMAC-SHA256 of '<t>.<raw body>' with the webhook
    signing secret, constant-time compared. Timestamps older than UPIGW_MAX_SKEW are stale."""
    if not UPIGW_WEBHOOK_SECRET or not sig_header:
        return False
    mm = re.match(r"^t=(\d{1,12}),v1=([0-9a-f]{64})$", sig_header.strip())
    if not mm:
        return False
    t = int(mm.group(1))
    if abs(time.time() - t) > UPIGW_MAX_SKEW:
        return False
    want = hmac.new(UPIGW_WEBHOOK_SECRET.encode(), f"{t}.".encode() + raw_body,
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, mm.group(2))


# ---------------------------------------------------------------- CryptAPI (crypto to your own wallets)
def crypt_coins():
    """Configured coins as [{key, address}] sorted for stable buttons."""
    return [{"key": k.replace("/", "_"), "coin": k, "address": v} for k, v in sorted(CRYPT_ADDRS.items())]


def _crypt_url(coin, endpoint, params):
    base = CRYPTAPI_BASE.rstrip("/")
    q = urllib.parse.urlencode(params)
    return f"{base}/{coin}/{endpoint}/?{q}"


def crypt_convert(coin, cents, currency):
    """Order price -> the coin amount to pay (CryptAPI's live rate)."""
    out = _http(_crypt_url(coin, "convert", {"from": (currency or "USD")[:8].upper(), "value": f"{cents / 100:.2f}"}))
    value = str(out.get("value_coin") or "").strip()
    if not value:
        raise GatewayError("no conversion rate returned")
    return value


def crypt_create(oid, plan, coin, currency="USD"):
    """Mint a payment address for this order (per-order callback nonce baked into the URL)."""
    addr = CRYPT_ADDRS.get(coin)
    if not addr:
        raise GatewayError(f"coin {coin} is not configured")
    amount = crypt_convert(coin, plan.cents, currency)
    nonce = secrets.token_hex(16)
    callback = f"{WEBAPP_URL}/pay/cryptapi/cb?oid={oid}&nonce={nonce}"
    params = {"address": addr, "callback": callback, "pending": "1"}
    if CRYPTAPI_API_KEY:
        params["apikey"] = CRYPTAPI_API_KEY
    out = _http(_crypt_url(coin, "create", params))
    if not out.get("address_in"):
        raise GatewayError("CryptAPI returned no payment address")
    meta_set(oid, provider="cryptapi", coin=coin, address_in=str(out["address_in"])[:128],
             expected=amount, nonce=nonce, recv={})
    return {"coin": coin, "address": str(out["address_in"]), "amount": amount,
            "uri": _coin_uri(coin, str(out["address_in"]), amount)}


def _coin_uri(coin, address, amount):
    """A wallet-style URI for the QR code: bitcoin:addr?amount=x, ethereum:addr. Tokens (USDT on
    Tron, ...) have no standard URI scheme, so their QR is the plain address - what wallets expect."""
    base = (coin.split("/")[-1]).lower()
    if base in ("btc", "ltc", "bch"):
        return f"{base}:{address}?amount={amount}"
    if base in ("eth", "polygon", "avax"):
        return f"{base}:{address}"
    return address  # tokens and chains without a URI convention: the QR is the address itself


_PUBKEY_CACHE = {"key": "", "ts": 0}  # fetched at most once a day


def crypt_pubkey(force=False):
    """CryptAPI's RSA public key (verified-domain callbacks are signed with the matching private key)."""
    if not force and _PUBKEY_CACHE["key"] and time.time() - _PUBKEY_CACHE["ts"] < 86400:
        return _PUBKEY_CACHE["key"]
    url = f"{CRYPTAPI_BASE.rstrip('/')}/pubkey/"
    if CRYPTAPI_API_KEY:
        url += "?" + urllib.parse.urlencode({"apikey": CRYPTAPI_API_KEY})
    out = _http(url)
    key = (out.get("pubkey") or "").strip()
    if not key:
        raise GatewayError("no pubkey returned")
    _PUBKEY_CACHE.update(key=key, ts=time.time())
    return key


def crypt_verify(raw_target, sig_b64):
    """X-CA-Signature: base64 RSA-SHA256 over '{public_base}{request_target}' - the scheme CryptAPI's
    official plugins verify (openssl_verify / SHA256). raw_target = path + '?query' as received."""
    if not sig_b64 or not WEBAPP_URL:
        return False
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_public_key(crypt_pubkey().encode())
        key.verify(base64.b64decode(sig_b64, validate=True),
                   f"{WEBAPP_URL}{raw_target}".encode(),
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- choosing + starting a payment
def methods(plan, currency=None):
    """Payment options for a plan, in a stable order, with what each needs from the buyer."""
    from subscriptions import CURRENCY  # the catalog's display currency: the gateway is INR-only
    cur = (currency or CURRENCY or "").upper()
    out = []
    if upigw_ready() and upigw_currency_ok(cur):
        out.append({"key": "upigw", "kind": "upi", "auto": True,
                    "label": "UPI - checkout page (auto-confirm)"})
    if UPI_VPA:
        out.append({"key": "upi", "kind": "upi", "auto": False,
                    "label": "UPI app / QR (confirm with /paid)"})
    if crypt_ready():
        for c in crypt_coins():
            out.append({"key": f"crypt:{c['key']}", "kind": "crypto", "auto": True, "coin": c["coin"],
                        "label": f"{c['key'].replace('_', ' ').upper()} (crypto, auto-confirm)"})
    return out


def upi_intent(amount_inr, ref, note="vpsbot plan"):
    """The classic UPI deep link: opens GPay/PhonePe/Paytm with the payee and amount pre-filled."""
    q = urllib.parse.urlencode({"pa": UPI_VPA, "pn": UPI_NAME, "am": f"{amount_inr:.2f}",
                                "cu": "INR", "tn": note[:48], "tr": str(ref)[:35]})
    return f"upi://pay?{q}"


def start(oid, plan, key, buyer_name="", currency="USD"):
    """Start one payment for an open order. Returns a dict the chat UI and the Mini App both render.

    {mode: 'upigw'|'upi'|'crypto', auto: bool, text: str, buttons: [{label,url}], qr: <png bytes>,
     address, amount, coin, uri, checkout_url, intent}
    """
    key = (key or "").strip()
    mkey = key[6:] if key.startswith("crypt:") else key
    if key == "upigw":
        r = upigw_create(oid, plan, buyer_name)
        return {"mode": "upigw", "auto": True, "checkout_url": r["checkout_url"],
                "intent": r.get("intent"), "qr": qr_png(r["intent"]) if r.get("intent") else None,
                "text": "UPI checkout created on the gateway - it confirms automatically when you pay."}
    if key == "upi" and UPI_VPA:
        link = upi_intent(plan.cents / 100, f"vpsbot{oid}", note=f"{plan.name} plan")
        meta_set(oid, provider="upi", expected=f"{plan.cents / 100:.2f}")
        return {"mode": "upi", "auto": False, "qr": qr_png(link), "intent": link,
                "vpa": UPI_VPA, "amount": f"{plan.cents / 100:.2f}",
                "text": f"Pay {plan.cents / 100:.2f} INR to {UPI_VPA}, then send /paid <UTR reference>."}
    if mkey and (mkey.replace("_", "/") in CRYPT_ADDRS):
        r = crypt_create(oid, plan, mkey.replace("_", "/"), currency)
        return {"mode": "crypto", "auto": True, "qr": qr_png(r["uri"]), "coin": r["coin"],
                "address": r["address"], "amount": r["amount"], "uri": r["uri"],
                "text": f"Send exactly {r['amount']} {r['coin'].replace('/', ' ').upper()} to the address "
                        f"- it confirms on-chain, automatically."}
    raise GatewayError("that payment method is not configured")


# ---------------------------------------------------------------- webhook processing (called by webapi)
def upigw_callback(raw_body, sig_header):
    """Verify a UPI Gateway webhook and decide what to do with it.

    Delivery is at-least-once and may arrive out of order: every event_id is remembered (a replay
    is ack'd and dropped), event_sequence keeps an older event from reverting a newer state, and
    order.paid - which the gateway itself lets win over any earlier state - is honored whenever
    the order is still open and the amount matches (the gateway keeps a 24-hour late-payment
    recovery window after expiry, so a paid can legitimately arrive after an expired).

    Returns (event, error): event is None on failure. Actions:
      paid   - order.paid with a matching amount: activate the order
      closed - order.expired / sandbox order.failed / order.timeout: tell the buyer
      review - order.under_review: the buyer's UTR is being verified on the gateway
      ignore - not ours / not applicable / stale sequence: ack and move on
    """
    if not upigw_verify(raw_body, sig_header):
        return None, "bad or stale signature"
    try:
        d = json.loads(raw_body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "bad json"
    data = d.get("data") or {}
    co = str(data.get("client_order_id") or "")
    if not co.startswith("vpsbot") or not co[6:].isdigit():
        return None, "no order in payload"
    oid = int(co[6:])
    o = store.order_get(oid)
    if not o:
        return None, "unknown order"
    event = str(d.get("event") or "")
    eid = str(d.get("event_id") or "")[:64]
    try:
        seq = int(d.get("event_sequence") or 0)
    except (TypeError, ValueError):
        seq = 0
    m = meta_get(oid)
    events = dict(m.get("gw_events") or {})
    last_seq = int(m.get("gw_last_seq") or 0)

    def seen(apply=True):  # remember the event id (dedupe) and, unless it is stale, the sequence
        if eid:
            events[eid] = seq
        meta_set(oid, gw_events=events,
                 **({"gw_last_seq": max(last_seq, seq)} if apply else {}))

    if eid and eid in events:  # a replayed delivery: ack, never apply twice
        return {"action": "dup", "oid": oid, "why": f"event {eid[:12]} already seen"}, None
    if o["status"] not in ("pending", "awaiting"):
        seen(apply=False)
        return {"action": "ignore", "oid": oid, "why": f"order is {o['status']}"}, None
    if event != "order.paid" and seq and seq <= last_seq:  # out of order: an older state
        seen(apply=False)
        return {"action": "ignore", "oid": oid, "why": f"stale sequence {seq} <= {last_seq}"}, None
    if event == "order.paid":
        ref = str(data.get("utr") or data.get("order_id") or "")[:128]
        try:
            paise = round(float(data.get("amount") or 0) * 100)
        except (TypeError, ValueError):
            return None, "bad amount"
        if paise != o["cents"]:  # what the buyer paid must be what the order costs
            log.warning("UPI Gateway webhook amount mismatch on #%s: %s != %s", oid, paise, o["cents"])
            return {"action": "mismatch", "oid": oid, "paise": paise, "ref": ref}, None
        seen()
        return {"action": "paid", "oid": oid, "ref": ref, "paise": paise}, None
    seen()  # a state transition on an open order: remember it, then classify
    if event == "order.under_review":
        return {"action": "review", "oid": oid, "ref": str(data.get("submitted_utr") or "")[:128]}, None
    if event in ("order.expired", "order.failed", "order.timeout"):
        return {"action": "closed", "oid": oid, "event": event.split(".", 1)[-1]}, None
    return {"action": "ignore", "oid": oid, "why": f"event {event or '?'}"}, None


def cryptapi_callback(params, raw_target, sig_b64):
    """Verify a CryptAPI callback and decide what to do with it.

    The signature covers '{WEBAPP_URL}{raw_target}' exactly as the request arrived (the scheme their
    official plugins verify with openssl / RSA-SHA256). Per-uuid payments are recorded, never
    double-counted, and partial payments accumulate until the confirmed total is within
    CRYPTAPI_TOLERANCE % of the expected amount. Returns (event, error).

      seen  - unconfirmed tx seen on-chain: tell the buyer it is waiting for confirmations
      short - confirmed but not enough yet (partial payment): keep waiting
      paid  - confirmed total reached: activate the order
      ignore- the order is not open any more (approved/expired): ack and move on
    """
    if not crypt_verify(raw_target, sig_b64):
        return None, "bad signature"
    try:
        oid = int(params.get("oid") or 0)
    except (TypeError, ValueError):
        oid = 0
    m = meta_get(oid) if oid else {}
    if not m or not m.get("nonce"):
        return None, "unknown order"
    if not hmac.compare_digest(str(m["nonce"]), str(params.get("nonce") or "")):
        return None, "bad nonce"
    o = store.order_get(oid)
    if not o:
        return None, "unknown order"
    if o["status"] not in ("pending", "awaiting"):
        return {"action": "ignore", "oid": oid, "why": f"order is {o['status']}"}, None
    uuid = str(params.get("uuid") or "")[:64]
    if not uuid:
        return None, "no uuid"
    try:
        value = float(str(params.get("value_coin") or "0"))
    except (TypeError, ValueError):
        return None, "bad value"
    recv = dict(m.get("recv") or {})
    recv[uuid] = {"v": value, "pending": 1 if str(params.get("pending") or "0") == "1" else 0,
                  "tx": str(params.get("txid_in") or "")[:128], "ts": int(time.time())}
    meta_set(oid, recv=recv)
    try:
        expected = float(m.get("expected") or 0)
    except (TypeError, ValueError):
        expected = 0
    if expected <= 0:
        return None, "order has no expected amount"
    confirmed = sum(e["v"] for e in recv.values() if not e["pending"])
    if recv[uuid]["pending"]:
        return {"action": "seen", "oid": oid, "confirmed": confirmed, "expected": expected,
                "tx": recv[uuid]["tx"] or uuid[:16]}, None
    if confirmed + 1e-12 >= expected * (1 - CRYPTAPI_TOLERANCE / 100):
        return {"action": "paid", "oid": oid, "ref": recv[uuid]["tx"] or uuid,
                "confirmed": round(confirmed, 12), "expected": expected}, None
    return {"action": "short", "oid": oid, "confirmed": round(confirmed, 12), "expected": expected}, None
