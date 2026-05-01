#!/usr/bin/env python3
"""NaehaVPN server entrypoint."""

from naehavpn.node import main
from naehavpn.tunnel import require_admin


if __name__ == "__main__":
    require_admin()
    main()
