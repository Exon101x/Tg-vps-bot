"""Payment gateways for the subscription system: UPI (YouPI-PG / direct intent links) and crypto (CryptAPI).

Three ways an owner can take payments, configured with env vars, all optional and independent:

  YOUPI (auto-verified UPI)   YOUPI_URL + YOUPI_WEBHOOK_SECRET (+ optional YOUPI_MERCHANT_ID /
                              YOUPI_API_KEY). A session is created on the self-hosted YouPI-PG
                              (https://github.com/youpi-pg/YouPI-PG) and the buyer gets a checkout
                              link with a dynamic QR + UPI app deep links. When the buyer pays, YouPI's
                              banking observers fire a signed webhook at this bot:
                              POST {WEBAPP_URL}/pay/youpi/cb, header X-Gateway-Signature:
                              sha256=<hmac_sha256(webhook_secret, raw_body)>. Verified + amount
                              matched + idempotent -> the plan (and its VPS) activates instantly.
                              INR only (it rides the NPCI UPI rails).

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

Security: every webhook is verified before anything happens (HMAC-SHA256 for YouPI, RSA-SHA256 for
CryptAPI), nonces are compared with hmac.compare_digest, amounts must match the order, approvals are
idempotent (a replayed or retried webhook can never activate twice), and every decision is audit-
logged. Gateway URLs come from the owner's env, never from user input.

Source: https://github.com/Exon101x/Tg-vps-bot - fork it, keep the link, sell your own plans.
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request

import store
from util import env_int

log = logging.getLogger("payments")

REPO_URL = "https://github.com/Exon101x/Tg-vps-bot"

# ---------------------------------------------------------------- config (all optional, never crashes)
UPI_VPA = os.getenv("UPI_VPA", "").strip()                 # the owner's UPI id, e.g. owner@okicici
UPI_NAME = (os.getenv("UPI_NAME", "").strip() or "vpsbot")[:64]

YOUPI_URL = os.getenv("YOUPI_URL", "").strip().rstrip("/")  # the self-hosted YouPI-PG base URL
YOUPI_MERCHANT_ID = os.getenv("YOUPI_MERCHANT_ID", "").strip()
YOUPI_API_KEY = os.getenv("YOUPI_API_KEY", "").strip()
YOUPI_WEBHOOK_SECRET = os.getenv("YOUPI_WEBHOOK_SECRET", "").strip()

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


def youpi_ready():
    return bool(YOUPI_URL and YOUPI_WEBHOOK_SECRET and WEBAPP_URL)


def youpi_currency_ok(currency):
    return (currency or "").upper() == "INR"


def crypt_ready():
    return bool(CRYPT_ADDRS and WEBAPP_URL)


# ---------------------------------------------------------------- tiny HTTP client (stdlib, blocking)
def _http(url, data=None, headers=None, timeout=25):
    """One request, one shot. Returns parsed JSON (dict). Raises GatewayError on transport/API error."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
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


# ---------------------------------------------------------------- YouPI-PG (self-hosted UPI gateway)
def youpi_create(oid, plan, buyer_name=""):
    """Create a checkout session on the owner's YouPI-PG. Returns {payment_id, checkout_url,...}."""
    body = json.dumps({
        "orderId": f"vpsbot-{oid}",
        "amount": f"{plan.cents / 100:.2f}", "currency": "INR",
        "description": f"vpsbot {plan.name} plan ({plan.days} days)",
        "customer": {"name": (buyer_name or "")[:64]},
        "metadata": {"order": oid, "bot": REPO_URL},
    }).encode()
    headers = {"Content-Type": "application/json", "Idempotency-Key": f"vpsbot-{oid}"}
    if YOUPI_MERCHANT_ID:
        headers["X-Merchant-Id"] = YOUPI_MERCHANT_ID
    if YOUPI_API_KEY:
        headers["X-Merchant-Api-Key"] = YOUPI_API_KEY
    out = _http(YOUPI_URL + "/api/v1/payments", data=body, headers=headers)
    if not out.get("checkoutUrl") or not out.get("paymentId"):
        raise GatewayError("YouPI returned no checkout URL")
    meta_set(oid, provider="youpi", youpi_pid=str(out["paymentId"])[:64],
             youpi_checkout=str(out["checkoutUrl"])[:500], amount_paise=plan.cents)
    return {"payment_id": str(out["paymentId"]), "checkout_url": str(out["checkoutUrl"]),
            "status": out.get("status", "INITIATED")}


def youpi_status(oid):
    """Live status from the gateway (the Mini App polls this while the checkout is open)."""
    m = meta_get(oid)
    if not m.get("youpi_pid"):
        return None
    try:
        out = _http(f"{YOUPI_URL}/api/v1/payments/{urllib.parse.quote(m['youpi_pid'])}")
        return {k: out.get(k) for k in ("paymentId", "status", "method", "verificationLevel", "completedAt")}
    except Exception as e:
        log.warning("YouPI status lookup failed for order %s: %s", oid, e)
        return None


def youpi_verify(raw_body, sig_header):
    """X-Gateway-Signature: sha256=<hex> - HMAC-SHA256 of the RAW body with the webhook secret."""
    if not YOUPI_WEBHOOK_SECRET or not sig_header:
        return False
    sig = (sig_header or "").strip().lower()
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    want = hmac.new(YOUPI_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


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
    from subscriptions import CURRENCY  # the catalog's display currency: YouPI is INR-only
    cur = (currency or CURRENCY or "").upper()
    out = []
    if youpi_ready() and youpi_currency_ok(cur):
        out.append({"key": "youpi", "kind": "upi", "auto": True,
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

    {mode: 'youpi'|'upi'|'crypto', auto: bool, text: str, buttons: [{label,url}], qr: <png bytes>,
     address, amount, coin, uri, checkout_url}
    """
    key = (key or "").strip()
    mkey = key[6:] if key.startswith("crypt:") else key
    if key == "youpi":
        r = youpi_create(oid, plan, buyer_name)
        return {"mode": "youpi", "auto": True, "checkout_url": r["checkout_url"],
                "qr": None, "text": "UPI checkout created - it confirms automatically when you pay."}
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
def youpi_callback(raw_body, sig_header):
    """Verify a YouPI-PG webhook and decide what to do with it.

    Returns (event, error): event is None on failure. Actions:
      paid   - SUCCESS with a matching amount: activate the order
      closed - FAILED / EXPIRED / CANCELLED: tell the buyer
      ignore - malformed / currency we did not expect: log only
    """
    if not youpi_verify(raw_body, sig_header):
        return None, "bad signature"
    try:
        d = json.loads(raw_body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "bad json"
    meta = d.get("metadata") or {}
    oid = 0
    try:
        oid = int(meta.get("order") or 0)
    except (TypeError, ValueError):
        oid = 0
    if not oid:  # older gateway builds only send the merchant orderId
        mo = str(d.get("orderId") or "")
        oid = int(mo.rsplit("-", 1)[-1]) if mo.rsplit("-", 1)[-1].isdigit() else 0
    if not oid:
        return None, "no order in payload"
    o = store.order_get(oid)
    if not o:
        return None, "unknown order"
    event = str(d.get("event") or "").upper()
    ref = str(d.get("utr") or d.get("paymentId") or "")
    if event == "SUCCESS":
        if str(d.get("method") or "UPI").upper() != "UPI":
            return {"action": "ignore", "oid": oid, "why": f"method {d.get('method')}"}, None
        try:
            paise = int(d.get("amountPaise") or round(float(d.get("amount") or 0) * 100))
        except (TypeError, ValueError):
            return None, "bad amount"
        if paise != o["cents"]:  # what the buyer paid must be what the order costs
            log.warning("YouPI webhook amount mismatch on #%s: %s != %s", oid, paise, o["cents"])
            return {"action": "mismatch", "oid": oid, "paise": paise, "ref": ref}, None
        return {"action": "paid", "oid": oid, "ref": ref[:128], "paise": paise}, None
    if event in ("FAILED", "EXPIRED", "CANCELLED"):
        return {"action": "closed", "oid": oid, "event": event, "ref": ref[:128]}, None
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
