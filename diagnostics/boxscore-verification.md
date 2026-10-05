# Live box-score verification

Run: `2026-08-13 14:04:54 UTC` · runner `Linux` · attempt `3`

Season `2025-2026` · season URL suffix `25-26`

Exit IP `4.246.117.83` · US Moses Lake · org `AS8075 Microsoft Corporation`

## 1. Plain HTTP reachability

- status `406`, 6,977 bytes
- page title: `406 - Not Acceptable`
- response headers:
    - `Connection: close`
    - `Content-Length: 6977`
    - `Retry-After: 0`
    - `Cache-Control: no-store`
    - `Accept-Ranges: bytes`
    - `Date: Thu, 13 Aug 2026 14:04:54 GMT`
    - `X-Cache: MISS`
    - `X-Cache-Hits: 0`
    - `X-Timer: S1786629895.769627,VS0,VE44`
    - `Set-Cookie: mp_ad_targeting=session%3DD%26subSession%3D4; Path=/; Secure; SameSite=Lax`
    - `Vary: Accept-Encoding,Origin`
    - `alt-svc: h3=":443";ma=86400,h3-29=":443";ma=86400,h3-27=":443";ma=86400`

```
 
<html>
    <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width, initial-scale=1, minimum-scale=1, user-scalable=no' />
        <title>406 - Not Acceptable</title>
        <style>
            @font-face {
                font-family: Siro;
                src: url(https://asset.maxpreps.io/includes/font/siro_regular_macroman/siro-regular-webfont.woff2) format('woff2'), url(https://asset.maxpreps.io/includes/font/siro_regular_macroman/siro-regular-webfont.woff) format('woff');
                font-weight: 100 400;
                font-style: normal;
                font-display: swap;
            }
        </style>
        <style>
            bod
```
- **FAIL** — 406 Not Acceptable. Reverting the request headers did NOT clear this, so it is keyed on the exit IP, not the headers — MaxPreps appears to reject datacenter/cloud ranges (GitHub runners are Azure). CI cannot verify or scrape; only a residential/VPN exit in an allowed country works.
