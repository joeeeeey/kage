"""Kage's own service entries (O1c5): `python3 -m kage --deployment DIR serve dashboard|receiver …`, the programs a
generated deployment's services.json names (the followup service is the program's own `run`). A deployment that
ships its own dashboard / receiver programs never reaches this package."""
from __future__ import annotations

SERVICES = ('dashboard', 'receiver')


def main(assembly, runtime, argv):
    """Run service `argv[0]` of an assembled deployment; returns the exit status."""
    if not argv or argv[0] not in SERVICES:
        print(f"usage: serve {{{','.join(SERVICES)}}} [options]")
        return 2
    if argv[0] == 'dashboard':
        from kage.serve import dashboard
        return dashboard.main(assembly, runtime, argv[1:])
    from kage.serve import receiver
    return receiver.main(assembly, runtime, argv[1:])
