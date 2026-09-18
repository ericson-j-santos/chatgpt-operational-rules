from __future__ import annotations

import json
import socket
import subprocess
import time

HOST = "192.168.1.60"
HOSTNAME = "Noteri"
MAC = "78-46-5c-1a-78-bb"
BROADCASTS = ("192.168.1.255", "255.255.255.255")
PORTS = (7, 9)
TCP_PORTS = (135, 445, 3389, 5985, 5986, 22)


def run(args: list[str], timeout: int = 10) -> dict[str, object]:
    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "rc": cp.returncode,
            "stdout_tail": cp.stdout[-2000:],
            "stderr_tail": cp.stderr[-1000:],
        }
    except Exception as exc:
        return {"error_type": type(exc).__name__}


def magic_packet() -> bytes:
    raw = bytes.fromhex(MAC.replace("-", ""))
    return b"\xff" * 6 + raw * 16


def send_wol_burst() -> int:
    packet = magic_packet()
    sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(5):
            for address in BROADCASTS:
                for port in PORTS:
                    sock.sendto(packet, (address, port))
                    sent += 1
            time.sleep(0.25)
    finally:
        sock.close()
    return sent


def tcp_probe() -> dict[str, str]:
    result: dict[str, str] = {}
    for port in TCP_PORTS:
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            sock.connect((HOST, port))
            result[str(port)] = "open"
        except Exception as exc:
            result[str(port)] = type(exc).__name__
        finally:
            sock.close()
    return result


def main() -> int:
    before = run(["arp.exe", "-a", HOST])
    flush = run(["arp.exe", "-d", HOST])
    sent = send_wol_burst()
    time.sleep(15)
    ping = run(["ping.exe", "-4", "-n", "2", "-w", "1500", HOSTNAME], timeout=8)
    tcp = tcp_probe()
    after = run(["arp.exe", "-a", HOST])
    after_text = str(after.get("stdout_tail", "")).casefold()
    mac_reappeared = MAC.casefold() in after_text
    any_tcp_open = any(value == "open" for value in tcp.values())

    payload = {
        "host": HOST,
        "hostname": HOSTNAME,
        "wol_packets_sent": sent,
        "arp_flush_rc": flush.get("rc"),
        "ping_rc": ping.get("rc"),
        "mac_reappeared_after_flush": mac_reappeared,
        "any_tcp_open": any_tcp_open,
        "tcp_ports": tcp,
        "link_recovery_evidenced": bool(mac_reappeared or any_tcp_open or ping.get("rc") == 0),
        "secret_value_exposed": False,
    }
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0 if payload["link_recovery_evidenced"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
