# Copyright (C) 2020 Marcel Meuter
# This is a proof of concept in order to reduce the noise e.g. created by telemetry of the operating system or the used
# browser to reduce the traffic dump created by fortrace to contain as much packets related to the actual application
# (e.g. a video stream from youtube.com).
# Our motivation is to use fortrace to create datasets as close as possible to the VPN-nonVPN dataset (ISCXVPN2016)
# (https://www.unb.ca/cic/datasets/vpn.html) used by multiple traffic/application classification machine learning
# models. The noise reduction approach of the authors is to simply keep open only the required applications to capture
# the wanted traffic: "To facilitate the labeling process, when capturing the traffic all unnecessary services and
# applications were closed. (The only application executed was the objective of the capture, e.g., Skype voice-call,
# SFTP file transfer, etc.) While this obviously the first and most straightforward approach to the reduce the noise,
# the operating system, the browser and other background services (e.g. for telemetry) continuously generate traffic,
# which is not related to the actual application at all. As a result we try to further enhance our traffic dumps by
# filtering for known noise sources as an additional pre-processing step before using our generated datasets.

# PyShark is too slow and scapy is bloated (even worse), as a result this PoC is not intended for productive use on
# big (i.e. >1GB) .pcap dumps.
# If you are going to rewrite this script, do it like this: Don't use Python, use Rust and a fitting .pcap parsing
# library such as pcap-parser based on Nom. This allows you to:
# 1. Work on a stream of memory chunks instead of loading the whole dump (!) into memory at once.
# 2. Use parallelism to increase the performance tremendously - the simple algorithm shown here (identify DNS requests
# to the blacklisted IPs and store the (resolved) IPs is splittable into chunks and parallelizable. Filtering the
# complete dump on unwanted protocols and the blacklisted source/destination IPs afterwards is obviously parallelizable
# as well.
import pyshark
import argparse


def load_blacklist():
    with open('blacklist.txt') as f:
        return f.read().splitlines()


def resolve_dns_requests(blacklist, pcap):
    ips = set()
    cap = pyshark.FileCapture(pcap, display_filter='dns')

    for i, packet in enumerate(cap):
        if 'a' in packet.dns.field_names:
            if packet.dns.qry_name in blacklist:
                print(f'[~] Blacklisting {packet.dns.qry_name} @ {packet.dns.a}.')
                ips.add(packet.dns.a)

    cap.close()
    return ips


def apply_filter(pcap, ips, keep_dns, keep_arp, keep_icmp, keep_dhcp):
    display_filter = ''
    for ip in ips:
        display_filter += f'!(ip.addr == {ip}) and '

    display_filter = display_filter[:-4]

    if not keep_arp:
        display_filter += 'and !arp '

    if not keep_dhcp:
        display_filter += 'and !dhcp and !dhcpv6 '

    if not keep_icmp:
        display_filter += 'and !icmp and !icmpv6 '

    if not keep_dns:
        display_filter += 'and !dns and !llmnr and !mdns and !nbns '

    # There is a lot for additional noise generated by the operating system. For the moment, we will just filter these
    # as well.
    display_filter += 'and !nfs and !ssdp and !mount and !igmp and !nlm and !portmap and !stp and !browser'

    # A little bit hacky to modify our dump by applying a display filter, but pyshark does not expose any functionality
    # to modify loaded file dumps and scapy is way to slow and bloated to be used for anything but interactive sessions.
    # And yes, BPF filters are faster than a display filter, but they are not available for file captures.
    # https://github.com/KimiNewt/pyshark/issues/69
    capture = pyshark.FileCapture(pcap, output_file='filtered.pcap', display_filter=display_filter)
    capture.load_packets()


def main():
    # Parse command line arguments.
    parser = argparse.ArgumentParser(description='Haystack dump and config validator.')
    parser.add_argument('pcap', type=str, help='path to the .pcap file')
    parser.add_argument('--keep-dns', help='remove dns requests', default=False, required=False,
                        action='store_true')
    parser.add_argument('--keep-arp', help='remove arp requests', default=False, required=False,
                        action='store_true')
    parser.add_argument('--keep-icmp', help='remove icmp requests', default=False, required=False,
                        action='store_true')
    parser.add_argument('--keep-dhcp', help='remove dhcp requests', default=False, required=False,
                        action='store_true')

    args = parser.parse_args()

    blacklist = load_blacklist()
    print(f'[+] Loaded {len(blacklist)} domains from the blacklist.')

    noisy_ips = resolve_dns_requests(blacklist, args.pcap)
    print(f'[+] Identified noisy {len(noisy_ips)} IPs (e.g. telemetry requests).')

    apply_filter(args.pcap, noisy_ips, args.keep_dns, args.keep_arp, args.keep_icmp, args.keep_dhcp)

    print('[+] Successfully filtered noise from the .pcap dump.')


if __name__ == '__main__':
    main()
