# White-label brand configuration

HOSPES ships as a product, not as one show. Every operator-visible surface —
the login page, the dashboard header, the guest portal — draws its name, logo,
favicon, palette, and typeface from tracked YAML. Re-branding an installation
for a client is an edit to that YAML and a restart: **no code change, no
rebuild, no template fork.**

## Where a brand lives

| File | Scope |
|---|---|
| `config/brand.yaml` | The installation default. Serves the login page and any show that declares no brand of its own. |
| `config/brands/<show>.yaml` | One show's override, referenced by `brand_ref` in `config/shows/<show>.yaml`. |
| `config/brand-assets/*.svg` | The tracked logo and favicon custody directory. |

Every field:

```yaml
version: 1
show_id: client-x                                  # per-show files only
show_name: "Client: Podcast X"                     # replaces HOSPES in every rendered page
logo_path: config/brand-assets/client-x-logo.svg   # tracked asset, served at /operator/brand/logo
favicon_path: config/brand-assets/client-x-favicon.svg
primary_color: "#596d8f"                           # --brand-primary
secondary_color: "#182131"                         # --brand-secondary
accent_color: "#d2b375"                            # --brand-accent
font_family: ui-sans-serif, system-ui, sans-serif  # --brand-font
custom_domain: guests.podcast-x.example            # or null
```

A partial file is a supported profile, not an error: `hospes.branding.merge_brand`
layers whatever is present over the shipped defaults, so an operator who only
wants a new `show_name` writes one line. A per-show brand file is held to the
full contract by `hospes config validate`, because a client show that silently
falls back to the HOSPES palette is a white-label failure, not a default.

## What the browser receives

`hospes/branding.py` is the only renderer. It publishes five CSS custom
properties into a `:root` block injected before `</head>`:

```css
:root{--brand-primary:#596d8f;--brand-secondary:#182131;--brand-accent:#d2b375;--brand-font:ui-sans-serif, system-ui, sans-serif;--brand-logo:url("/operator/brand/logo")}
```

`dashboard/assets/styles.css` reads all five through `var()` — body typeface,
`.brand-mark`, `.eyebrow`, `.mode-badge`, `.btn-primary`, and the login shell —
so the injected block re-themes the product rather than decorating an unused
variable.

**The policy names the style, and that is load-bearing.** The operator serves
`default-src 'self'`, which forbids inline styles outright: an injected
`<style>` block is dropped by the browser unless the policy names it. Every
branded response therefore carries its own header —

```
content-security-policy: default-src 'self'; …; style-src 'self' 'sha256-<digest of the exact block>'
```

— computed by `branding.content_security_policy` over the same string that was
injected. `'unsafe-inline'` is never used, and a brand cannot widen the policy:
the hash covers exactly one validated block. Before this contract the variables
were injected and then silently discarded by the browser, which is why the
predicate asserts the header and not merely the markup.

## Served assets

| Route | Serves |
|---|---|
| `GET /operator/brand/logo` | The active show's `logo_path`. |
| `GET /operator/brand/favicon.ico` | The active show's `favicon_path`. |
| `GET /guest/brand/logo`, `GET /guest/brand/favicon.ico` | The guest portal's brand. |
| `GET /guest/brand.css` | The portal's same-origin stylesheet (`style-src 'self'`). |

Brand assets live outside the dashboard static mount, so the *only* way one
leaves the server is a brand route, which resolves the active show rather than
a caller-supplied path. `branding.brand_asset` rejects traversal, absolute
paths, nested paths, symlinks, unknown media types, and any file outside
`config/brand-assets/`.

**Before login there is no show scope, so only the default brand is served.**
The login page must be branded, but an unauthenticated caller must not be able
to enumerate a tenant's shows or their marks. The operator resolves a per-show
brand only from the signed, HttpOnly show cookie; a missing, foreign, or
tampered cookie falls back to `config/brand.yaml`.

## Custom domain

`custom_domain` is a bare lowercase hostname — no scheme, no port, no path.
`branding.custom_domain` rejects anything else, and `hospes config validate`
fails closed on a malformed value rather than emitting a broken guest link.

The consumer is the guest portal: `guest_portal.create_portal_token` routes the
one-time link through `branding.portal_base_url`, so a configured domain
produces `https://guests.podcast-x.example/guest/#token=…` and an unconfigured
one falls back to the caller's `base_url`. The returned receipt carries
`public_base_url` so the chosen origin is recorded rather than inferred.

## Changing a brand

```bash
$EDITOR config/brands/client-x.yaml     # show_name, colours, custom_domain
$EDITOR config/brand-assets/            # drop in the client's logo + favicon
python3 -m hospes config validate       # fails closed on an unsafe brand
# restart the operator process
```

The predicate is `python -m pytest tests/issue_predicates/test_issue_30.py -q`.
