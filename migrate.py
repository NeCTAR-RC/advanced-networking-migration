#!/usr/bin/env python

import argparse
from functools import lru_cache

import openstack
from prettytable import PrettyTable

# easy to understand terms
NAME_MIDONET = 'legacy'
NAME_OVN = 'modern'

CACHE_TIMEOUT = '2 minutes'
TAG_LEGACY = 'legacy-networking'


@lru_cache
def get_conn():
    return openstack.connect(cloud='envvars')


def is_tenant_manager():
    conn = get_conn()
    # NOTE: use role assignment, check_grant does not work due to policy
    try:
        r = conn.get_role('TenantManager')
    except openstack.exceptions.ForbiddenException:
        return False

    if r is None:
        return False

    role = r.id
    user = conn.current_user_id
    project = conn.current_project_id

    query = {'user': user, 'project': project, 'role': role}

    ra = conn.list_role_assignments(query)

    return bool(ra)


def is_legacy_project():
    conn = get_conn()
    project = conn.identity.get_project(conn.current_project_id)
    return TAG_LEGACY in project.tags


def is_legacy_network(network_or_id):
    conn = get_conn()
    if isinstance(network_or_id, openstack.network.v2.network.Network):
        network = network_or_id
    else:
        network = conn.network.get_network(network_or_id)

    return network.provider_network_type == 'midonet'


# Check if user has TenantManager role. This is necessary as policies only
# allow TenantManager, even for checking resources
def check_sanity() -> bool:
    conn = get_conn()
    if not is_tenant_manager():
        print("You do not have sufficient privileges to migrate the project. "
              "You need the 'TenantManager' role on this project")
        return False

    # check if project tag and neutron cache matches
    network = list(conn.network.networks(is_router_external=True,
                                         tags='nectar:floating'))[0]
    if is_legacy_network(network) != is_legacy_project():
        print("Project networking may have been recently switched. "
              f"Please wait for {CACHE_TIMEOUT} and try again.")
        return False

    return True


# check if project tag and neutron cache matches
def check_sync() -> bool:
    conn = get_conn()

    external_network = conn.network.networks(is_router_external=True)[0]

    return is_legacy_network(external_network) == is_legacy_project()


class PrettyResource:
    header = ['Type', 'ID', 'Name', 'Recommendation']

    def __init__(self, obj):
        if not isinstance(obj, openstack.resource.Resource):
            raise TypeError(
                f"Expected openstack.resource.Resource, got {type(obj)}"
                )

        self.obj = obj
        self.type = obj.resource_key.capitalize()
        self.id = obj.id
        self.name = obj.name
        self.recommendation = self._get_recommendation()

    def _get_recommendation(self) -> str:
        raise NotImplementedError

    def prettyrow(self) -> list:
        return [self.type, self.id, self.name, self.recommendation or '']


class PrettyFIP(PrettyResource):
    def __init__(self, obj):
        super().__init__(obj)

    def _get_recommendation(self) -> str:
        if is_legacy_network(self.obj.floating_network_id):
            # if port_id is None, it is not attached to a server
            if self.obj.port_id is None:
                return 'Unused, Delete'
            else:
                return 'Replace'


class PrettyNetwork(PrettyResource):
    def __init__(self, obj):
        super().__init__(obj)

    def _get_recommendation(self) -> str:
        if is_legacy_network(self.obj):
            return 'Replace'


class PrettyRouter(PrettyResource):
    def __init__(self, obj):
        super().__init__(obj)

    def _get_recommendation(self) -> str:
        conn = get_conn()

        # if router has no interfaces, it is not attached to any network
        filters = {'device_id': self.obj.id,
                   'device_owner': 'network:router_interface'}
        interfaces = list(conn.network.ports(**filters))
        if len(interfaces) == 0:
            return 'No networks attached, Delete'

        # if router is not attached to an external network, we cannot tell if
        # it is legacy
        # NOTE: this is a bit of an outlier, do not worry about it
        if self.obj.external_gateway_info is None:
            return

        if is_legacy_network(self.obj.external_gateway_info['network_id']):
            return 'Replace'


class PrettyServer(PrettyResource):
    def __init__(self, obj):
        super().__init__(obj)

    def _get_recommendation(self) -> str:
        conn = get_conn()
        interfaces = conn.compute.server_interfaces(self.obj)
        legacy_networks = []
        for interface in interfaces:
            network = conn.network.get_network(interface.net_id)
            if is_legacy_network(network):
                legacy_networks.append(network.name)

        if legacy_networks:
            return f"Switch to {NAME_OVN} network"


def _resources_to_prettyrows(resources, all_resources=False) -> list:
    resources = [_resource_to_prettyresource(r) for r in resources]
    if not all_resources:
        resources = [r for r in resources if r.recommendation]
    return [resource.prettyrow() for resource in resources]


def _resource_to_prettyresource(obj) -> PrettyResource:
    if not isinstance(obj, openstack.resource.Resource):
        raise TypeError(
            f"Expected openstack.resource.Resource, got {type(obj)}"
            )

    _query_map = {
        'floatingip': 'PrettyFIP',
        'network': 'PrettyNetwork',
        'router': 'PrettyRouter',
        'server': 'PrettyServer',
    }

    if obj.resource_key not in _query_map:
        raise ValueError(f"Unknown resource type {obj.resource_key}")

    return globals()[_query_map[obj.resource_key]](obj)


def check(args) -> None:
    all_resources = args.all_resources

    conn = get_conn()
    # conn.current_project.name may not be populated from auth args, get it
    # from keystone
    project_name = conn.identity.get_project(conn.current_project_id).name

    # print project information
    print(f"Project: {project_name}")
    print(f"Networking: {NAME_MIDONET if is_legacy_project() else NAME_OVN}")

    print("Checking resources. This might take a while...")

    pt = PrettyTable(align='l')
    pt.field_names = PrettyResource.header

    pt.add_rows(_resources_to_prettyrows(conn.network.networks(
        is_router_external=False, project_id=conn.current_project_id),
        all_resources))
    pt.add_rows(_resources_to_prettyrows(conn.network.routers(),
                                         all_resources))
    pt.add_rows(_resources_to_prettyrows(conn.network.ips(), all_resources))
    pt.add_rows(_resources_to_prettyrows(conn.compute.servers(),
                                         all_resources))

    # Print general recommendation
    if not is_legacy_project() and pt.rowcount == 0:
        # BIG YAY!
        output = (f"Congratulations! Your project is already using {NAME_OVN} "
                  "networking. No resources needs to be migrated.")

    else:
        output = []
        if is_legacy_project():
            output.append(f"Project {project_name} needs to switch to "
                          f"{NAME_OVN} networking.")

        else:
            output.append(f"Project {project_name} is already using "
                          f"{NAME_OVN} networking.")
        if pt.rowcount:
            output.append("The following resources needs to be migrated:")
        output = ' '.join(output)

    print(output)

    if pt.rowcount:
        print(pt)


def switch(args) -> None:
    print("Switching project networking")
    networking = args.networking
    conn = get_conn()
    if not is_tenant_manager():
        print("You do not have sufficient privileges to switch this project. "
              "You need the 'TenantManager' role on this project")
        return

    project = conn.identity.get_project(conn.current_project_id)
    if networking == NAME_MIDONET:
        if is_legacy_project():
            print(f"Project is already set to {NAME_MIDONET} networking")
        else:
            project.add_tag(session=conn.identity, tag=TAG_LEGACY)
            print(f"Project set to {NAME_MIDONET} networking.")
            print(f"Please wait {CACHE_TIMEOUT} for changes to propagate to "
                  "all service.")

    elif networking == NAME_OVN:
        if not is_legacy_project():
            print(f"Project is already set to {NAME_OVN} networking")
        else:
            project.remove_tag(session=conn.identity, tag=TAG_LEGACY)
            print(f"Project set to {NAME_OVN} networking.")
            print(f"Please wait {CACHE_TIMEOUT} for changes to propagate to "
                  "all services.")


def main():
    parser = argparse.ArgumentParser(
        description='Helper script to switch between legacy and ovn network')
    subparsers = parser.add_subparsers(
        dest='command', required=True, help='command')

    parser_check = subparsers.add_parser('check',
                                         help='Check status of project.')
    parser_check.add_argument('--all-resources', '--all', action='store_true',
                              help=('Show all resources, not just those with '
                                    'recommendations'))
    parser_check.set_defaults(command=check)

    parser_switch = subparsers.add_parser('switch',
                                          help='Set project networking')
    parser_switch.add_argument('networking', choices=[NAME_MIDONET, NAME_OVN],
                               help='network type')
    parser_switch.set_defaults(command=switch)

    args = parser.parse_args()

    if not check_sanity():
        return

    args.command(args)


if __name__ == '__main__':
    main()
