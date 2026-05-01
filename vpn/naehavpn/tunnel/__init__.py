"""NaehaVPN tunnel exports."""

from .device import TunDevice, create_tun, require_admin
from .mtu import MtuProfile, compute_mtu_profile
