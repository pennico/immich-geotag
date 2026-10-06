# Security policy

## Reporting a vulnerability

If you find a security issue in this project, please report it
privately:

- open a private security advisory on this repository
  (Security tab → "Report a vulnerability"), or
- contact the maintainer on GitHub: [@pennico](https://github.com/pennico)

Please do not open a public issue with security details.

## Scope notes

This tool talks to your own Immich server and to external APIs (Google
Cloud Vision, SerpApi, Gemini, Nominatim) using keys you provide. API
keys live in `.env`, which is gitignored — never commit them. The tool
only ever *adds* GPS coordinates; it does not modify or delete photos
or existing locations.
