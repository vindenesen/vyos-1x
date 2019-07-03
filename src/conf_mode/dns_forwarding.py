#!/usr/bin/env python3
#
# Copyright (C) 2018 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

import sys
import os

import argparse
import jinja2
import netifaces

import vyos.util

from vyos.config import Config
from vyos import ConfigError


parser = argparse.ArgumentParser()
parser.add_argument("--dhclient", action="store_true",
                    help="Started from dhclient-script")

config_file = r'/etc/powerdns/recursor.conf'

# XXX: pdns recursor doesn't like whitespace near entry separators,
# especially in the semicolon-separated lists of name servers.
# Please be careful if you edit the template.
config_tmpl = """
### Autogenerated by dns_forwarding.py ###

# Non-configurable defaults
daemon=yes
threads=1
allow-from=0.0.0.0/0, ::/0
log-common-errors=yes
non-local-bind=yes
query-local-address=0.0.0.0
query-local-address6=::

# cache-size
max-cache-entries={{ cache_size }}

# negative TTL for NXDOMAIN
max-negative-ttl={{ negative_ttl }}

# ignore-hosts-file
export-etc-hosts={{ export_hosts_file }}

# listen-on
local-address={{ listen_on | join(',') }}

# domain ... server ...
{% if domains -%}

forward-zones-recurse={% for d in domains %}
{{ d.name }}={{ d.servers | join(";") }}
{{- "," if not loop.last -}}
{% endfor %}

{% endif %}

# dnssec
dnssec={{ dnssec }}

{% if name_servers -%}
# name-server
forward-zones-recurse=.={{ name_servers | join(';') }}
{% else %}
# no name-servers specified - start full recursor
{% endif %}

"""

default_config_data = {
    'cache_size': 10000,
    'export_hosts_file': 'yes',
    'listen_on': [],
    'interfaces': [],
    'name_servers': [],
    'negative_ttl': 3600,
    'domains': [],
    'dnssec': 'process-no-validate'
}


# borrowed from: https://github.com/donjajo/py-world/blob/master/resolvconfReader.py, THX!
def get_resolvers(file):
    try:
        with open(file, 'r') as resolvconf:
            lines = [line.split('#', 1)[0].rstrip()
                     for line in resolvconf.readlines()]
            resolvers = [line.split()[1]
                         for line in lines if 'nameserver' in line]
        return resolvers
    except IOError:
        return []


def get_config(arguments):
    dns = default_config_data
    conf = Config()

    if arguments.dhclient:
        conf.exists = conf.exists_effective
        conf.return_value = conf.return_effective_value
        conf.return_values = conf.return_effective_values

    if not conf.exists('service dns forwarding'):
        return None

    conf.set_level('service dns forwarding')

    if conf.exists('cache-size'):
        cache_size = conf.return_value('cache-size')
        dns['cache_size'] = cache_size

    if conf.exists('negative-ttl'):
        negative_ttl = conf.return_value('negative-ttl')
        dns['negative_ttl'] = negative_ttl

    if conf.exists('domain'):
        for node in conf.list_nodes('domain'):
            servers = conf.return_values("domain {0} server".format(node))
            domain = {
                "name": node,
                "servers": bracketize_ipv6_addrs(servers)
            }
            dns['domains'].append(domain)

    if conf.exists('ignore-hosts-file'):
        dns['export_hosts_file'] = "no"

    if conf.exists('name-server'):
        name_servers = conf.return_values('name-server')
        dns['name_servers'] = dns['name_servers'] + name_servers

    if conf.exists('system'):
        conf.set_level('system')
        system_name_servers = []
        system_name_servers = conf.return_values('name-server')
        if not system_name_servers:
            print(
                "DNS forwarding warning: No name-servers set under 'system name-server'\n")
        else:
            dns['name_servers'] = dns['name_servers'] + system_name_servers
        conf.set_level('service dns forwarding')

    dns['name_servers'] = bracketize_ipv6_addrs(dns['name_servers'])

    if conf.exists('listen-address'):
        dns['listen_on'] = conf.return_values('listen-address')

    if conf.exists('dnssec'):
        dns['dnssec'] = conf.return_value('dnssec')

    ## Hacks and tricks

    # The old VyOS syntax that comes from dnsmasq was "listen-on $interface".
    # pdns wants addresses instead, so we emulate it by looking up all addresses
    # of a given interface and writing them to the config
    if conf.exists('listen-on'):
        print("WARNING: since VyOS 1.2.0, \"service dns forwarding listen-on\" is a limited compatibility option.")
        print("It will only make DNS forwarder listen on addresses assigned to the interface at the time of commit")
        print("which means it will NOT work properly with VRRP/clustering or addresses received from DHCP.")
        print("Please reconfigure your system with \"service dns forwarding listen-address\" instead.")

        interfaces = conf.return_values('listen-on')

        listen4 = []
        listen6 = []
        for interface in interfaces:
            try:
                addrs = netifaces.ifaddresses(interface)
            except ValueError:
                print(
                    "WARNING: interface {0} does not exist".format(interface))
                continue

            if netifaces.AF_INET in addrs.keys():
                for ip4 in addrs[netifaces.AF_INET]:
                    listen4.append(ip4['addr'])

            if netifaces.AF_INET6 in addrs.keys():
                for ip6 in addrs[netifaces.AF_INET6]:
                    listen6.append(ip6['addr'])

            if (not listen4) and (not (listen6)):
                print(
                    "WARNING: interface {0} has no configured addresses".format(interface))

        dns['listen_on'] = dns['listen_on'] + listen4 + listen6

        # Save interfaces in the dict for the reference
        dns['interfaces'] = interfaces

    # Add name servers received from DHCP
    if conf.exists('dhcp'):
        interfaces = []
        interfaces = conf.return_values('dhcp')
        for interface in interfaces:
            dhcp_resolvers = get_resolvers(
                "/etc/resolv.conf.dhclient-new-{0}".format(interface))
            if dhcp_resolvers:
                dns['name_servers'] = dns['name_servers'] + dhcp_resolvers

    return dns


def bracketize_ipv6_addrs(addrs):
    """Wraps each IPv6 addr in addrs in [], leaving IPv4 addrs untouched."""
    return ['[{0}]'.format(a) if a.count(':') > 1 else a for a in addrs]


def verify(dns):
    # bail out early - looks like removal from running config
    if dns is None:
        return None

    if not dns['listen_on']:
        raise ConfigError(
            "Error: DNS forwarding requires either a listen-address (preferred) or a listen-on option")

    if dns['domains']:
        for domain in dns['domains']:
            if not domain['servers']:
                raise ConfigError(
                    'Error: No server configured for domain {0}'.format(domain['name']))

    return None


def generate(dns):
    # bail out early - looks like removal from running config
    if dns is None:
        return None

    tmpl = jinja2.Template(config_tmpl, trim_blocks=True)

    config_text = tmpl.render(dns)
    with open(config_file, 'w') as f:
        f.write(config_text)
    return None


def apply(dns):
    if dns is not None:
        os.system("systemctl restart pdns-recursor")
    else:
        # DNS forwarding is removed in the commit
        os.system("systemctl stop pdns-recursor")
        if os.path.isfile(config_file):
            os.unlink(config_file)


if __name__ == '__main__':
    args = parser.parse_args()

    if args.dhclient:
        # There's a big chance it was triggered by a commit still in progress
        # so we need to wait until the new values are in the running config
        vyos.util.wait_for_commit_lock()

    try:
        c = get_config(args)
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        sys.exit(1)
