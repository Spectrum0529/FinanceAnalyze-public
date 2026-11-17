"""Exact public team-port factory, no gateway or alternate graph."""
def create_server_model_ports(profile):
    from finance_platform.research.team_ports import create_server_model_ports as create
    return create(profile)
