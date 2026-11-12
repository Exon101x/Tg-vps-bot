"""Payment gateways: UPI intent links + QR, a live fake UPI Gateway (order creation, timestamped
HMAC webhooks, event dedupe, out-of-order sequences), CryptAPI RSA-signed callbacks, amount/nonce/
idempotency guards, and the Mini App pay routes - end-to-end over real HTTP."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import asyncio, base64, hashlib, hmac, http.client, json, secrets as pysecrets, tempfile, threading, time, urllib.parse
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
TMP = tempfile.mkdtemp()
os.environ.update(SECRET_KEY=Fernet.generate_key().decode(), DATABASE_URL=f"sqlite:///{TMP}/pay.db",
                  BOT_TOKEN="123456:ABC-DEF1234ghIklp-zyx51W2po", API_ID="1", API_HASH="0123456789abcdef0123456789abcdef",
                  ALLOWED_USERS="7,9", ADMINS="", DEV_ID="7", LOG_LEVEL="WARNING",
                  SUBSCRIPTION_CURRENCY="INR", SESSION_DIR=os.path.join(TMP, "sess"),
                  PAYMENT_INFO="pay the owner, then /paid <ref> ({amount} {currency} for {plan})")
import store, subscriptions, payments, webapi  # noqa: E402

ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
TOKEN, ADMIN, BUYER, OTHER = os.environ["BOT_TOKEN"], 7, 9, 8
GW_KEY, GW_SECRET, GW_WEBHOOK_SECRET = "key_test123", "sec_test123", "whsec_test123"

# ---------------------------------------------------------------- 1. gateway config shapes
PRO = subscriptions.plan("pro")
ok(PRO and not PRO.free and PRO.cents == 250, "1. the PRO plan costs 2.50 INR (250 paise)")
ok(subscriptions.CURRENCY == "INR", "1b. the catalog currency is INR")

# ---------------------------------------------------------------- 2. the HTTP server + a fake UPI Gateway
class StubEngine:
    def enqueue(self, jid): pass
    async def retry(self, jid): return False
    async def stop(self, jid): pass

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
srv = asyncio.run_coroutine_threadsafe(webapi.start(0, "127.0.0.1"), loop).result(10)
PORT = srv.sockets[0].getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"

GW = {"orders": {}, "n": 0, "auth_fails": 0}  # the fake gateway's database

async def gw_handle(reader, writer):
    """A miniature of github.com/Exon101x/upi-gateway: POST /api/v1/orders (X-Api-Key/X-Api-Secret,
    client_order_id unique), GET /api/v1/orders/{order_no|client_order_id}. Webhooks are posted by
    the test itself, signed like the real gateway (X-Signature: t=...,v1=hmac(t.body))."""
    try:
        line = (await asyncio.wait_for(reader.readline(), 10)).decode()
        parts = line.split()
        method, path = parts[0].upper(), parts[1].split("?")[0]
        headers = {}
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode(errors="replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", "0") or 0)
        body = await reader.read(n) if n else b""

        def reply(code, obj):
            data = json.dumps(obj).encode()
            head = (f"HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: "
                    f"{len(data)}\r\nConnection: close\r\n\r\n").encode()
            writer.write(head + data)

        if headers.get("x-api-key") != GW_KEY or headers.get("x-api-secret") != GW_SECRET:
            GW["auth_fails"] += 1
            return reply(401, {"message": "Invalid API credentials."})
        if method == "POST" and path == "/api/v1/orders":
            d = json.loads(body)
            if not str(d.get("amount", 0)).replace(".", "").isdigit() or float(d["amount"]) < 1:
                return reply(422, {"message": "The amount must be at least 1."})
            for o in GW["orders"].values():
                if o["client_order_id"] == d.get("client_order_id"):
                    return reply(422, {"message": "The client order id has already been taken.",
                                       "errors": {"client_order_id": ["taken"]}})
            GW["n"] += 1
            no = f"ORD{GW['n']:018d}"
            am = float(d["amount"])
            GW["orders"][no] = {"order_id": no, "client_order_id": d["client_order_id"],
                                "amount": f"{am:.2f}", "currency": "INR", "status": "pending",
                                "utr": None, "paid_at": None,
                                "expires_at": "2026-01-01T12:15:00+05:30",
                                "environment": "live", "test": False,
                                "payment_url": f"{GW_BASE}/pay/{no}",
                                "upi_intent": "upi://pay?" + urllib.parse.urlencode(
                                    {"pa": "merchant@upi", "pn": "Gateway", "am": f"{am:.2f}",
                                     "cu": "INR", "tr": no})}
            return reply(201, {"success": True, "data": GW["orders"][no]})
        if method == "GET" and path.startswith("/api/v1/orders/"):
            q = path.rsplit("/", 1)[-1]
            for o in GW["orders"].values():
                if o["order_id"] == q or o["client_order_id"] == q:
                    return reply(200, {"success": True, "data": o})
            return reply(404, {"message": "No query results for model."})
        return reply(404, {"message": "Not found."})
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass

gw_srv = asyncio.run_coroutine_threadsafe(asyncio.start_server(gw_handle, "127.0.0.1", 0), loop).result(10)
GW_PORT = gw_srv.sockets[0].getsockname()[1]
GW_BASE = f"http://127.0.0.1:{GW_PORT}"

def req(method, path, body=None, headers=None, raw=False, port=PORT):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    if raw:
        return r.status, data
    try:
        return r.status, json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return r.status, data

def make_init(uid):
    fields = {"auth_date": str(int(time.time())), "query_id": "AAF17CE0",
              "user": json.dumps({"id": uid, "first_name": "U" + str(uid), "username": "u" + str(uid)})}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    sig = hmac.new(hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest(), dcs.encode(),
                   hashlib.sha256).hexdigest()
    return "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in fields.items()) + "&hash=" + sig

def as_(uid, route, data=None):
    return req("POST", "/api/" + route, json.dumps(data or {}),
               {"X-Tg-Init-Data": make_init(uid), "Content-Type": "application/json"})

webapi.configure(app=None, engine=StubEngine(), token=TOKEN, allowed={ADMIN, BUYER}, webapp_url=BASE, admins={ADMIN})
# gateways are "configured" by pointing the module at the test values (payments reads env at import)
payments.WEBAPP_URL = BASE
webapi.WEBAPP_URL = BASE
payments.UPI_VPA, payments.UPI_NAME = "owner@okaxis", "vpsbot test"
payments.UPIGW_URL, payments.UPIGW_API_KEY = GW_BASE, GW_KEY
payments.UPIGW_API_SECRET, payments.UPIGW_WEBHOOK_SECRET = GW_SECRET, GW_WEBHOOK_SECRET
payments.UPIGW_MAX_SKEW = 300
payments.CRYPT_ADDRS = {"btc": "bc1qtestaddress00000000000000000000000000"}

PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
payments._PUBKEY_CACHE.update(key=PRIV.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(), ts=time.time())

def crypt_sign(target: str):
    sig = PRIV.sign(f"{BASE}{target}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()

def gw_event(order_no, oid, event, seq, eid=None, amount="2.50", utr="UTR123456789", status="paid"):
    """A webhook exactly like the gateway's outbox sends it, signed X-Signature: t=...,v1=..."""
    data = {"order_id": order_no, "client_order_id": f"vpsbot{oid}", "amount": amount,
            "currency": "INR", "status": status, "utr": utr, "submitted_utr": None,
            "environment": "live", "test": False, "payment_url": f"{GW_BASE}/pay/{order_no}",
            "upi_intent": None, "customer": {"name": "Buyer", "email": None, "mobile": None},
            "paid_at": None, "expires_at": None, "original_expires_at": None}
    body = json.dumps({"event_id": eid or pysecrets.token_hex(8), "event_sequence": seq,
                       "occurred_at": "2026-01-01T12:00:00+00:00", "event": event,
                       "data": data}).encode()
    t = str(int(time.time()))
    sig = f"t={t},v1=" + hmac.new(GW_WEBHOOK_SECRET.encode(), f"{t}.".encode() + body,
                                  hashlib.sha256).hexdigest()
    return body, {"X-Signature": sig, "X-Event": event, "Content-Type": "application/json"}

# ---------------------------------------------------------------- 3. payment options, intent link, QR
meth = payments.methods(PRO)
ok([m["key"] for m in meth] == ["upigw", "upi", "crypt:btc"],
   "2. methods: the UPI Gateway checkout, direct UPI, then BTC - in that order")
link = payments.upi_intent(2.5, "vpsbot1", note="PRO plan")
ok(link.startswith("upi://pay?") and "pa=owner%40okaxis" in link and "am=2.50" in link
   and "cu=INR" in link and "tr=vpsbot1" in link, "2b. the UPI intent link carries payee/amount/ref")
png = payments.qr_png(link)
ok(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 400, "2c. QR renders to a real PNG")
ok(payments.qr_data_url(link).startswith("data:image/png;base64,"), "2d. and to a data URL for the Mini App")

# ---------------------------------------------------------------- 4. the UPI Gateway: create, checkout, dedupe
store.touch_user(BUYER, "Buyer", "buyer")
o1, fresh = subscriptions.order_create(BUYER, PRO)
ok(fresh and o1["status"] == "pending", "3. order opened for the buyer")
r1 = payments.start(o1["id"], PRO, "upigw", "Buyer", "INR")
ok(r1["mode"] == "upigw" and r1["checkout_url"].startswith(GW_BASE + "/pay/ORD")
   and r1["intent"].startswith("upi://pay?pa=merchant%40upi") and "am=2.50" in r1["intent"]
   and r1["qr"][:8] == b"\x89PNG\r\n\x1a\n",
   "4. upigw create: checkout page + gateway UPI deep link + its QR, from the live order")
no1 = payments.meta_get(o1["id"])["gw_order"]
r1b = payments.start(o1["id"], PRO, "upigw", "Buyer", "INR")  # same order again: client_order_id taken
ok(r1b["checkout_url"] == r1["checkout_url"] and GW["n"] == 1 and payments.meta_get(o1["id"])["gw_order"] == no1,
   "4b. a retried create reuses the gateway's existing order (no duplicate)")
st, r = req("POST", "/pay/upigw/cb", *gw_event(no1, o1["id"], "order.paid", 1, eid="E1"))
ok(st == 200 and r.get("ok") is True, "5. signed order.paid webhook accepted")
o1 = store.order_get(o1["id"])
ok(o1["status"] == "approved" and o1["ref"] == "UTR123456789", "5b. the order approved itself with the UTR as ref")
ok(subscriptions.effective(BUYER).key == "pro", "5c. the plan is live")
exp_first = store.sub_get(BUYER)["expires"]
st, r = req("POST", "/pay/upigw/cb", *gw_event(no1, o1["id"], "order.paid", 1, eid="E1"))
ok(st == 200 and store.sub_get(BUYER)["expires"] == exp_first,
   "6. a replayed event_id is ack'd, nothing activates twice")

# ---------------------------------------------------------------- 5. the guards: signature, timestamp, amount, order
o2, _ = subscriptions.order_create(BUYER, PRO)
payments.start(o2["id"], PRO, "upigw", "Buyer", "INR")
no2 = payments.meta_get(o2["id"])["gw_order"]
body, hdrs = gw_event(no2, o2["id"], "order.paid", 2, amount="1.00")
st, r = req("POST", "/pay/upigw/cb", body, hdrs)
ok(st == 200 and store.order_get(o2["id"])["status"] == "pending",
   "7. an amount mismatch never activates (the owner gets alerted instead)")
body, hdrs = gw_event(no2, o2["id"], "order.paid", 3)
hdrs["X-Signature"] = "t=" + hdrs["X-Signature"].split("t=")[1][:-8] + "deadbeef"  # mangled v1
st, r = req("POST", "/pay/upigw/cb", body, hdrs)
ok(st == 403 and store.order_get(o2["id"])["status"] == "pending", "7b. a forged signature is rejected")
body, hdrs = gw_event(no2, o2["id"], "order.paid", 4)
t = str(int(time.time()) - 900)  # older than the 300-second window
hdrs["X-Signature"] = f"t={t},v1=" + hmac.new(GW_WEBHOOK_SECRET.encode(), f"{t}.".encode() + body,
                                              hashlib.sha256).hexdigest()
st, r = req("POST", "/pay/upigw/cb", body, hdrs)
ok(st == 403, "7c. a stale timestamp is rejected, even with a valid signature")
body, hdrs = gw_event(no2, o2["id"], "order.paid", 5, amount="2.5x")
st, r = req("POST", "/pay/upigw/cb", body, hdrs)
ok(st == 403, "7d. a malformed amount is rejected")
st, r = req("POST", "/pay/upigw/cb", *gw_event(no2, 999999, "order.paid", 6))
ok(st == 403, "7e. a webhook for an unknown order is rejected")

# ---------------------------------------------------------------- 6. out-of-order events, under_review, expiry
st, r = req("POST", "/pay/upigw/cb", *gw_event(no2, o2["id"], "order.under_review", 10, status="review"))
ok(st == 200 and store.order_get(o2["id"])["status"] == "pending",
   "8. order.under_review tells the buyer, the order stays open")
st, r = req("POST", "/pay/upigw/cb", *gw_event(no2, o2["id"], "order.under_review", 9, status="review"))
ok(st == 200 and store.order_get(o2["id"])["status"] == "pending" and GW["auth_fails"] == 0,
   "8b. an older sequence arriving late is ack'd and ignored (no state rewind)")
st, r = req("POST", "/pay/upigw/cb", *gw_event(no2, o2["id"], "order.expired", 11, status="expired"))
ok(st == 200 and store.order_get(o2["id"])["status"] == "pending",
   "8c. order.expired tells the buyer nothing was charged, the order stays open (late-payment window)")
st, r = req("POST", "/pay/upigw/cb", *gw_event(no2, o2["id"], "order.paid", 12))
ok(st == 200 and store.order_get(o2["id"])["status"] == "approved"
   and store.sub_get(BUYER)["expires"] == exp_first + PRO.days * 86400,
   "9. a paid arriving after an expired still wins - and extends the sub from where it is")

# ---------------------------------------------------------------- 7. wrong API credentials fail the create
o3, _ = subscriptions.order_create(BUYER, PRO)
payments.UPIGW_API_SECRET = "wrong-secret"
try:
    payments.start(o3["id"], PRO, "upigw", "Buyer", "INR")
    ok(False, "10. wrong merchant credentials are refused by the gateway")
except payments.GatewayError as e:
    ok("credentials" in str(e).lower() and GW["auth_fails"] > 0, "10. wrong merchant credentials are refused")
payments.UPIGW_API_SECRET = GW_SECRET

# ---------------------------------------------------------------- 8. CryptAPI: RSA-signed callbacks, partials, replays
o4, _ = subscriptions.order_create(BUYER, PRO)
nonce4 = payments.meta_set(o4["id"], provider="cryptapi", coin="btc", expected="0.002",
                          nonce=pysecrets.token_hex(16), recv={})["nonce"]
target = ("/pay/cryptapi/cb?oid=" + str(o4["id"]) + "&nonce=" + nonce4 +
          "&uuid=txa&value_coin=0.001&pending=0&txid_in=TXA&coin=btc&confirmations=1")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and data == b"*ok*", "11. a valid signed crypto callback gets the literal *ok* ack")
ok(store.order_get(o4["id"])["status"] == "pending", "11b. half the amount: order still open (partial)")
target = target.replace("uuid=txa", "uuid=txb").replace("txid_in=TXA", "txid_in=TXB")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
o4 = store.order_get(o4["id"])
ok(st == 200 and o4["status"] == "approved" and o4["ref"] == "TXB",
   "12. the second half completes the payment -> auto-approved with the tx id")
ok(store.sub_get(BUYER)["expires"] == exp_first + 2 * PRO.days * 86400,
   "12b. a paid order extends the sub from where it is (+30 days, not a fresh start)")
target = target.replace("uuid=txb", "uuid=txc")  # a third payment after approval: ignored, still *ok*
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and data == b"*ok*" and store.order_get(o4["id"])["ref"] == "TXB",
   "12c. callbacks after approval are ack'd but never double-activate")
o5, _ = subscriptions.order_create(BUYER, PRO)
payments.meta_set(o5["id"], provider="cryptapi", coin="btc", expected="0.002",
                 nonce=pysecrets.token_hex(16), recv={})
target = "/pay/cryptapi/cb?oid=" + str(o5["id"]) + "&nonce=wrong&uuid=z&value_coin=9&pending=0"
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 403 and store.order_get(o5["id"])["status"] == "pending", "13. a wrong nonce is rejected")
target = "/pay/cryptapi/cb?oid=" + str(o5["id"]) + "&nonce=x&uuid=z&value_coin=9&pending=0"
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": "not-base64!!"})
ok(st == 403, "13b. a garbage signature is rejected")
o6, _ = subscriptions.order_create(BUYER, PRO)
m6 = payments.meta_set(o6["id"], provider="cryptapi", coin="btc", expected="0.001", recv={})
target = ("/pay/cryptapi/cb?oid=" + str(o6["id"]) + "&nonce=" + m6["nonce"] +
          "&uuid=txp&value_coin=0.001&pending=1&txid_in=TXP")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and store.order_get(o6["id"])["status"] == "pending",
   "13c. pending (unconfirmed) txs are seen but do not approve")
target = target.replace("pending=1", "pending=0")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and store.order_get(o6["id"])["status"] == "approved", "13d. the same uuid confirmed approves once")

# ---------------------------------------------------------------- 9. the Mini App pay flow
st, r = as_(BUYER, "plans/order", {"plan": "pro"})
ok(st == 200 and [m["key"] for m in r["methods"]] == ["upigw", "upi", "crypt:btc"],
   "14. plans/order lists the payment methods")
oid7 = r["order"]["id"]
st, r = as_(BUYER, "plans/pay", {"id": oid7, "method": "upigw"})
ok(st == 200 and r["payment"]["mode"] == "upigw" and r["payment"]["auto"] is True
   and r["payment"]["checkout_url"].startswith(GW_BASE + "/pay/ORD")
   and r["payment"]["qr"].startswith("data:image/png;base64,"),
   "15. plans/pay renders the gateway checkout + QR for the app")
st, r = as_(OTHER, "plans/pay", {"id": oid7, "method": "upigw"})
ok(st in (403, 404), "15b. someone else's order cannot be paid through the app")
no7 = payments.meta_get(oid7)["gw_order"]
GW["orders"][no7]["status"], GW["orders"][no7]["utr"] = "paid", "UTR777777"  # paid, webhook lost
st, r = as_(BUYER, "plans/order_status", {"id": oid7})
ok(st == 200 and r["gateway"]["status"] == "paid" and r["order"]["status"] == "approved"
   and r["order"]["ref"] == "UTR777777",
   "16. the status poll self-heals a lost webhook from the gateway's live status")
st, r = as_(BUYER, "plans/order", {"plan": "pro"})
oid8 = r["order"]["id"]
st, r = as_(BUYER, "plans/pay", {"id": oid8, "method": "upi"})
ok(st == 200 and r["payment"]["mode"] == "upi" and r["payment"]["intent"].startswith("upi://pay?")
   and r["payment"]["auto"] is False,
   "16b. the direct UPI method still works in the app (QR + intent, manual confirm)")
st, r = as_(OTHER, "plans/order_status", {"id": oid7})
ok(st in (403, 404), "16c. order_status refuses someone else's order")

# ---------------------------------------------------------------- 10. the chat buttons exist
kinds = {"buy", "pay", "pl", "ord"}
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.py")).read()
ok(all(f'kind == "{k}"' in src for k in kinds), "17. the buy/pay/pl/ord callback kinds are all handled")
ok('r["mode"] == "upigw"' in src, "17b. the chat flow renders the gateway checkout")
ok("qrcode" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "requirements.txt")).read(),
   "17c. the QR dependency is pinned in requirements.txt")

print("OK payments")
