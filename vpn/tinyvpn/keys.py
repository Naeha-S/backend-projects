"""Generate X25519 key pairs for tinyvpn."""

from __future__ import annotations

import argparse
import base64

from .crypto import generate_private_key, private_key_bytes, public_key_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate X25519 keys")
    parser.add_argument("--private-out", required=True)
    parser.add_argument("--public-out", required=True)
    args = parser.parse_args()
    key = generate_private_key()
    with open(args.private_out, "wb") as priv:
        priv.write(base64.b64encode(private_key_bytes(key)))
    with open(args.public_out, "wb") as pub:
        pub.write(base64.b64encode(public_key_bytes(key.public_key())))


if __name__ == "__main__":
    main()
