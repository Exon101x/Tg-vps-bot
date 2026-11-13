"""Build the deployable Mini App for static hosting (Cloudflare Pages, Netlify, Vercel, GitHub
Pages, or your own nginx): rewrites webapp/ into webapp-dist/ with

  * root-relative asset paths (a static host serves from its root, not from /static/),
  * window.API_BASE injected into index.html, pointing the app at the bot's public address,
  * a small _headers file (Netlify/Cloudflare) and _redirects for SPA-ish 404s -> /.

The bot serves the same app itself at /app; this build only exists for the split setup:

    python scripts/build_webapp.py https://vps.example.com
    python scripts/build_webapp.py            # API_BASE from WEBAPP_URL, else same-origin

Then point the static host's output directory at webapp-dist/, and on the bot set
ALLOWED_ORIGINS to the host the app is served from (see DEPLOY.md, 'Hosting the Mini App
elsewhere'). The webapp/ sources stay the single source of truth: rerun after editing them.
"""
import json
import os
import shutil
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "webapp")
OUT = os.path.join(ROOT, "webapp-dist")


def headers_file(api_base):
    """Netlify/Cloudflare _headers: cache policy plus a CSP shaped like the one the bot itself
    sends - the difference being connect-src, which must allow the API host."""
    csp = ("default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
           f"connect-src 'self'{f' {api_base}' if api_base else ''}; img-src 'self' data:; "
           "object-src 'none'; base-uri 'self'; "
           "frame-ancestors https://web.telegram.org https://*.telegram.org; form-action 'none'")
    return f"""\
# permissive cache for assets, no cache for the shell: deploys show at once
/static/*
  Cache-Control: public, max-age=3600
/
  Cache-Control: no-cache
  Content-Security-Policy: {csp}
"""


REDIRECTS = """\
# any unknown path is the app shell (the app is one page); keep 200 so relative assets resolve
/*  /index.html  200
"""


def readme(api_base):
    host = "https://your-app.pages.dev"
    return f"""# vpsbot Mini App - static hosting build

This folder is the Mini App ready for any static host. The API it calls
{f"(`{api_base}`)" if api_base else "(same origin)"} is served by the **bot itself**;
rebuild with `python scripts/build_webapp.py <your-bot-https-address>` if that changes.

Deploy this folder, then on the bot set one variable:

    ALLOWED_ORIGINS={host}     # the address the host gave you

(Also fine as a comma list. `WEBAPP_URL` origins are always allowed.)

## Cloudflare Pages (recommended: free, fast, no build)

Dashboard -> Workers & Pages -> Create -> Pages -> Upload assets: drop this folder.
Note the `https://<name>.pages.dev` address it prints.

## Netlify

Drag this folder onto https://app.netlify.com/drop, or `netlify deploy --dir webapp-dist`.
The `_headers` and `_redirects` files are already here and are picked up automatically.

## Vercel

`vercel deploy --prebuilt` with this folder as the output, or point a project's
output directory at `webapp-dist/`. (_headers/_redirects are ignored; Vercel's defaults
are equivalent for a single-page app this small.)

## Your own nginx (on the same or another VPS)

    server {{
        listen 443 ssl;
        server_name app.example.com;
        root /var/www/vpsbot-webapp;     # this folder
        location / {{ try_files $uri /index.html; }}
    }}

## After deploying

* set `ALLOWED_ORIGINS` on the bot and restart it,
* in BotFather -> /mybots -> Bot Settings -> Menu Button (or the bot's `WEBAPP_URL`),
  point the app at `{host}`.

Sign-in needs no setup: every request carries Telegram's initData signature, which
the bot verifies - from any origin.
"""



def build(api_base):
    api_base = (api_base or "").strip().rstrip("/")
    if api_base and not api_base.startswith("https://"):
        sys.exit(f"API base must be an https address (Telegram only opens https web apps): {api_base!r}")
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(os.path.join(OUT, "static"))
    shutil.copy2(os.path.join(SRC, "logo.png"), os.path.join(OUT, "static", "logo.png"))

    js = open(os.path.join(SRC, "app.js"), encoding="utf-8").read()
    css = open(os.path.join(SRC, "app.css"), encoding="utf-8").read()
    html = open(os.path.join(SRC, "index.html"), encoding="utf-8").read()

    with open(os.path.join(OUT, "static", "app.js"), "w", encoding="utf-8") as f:
        f.write(js)
    with open(os.path.join(OUT, "static", "app.css"), "w", encoding="utf-8") as f:
        f.write(css)

    # inject API_BASE right before app.js so the const in app.js picks it up
    inject = f"<script>window.API_BASE={json.dumps(api_base)};</script>\n" if api_base else ""
    html = html.replace('<script src="/static/app.js"></script>',
                        f"{inject}<script src=\"/static/app.js\"></script>")
    if api_base and "window.API_BASE" not in html:
        sys.exit("index.html changed: the app.js script tag was not found to inject API_BASE after")
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    with open(os.path.join(OUT, "_headers"), "w", encoding="utf-8") as f:
        f.write(headers_file(api_base))
    with open(os.path.join(OUT, "_redirects"), "w", encoding="utf-8") as f:
        f.write(REDIRECTS)
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme(api_base))

    print(f"built {os.path.relpath(OUT, ROOT)}/ for static hosting"
          + (f" - API base: {api_base}" if api_base else " - same-origin API (the bot serves it)"))


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else os.getenv("WEBAPP_URL", "")
    build(base)
