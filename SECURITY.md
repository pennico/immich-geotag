# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub's private
vulnerability reporting:

- go to the [Security tab → "Report a vulnerability"](https://github.com/pennico/immich-geotag/security/advisories/new),
  fill in the form, and submit. Only the maintainer can see the report.

Please do not open a public issue with security details. If the form is
unavailable for any reason, open a public issue that only says you have
a security report and asks for a private channel — without details —
and the maintainer will follow up.

You can expect an acknowledgement within a week. Fixes are released on
`main`; there are no separately maintained release branches.

## Scope notes

This tool talks to your own Immich server and to external APIs (Google
Cloud Vision, SerpApi, Gemini, Nominatim) using keys you provide. API
keys live in `.env`, which is gitignored — never commit them. The tool
only ever *adds* GPS coordinates; it does not modify or delete photos
or existing locations.

The optional review app (`time_fill_review_app.py --serve`) binds
`127.0.0.1` only and its write endpoint (`POST /apply`) refuses
cross-site requests: it requires a per-run token embedded in the page,
a JSON content type, a loopback `Host` header, and a same-origin
`Origin`. Bugs that bypass these checks are in scope.

The `dispatcher/` directory is the automation that develops this repo
(see "How this repo is built" in the README); it runs only on the
maintainer's own infrastructure and acts only on trusted authors.
Reports about it are welcome too.
