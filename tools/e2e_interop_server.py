# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server side of the echannel interop test. Loads searx/echannel.py standalone
(no searx package init) and either generates a key pair or processes a request.

Usage:
    python tools/e2e_interop_server.py gen
    E2E_PRIVATE_KEY=... python tools/e2e_interop_server.py process <req.json> <expected-plaintext>
"""
import importlib.util
import json
import os
import sys

_PATH = os.path.join(os.path.dirname(__file__), "..", "searx", "echannel.py")
_spec = importlib.util.spec_from_file_location("echannel", _PATH)
ech = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ech)


def main() -> None:
    mode = sys.argv[1]
    if mode == "gen":
        priv, pub = ech._generate_keypair()  # noqa: SLF001 (test)
        print(json.dumps({"priv": priv, "pub": pub}))
        return
    if mode == "process":
        ech.reset_cache_for_tests()
        with open(sys.argv[2], encoding="utf-8") as fh:
            body = json.load(fh)
        expected = sys.argv[3].encode("utf-8")
        plaintext, sess = ech.open_request(body)
        if plaintext != expected:
            raise SystemExit(f"SERVER MISMATCH: got {plaintext!r}, expected {expected!r}")
        resp = sess.encrypt("pong: " + plaintext.decode("utf-8"))
        print(json.dumps(resp))
        return
    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
