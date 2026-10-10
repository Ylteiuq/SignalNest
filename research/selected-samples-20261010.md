# Selected sanitized research samples

This repository keeps a small set of offline HTML examples that directly support parser regression:

- One UC list page with a representative unsupported external link.
- One structural EK page with student names and academic records redacted.
- Three CS pagination pages and one CS notice page used by the parser tests.

Contact strings, QQ group identifiers, third-party document identifiers, and tracking comments were removed from retained pages. Each adjacent JSON file records the hash and byte count of the sanitized copy; original page bodies and original body hashes are not included. The CS decision profiles are synthetic and omit institution, college, major, and entry year.

The remaining capture sets, surveys, intermediate evaluation data, and repeated full-output snapshots are excluded from this repository payload. The selected files demonstrate the parser boundaries and make the regression inputs reviewable without publishing the full research archive.
