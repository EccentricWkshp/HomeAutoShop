"""
Transcribed manufacturer trouble-code lists, one JSON file per make.

Data, not code (SPEC §8.1a), for the same reason the VIN schemes are: three
thousand rows transcribed from somebody's published document is a table, and a
table in a Python module invites somebody to put a condition in it.

**Not translated, on purpose.** The generic SAE set in `dtc.py` goes through
gettext because those definitions are standardized and finite. These are one
manufacturer's own wording for its own codes; a translation of them would be
somebody's paraphrase of a diagnostic definition, which is the exact thing
§8.3c refuses to do.

Each file carries the make, the makes it also covers, and where it came from —
`build_dtc_list` writes them and is how a committed table is checked against
its source, since the source documents are not in this repository.
"""
