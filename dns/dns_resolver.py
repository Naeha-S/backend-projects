import os
import random
import argparse
import dns.dnssec  # type: ignore[import-not-found]
import dns.message  # type: ignore[import-not-found]
import dns.name  # type: ignore[import-not-found]
import dns.rdatatype  # type: ignore[import-not-found]
import dns.rrset  # type: ignore[import-not-found]
import socket
import struct
import sys
import time

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[38;5;218m"
CYAN = "\033[38;5;225m"
YELLOW = "\033[38;5;219m"
MAGENTA = "\033[38;5;205m"
RED = "\033[38;5;197m"
ORANGE = "\033[38;5;212m"
WHITE = "\033[38;5;255m"
GRAY = "\033[38;5;247m"
BLUE = "\033[38;5;183m"
PINK_1 = "\033[38;5;211m"
PINK_2 = "\033[38;5;217m"
PINK_3 = "\033[38;5;224m"

DNS_SERVER = "8.8.8.8"
DNS_PORT = 53
ROOT_SERVERS = [
    "198.41.0.4",
    "199.9.14.201",
    "192.33.4.12",
    "199.7.91.13",
    "192.203.230.10",
    "192.5.5.241",
    "192.112.36.4",
    "198.97.190.53",
    "192.36.148.17",
    "192.58.128.30",
    "193.0.14.129",
    "199.7.83.42",
    "202.12.27.33",
]
RECORD_TYPES = {
    1: "A",
    2: "NS",
    5: "CNAME",
    6: "SOA",
    12: "PTR",
    15: "MX",
    16: "TXT",
    28: "AAAA",
    41: "OPT",
    43: "DS",
    46: "RRSIG",
    47: "NSEC",
    48: "DNSKEY",
    50: "NSEC3",
    255: "ANY",
}
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
CACHE = {}
DNSSEC_ZONE_CACHE = {}
ROOT_TRUST_ANCHOR = {
    "key_tag": 20326,
    "algorithm": 8,
    "digest_type": 2,
    "digest": "E06D44B80B8F1D39A95C0B0D7C65D08458E880409BBC683457104237C7F8EC8D",
}

def clear():
    os.system("cls" if os.name == "nt" else "clear")

def journal_banner():
    """Print the project header."""
    print(f"""
{MAGENTA}{BOLD}  .:* DNS LAB // NAEHA EDITION *:.{RESET}
{PINK_1}  -------------------------------------------------------------{RESET}
{PINK_2}  handmade resolver  |  raw UDP packets  |  RFC 1035 decoding{RESET}
{PINK_3}  modes: A / AAAA / MX / NS / SHOWCASE (A -> NS + raw NS bytes){RESET}
{PINK_1}  -------------------------------------------------------------{RESET}
""")

def build_query(domain, qtype=1, rd=True, dnssec_ok=False):
    """Build a DNS query packet manually (RFC 1035 format)."""
    tx_id = random.randint(0, 0xFFFF)
    flags = 0x0100 if rd else 0x0000
    qdcount = 1
    arcount = 1 if dnssec_ok else 0
    header = struct.pack(">HHHHHH", tx_id, flags, qdcount, 0, 0, arcount)

    question = b""
    if domain != ".":
        for part in domain.rstrip(".").split("."):
            if not part:
                continue
            encoded = part.encode()
            question += struct.pack("B", len(encoded)) + encoded
    question += b"\x00"
    question += struct.pack(">HH", qtype, 1)  # QTYPE + QCLASS(IN)

    if dnssec_ok:
        ttl = 0x00008000
        question += b"\x00" + struct.pack(">HHIH", 41, 1232, ttl, 0)

    return header + question, tx_id


def cache_key(domain, qtype):
    return (domain.rstrip(".").lower(), qtype)


def cache_get(domain, qtype):
    entry = CACHE.get(cache_key(domain, qtype))
    if not entry:
        return None
    if entry["expires_at"] <= time.time():
        CACHE.pop(cache_key(domain, qtype), None)
        return None
    return entry["parsed"]


def cache_put(domain, qtype, parsed):
    ttl_values = [rr["ttl"] for rr in parsed.get("answers", []) if rr.get("ttl")]
    ttl_values += [rr["ttl"] for rr in parsed.get("authority", []) if rr.get("ttl")]
    ttl_values += [rr["ttl"] for rr in parsed.get("additional", []) if rr.get("ttl")]
    ttl = max(1, min(ttl_values) if ttl_values else 60)
    CACHE[cache_key(domain, qtype)] = {"parsed": parsed, "expires_at": time.time() + ttl}


def recv_exactly(sock, size):
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("unexpected TCP EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_query(query, server=DNS_SERVER, port=DNS_PORT, use_tcp=False):
    """Send one DNS request and return response bytes plus elapsed ms."""
    start = time.time()
    if use_tcp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect((server, port))
            sock.sendall(struct.pack(">H", len(query)) + query)
            response_size = struct.unpack(">H", recv_exactly(sock, 2))[0]
            response = recv_exactly(sock, response_size)
            return response, (time.time() - start) * 1000
        finally:
            sock.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    try:
        sock.sendto(query, (server, port))
        response, _ = sock.recvfrom(4096)
        return response, (time.time() - start) * 1000
    finally:
        sock.close()


def wire_message(domain, qtype, server=DNS_SERVER, use_tcp=False, dnssec_ok=False, rd=True):
    """Query a DNS server and parse the wire response into a dnspython message."""
    query, _ = build_query(domain, qtype, rd=rd, dnssec_ok=dnssec_ok)
    response, elapsed = send_query(query, server=server, use_tcp=use_tcp)
    return dns.message.from_wire(response), response, elapsed


def first_rrset(message, rdtype):
    for rrset in message.answer:
        if rrset.rdtype == rdtype:
            return rrset
    for rrset in message.authority:
        if rrset.rdtype == rdtype:
            return rrset
    return None


def matching_rrsig(message, rrset):
    for sigset in message.answer:
        if sigset.rdtype != dns.rdatatype.RRSIG or len(sigset) == 0:
            continue
        if sigset.name.to_text() != rrset.name.to_text():
            continue
        for rrsig in sigset:
            if int(rrsig.covers()) == int(rrset.rdtype):
                return sigset
    return None


def zone_text(name):
    return str(name).rstrip(".") or "."


def trusted_root_keys():
    if dns.name.root in DNSSEC_ZONE_CACHE:
        return DNSSEC_ZONE_CACHE[dns.name.root]

    message, _, _ = wire_message(".", dns.rdatatype.DNSKEY, dnssec_ok=True, use_tcp=True)
    dnskey_rrset = first_rrset(message, dns.rdatatype.DNSKEY)
    rrsig_rrset = first_rrset(message, dns.rdatatype.RRSIG)
    if not dnskey_rrset or not rrsig_rrset:
        raise ValueError("root DNSKEY response missing DNSKEY or RRSIG")

    trusted_keys = []
    for key in dnskey_rrset:
        ds = dns.dnssec.make_ds(dns.name.root, key, "SHA256")
        if (
            ds.key_tag == ROOT_TRUST_ANCHOR["key_tag"]
            and ds.algorithm == ROOT_TRUST_ANCHOR["algorithm"]
            and ds.digest_type == ROOT_TRUST_ANCHOR["digest_type"]
            and ds.digest.hex().upper() == ROOT_TRUST_ANCHOR["digest"]
        ):
            trusted_keys.append(key)

    if not trusted_keys:
        raise ValueError("root trust anchor did not match any root DNSKEY")

    trusted_rrset = dns.rrset.from_rdata_list(dnskey_rrset.name, dnskey_rrset.ttl, trusted_keys)
    dns.dnssec.validate(dnskey_rrset, rrsig_rrset, {dns.name.root: trusted_rrset})
    DNSSEC_ZONE_CACHE[dns.name.root] = dnskey_rrset
    return dnskey_rrset


def validate_zone_keys(zone_name):
    zone_name = dns.name.from_text(zone_text(zone_name))
    if zone_name in DNSSEC_ZONE_CACHE:
        return DNSSEC_ZONE_CACHE[zone_name]

    if zone_name == dns.name.root:
        return trusted_root_keys()

    parent = zone_name.parent()
    parent_keys = validate_zone_keys(parent)

    ds_message, _, _ = wire_message(zone_text(zone_name), dns.rdatatype.DS, dnssec_ok=True, use_tcp=True)
    ds_rrset = first_rrset(ds_message, dns.rdatatype.DS)
    ds_rrsig = first_rrset(ds_message, dns.rdatatype.RRSIG)
    if not ds_rrset or not ds_rrsig:
        raise ValueError(f"no DNSSEC DS chain found for {zone_text(zone_name)}")
    dns.dnssec.validate(ds_rrset, ds_rrsig, {parent: parent_keys})

    dnskey_message, _, _ = wire_message(zone_text(zone_name), dns.rdatatype.DNSKEY, dnssec_ok=True, use_tcp=True)
    dnskey_rrset = first_rrset(dnskey_message, dns.rdatatype.DNSKEY)
    dnskey_rrsig = first_rrset(dnskey_message, dns.rdatatype.RRSIG)
    if not dnskey_rrset or not dnskey_rrsig:
        raise ValueError(f"no DNSKEY RRset found for {zone_text(zone_name)}")

    trusted_keys = []
    for dnskey in dnskey_rrset:
        for ds in ds_rrset:
            try:
                candidate = dns.dnssec.make_ds(zone_name, dnskey, ds.digest_type)
            except Exception:
                continue
            if candidate.digest == ds.digest:
                trusted_keys.append(dnskey)

    if not trusted_keys:
        raise ValueError(f"no DNSKEY in {zone_text(zone_name)} matched its parent DS")

    trusted_rrset = dns.rrset.from_rdata_list(dnskey_rrset.name, dnskey_rrset.ttl, trusted_keys)
    dns.dnssec.validate(dnskey_rrset, dnskey_rrsig, {zone_name: trusted_rrset})
    DNSSEC_ZONE_CACHE[zone_name] = dnskey_rrset
    return dnskey_rrset


def validate_dnssec_response(domain, qtype, use_tcp=False):
    """Validate the signed answer RRset against the DNSSEC chain of trust."""
    try:
        message, _, _ = wire_message(domain, qtype, dnssec_ok=True, use_tcp=True)
    except Exception as exc:
        return {"ok": False, "zone": None, "error": str(exc)}

    answer_rrsets = [rrset for rrset in message.answer if rrset.rdtype != dns.rdatatype.RRSIG]
    if not answer_rrsets:
        return {"ok": False, "zone": None, "error": "no answer RRset to validate"}

    signature_sets = [rrset for rrset in message.answer if rrset.rdtype == dns.rdatatype.RRSIG]
    if not signature_sets:
        return {"ok": False, "zone": None, "error": "response had no RRSIGs"}

    zone_name = signature_sets[0][0].signer
    zone_keys = validate_zone_keys(zone_name)

    validated = []
    for rrset in answer_rrsets:
        rrsig = matching_rrsig(message, rrset)
        if rrsig is None:
            continue
        dns.dnssec.validate(rrset, rrsig, {zone_name: zone_keys})
        validated.append(rrset.name.to_text())

    if not validated:
        return {"ok": False, "zone": zone_text(zone_name), "error": "no signed answer RRset matched the response"}

    return {"ok": True, "zone": zone_text(zone_name), "validated": validated}


def parse_rr(data, offset):
    name, offset = parse_name(data, offset)
    if offset + 10 > len(data):
        return None, offset

    rtype, rclass, ttl, rdlength = struct.unpack(">HHIH", data[offset : offset + 10])
    offset += 10
    rdata_raw = data[offset : offset + rdlength]
    rdata = rdata_raw.hex()

    if rtype == 1 and rdlength == 4:
        rdata = ".".join(str(b) for b in rdata_raw)
    elif rtype == 28 and rdlength == 16:
        parts = [f"{rdata_raw[i]:02x}{rdata_raw[i + 1]:02x}" for i in range(0, 16, 2)]
        rdata = ":".join(parts)
    elif rtype in (2, 5, 12):
        rdata, _ = parse_name(data, offset)
    elif rtype == 15:
        pref = struct.unpack(">H", rdata_raw[:2])[0] if rdlength >= 2 else 0
        name_part, _ = parse_name(data, offset + 2)
        rdata = f"{pref} {name_part}"
    elif rtype == 16 and rdlength:
        rdata = rdata_raw[1:].decode(errors="replace")
    elif rtype == 6:
        primary, primary_end = parse_name(data, offset)
        mailbox, mailbox_end = parse_name(data, primary_end)
        rdata = f"{primary} {mailbox}"
    elif rtype == 41:
        rdata = "EDNS0"

    record = {
        "name": name,
        "type": RECORD_TYPES.get(rtype, str(rtype)),
        "ttl": ttl,
        "rdata": rdata,
        "rdata_raw": rdata_raw.hex(),
    }
    return record, offset + rdlength


def parse_rr_section(data, offset, count):
    records = []
    for _ in range(count):
        record, offset = parse_rr(data, offset)
        if not record:
            break
        records.append(record)
    return records, offset

def print_hex_dump(data, label="RAW RESPONSE"):
    """Render bytes as a colored hex dump."""
    print(f"\n{YELLOW}{BOLD}  ┌─ {label} ({len(data)} bytes) {'─' * (44 - len(label))}┐{RESET}")
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        offset = f"{GRAY}  │ {i:04x}{RESET}"
        hex_part = " ".join(f"{PINK_2}{b:02x}{RESET}" for b in chunk)
        hex_part = hex_part.ljust(16 * 3 - 1)
        ascii_part = "".join(
            f"{PINK_1}{chr(b)}{RESET}" if 32 <= b < 127 else f"{GRAY}·{RESET}"
            for b in chunk
        )
        print(f"{offset}  {hex_part}  {GRAY}│{RESET} {ascii_part}")
        time.sleep(0.02)
    print(f"{YELLOW}{BOLD}  └{'─' * 54}┘{RESET}\n")


def pause(seconds, pace=1.0):
    if pace > 0:
        time.sleep(seconds * pace)


def dnssec_chain_line(zone_name):
    zone_name = (zone_name or "").strip().rstrip(".")
    if not zone_name:
        return "."
    labels = zone_name.split(".")
    chain = ["."]
    for idx in range(len(labels) - 1, -1, -1):
        chain.append(".".join(labels[idx:]) + ".")
    return " -> ".join(chain)

def parse_name(data, offset):
    """Parse a DNS name at the given offset, including pointer compression."""
    labels = []
    jumped = False
    jump_offset = None
    max_jumps = 10
    jumps = 0

    while True:
        if offset >= len(data):
            break
        length = data[offset]

        if length == 0:
            offset += 1
            break
        elif (length & 0xC0) == 0xC0:          # compression pointer
            if offset + 1 >= len(data):
                break
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                jump_offset = offset + 2
            jumped = True
            offset = ptr
            jumps += 1
            if jumps > max_jumps:
                break
        else:
            offset += 1
            labels.append(data[offset:offset + length].decode(errors="replace"))
            offset += length

    return ".".join(labels), (jump_offset if jumped else offset)

def parse_response(data):
    """Parse DNS header, question, and answer sections from raw bytes."""
    if len(data) < 12:
        return None

    tx_id, flags, qdcount, ancount, nscount, arcount = struct.unpack(">HHHHHH", data[:12])

    qr = (flags >> 15) & 1
    opcode = (flags >> 11) & 0xF
    aa = (flags >> 10) & 1
    tc = (flags >> 9) & 1
    rd = (flags >> 8) & 1
    ra = (flags >> 7) & 1
    rcode = flags & 0xF

    offset = 12

    questions = []
    for _ in range(qdcount):
        name, offset = parse_name(data, offset)
        if offset + 4 > len(data):
            break
        qtype, qclass = struct.unpack(">HH", data[offset : offset + 4])
        offset += 4
        questions.append({"name": name, "type": RECORD_TYPES.get(qtype, str(qtype)), "class": qclass})

    answers, offset = parse_rr_section(data, offset, ancount)
    authority, offset = parse_rr_section(data, offset, nscount)
    additional, offset = parse_rr_section(data, offset, arcount)

    return {
        "tx_id": tx_id,
        "flags": flags,
        "qr": qr,
        "aa": aa,
        "tc": tc,
        "rd": rd,
        "ra": ra,
        "rcode": rcode,
        "rcode_str": RCODES.get(rcode, str(rcode)),
        "questions": questions,
        "answers": answers,
        "authority": authority,
        "additional": additional,
        "ancount": ancount,
        "nscount": nscount,
        "arcount": arcount,
    }

def print_journal_parse(parsed, domain, elapsed):
    """Print decoded DNS fields in a compact, demo-friendly layout."""
    w = 60
    print(f"{MAGENTA}{BOLD}  ┌{'─' * w}┐")
    print(f"  │{'  PARSED DNS STORY':^{w}}│")
    print(f"  └{'─' * w}┘{RESET}\n")

    print(f"{WHITE}{BOLD}  CORE HEADER{RESET}")
    print(f"  {GRAY}{'─'*56}{RESET}")
    rcode_color = GREEN if parsed['rcode'] == 0 else RED
    print(f"  {DIM}Transaction ID{RESET}   {PINK_2}0x{parsed['tx_id']:04X}{RESET}")
    print(f"  {DIM}Status        {RESET}   {rcode_color}{BOLD}{parsed['rcode_str']}{RESET}")
    print(f"  {DIM}Flags         {RESET}   {GRAY}QR={parsed['qr']} AA={parsed['aa']} TC={parsed['tc']} RD={parsed['rd']} RA={parsed['ra']}{RESET}")
    print(f"  {DIM}Answers       {RESET}   {YELLOW}{parsed['ancount']}{RESET}")
    print(f"  {DIM}Query time    {RESET}   {ORANGE}{elapsed:.1f}ms{RESET}\n")

    print(f"{WHITE}{BOLD}  QUESTION CHECK{RESET}")
    print(f"  {GRAY}{'─'*56}{RESET}")
    for q in parsed['questions']:
        print(f"  {DIM}Domain   {RESET}  {PINK_1}{BOLD}{q['name']}{RESET}  {GRAY}({q['type']} IN){RESET}")
    print()

    if parsed['answers']:
        print(f"{WHITE}{BOLD}  ANSWER RECORDS{RESET}")
        print(f"  {GRAY}{'─'*56}{RESET}")
        print(f"  {GRAY}{'TYPE':<8} {'TTL':<8} {'VALUE':<30} RAW (hex){RESET}")
        print(f"  {GRAY}{'─'*56}{RESET}")
        for ans in parsed['answers']:
            tcolor = GREEN if ans['type'] == 'A' else BLUE if ans['type'] == 'AAAA' else ORANGE
            raw_preview = ans['rdata_raw'][:12] + ("…" if len(ans['rdata_raw']) > 12 else "")
            print(f"  {tcolor}{BOLD}{ans['type']:<8}{RESET} {YELLOW}{ans['ttl']:<8}{RESET} {WHITE}{ans['rdata']:<30}{RESET} {GRAY}{raw_preview}{RESET}")
        print()
    else:
        print(f"  {RED}No answer records found.{RESET}\n")

    ns_answers = [a for a in parsed['answers'] if a['type'] == 'NS']
    if ns_answers:
        print(f"{WHITE}{BOLD}  NS RAW (for N.S.){RESET}")
        print(f"  {GRAY}{'─'*56}{RESET}")
        print(f"  {GRAY}{'NAMESERVER':<38} RAW (hex){RESET}")
        print(f"  {GRAY}{'─'*56}{RESET}")
        for ans in ns_answers:
            print(f"  {CYAN}{ans['rdata']:<38}{RESET} {GRAY}{ans['rdata_raw']}{RESET}")
        print()

    print(f"  {GRAY}Queried {DNS_SERVER}:{DNS_PORT} · raw UDP · struct.pack · RFC 1035{RESET}")
    print(f"  {PINK_3}{DIM}look cute, parse hard.{RESET}\n")

def resolve(domain, qtype=1, show_header=True, validate_dnssec=False, show_hex=True, pace=1.0):
    """Resolve one record type for a domain and print raw + parsed output."""
    if show_header:
        clear()
        journal_banner()

    print(f"  {GRAY}Resolving {CYAN}{BOLD}{domain}{RESET}{GRAY} → building raw DNS packet...{RESET}\n")
    pause(0.4, pace)

    query, _ = build_query(domain, qtype)

    if show_hex:
        print_hex_dump(query, "QUERY PACKET (sent)")
        pause(0.3, pace)

    print(f"  {GRAY}Sending UDP → {DNS_SERVER}:{DNS_PORT} ...{RESET}")
    try:
        response, elapsed = send_query(query)
    except socket.timeout:
        print(f"  {RED}Request timed out after 5s.{RESET}\n")
        return
    except OSError as e:
        print(f"  {RED}Network error: {e}{RESET}\n")
        return

    pause(0.3, pace)
    print(f"  {GREEN}Response received!{RESET} {GRAY}({len(response)} bytes in {elapsed:.1f}ms){RESET}\n")
    pause(0.4, pace)

    if show_hex:
        print_hex_dump(response, "RESPONSE PACKET (received)")
        pause(0.5, pace)

    parsed = parse_response(response)
    if parsed:
        print_journal_parse(parsed, domain, elapsed)
        if validate_dnssec:
            try:
                dnssec_result = validate_dnssec_response(domain, qtype)
            except Exception as exc:
                dnssec_result = {"ok": False, "zone": None, "error": str(exc)}

            print(f"{WHITE}{BOLD}  DNSSEC VALIDATION{RESET}")
            print(f"  {GRAY}{'─'*56}{RESET}")
            if dnssec_result.get("ok"):
                validated_list = ", ".join(dnssec_result.get("validated", []))
                chain = dnssec_chain_line(dnssec_result.get("zone"))
                print(f"  {GREEN}{BOLD}PASSED{RESET} {GRAY}zone={dnssec_result.get('zone')} validated={validated_list}{RESET}")
                print(f"  {PINK_3}{DIM}chain:{RESET} {GRAY}{chain}{RESET}\n")
            else:
                error_text = dnssec_result.get("error", "validation failed")
                zone_label = dnssec_result.get("zone") or "unknown"
                print(f"  {RED}{BOLD}FAILED{RESET} {GRAY}zone={zone_label} reason={error_text}{RESET}\n")
    else:
        print(f"  {RED}Failed to parse response.{RESET}\n")

def run_journal_showcase(domain, validate_dnssec=False, show_hex=True, pace=1.0):
    """Run two lookups back-to-back for demo flow: A then NS."""
    clear()
    journal_banner()
    print(f"  {MAGENTA}{BOLD}Showcase mode:{RESET} {GRAY}running A then NS for {PINK_1}{domain}{RESET}\n")
    pause(0.3, pace)

    for qtype in (1, 2):
        qlabel = RECORD_TYPES.get(qtype, str(qtype))
        print(f"{YELLOW}{BOLD}  === {qlabel} LOOKUP ==={RESET}")
        resolve(domain, qtype, show_header=False, validate_dnssec=validate_dnssec, show_hex=show_hex, pace=pace)
        pause(0.2, pace)


def parse_cli_args():
    parser = argparse.ArgumentParser(prog="naeha-dig", description="Handmade DNS resolver with DNSSEC validation.")
    parser.add_argument("domain", help="domain to query")
    parser.add_argument("record_type", nargs="?", default="A", help="record type like A, NS, MX, AAAA")
    parser.add_argument("--dnssec", action="store_true", help="request DNSSEC records and validate the chain of trust")
    parser.add_argument("--tcp", action="store_true", help="reserved for future use; validation uses TCP internally")
    parser.add_argument("--no-hex", action="store_true", help="hide hex dumps")
    parser.add_argument("--recording", action="store_true", help="presentation preset: cleaner output and smoother pacing")
    parser.add_argument("--pace", type=float, default=1.0, help="timing multiplier for transitions (default: 1.0)")
    parser.add_argument("--showcase", action="store_true", help="run A then NS showcase mode")
    return parser.parse_args()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"\n  {YELLOW}Usage:{RESET}  python dns_resolver.py <domain> [record_type] [--dnssec] [--no-hex]\n")
        print(f"  {GRAY}Examples:{RESET}")
        print(f"    python dns_resolver.py google.com")
        print(f"    python dns_resolver.py cloudflare.com MX --dnssec")
        print(f"    python dns_resolver.py cloudflare.com MX --dnssec --recording")
        print(f"    python dns_resolver.py google.com NS")
        print(f"    python dns_resolver.py google.com SHOWCASE\n")
        sys.exit(1)

    args = parse_cli_args()
    domain = args.domain.lower().strip()
    qtype_str = args.record_type.upper().strip()

    show_hex = not args.no_hex
    pace = args.pace
    if args.recording:
        show_hex = False
        pace = min(pace, 0.7)

    if qtype_str == "SHOWCASE" or args.showcase:
        run_journal_showcase(domain, validate_dnssec=args.dnssec, show_hex=show_hex, pace=pace)
        sys.exit(0)

    qtype_map = {v: k for k, v in RECORD_TYPES.items()}
    qtype = qtype_map.get(qtype_str, 1)

    resolve(domain, qtype, validate_dnssec=args.dnssec, show_hex=show_hex, pace=pace)
