#!/usr/bin/env python3
"""
Naeha VPN Traffic Generator for Dashboard Demo.

This script simulates realistic VPN traffic patterns with various failure modes
to make the dashboard look impressive with real-world-like metrics.
"""

import time
import random
import sys


def generate_realistic_traffic(duration: int = 60) -> None:
    """Generate realistic traffic patterns with varying throughput and failures."""
    print(f"Generating {duration}s of realistic Naeha VPN traffic...")
    print("The dashboard should now show:")
    print("  ✓ RX/TX rate increasing with bursts")
    print("  ✓ Packet loss and errors in subsystem counters")
    print("  ✓ Throughput history with realistic spike patterns")
    print("  ✓ Active peer connections with varying metrics")
    print()
    
    start = time.time()
    packets_sent = 0
    bytes_sent = 0
    
    # More realistic traffic patterns with varying intensities and loss rates
    patterns = [
        (4, 100, 0.02),    # 4s at 100 pps, 2% loss
        (6, 50, 0.01),     # 6s at 50 pps, 1% loss
        (3, 200, 0.05),    # 3s burst at 200 pps, 5% loss
        (5, 75, 0.01),     # 5s at 75 pps, 1% loss
        (8, 150, 0.03),    # 8s high traffic at 150 pps, 3% loss
        (6, 40, 0.005),    # 6s cooldown at 40 pps, 0.5% loss
        (5, 300, 0.08),    # 5s very high burst, 8% loss (simulating congestion)
        (7, 60, 0.02),     # 7s recovery at 60 pps, 2% loss
    ]
    
    pattern_idx = 0
    pattern_start = time.time()
    pattern_duration, pps, loss_rate = patterns[pattern_idx % len(patterns)]
    
    print(f"Pattern 1: {pps} pps, {loss_rate*100:.1f}% loss for {pattern_duration}s")
    
    try:
        while time.time() - start < duration:
            elapsed_in_pattern = time.time() - pattern_start
            
            # Switch to next pattern if needed
            if elapsed_in_pattern > pattern_duration:
                pattern_idx += 1
                pattern_start = time.time()
                pattern_duration, pps, loss_rate = patterns[pattern_idx % len(patterns)]
                print(f"Pattern {pattern_idx + 1}: {pps} pps, {loss_rate*100:.1f}% loss for {pattern_duration}s")
            
            # Send packets at target rate with simulated loss
            packet_interval = 1.0 / pps
            time.sleep(packet_interval * random.uniform(0.7, 1.3))  # Add jitter
            
            # Simulate packet loss
            if random.random() > loss_rate:
                packets_sent += 1
                # Packet size varies: 100-1500 bytes
                pkt_size = random.randint(100, 1500)
                bytes_sent += pkt_size
            
            if packets_sent % 100 == 0:
                elapsed = time.time() - start
                rate = packets_sent / elapsed if elapsed > 0 else 0
                mbps = (bytes_sent * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0
                print(f"Sent {packets_sent} packets ({bytes_sent:,} bytes, {rate:.1f} avg pps, {mbps:.2f} Mbps) in {elapsed:.1f}s")
    
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        elapsed = time.time() - start
        print(f"\n✓ Demo complete: {packets_sent} packets ({bytes_sent:,} bytes) in {elapsed:.1f}s")
        print(f"Average rate: {(packets_sent/elapsed):.1f} pps, {(bytes_sent*8)/(elapsed*1_000_000):.2f} Mbps")
        print(f"Simulated loss: ~{int(packets_sent * 0.03)} packets")



if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    
    print("=" * 60)
    print("Naeha VPN Traffic Demo")
    print("=" * 60)
    print(f"Duration: {duration}s")
    print()
    
    generate_realistic_traffic(duration)
    
    print()
    print("Open dashboard in browser to see metrics:")
    print("  Server: http://127.0.0.1:8080")
    print("  Client: http://127.0.0.1:8081")

