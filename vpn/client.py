#!/usr/bin/env python3
"""tinyvpn client entrypoint."""

from tinyvpn.node import main
from tinyvpn.tun import require_admin


if __name__ == "__main__":
    require_admin()
    main()
