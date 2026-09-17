#!/usr/bin/env python3
from __future__ import annotations

import json

from scripts.host_router import HostRouter, NodeHealth

DESKTOP = 'DESKTOP-PDQK954'
NOTERI = 'Noteri'


def main() -> int:
    corr_failover = 'host-router-runtime-e2e-failover-20260917'
    corr_recovered = 'host-router-runtime-e2e-recovered-20260917'

    first = HostRouter(DESKTOP, NOTERI).select(
        corr_failover,
        [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)],
    )
    restarted = HostRouter(DESKTOP, NOTERI).select(
        corr_failover,
        [NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)],
    )
    recovered = HostRouter(DESKTOP, NOTERI).select(
        corr_recovered,
        [NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)],
    )

    if first != restarted:
        raise AssertionError(f'persistent decision changed after restart: {first!r} != {restarted!r}')
    if first.node_id != NOTERI:
        raise AssertionError(f'failover was not preserved: {first!r}')
    if recovered.node_id != DESKTOP:
        raise AssertionError(f'new correlation did not return to primary: {recovered!r}')
    if recovered.fencing_token <= first.fencing_token:
        raise AssertionError('fencing token did not increase for new correlation')

    print(json.dumps({
        'status': 'ok',
        'auto_store': True,
        'failover': first.__dict__,
        'restart': restarted.__dict__,
        'recovered': recovered.__dict__,
        'persistent_restart': True,
        'fencing_increased': True,
    }, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
