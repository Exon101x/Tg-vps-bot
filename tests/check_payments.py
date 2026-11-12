"""Payment gateways: UPI intent links + QR, YouPI-PG signed webhooks, CryptAPI RSA-signed callbacks,
amount/nonce/idempotency guards, and the Mini App pay routes - end-to-end over real HTTP."""
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
YOUPI_SECRET = "webhook-secret-42"

# ---------------------------------------------------------------- 1. gateway config shapes
PRO = subscriptions.plan("pro")
ok(PRO and not PRO.free and PRO.cents == 250, "1. the PRO plan costs 2.50 INR (250 paise)")
ok(subscriptions.CURRENCY == "INR", "1b. the catalog currency is INR")

# ---------------------------------------------------------------- 2. the HTTP server + a fake payment setup
class StubEngine:
    def enqueue(self, jid): pass
    async def retry(self, jid): return False
    async def stop(self, jid): pass

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
srv = asyncio.run_coroutine_threadsafe(webapi.start(0, "127.0.0.1"), loop).result(10)
PORT = srv.sockets[0].getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"

def req(method, path, body=None, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
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
payments.YOUPI_URL, payments.YOUPI_WEBHOOK_SECRET = "http://127.0.0.1:1", YOUPI_SECRET
payments.CRYPT_ADDRS = {"btc": "bc1qtestaddress00000000000000000000000000"}
payments.YOUPI_MERCHANT_ID, payments.YOUPI_API_KEY = "", ""

PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
payments._PUBKEY_CACHE.update(key=PRIV.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(), ts=time.time())

def youpi_sign(body: bytes):
    return "sha256=" + hmac.new(YOUPI_SECRET.encode(), body, hashlib.sha256).hexdigest()

def crypt_sign(target: str):
    sig = PRIV.sign(f"{BASE}{target}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()

# ---------------------------------------------------------------- 3. payment options, intent link, QR
meth = payments.methods(PRO)
ok([m["key"] for m in meth] == ["youpi", "upi", "crypt:btc"],
   "2. methods: YouPI checkout, direct UPI, then BTC - in that order")
link = payments.upi_intent(2.5, "vpsbot1", note="PRO plan")
ok(link.startswith("upi://pay?") and "pa=owner%40okaxis" in link and "am=2.50" in link
   and "cu=INR" in link and "tr=vpsbot1" in link, "2b. the UPI intent link carries payee/amount/ref")
png = payments.qr_png(link)
ok(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 400, "2c. QR renders to a real PNG")
ok(payments.qr_data_url(link).startswith("data:image/png;base64,"), "2d. and to a data URL for the Mini App")

# ---------------------------------------------------------------- 4. YouPI: signed webhook activates, replay is inert
store.touch_user(BUYER, "Buyer", "buyer")
o1, fresh = subscriptions.order_create(BUYER, PRO)
ok(fresh and o1["status"] == "pending", "3. order opened for the buyer")
body = json.dumps({"event": "SUCCESS", "paymentId": "pid-1", "orderId": f"vpsbot-{o1['id']}",
                   "amount": "2.50", "amountPaise": PRO.cents, "currency": "INR", "method": "UPI",
                   "utr": "123456789012", "metadata": {"order": o1["id"]}}).encode()
st, r = req("POST", "/pay/youpi/cb", body, {"X-Gateway-Signature": youpi_sign(body),
                                            "Content-Type": "application/json"})
ok(st == 200 and r.get("ok") is True, "4. signed YouPI SUCCESS webhook accepted")
o1 = store.order_get(o1["id"])
ok(o1["status"] == "approved" and o1["ref"] == "123456789012", "4b. the order approved itself with the UTR as ref")
ok(subscriptions.effective(BUYER).key == "pro", "4c. the plan is live")
exp_first = store.sub_get(BUYER)["expires"]
st, r = req("POST", "/pay/youpi/cb", body, {"X-Gateway-Signature": youpi_sign(body),
                                            "Content-Type": "application/json"})
ok(st == 200 and store.sub_get(BUYER)["expires"] == exp_first,
   "5. replayed webhook ack'd, nothing activated twice")
body_bad = body.replace(b'"amountPaise": 250', b'"amountPaise": 100')
st, r = req("POST", "/pay/youpi/cb", body_bad, {"X-Gateway-Signature": youpi_sign(body_bad),
                                                "Content-Type": "application/json"})
o2, _ = subscriptions.order_create(BUYER, PRO)
body2 = json.dumps({"event": "SUCCESS", "paymentId": "pid-2", "orderId": f"vpsbot-{o2['id']}",
                    "amount": "1.00", "amountPaise": 100, "currency": "INR", "method": "UPI",
                    "utr": "999", "metadata": {"order": o2["id"]}}).encode()
st, r = req("POST", "/pay/youpi/cb", body2, {"X-Gateway-Signature": youpi_sign(body2),
                                             "Content-Type": "application/json"})
ok(st == 200 and store.order_get(o2["id"])["status"] == "pending",
   "6. an amount mismatch never activates (the owner gets alerted instead)")
st, r = req("POST", "/pay/youpi/cb", body2, {"X-Gateway-Signature": "sha256=" + "0" * 64,
                                             "Content-Type": "application/json"})
ok(st == 403 and store.order_get(o2["id"])["status"] == "pending", "6b. a forged signature is rejected")

# ---------------------------------------------------------------- 5. CryptAPI: RSA-signed callbacks, partials, replays
o3, _ = subscriptions.order_create(BUYER, PRO)
nonce3 = payments.meta_set(o3["id"], provider="cryptapi", coin="btc", expected="0.002",
                          nonce=pysecrets.token_hex(16), recv={})["nonce"]
target = ("/pay/cryptapi/cb?oid=" + str(o3["id"]) + "&nonce=" + nonce3 +
          "&uuid=txa&value_coin=0.001&pending=0&txid_in=TXA&coin=btc&confirmations=1")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and data == b"*ok*", "7. a valid signed crypto callback gets the literal *ok* ack")
ok(store.order_get(o3["id"])["status"] == "pending", "7b. half the amount: order still open (partial)")
target = target.replace("uuid=txa", "uuid=txb").replace("txid_in=TXA", "txid_in=TXB")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
o3 = store.order_get(o3["id"])
ok(st == 200 and o3["status"] == "approved" and o3["ref"] == "TXB",
   "8. the second half completes the payment -> auto-approved with the tx id")
ok(store.sub_get(BUYER)["expires"] == exp_first + PRO.days * 86400,
   "8b. a paid order extends the sub from where it is (+30 days, not a fresh start)")
target = target.replace("uuid=txb", "uuid=txc")  # a third payment after approval: ignored, still *ok*
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and data == b"*ok*" and store.order_get(o3["id"])["ref"] == "TXB",
   "8c. callbacks after approval are ack'd but never double-activate")
o4, _ = subscriptions.order_create(BUYER, PRO)
payments.meta_set(o4["id"], provider="cryptapi", coin="btc", expected="0.002",
                 nonce=pysecrets.token_hex(16), recv={})
target = "/pay/cryptapi/cb?oid=" + str(o4["id"]) + "&nonce=wrong&uuid=z&value_coin=9&pending=0"
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 403 and store.order_get(o4["id"])["status"] == "pending", "9. a wrong nonce is rejected")
target = "/pay/cryptapi/cb?oid=" + str(o4["id"]) + "&nonce=x&uuid=z&value_coin=9&pending=0"
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": "not-base64!!"})
ok(st == 403, "9b. a garbage signature is rejected")
o5, _ = subscriptions.order_create(BUYER, PRO)
m5 = payments.meta_set(o5["id"], provider="cryptapi", coin="btc", expected="0.001", recv={})
target = ("/pay/cryptapi/cb?oid=" + str(o5["id"]) + "&nonce=" + m5["nonce"] +
          "&uuid=txp&value_coin=0.001&pending=1&txid_in=TXP")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and store.order_get(o5["id"])["status"] == "pending",
   "9c. pending (unconfirmed) txs are seen but do not approve")
target = target.replace("pending=1", "pending=0")
st, data = req("GET", target, raw=True, headers={"X-CA-Signature": crypt_sign(target)})
ok(st == 200 and store.order_get(o5["id"])["status"] == "approved", "9d. the same uuid confirmed approves once")

# ---------------------------------------------------------------- 6. the Mini App pay flow (no external calls: direct UPI)
st, r = as_(BUYER, "plans/order", {"plan": "pro"})
ok(st == 200 and [m["key"] for m in r["methods"]] == ["youpi", "upi", "crypt:btc"],
   "10. plans/order lists the payment methods")
oid6 = r["order"]["id"]
st, r = as_(BUYER, "plans/pay", {"id": oid6, "method": "upi"})
ok(st == 200 and r["payment"]["mode"] == "upi" and r["payment"]["qr"].startswith("data:image/png;base64,")
   and r["payment"]["intent"].startswith("upi://pay?") and r["payment"]["auto"] is False,
   "11. plans/pay renders a UPI payment: QR + intent, manual confirm")
st, r = as_(OTHER, "plans/pay", {"id": oid6, "method": "upi"})
ok(st in (403, 404), "11b. someone else's order cannot be paid through the app")
st, r = as_(BUYER, "plans/order_status", {"id": oid6})
ok(st == 200 and r["provider"] == "upi" and r["order"]["status"] == "pending",
   "12. order_status reports the open payment for the polling UI")

# ---------------------------------------------------------------- 7. the chat buttons exist
kinds = {"buy", "pay", "pl", "ord"}
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.py")).read()
ok(all(f'kind == "{k}"' in src for k in kinds), "13. the buy/pay/pl/ord callback kinds are all handled")
ok("qrcode" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "requirements.txt")).read(),
   "13b. the QR dependency is pinned in requirements.txt")

print("OK payments")
