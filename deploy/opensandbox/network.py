"""Install/check only YiPilot's dedicated bridge rules. Requires host root."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from policy import BRIDGE, NETWORK, PROFILE, validate_network

INPUT = "YIPILOT-INPUT"
FORWARD = "YIPILOT-FORWARD"
RULES = {
    INPUT: [["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
            ["-j", "DROP"]],
    FORWARD: [["-i", BRIDGE, "-j", "DROP"], ["-o", BRIDGE, "-j", "DROP"]],
}
JUMPS = {"INPUT": ["-i", BRIDGE, "-j", INPUT], "DOCKER-USER": ["-j", FORWARD]}


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def iptables(*args):
    return command(["iptables", "-w", "5", *args])


def setup(install=False):
    if os.geteuid() != 0:
        raise ValueError("Run on the Linux Docker host as root")
    if install:
        command(["modprobe", "br_netfilter"])
        command(["sysctl", "-w", "net.bridge.bridge-nf-call-iptables=1"])
    if Path("/proc/sys/net/bridge/bridge-nf-call-iptables").read_text().strip() != "1":
        raise ValueError("Bridge IPv4 firewall traversal must be enabled")
    names = command(["docker", "network", "ls", "--format", "{{.Name}}"]).splitlines()
    if NETWORK not in names and install:
        command(["docker", "network", "create", "--internal", "--driver", "bridge",
                 "--opt", f"com.docker.network.bridge.name={BRIDGE}",
                 "--opt", "com.docker.network.bridge.enable_icc=false",
                 "--label", f"io.yipilot.profile={PROFILE}", NETWORK])
    network = json.loads(command(["docker", "network", "inspect", NETWORK]))[0]
    validate_network(network)
    if network.get("Labels", {}).get("io.yipilot.profile") != PROFILE:
        raise ValueError("The network is not owned by this profile")
    chains = iptables("-S")
    for chain, rules in RULES.items():
        if f"-N {chain}" not in chains.splitlines():
            if not install:
                raise ValueError(f"Missing firewall chain {chain}")
            iptables("-N", chain)
        actual = iptables("-S", chain).splitlines()[1:]
        expected = ["-A " + chain + " " + " ".join(rule) for rule in rules]
        if not actual and install:
            for rule in rules:
                iptables("-A", chain, *rule)
        elif actual != expected:
            raise ValueError(f"Unexpected rules in {chain}; refusing to overwrite them")
    for parent, jump in JUMPS.items():
        expected = "-A " + parent + " " + " ".join(jump)
        rules = [line for line in iptables("-S", parent).splitlines() if line.startswith("-A ")]
        if expected not in rules and install:
            iptables("-I", parent, "1", *jump)
        elif not rules or rules[0] != expected:
            raise ValueError(f"Required rule must be first in {parent}")
    forwarding = [line for line in iptables("-S", "FORWARD").splitlines() if line.startswith("-A ")]
    if not forwarding or forwarding[0] != "-A FORWARD -j DOCKER-USER":
        raise ValueError("Docker's first forwarding rule must traverse DOCKER-USER")
    if install:
        setup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    setup(parser.parse_args().install)
    print("YiPilot network and host firewall verified")
