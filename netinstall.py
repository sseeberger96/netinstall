import argparse
import itertools
import logging
import os
import socket
import struct
import threading
import time

from ptftplib import tftpserver
from scapy.all import *


parser = argparse.ArgumentParser()
parser.add_argument('--interface', '-i', default=conf.iface)
parser.add_argument('--boot-file', default='netboot.xyz.efi')
parser.add_argument('--verbose', dest='log_level', action='store_const',
                    const=logging.DEBUG, default=logging.INFO)

group = parser.add_argument_group('TFTP server')
group.add_argument('--no-tftpd', dest='tftpd', action='store_false')
group.add_argument('--tftp-dir', default='./tftproot')
group.add_argument('--rfc1350', action='store_true')

group = parser.add_argument_group('Address Overrides')
group.add_argument('--boot-server',
    help='address of boot server listening on UDP port 4011 '
         '(default: IP address of selected interface)')
group.add_argument('--dhcp-server',
    help='address from which to send DHCP Offers '
         '(default: same as boot server address)')
group.add_argument('--tftp-server',
    help='address of TFTP server '
         '(default: same as boot server address)')

args = parser.parse_args()


args.tftp_dir = os.path.abspath(args.tftp_dir)
if args.boot_server is None:
    args.boot_server = get_if_addr(args.interface)
if args.dhcp_server is None:
    args.dhcp_server = args.boot_server
if args.tftp_server is None:
    args.tftp_server = args.boot_server


logging.basicConfig(level=args.log_level)


def dhcp_options_to_dict(options):
    return dict(itertools.takewhile(lambda x: x != 'end', options))


class TFTPServerThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

        self.server = tftpserver.TFTPServer(args.interface, args.tftp_dir,
                                            strict_rfc1350=args.rfc1350)

    def run(self):
        self.server.serve_forever()


class BootServerThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

        # Bind to port 4011
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((args.boot_server, 4011))

    def send_pxe_ack(self, request, sender):
        req_options = dhcp_options_to_dict(request[DHCP].options)
        options = {
            'message-type': 'ack',
            'server_id': args.boot_server,
            'vendor_class_id': 'PXEClient',
            'pxe_client_machine_identifier':
                req_options['pxe_client_machine_identifier'],
        }

        packet = (
            BOOTP(
                op='BOOTREPLY',
                xid=request[BOOTP].xid,
                ciaddr=request[BOOTP].ciaddr,
                siaddr=args.tftp_server,
                chaddr=request[BOOTP].chaddr,

                sname=args.tftp_server,
                file=args.boot_file,
            )/
            DHCP(options=list(options.items()) + ['end'])
        )

        logging.debug('Sending DHCP ACK: %r', packet)
        self.sock.sendto(bytes(packet), sender)

    def run(self):
        while True:
            packet, addr = self.sock.recvfrom(65536)
            packet = BOOTP(packet)
            logging.debug('Received proxyDHCP packet: %r', packet)
            logging.debug('Sender is %r', addr)

            options = dhcp_options_to_dict(packet[DHCP].options)
            if options.get('message-type') == 3:  # DHCP Request
                self.send_pxe_ack(packet, addr)


class DHCPSnifferThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

    def send_pxe_offer(self, request):
        req_options = dhcp_options_to_dict(request[DHCP].options)

        options = {
            'message-type': 'offer',
            'server_id': args.dhcp_server,
            'vendor_class_id': 'PXEClient',
            'pxe_client_machine_identifier':
                req_options['pxe_client_machine_identifier'],
        }

        packet = (
            Ether(dst='ff:ff:ff:ff:ff:ff')/
            IP(src=args.dhcp_server, dst='255.255.255.255')/
            UDP(sport=67, dport=68)/
            BOOTP(
                op='BOOTREPLY',
                xid=request[BOOTP].xid,
                flags=1 << 15,  # broadcast flag
                siaddr=args.boot_server,  # next is boot server
                chaddr=request[BOOTP].chaddr,
            )/
            DHCP(options=list(options.items()) + ['end'])
        )

        logging.debug('Sending extended DHCP Offer: %r', packet)
        sendp(packet, iface=args.interface)

    def handle_dhcp_packet(self, packet):
        logging.debug('Received DHCP packet: %r', packet)

        options = dhcp_options_to_dict(packet[DHCP].options)
        if options.get('message-type') == 1:  # DHCP Discover
            if options.get('vendor_class_id', b'').startswith(b'PXEClient:'):
                logging.info('Received DHCP Discover from PXE client')
                self.send_pxe_offer(packet)

    def run(self):
        sniff(
            filter='udp and src port 68 and dst port 67',
            prn=self.handle_dhcp_packet,
            iface=args.interface
        )


BootServerThread().start()
DHCPSnifferThread().start()
if args.tftpd:
    TFTPServerThread().start()

while True:
    time.sleep(1)