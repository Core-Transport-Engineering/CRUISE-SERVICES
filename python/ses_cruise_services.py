# -*- mode: python; python-indent: 4 -*-
import json
import re

import _ncs
import resource_manager.id_allocator as id_allocator
import resource_manager.ipaddress_allocator as ip_allocator
import ncs
import ncs.dp
import requests
from ncs.dp import Action

from zenoss import Zenoss
import device_helper



# ---------------------------------
# SERVICE CALLBACK OBJECT/FUNCTIONS
# ---------------------------------


class ServiceCallbacks (ncs.application.Service):
    @ncs.application.Service.create
    def cb_create(self, tctx, root, service, proplist):

        def setup_pe_interfaces_policy(service, point, pe, service_name, qos_interface):

            pm_tv = ncs.template.Variables ()
            pm_tv.add ('PE', pe)
            pm_tv.add ('L3_SERV_NAME', service_name)

            interface_bandwidth = qos_interface.QoS.interface_bandwidth
            pm_tv.add ('INT_BW', interface_bandwidth)
            self.log.info ("                        Interface bandwidth: ", interface_bandwidth, " Mbps")

            pm_tv.add('PM_ID', qos_interface.id_int)

            serv_qos_profile = qos_interface.QoS.QoS_profile
            pm_tv.add ('SERV_QOS_PROFILE', qos_interface.QoS.QoS_profile)

            pm_tv.add ("POLICER", "")
            pm_tv.add ('DATA-VIDEO', '')

            if serv_qos_profile == "default-QoS-profile":
                self.log.info ("                        Default QoS profile")
                qos_type = qos_interface.QoS.policy_map_default
                if qos_type == "SP-DATA-INGRESS-MODEL-PIPE":
                    self.log.info ("                        Child policy type DATA")
                    pm_tv.add ('DATA-VIDEO', "SP-DATA-INGRESS-MODEL-PIPE")
                else:
                    self.log.info ("                        Child policy type VIDEO")
                    pm_tv.add ('DATA-VIDEO', "SP-VIDEO-INGRESS-MODEL-PIPE")

            if serv_qos_profile == "No-QoS-profile":
                self.log.info ("                        No QoS profile")

                qos_type = qos_interface.QoS.no_qos_profile
                if qos_type == "POLICER-ONLY":
                    self.log.info ("                        Policer only")
                    pm_tv.add ("POLICER", "enable")

                if qos_type == "POLICER-DISABLED":
                    pm_tv.add ("POLICER", "disable")
                    self.log.info ("                        Policer disabled")


            tmpl = ncs.template.Template (service)
            device_type_yang = root.devices.device[point.access_pe].device_type.cli.ned_id

            if device_type_yang == "ios-id:cisco-ios":
                tmpl.apply ('cruise-xe-policy-map', pm_tv)
            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                tmpl.apply ('cruise-xr-policy-map', pm_tv)

        def setup_static_customer_routes(service, point, pe, serv_customer_prefix, serv_customer_prefix_mask, serv_customer_prefix_nh):

            serv_vrf_name = service.name
            custrt_tv = ncs.template.Variables ()
            custrt_tv.add ('PE', pe)
            custrt_tv.add ('VRF_NAME', serv_vrf_name)
            custrt_tv.add ('CUSTOMER-PREFIX', serv_customer_prefix)
            custrt_tv.add ('CUSTOMER-PREFIX-MASK', serv_customer_prefix_mask)
            custrt_tv.add ('CUSTOMER-PREFIX-NH', serv_customer_prefix_nh)
            tmpl = ncs.template.Template (service)
            tmpl.apply ('cruise-xe-static-routing', custrt_tv)

        def get_pe_ned_type(root, service):

            endpoints = service.endpoint
            pe_ned_type = {}
            for point in endpoints:
                pe = point.access_pe
                pe_ned_type[pe] = root.devices.device[pe].device_type.cli.ned_id
                if pe_ned_type[pe] == "ios-id:cisco-ios":
                    pe_ned_type[pe] = "cisco-ios"
                if pe_ned_type[pe] == "cisco-ios-xr-id:cisco-ios-xr":
                    pe_ned_type[pe] = "cisco-ios-xr"
            return pe_ned_type

        def get_pe_bgp_asn_no(root, service, endpoints):

            pe_ned_type = {}
            pe_lpbk_ip = {}
            pe_bgp_as_no = {}
            pe_ned_type = get_pe_ned_type (root, service)

            for point in endpoints:
                pe = point.access_pe
                dev_type = str (pe_ned_type[pe])
                if dev_type == "cisco-ios":
                    bgp_oid = root.devices.device[pe].config.ios__router.bgp
                elif dev_type == "cisco-ios-xr":
                    bgp_oid = root.devices.device[pe].config.cisco_ios_xr__router.bgp.bgp_no_instance
                else:
                    self.log.info ('Error: No Matching Device Type')

                for bgp_inst in bgp_oid:
                    if dev_type == "cisco-ios":
                        pe_bgp_as_no[pe] = bgp_inst.as_no
                    if dev_type == "cisco-ios-xr":
                        pe_bgp_as_no[pe] = bgp_inst.id
                    pe_lpbk_ip[pe] = bgp_inst.bgp.router_id
            return pe_bgp_as_no, pe_lpbk_ip

        def vlan_allocation(is_vlan_id, service, service_path, vlan_pool, tctx, root, interf):
            vlan_id = ""
            if is_vlan_id:
                unique_allocation = str (service) + '_' + service.name + '_int_' + str (interf) + '_VLAN_' + str (is_vlan_id)
                id_allocator.id_request (service, service_path, 'sspa-bot', vlan_pool, unique_allocation, False, is_vlan_id)
                vlan_id = id_allocator.id_read ('sspa-bot', root, vlan_pool, unique_allocation)

                if not vlan_id:
                    self.log.info ("                        VLAN ID Allocation not ready")
                    return False, "Null"

            else:
                unique_allocation = str (service) + '_' + service.name + '_int_' + str (interf)
                id_allocator.id_request (service, service_path, 'sspa-bot', vlan_pool, unique_allocation, False)
                vlan_id = id_allocator.id_read (tctx, root, vlan_pool, unique_allocation)
                if not vlan_id:
                    self.log.info ("                        VLAN ID Allocation not ready")
                    return False, "Null"

            if vlan_id != None:
                return True, vlan_id

        def servinst_allocation(service, service_path, servinst_pool, tctx, root, interf):
            servinst_id = ""
            unique_allocation = str (service) + '_' + service.name + '_int_' + str (interf)
            id_allocator.id_request (service, service_path, 'sspa-bot', servinst_pool, unique_allocation, False)
            servinst_id = id_allocator.id_read (tctx, root, servinst_pool, unique_allocation)
            if not servinst_id:
                self.log.info ("                        SERV-INST ID Allocation not ready")
                return False, "Null"
            else:
                return True, servinst_id

        def bdid_allocation(service, pool_name, interface_id,  tctx, root):
            bd_id = None
            unique_allocation = str (service) + '_' + str (service.name) + '_' + str (interface_id)
            id_allocator.id_request (service, service_path, 'sspa-bot', pool_name, unique_allocation, False)

            bd_id = id_allocator.id_read (tctx.username, root, pool_name, unique_allocation)
            if not bd_id:
                self.log.info ("                        BD ID Alloc not ready")
                return "Null", False
            if bd_id != None:
                return bd_id, True

        def vcid_allocation(service, pool_name, interface_id,  tctx, root):
            vc_id = None
            unique_allocation = str (service) + '_' + str (service.name) + '_' + str (interface_id)
            id_allocator.id_request (service, service_path, 'sspa-bot', pool_name, unique_allocation, False)

            vc_id = id_allocator.id_read (tctx.username, root, pool_name, unique_allocation)
            if not vc_id:
                self.log.info ("                        VD ID Alloc not ready")
                return "Null", False
            if vc_id != None:
                return vc_id, True

        def ip_allocation(service, interface_pool, tctx, root, interface, lenght):
            unique_ip_allocation = service.name + '_' + str (interface)
            ipaddress_allocator.net_request (service, service_path, tctx.username, interface_pool, unique_ip_allocation, lenght)
            network = ipaddress_allocator.net_read (tctx.username, root, interface_pool, unique_ip_allocation)
            if not network:
                self.log.info ("IP Address Allocation not ready")
                return False
            else:
                return network

        def get_first_ip(network):
            net_encode = unicode (network)
            network = ipaddress.ip_network (net_encode)
            ip = network[1]
            mask = network.netmask
            return str (ip), str (mask)

        def get_pool(device):
            router_name, router_location = device.split ('.')
            router_location = router_location.upper ()
            router_number = router_name[7:]
            interface_pool = ("IP_DATA_CUST_%s_%s") % (router_location, router_number)
            return interface_pool

        def mep_allocation (service, tctx, root, device, interface_id):
            pool_name_mep = 'MEP_POOL_CRUISE'
            mep_id = None
            unique_mep_allocation = str (service) + '_' + str (service.name) + '_'  + str (interface_id)
            id_allocator.id_request (service, service_path, 'sspa-bot', pool_name_mep, unique_mep_allocation, True)
            mep_id = id_allocator.id_read (tctx.username, root, pool_name_mep, unique_mep_allocation)
            if not mep_id:
                self.log.info ("                        MEP ID Allocation not ready")
                return "None", False
            if mep_id != None:
                return mep_id, True

        def mac_allocation (service, tctx, root, device, interf):
            pool_name_mac = 'MAC_POOL'
            mac_id = None
            unique_mac_allocation = str (service) + '_' + str (service.name) + '_' + str (device) + '_' + str (interf)
            id_allocator.id_request (service, service_path, 'sspa-bot', pool_name_mac, unique_mac_allocation, False)
            mac_id = id_allocator.id_read (tctx.username, root, pool_name_mac, unique_mac_allocation)
            if not mac_id:
                self.log.info ("                        MAC ID Allocation not ready")
                return "None", False
            if mac_id != None:
                mac_address = number_to_mac(mac_id)
                return mac_address, True

        def number_to_mac(decimal_value):
            hex_value = ('%x' % decimal_value).zfill (8)
            partial_mac = ':'.join (str (hex_value).zfill (12)[i:i + 2] for i in range (2, 12, 2))
            mac = "0A:" + str (partial_mac)
            return mac

        def setup_bgp_route_map_in(service, device, neighbor, route_map_name):
            val_final = ''

            bgp_rm_tv = ncs.template.Variables ()
            tmpl = ncs.template.Template (service)

            device_type_yang = root.devices.device[device.access_pe].device_type.cli.ned_id

            bgp_rm_tv.add ('PE', device.access_pe)

            bgp_rm_tv.add ('PREFIX_LIST', '')
            bgp_rm_tv.add ('COMMUNITY_LIST', '')
            bgp_rm_tv.add ('COMM_LIST_NAME', '')
            bgp_rm_tv.add ('AS_PATH', '')
            bgp_rm_tv.add ('LOCAL_PREF', '')
            bgp_rm_tv.add ('METRIC', '')
            bgp_rm_tv.add ('COMMUNITY', '')
            bgp_rm_tv.add ('PREPEND', '')

            bgp_rm_tv.add ('RPL_VAL', '')

            bgp_rm_tv.add ('RM_NAME', route_map_name)

            self.log.info ("            Create route-map: ", route_map_name)

            route_map_len = len(neighbor.bgp_route_map_in.route_map_bgp.route_map_seq)
            route_map_len = route_map_len * 10

            for route_map_sequence in neighbor.bgp_route_map_in.route_map_bgp.route_map_seq:
                route_map_seq = route_map_sequence.route_map_seq
                bgp_rm_tv.add ('RM_SEQ', route_map_seq)

                route_map_operation = route_map_sequence.operation
                bgp_rm_tv.add ('RM_OPER', route_map_operation)

                if route_map_seq == 10:
                    xr_seq = "  if"
                else:
                    xr_seq = "  elseif"

                self.log.info ("                Create route-map sequence: ", route_map_seq, " operation: ",
                               route_map_operation)

                route_map_match_sequence = route_map_sequence.match.match

                ######
                xr_match_prefix_list = ''
                xr_match_community_list = ''
                xr_set_med = ''
                xr_set_local_preference = ''
                xr_set_community = ''
                xr_set_prepend = ''
                xr_rpl_end = ''

                self.log.info ('                    Match statements')
                for match_sequence in route_map_match_sequence:
                    match_seq = match_sequence.match_seq
                    match_option = match_sequence.match_options

                    if match_option == "prefix-list":
                        bgp_rm_tv.add ('PREFIX_LIST', 'enable')
                        prefix_list_name = "PL-MATCH-" + str (route_map_name) + "-" + str (route_map_seq)
                        bgp_rm_tv.add ('PL_NAME', prefix_list_name)

                        prefix_list_sequence = match_sequence.match_prefix_list.prefix_list.prefix_list
                        for prefix_list in prefix_list_sequence:
                            prefix_list_seq = prefix_list.prefix_list_seq
                            bgp_rm_tv.add ('SEQ', prefix_list_seq)

                            prefix_list_operation = prefix_list.operation
                            bgp_rm_tv.add ('ACTION', prefix_list_operation)

                            prefix_list_ip_network = prefix_list.ip_network
                            prefix_list_ip_mask = prefix_list.ip_mask
                            prefix = str (prefix_list_ip_network) + str (prefix_list_ip_mask)
                            bgp_rm_tv.add ('PREFIX', prefix)

                            prefix_list_length = prefix_list.lenght
                            bgp_rm_tv.add ('LENGHT', prefix_list_length)

                            self.log.info ("                            Create prefix-list: ", prefix_list_name,
                                           " sequence: ", prefix_list_seq, " ", prefix_list_operation, " ",
                                           prefix_list_ip_network, " ", prefix_list_ip_mask, " ", prefix_list_length)

                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map-prefix-list', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map-prefix-list', bgp_rm_tv)

                        xr_match_prefix_list = xr_seq + " destination in " + prefix_list_name + " then\r\n"

                        if device_type_yang == "ios-id:cisco-ios":
                            tmpl.apply ('cruise-xe-bgp-route-map-prefix-list', bgp_rm_tv)
                        elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                            tmpl.apply ('cruise-xr-bgp-route-map-prefix-list', bgp_rm_tv)

                    if match_option == "community-list":
                        bgp_rm_tv.add ('COMMUNITY_LIST', 'enable')
                        community_list_name = "COMM-MATCH-" + str (route_map_name) + "-" + str (route_map_seq)
                        bgp_rm_tv.add ('COMM_LIST_NAME', community_list_name)

                        community_list_sequence = match_sequence.match_community_list.community_list.community_list
                        for community_list in community_list_sequence:
                            community_list_seq = community_list.community_list_seq
                            bgp_rm_tv.add ('SEQ', community_list_seq)

                            community_list_operation = community_list.operation
                            bgp_rm_tv.add ('ACTION', community_list_operation)

                            community_list_community = community_list.community
                            bgp_rm_tv.add ('COMMUNITY', community_list_community)


                            self.log.info ("                            Create community-list: ", community_list_name,
                                           " sequence: ", community_list_seq, " ", community_list_operation, " ",
                                           community_list_community)

                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map-community-list', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map-community-list', bgp_rm_tv)

                        xr_match_community_list = xr_seq + " community matches-any " + community_list_name + " then\r\n"


                        if device_type_yang == "ios-id:cisco-ios":
                            tmpl.apply ('cruise-xe-bgp-route-map-community-list', bgp_rm_tv)
                        elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                            tmpl.apply ('cruise-xr-bgp-route-map-community-list', bgp_rm_tv)

                route_map_set_sequence = route_map_sequence.set.set
                self.log.info ('                    Set statements')

                bgp_rm_tv.add ('AS_PATH', '')
                bgp_rm_tv.add ('LOCAL_PREF', '')
                bgp_rm_tv.add ('METRIC', '')
                bgp_rm_tv.add ('COMMUNITY', '')
                bgp_rm_tv.add ('ADDITIVE', '')
                bgp_rm_tv.add ('PREPEND', '')

                for set_sequence in route_map_set_sequence:

                    set_seq = set_sequence.set_seq
                    set_option = set_sequence.set_options
                    self.log.info ("                            Set sequence: ", set_seq)

                    if set_option == "as-path":
                        if len (set_sequence.set_as_path.as_path) > 0:
                            as_paths = []
                            for as_path in set_sequence.set_as_path.as_path:
                                as_path_prepend = as_path.prepend
                                as_paths.append(as_path_prepend)

                            as_path_list = ' '.join (str (e) for e in as_paths)
                            as_path_list_len = len(as_paths)
                            self.log.info ("                                Set AS-PATH prepend: ", as_path_list)

                            bgp_rm_tv.add ('AS_PATH', as_path_list)
                            xr_set_prepend = "    prepend as-path " + str(as_path_prepend) + " " + str(as_path_list_len) + "\r\n"

                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                    if set_option == "community":
                        if len (set_sequence.set_community.community) > 0:
                            additive = set_sequence.set_community.additive
                            if additive == "true":
                                bgp_rm_tv.add ('ADDITIVE', 'true')

                            self.log.info ("                                Set community additive: ", additive)

                            for community in set_sequence.set_community.community:
                                self.log.info ("                                Set community : ", community)
                                bgp_rm_tv.add ('COMMUNITY_LIST', 'enable')
                                community_list_name = "COMM-SET-" + str (route_map_name) + "-" + str (route_map_seq)
                                bgp_rm_tv.add ('COMM_LIST_NAME', community_list_name)
                                bgp_rm_tv.add ('COMMUNITY', community)

                                # have to build a comm-list for XR
                                self.log.info ("                            Create community-list: ", community_list_name, " ",  community)

                                if device_type_yang == "ios-id:cisco-ios":
                                    tmpl.apply ('cruise-xe-bgp-route-map-community-list', bgp_rm_tv)
                                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                    tmpl.apply ('cruise-xr-bgp-route-map-community-list', bgp_rm_tv)

                                ######
                            xr_set_community = "    set community " + community_list_name + "\r\n"
                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                    if set_option == "local-preference":
                        if set_sequence.set_local_preference is not None:
                            local_preference = set_sequence.set_local_preference
                            self.log.info ("                                Set local-preference: ", local_preference)

                            xr_set_local_preference = "    set local-preference " + str(local_preference) + "\r\n"
                            bgp_rm_tv.add ('LOCAL_PREF', local_preference)
                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                    if set_option == "metric":
                        if set_sequence.set_metric is not None:
                            metric = set_sequence.set_metric
                            self.log.info ("                                Set metric: ", metric)
                            bgp_rm_tv.add ('METRIC', metric)
                            xr_set_med = "    set med " + str(metric) + "\r\n"

                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                if route_map_seq == route_map_len:
                    xr_rpl_end = "  endif\r\n"

                val_seq = ''
                val_seq += (
                    xr_match_community_list +
                    xr_match_prefix_list +
                    xr_set_prepend +
                    xr_set_community +
                    xr_set_local_preference +
                    xr_set_med +
                    "    pass\r\n" +
                    xr_rpl_end

                )

                val_final += val_seq
                # self.log.info ("                Configure route-map: ", route_map_name, "\r\n", val_final)

                bgp_rm_tv.add ('RPL_VAL', val_final)
                if device_type_yang == "ios-id:cisco-ios":
                    tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                    tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

        def setup_bgp_route_map_out(service, device, neighbor, route_map_name):
            val_final = ''

            bgp_rm_tv = ncs.template.Variables ()
            tmpl = ncs.template.Template (service)

            device_type_yang = root.devices.device[device.access_pe].device_type.cli.ned_id

            bgp_rm_tv.add ('PE', device.access_pe)

            bgp_rm_tv.add ('PREFIX_LIST', '')
            bgp_rm_tv.add ('COMMUNITY_LIST', '')
            bgp_rm_tv.add ('COMM_LIST_NAME', '')
            bgp_rm_tv.add ('AS_PATH', '')
            bgp_rm_tv.add ('LOCAL_PREF', '')
            bgp_rm_tv.add ('METRIC', '')
            bgp_rm_tv.add ('COMMUNITY', '')
            bgp_rm_tv.add ('RPL_VAL', '')
            bgp_rm_tv.add ('PREPEND', '')


            bgp_rm_tv.add ('RM_NAME', route_map_name)

            self.log.info ("            Create route-map: ", route_map_name)

            route_map_len = len(neighbor.bgp_route_map_out.route_map_bgp.route_map_seq)
            route_map_len = route_map_len * 10

            for route_map_sequence in neighbor.bgp_route_map_out.route_map_bgp.route_map_seq:
                route_map_seq = route_map_sequence.route_map_seq
                bgp_rm_tv.add ('RM_SEQ', route_map_seq)

                route_map_operation = route_map_sequence.operation
                bgp_rm_tv.add ('RM_OPER', route_map_operation)

                if route_map_seq == 10:
                    xr_seq = "  if"
                else:
                    xr_seq = "  elseif"

                self.log.info ("                Create route-map sequence: ", route_map_seq, " operation: ",
                               route_map_operation)

                route_map_match_sequence = route_map_sequence.match.match

                ######
                xr_match_prefix_list = ''
                xr_match_community_list = ''
                xr_set_med = ''
                xr_set_local_preference = ''
                xr_set_community = ''
                xr_set_prepend = ''
                xr_rpl_end = ''

                self.log.info ('                    Match statements')
                for match_sequence in route_map_match_sequence:
                    match_seq = match_sequence.match_seq
                    match_option = match_sequence.match_options

                    if match_option == "prefix-list":
                        bgp_rm_tv.add ('PREFIX_LIST', 'enable')
                        prefix_list_name = "PL-MATCH-" + str (route_map_name) + "-" + str (route_map_seq)
                        bgp_rm_tv.add ('PL_NAME', prefix_list_name)

                        prefix_list_sequence = match_sequence.match_prefix_list.prefix_list.prefix_list
                        for prefix_list in prefix_list_sequence:
                            prefix_list_seq = prefix_list.prefix_list_seq
                            bgp_rm_tv.add ('SEQ', prefix_list_seq)

                            prefix_list_operation = prefix_list.operation
                            bgp_rm_tv.add ('ACTION', prefix_list_operation)

                            prefix_list_ip_network = prefix_list.ip_network
                            prefix_list_ip_mask = prefix_list.ip_mask
                            prefix = str (prefix_list_ip_network) + str (prefix_list_ip_mask)
                            bgp_rm_tv.add ('PREFIX', prefix)

                            prefix_list_length = prefix_list.lenght
                            bgp_rm_tv.add ('LENGHT', prefix_list_length)

                            self.log.info ("                            Create prefix-list: ", prefix_list_name,
                                           " sequence: ", prefix_list_seq, " ", prefix_list_operation, " ",
                                           prefix_list_ip_network, " ", prefix_list_ip_mask, " ", prefix_list_length)

                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map-prefix-list', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map-prefix-list', bgp_rm_tv)

                        xr_match_prefix_list = xr_seq + " destination in " + prefix_list_name + " then\r\n"

                        if device_type_yang == "ios-id:cisco-ios":
                            tmpl.apply ('cruise-xe-bgp-route-map-prefix-list', bgp_rm_tv)
                        elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                            tmpl.apply ('cruise-xr-bgp-route-map-prefix-list', bgp_rm_tv)

                    if match_option == "community-list":
                        bgp_rm_tv.add ('COMMUNITY_LIST', 'enable')
                        community_list_name = "COMM-MATCH-" + str (route_map_name) + "-" + str (route_map_seq)
                        bgp_rm_tv.add ('COMM_LIST_NAME', community_list_name)

                        community_list_sequence = match_sequence.match_community_list.community_list.community_list
                        for community_list in community_list_sequence:
                            community_list_seq = community_list.community_list_seq
                            bgp_rm_tv.add ('SEQ', community_list_seq)

                            community_list_operation = community_list.operation
                            bgp_rm_tv.add ('ACTION', community_list_operation)

                            community_list_community = community_list.community
                            bgp_rm_tv.add ('COMMUNITY', community_list_community)


                            self.log.info ("                            Create community-list: ", community_list_name,
                                           " sequence: ", community_list_seq, " ", community_list_operation, " ",
                                           community_list_community)

                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map-community-list', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map-community-list', bgp_rm_tv)

                        xr_match_community_list = xr_seq + " community matches-any " + community_list_name + " then\r\n"


                        if device_type_yang == "ios-id:cisco-ios":
                            tmpl.apply ('cruise-xe-bgp-route-map-community-list', bgp_rm_tv)
                        elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                            tmpl.apply ('cruise-xr-bgp-route-map-community-list', bgp_rm_tv)

                route_map_set_sequence = route_map_sequence.set.set
                self.log.info ('                    Set statements')

                bgp_rm_tv.add ('AS_PATH', '')
                bgp_rm_tv.add ('LOCAL_PREF', '')
                bgp_rm_tv.add ('METRIC', '')
                bgp_rm_tv.add ('COMMUNITY', '')
                bgp_rm_tv.add ('ADDITIVE', '')

                for set_sequence in route_map_set_sequence:

                    set_seq = set_sequence.set_seq
                    set_option = set_sequence.set_options
                    self.log.info ("                            Set sequence: ", set_seq)

                    if set_option == "as-path":
                        if len (set_sequence.set_as_path.as_path) > 0:
                            as_paths = []
                            for as_path in set_sequence.set_as_path.as_path:
                                as_path_prepend = as_path.prepend
                                as_paths.append(as_path_prepend)

                            as_path_list = ' '.join (str (e) for e in as_paths)
                            as_path_list_len = len(as_paths)
                            self.log.info ("                                Set AS-PATH prepend: ", as_path_list)

                            bgp_rm_tv.add ('AS_PATH', as_path_list)
                            xr_set_prepend = "    prepend as-path " + str(as_path_prepend) + " " + str(as_path_list_len) + "\r\n"


                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                    if set_option == "community":
                        if len (set_sequence.set_community.community) > 0:
                            additive = set_sequence.set_community.additive
                            if additive == "true":
                                bgp_rm_tv.add ('ADDITIVE', 'true')

                            self.log.info ("                                Set community additive: ", additive)

                            for community in set_sequence.set_community.community:
                                self.log.info ("                                Set community : ", community)
                                bgp_rm_tv.add ('COMMUNITY_LIST', 'enable')
                                community_list_name = "COMM-SET-" + str (route_map_name) + "-" + str (route_map_seq)
                                bgp_rm_tv.add ('COMM_LIST_NAME', community_list_name)
                                bgp_rm_tv.add ('COMMUNITY', community)

                                # have to build a comm-list for XR
                                self.log.info ("                            Create community-list: ", community_list_name, " ",  community)

                                if device_type_yang == "ios-id:cisco-ios":
                                    tmpl.apply ('cruise-xe-bgp-route-map-community-list', bgp_rm_tv)
                                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                    tmpl.apply ('cruise-xr-bgp-route-map-community-list', bgp_rm_tv)

                                ######
                            xr_set_community = "    set community " + community_list_name + "\r\n"
                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                    if set_option == "local-preference":
                        if set_sequence.set_local_preference is not None:
                            local_preference = set_sequence.set_local_preference
                            self.log.info ("                                Set local-preference: ", local_preference)

                            xr_set_local_preference = "    set local-preference " + str(local_preference) + "\r\n"
                            bgp_rm_tv.add ('LOCAL_PREF', local_preference)
                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                    if set_option == "metric":
                        if set_sequence.set_metric is not None:
                            metric = set_sequence.set_metric
                            self.log.info ("                                Set metric: ", metric)
                            bgp_rm_tv.add ('METRIC', metric)
                            xr_set_med = "    set med " + str(metric) + "\r\n"

                            if device_type_yang == "ios-id:cisco-ios":
                                tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

                if route_map_seq == route_map_len:
                    xr_rpl_end = "  endif\r\n"

                val_seq = ''
                val_seq += (
                    xr_match_community_list +
                    xr_match_prefix_list +
                    xr_set_prepend +
                    xr_set_community +
                    xr_set_local_preference +
                    xr_set_med +
                    "    pass\r\n" +
                    xr_rpl_end

                )

                val_final += val_seq
                # self.log.info ("                Configure route-map: ", route_map_name, "\r\n", val_final)

                bgp_rm_tv.add ('RPL_VAL', val_final)
                if device_type_yang == "ios-id:cisco-ios":
                    tmpl.apply ('cruise-xe-bgp-route-map', bgp_rm_tv)
                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                    tmpl.apply ('cruise-xr-bgp-route-map', bgp_rm_tv)

        def configure_ethernet_oam(pe, pe_interface):
            cfm_tv = ncs.template.Variables ()
            cfm_tmpl = ncs.template.Template (service)

            cfm_tv.add ('PE', pe)
            cfm_tv.add ('SERVICE_NAME', service.name)
            cfm_tv.add ('EOAM', 'active')
            cfm_tv.add ('CPE_CFM', 'active')

            device_type_yang = root.devices.device[pe].device_type.cli.ned_id

            if device_type_yang == "ios-id:cisco-ios":
                if pe_interface.if_num_ge is not None:
                    serv_if_num = pe_interface.if_num_ge
                    serv_if_size = "GigabitEthernet"
                    cfm_tv.add ('IF_SIZE', serv_if_size)
                    cfm_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_tenge is not None:
                    serv_if_num = pe_interface.if_num_tenge
                    serv_if_size = "TenGigabitEthernet"
                    cfm_tv.add ('IF_SIZE', serv_if_size)
                    cfm_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_po is not None:
                    serv_if_num = pe_interface.if_num_po
                    serv_if_size = "Port-channel"
                    cfm_tv.add ('IF_SIZE', serv_if_size)
                    cfm_tv.add ('IF_NUM', serv_if_num)
            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                if pe_interface.if_num_ge_xr is not None:
                    serv_if_num = pe_interface.if_num_ge_xr
                    serv_if_size = "GigabitEthernet"
                    cfm_tv.add ('IF_SIZE', serv_if_size)
                    cfm_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_tenge_xr is not None:
                    serv_if_num = pe_interface.if_num_tenge_xr
                    serv_if_size = "TenGigE"
                    cfm_tv.add ('IF_SIZE', serv_if_size)
                    cfm_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_po_xr is not None:
                    serv_if_num = pe_interface.if_num_po_xr
                    serv_if_size = "Bundle-Ether"
                    cfm_tv.add ('IF_SIZE', serv_if_size)
                    cfm_tv.add ('IF_NUM', serv_if_num)

            cfm_tv.add ('IF_ID', pe_interface.id_int)
            cfm_tv.add ('IF_NUM', serv_if_num)
            cfm_tv.add ('IF_SIZE', serv_if_size)
            cfm_tv.add ('IF_END_TYPE', pe_interface.end_type)
            cfm_tv.add ('IF_ENCAP', pe_interface.encapsulation)
            cfm_tv.add ('IF_S_VLAN_ID', pe_interface.s_vlan_id)

            cfm_tv.add ('SERV_INST', pe_interface.se_id)
            cfm_tv.add ('BD_ID', pe_interface.bd_id)
            cfm_tv.add ('VC_ID', pe_interface.vc_id)
            cfm_tv.add ('PE_MEP_ID', pe_interface.mep_id)

            if device_type_yang == "ios-id:cisco-ios":
                cfm_tmpl.apply ('cruise-xe-oam-template', cfm_tv)
            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                cfm_tmpl.apply ('cruise-xr-oam-template', cfm_tv)

        def configure_sla_pe(pe, pe_interface):
            if pe_interface.connected_cpe.cpe_device_ethernet_sla == "true":
                sla_tv = ncs.template.Variables ()
                sla_tmpl = ncs.template.Template (service)
                device_type_yang = root.devices.device[pe].device_type.cli.ned_id

                sla_tv.add ('SERVICE_NAME', service.name)
                sla_tv.add ('CPE_CFM', 'active')
                sla_tv.add ('IP_SLA', 'enable')
                sla_tv.add ('PE', pe)

                sla_tv.add ('VC_ID', pe_interface.vc_id)
                sla_tv.add ('PE_MEP_ID', pe_interface.mep_id)

                if pe_interface.connected_cpe.cpe_device_in_nso == "true":
                    cpe_device = pe_interface.connected_cpe.cpe_device
                    sla_tv.add ('CPE_MEP_ID', 'None')

                else:
                    cpe_device = pe_interface.connected_cpe.cpe_device_manual
                    sla_tv.add ('CPE_MEP_ID', pe_interface.connected_cpe.cpe_device_mpid)

                sla_tv.add ('EOAM', 'active')
                sla_tv.add ('CPE_CFM', 'active')


                if device_type_yang == "ios-id:cisco-ios":
                    if pe_interface.if_num_ge is not None:
                        serv_if_num = pe_interface.if_num_ge
                        serv_if_size = "GigabitEthernet"
                        sla_tv.add ('IF_SIZE', serv_if_size)
                        sla_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_tenge is not None:
                        serv_if_num = pe_interface.if_num_tenge
                        serv_if_size = "TenGigabitEthernet"
                        sla_tv.add ('IF_SIZE', serv_if_size)
                        sla_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_po is not None:
                        serv_if_num = pe_interface.if_num_po
                        serv_if_size = "Port-channel"
                        sla_tv.add ('IF_SIZE', serv_if_size)
                        sla_tv.add ('IF_NUM', serv_if_num)
                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                    if pe_interface.if_num_ge_xr is not None:
                        serv_if_num = pe_interface.if_num_ge_xr
                        serv_if_size = "GigabitEthernet"
                        sla_tv.add ('IF_SIZE', serv_if_size)
                        sla_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_tenge_xr is not None:
                        serv_if_num = pe_interface.if_num_tenge_xr
                        serv_if_size = "TenGigE"
                        sla_tv.add ('IF_SIZE', serv_if_size)
                        sla_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_po_xr is not None:
                        serv_if_num = pe_interface.if_num_po_xr
                        serv_if_size = "Bundle-Ether"
                        sla_tv.add ('IF_SIZE', serv_if_size)
                        sla_tv.add ('IF_NUM', serv_if_num)

                sla_tv.add ('IF_ID', pe_interface.id_int)
                sla_tv.add ('IF_NUM', serv_if_num)
                sla_tv.add ('IF_SIZE', serv_if_size)
                sla_tv.add ('IF_END_TYPE', pe_interface.end_type)
                sla_tv.add ('IF_ENCAP', pe_interface.encapsulation)
                sla_tv.add ('IF_S_VLAN_ID', pe_interface.s_vlan_id)

                sla_tv.add ('SERV_INST', pe_interface.se_id)
                sla_tv.add ('BD_ID', pe_interface.bd_id)
                sla_tv.add ('VC_ID', pe_interface.vc_id)
                sla_tv.add ('PE_MEP_ID', pe_interface.mep_id)


                cpe_router = cpe_device.replace (".", "-")
                cpe_router = cpe_router.upper ()
                sla_tv.add ('CPE_ROUTER', cpe_router)

                pe_ipsla_pool = pe + '_IPSLA_POOL'
                unique_allocation = str (service) + '_' + service.name + "_" + str (pe) + "_" + str (pe_interface.id_int) + "_PE_CPE_RTT"
                id_allocator.id_request (service, service_path, 'sspa-bot', pe_ipsla_pool, unique_allocation, False)
                sla_id_cpe_rtt = id_allocator.id_read (tctx.username, root, pe_ipsla_pool, unique_allocation)

                pe_ipsla_pool = pe + '_IPSLA_POOL'
                unique_allocation = str (service) + '_' + service.name + "_" + str (pe) + "_" + str (
                    pe_interface.id_int) + "_PE_CPE_SLM"
                id_allocator.id_request (service, service_path, 'sspa-bot', pe_ipsla_pool, unique_allocation, False)
                sla_id_cpe_slm = id_allocator.id_read (tctx.username, root, pe_ipsla_pool, unique_allocation)

                if sla_id_cpe_slm is None:
                    self.log.info ("                            Allocation PE-CPE SLA ID SLM not ready")
                    sla_tv.add ('SLA_ID_CPE_SLM', '')
                    return
                elif sla_id_cpe_rtt is None:
                    self.log.info ("                            Allocation PE-CPE SLA ID RTT not ready")
                    sla_tv.add ('SLA_ID_CPE_RTT', '')
                    return
                else:
                    self.log.info ("                            Configure allocated PE-CPE SLA ID SLM: ",
                                   sla_id_cpe_slm)
                    sla_tv.add ('SLA_ID_CPE_SLM', sla_id_cpe_slm)
                    sla_tv.add ('SLA_ID_CPE_RTT', sla_id_cpe_rtt)


                    self.log.info ("                            Configure allocated PE-CPE SLA ID RTT: ", sla_id_cpe_rtt)


                    if device_type_yang == "ios-id:cisco-ios":
                        sla_tmpl.apply ('cruise-xe-sla-template', sla_tv)
                    elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                        sla_tmpl.apply ('cruise-xr-sla-template', sla_tv)

        def calc_sa_mac_addr(service, service_instance_id):
            self.log.info ("            Calculating Unique MAC address for Ethernet SLA Configuration for instance ID: ", service_instance_id)
            sid_len = len (str (service_instance_id))
            mac_last_tuple = str (service_instance_id)
            if (sid_len < 4):
                append_len = 4 - sid_len
                mac_last_tuple = ("0" * append_len) + str (service_instance_id)
            sa_mac_addr = "0001.0001." + mac_last_tuple
            return sa_mac_addr

        def configure_service_activation_testing(point, pe, pe_interface):
            sat_tv = ncs.template.Variables ()
            sat_tmpl = ncs.template.Template (service)
            device_type_yang = root.devices.device[pe].device_type.cli.ned_id

            sat_tv.add ('PE', pe)
            sat_tv.add ('SAT_DURATION', pe_interface.service_activation_testing.service_activation_testing_duration)
            sat_tv.add ('SAT_PACKET_SIZE', pe_interface.service_activation_testing.service_activation_testing_mtu)
            sat_bandwidth = pe_interface.service_activation_testing.service_activation_testing_bandwidth * 1000
            sat_tv.add ('SAT_BANDWIDTH', sat_bandwidth)


            if device_type_yang == "ios-id:cisco-ios":
                if pe_interface.if_num_ge is not None:
                    serv_if_num = pe_interface.if_num_ge
                    serv_if_size = "GigabitEthernet"
                    sat_tv.add ('IF_SIZE', serv_if_size)
                    sat_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_tenge is not None:
                    serv_if_num = pe_interface.if_num_tenge
                    serv_if_size = "TenGigabitEthernet"
                    sat_tv.add ('IF_SIZE', serv_if_size)
                    sat_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_po is not None:
                    serv_if_num = pe_interface.if_num_po
                    serv_if_size = "Port-channel"
                    sat_tv.add ('IF_SIZE', serv_if_size)
                    sat_tv.add ('IF_NUM', serv_if_num)
            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                if pe_interface.if_num_ge_xr is not None:
                    serv_if_num = pe_interface.if_num_ge_xr
                    serv_if_size = "GigabitEthernet"
                    sat_tv.add ('IF_SIZE', serv_if_size)
                    sat_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_tenge_xr is not None:
                    serv_if_num = pe_interface.if_num_tenge_xr
                    serv_if_size = "TenGigE"
                    sat_tv.add ('IF_SIZE', serv_if_size)
                    sat_tv.add ('IF_NUM', serv_if_num)
                if pe_interface.if_num_po_xr is not None:
                    serv_if_num = pe_interface.if_num_po_xr
                    serv_if_size = "Bundle-Ether"
                    sat_tv.add ('IF_SIZE', serv_if_size)
                    sat_tv.add ('IF_NUM', serv_if_num)

            sat_tv.add ('IF_NUM', serv_if_num)
            sat_tv.add ('IF_SIZE', serv_if_size)
            sat_tv.add ('SERV_INST', pe_interface.se_id)

            destination_mac_address = calc_sa_mac_addr (service.name, pe_interface.se_id)
            sat_tv.add ('SA_MAC_ADDR', destination_mac_address)

            pe_ipsla_pool = pe + '_IPSLA_POOL'
            unique_allocation = str (service) + '_' + service.name + "_" + str (pe) + "_" + str (pe_interface.id_int) + "_PE_CPE_SAT"
            id_allocator.id_request (service, service_path, 'sspa-bot', pe_ipsla_pool, unique_allocation, False)
            sla_id_sat = id_allocator.id_read (tctx.username, root, pe_ipsla_pool, unique_allocation)

            if sla_id_sat is None:
                self.log.info ("                            Allocation SLA ID SAT not ready")
                sat_tv.add ('SLA_ID_SAT', '')
                return
            else:
                self.log.info ("                            Configure allocated SLA ID SAT: ",  sla_id_sat)
                sat_tv.add ('SLA_ID_SAT', sla_id_sat)
                with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                    path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/pe-interfaces{%s}/service-activation-testing/sat-sla-id' % (
                        service.name, point.id, pe_interface.id_int)
                    t.set_elem (sla_id_sat, path)
                    t.apply ()
                    t.finish_trans ()

                with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                    path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/pe-interfaces{%s}/service-activation-testing/sat-sla-source' % (
                        service.name, point.id, pe_interface.id_int)
                    t.set_elem (pe, path)
                    t.apply ()
                    t.finish_trans ()

                self.log.info ("                            Configure allocated SLA ID SAT: ", sla_id_sat, " interface: ", serv_if_num, " SE: ",  pe_interface.se_id, " bandwidth: ", sat_bandwidth, " packet-size: ", pe_interface.service_activation_testing.service_activation_testing_mtu)

                if device_type_yang == "ios-id:cisco-ios":
                    sat_tmpl.apply ('cruise-xe-sat-template', sat_tv)
                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                    sat_tmpl.apply ('cruise-xr-sat-template', sat_tv)

        def configure_cpe(pe, pe_interface, cpe_device):
            cpe_device_type = device_helper.get_device_details (root, cpe_device)

            if "ASR-920" in cpe_device_type:
                cpe_type = "ASR-920"
            elif "mx204" in cpe_device_type:
                cpe_type = "MX204"

            cpe_tv = ncs.template.Variables ()
            cpe_tmpl = ncs.template.Template (service)

            self.log.info ("                    Configure EVC ID CPE: ", cpe_device)

            root.ralloc__resource_pools.id_pool.create (cpe_device + '_EVCID_POOL').range.start = bdid_pool_start
            root.ralloc__resource_pools.id_pool.create (cpe_device + '_EVCID_POOL').range.end = bdid_pool_end

            cpe_vc_id, d = vcid_allocation (service, cpe_device + '_EVCID_POOL', pe_interface.id_int, tctx, root)

            if d is False:
                return
            else:
                cpe_tv.add ('VC_ID', cpe_vc_id)
                self.log.info ("                    Configure allocated VC ID CPE device: ", cpe_device, " VC ID: ", cpe_vc_id)

            cpe_tv.add ('SERVICE_NAME', service.name)
            cpe_tv.add ('SERVICE_TYPE', "CRUISE L3VPN")

            cpe_tv.add ('CPE_DEVICE', cpe_device)
            cpe_tv.add ('DEVICE_TYPE', cpe_type)
            cpe_tv.add ('EOAM', 'active')
            cpe_tv.add ('CPE_CFM', 'active')

            cpe_interface_size = pe_interface.connected_cpe.cpe_device_interface.if_type

            if "ASR-920" in cpe_device_type or "ASR1001" in cpe_device_type:
                if cpe_interface_size == "GigabitEthernet":
                    cpe_interface_num = pe_interface.connected_cpe.cpe_device_interface.ios_xe.GigabitEthernet.if_num
                if cpe_interface_size == "TenGigabitEthernet":
                    cpe_interface_num = pe_interface.connected_cpe.cpe_device_interface.ios_xe.TenGigabitEthernet.if_num
                if cpe_interface_size == "PortChannel":
                    cpe_interface_num = pe_interface.connected_cpe.cpe_device_interface.ios_xe.PortChannel.if_num
                    cpe_interface_encapsulation = pe_interface.connected_cpe.cpe_device_interface.ios_xe.PortChannel.encapsulation
                    cpe_interface_s_vlan_id = pe_interface.connected_cpe.cpe_device_interface.ios_xe.PortChannel.s_vlan_id
                    cpe_interface_rewrite = pe_interface.connected_cpe.cpe_device_interface.ios_xe.PortChannel.rewrite
                    cpe_interface_c_vlans = pe_interface.connected_cpe.cpe_device_interface.ios_xe.PortChannel.c_vlan_id
                    cpe_interface_description = pe_interface.connected_cpe.cpe_device_interface.ios_xe.PortChannel.interface_description

            cpe_tv.add ('IF_ID', pe_interface.id_int)
            cpe_tv.add ('IF_SIZE', cpe_interface_size)
            cpe_tv.add ('IF_NUM', cpe_interface_num)
            cpe_tv.add ('IF_ENCAP', cpe_interface_encapsulation)
            cpe_tv.add ('CUSTOM_DESCR', cpe_interface_description)
            cpe_tv.add ('REWRITE', cpe_interface_rewrite)

            cpe_interface = str (cpe_interface_size) + str (cpe_interface_num)

            self.log.info ('                    Configure CPE interfaces for connected CPE: ', cpe_device,
                           ' interface:  ', cpe_interface)
            # Generate BDI

            self.log.info ('                    Configure BDI on CPE device: ', pe, " interface: ", cpe_interface)
            root.ralloc__resource_pools.id_pool.create (cpe_device + '_BDID_POOL').range.start = bdid_pool_start
            root.ralloc__resource_pools.id_pool.create (cpe_device + '_BDID_POOL').range.end = bdid_pool_end

            cpe_bd_id, a = bdid_allocation (service, cpe_device + '_BDID_POOL', pe_interface.id_int, tctx, root)

            if a is False:
                return
            else:
                cpe_tv.add ('BD_ID', cpe_bd_id)
                self.log.info ("                        Configure allocated BD ID for CPE: ", cpe_device,
                               " interface: ", cpe_interface, " BD ID: ", cpe_bd_id)
                self.log.info ("                        Configure allocated interface BVI for CPE: ", cpe_device,
                               " interface: ", " BVI", cpe_bd_id)

            # Generate Service-instance
            self.log.info ('                    Configure Service Instance on CPE device : ', cpe_device,
                           " interface: ", cpe_interface)
            per_interface_servinst_pool = cpe_device + '_' + cpe_interface + '_SERV_INST_POOL'
            root.ralloc__resource_pools.id_pool.create (per_interface_servinst_pool).range.start = serv_pool_start
            root.ralloc__resource_pools.id_pool.create (per_interface_servinst_pool).range.end = serv_pool_end
            service_instance = cpe_interface + "_" + str (pe_interface.id_int)

            b, cpe_serv_inst_id = servinst_allocation (service, service_path, per_interface_servinst_pool, 'sspa-bot',
                                                       root, service_instance)

            # Generate S-TAG
            self.log.info ('                    Configure S-VLAN on CPE device: ', cpe_device, " interface: ",
                           cpe_interface)

            if cpe_interface_encapsulation == 'dot1q-2tags':
                per_interface_vlan_pool = cpe_device + '_' + cpe_interface + '_VLANID_POOL_DOT1Q-2TAGS' + "_" + service.name
                root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.start = vlan_pool_start
                root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.end = vlan_pool_end
                a, cpe_serv_if_s_vlan_id = vlan_allocation (cpe_interface_s_vlan_id, service, service_path,
                                                            per_interface_vlan_pool, tctx, root, cpe_interface)
            else:
                per_interface_vlan_pool = cpe_device + '_' + cpe_interface + '_VLANID_POOL'
                root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.start = vlan_pool_start
                root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.end = vlan_pool_end
                a, cpe_serv_if_s_vlan_id = vlan_allocation (cpe_interface_s_vlan_id, service, service_path,
                                                            per_interface_vlan_pool, 'sspa-bot', root, cpe_interface)

            if service.ethernet_oam == "active":
                self.log.info ('                    Configure MEP ID on CPE device: ', cpe_device, " interface: ",
                               cpe_interface)
                cpe_mep_id, c = mep_allocation (service, tctx, root, cpe_device, cpe_interface)
            else:
                c = True

            if a is False:
                cpe_tv.add ('IF_S_VLAN_ID', "")
                return
            elif b is False:
                cpe_tv.add ('SERV_INST', "")
                return
            elif c is False:
                cpe_tv.add ('CPE_MEP_ID', "None")
                return
            else:
                self.log.info ("                    Configure allocated Service Instance on CPE device: ", cpe_device,
                               " interface: ", cpe_interface, " SE ID: ", cpe_serv_inst_id)
                self.log.info ("                    Configure allocated S-VLAN on CPE device: ", cpe_device,
                               " interface: ", cpe_interface, " S-VLAN ID: ", cpe_serv_if_s_vlan_id)

                if service.ethernet_oam == "active":
                    self.log.info ("                    Configure allocated MEP ID on CPE device: ", cpe_device,
                                   " interface: ", cpe_interface, " MEP ID: ", cpe_mep_id)
                    cpe_tv.add ('CPE_MEP_ID', cpe_mep_id)

                cpe_tv.add ('IF_S_VLAN_ID', cpe_serv_if_s_vlan_id)
                cpe_tv.add ('SERV_INST', cpe_serv_inst_id)

                if cpe_interface_encapsulation == 'dot1q-2tags':
                    cpe_c_vlans_list = []
                    for cpe_c_vlan in cpe_interface_c_vlans:
                        cpe_c_vlans_list.append (cpe_c_vlan)
                        cpe_c_vlans = ','.join (str (e) for e in cpe_c_vlans_list)

                    cpe_tv.add ('IF_C_VLAN_ID', cpe_c_vlans)
                    self.log.info ("                    Configured allocated C-VLAN on CPE device: ", cpe_device,
                                   " interface: ", cpe_interface, " C-VLAN: ", cpe_c_vlans)

                cpe_routing = pe_interface.connected_cpe.cpe_device_interface.routing.routing_enabled
                if cpe_routing == "true":
                    cpe_ip_addr = pe_interface.connected_cpe.cpe_device_interface.routing.cpe_ip_addr
                    cpe_mask = pe_interface.connected_cpe.cpe_device_interface.routing.cpe_mask
                    self.log.info ("                    Configured allocated BVI on CPE device: ", cpe_device,
                                   " interface BVI", cpe_bd_id, " IP: ", cpe_ip_addr, "/", cpe_mask)

                    cpe_tv.add ('CPE_IP_ADDR', cpe_ip_addr)
                    cpe_tv.add ('CPE_MASK', cpe_mask)
                else:
                    cpe_tv.add ('CPE_IP_ADDR', '')
                    cpe_tv.add ('CPE_MASK', '')

                cpe_device_type_yang = root.devices.device[cpe_device].device_type.cli.ned_id
                if cpe_device_type_yang == "ios-id:cisco-ios":
                    cpe_tmpl.apply ('cruise-cpe-xe-template', cpe_tv)
                elif cpe_device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                    cpe_tmpl.apply ('cruise-cpe-xr-template', cpe_tv)
                elif cpe_device_type_yang == "None":
                    cpe_tmpl.apply ('cruise-junos-oam-template', cpe_tv)

        def configure_l3vpn (tctx, root, service, point, proplist):

            device_type_yang = root.devices.device[point.access_pe].device_type.cli.ned_id

            interfaces_dict = {}

            pe = point.access_pe
            endpoints_list.append(pe)

            pe_bgp_as_no, pe_lpbk_ip = get_pe_bgp_asn_no (root, service, endpoints)
            as_no = pe_bgp_as_no[pe]


            ce_pe_rp = str(point.ce_pe_prot.routing)

            l3_tv = ncs.template.Variables ()
            tmpl = ncs.template.Template (service)

            serv_site_id = vpn_id

            serv_vrf_name = service_name
            pe_as_no = str (as_no)

            serv_rd = '12684' + ":" + str (serv_site_id)
            serv_rt = '12684' + ":" + str (serv_site_id)
            l3_tv.add ('RD', serv_rd)
            l3_tv.add ('SERV_SITE_ID', serv_site_id)

            l3_tv.add ('RT_EXPORT_LOCAL', serv_rt)
            l3_tv.add ('RT_IMPORT_LOCAL', serv_rt)
            l3_tv.add ('RT_IMPORT_EXT', '')
            l3_tv.add ('RT_EXPORT_EXT', '')
            l3_tv.add ('PE-CE-RM-IN', '')
            l3_tv.add ('PE-CE-RM-OUT', '')

            l3_tv.add ('RT_IMPORT_OPTION', point.vrf_leaking.vrf_import_local)
            l3_tv.add ('RT_EXPORT_OPTION', point.vrf_leaking.vrf_export_local)

            device_type = device_helper.get_device_details (root, pe)
            self.log.info ("        Configure device: ", pe, " device-type: ", device_type)

            with ncs.maapi.single_read_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                device_path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/access-pe' % (service.name, point.id)
                device_in_service_gen = t.exists (device_path)

            l3_tv.add ('PACKAGE', 'CRUISE L3VPN')
            l3_tv.add ('PE', pe)
            l3_tv.add ('PE_AS_NO', pe_as_no)

            l3_tv.add ('VRF_NAME', serv_vrf_name)
            l3_tv.add ('CE_PE_RP', ce_pe_rp)

            l3_tv.add ('REDISTRIBUTE_STATIC', point.routes_redistribution.static_routes)
            l3_tv.add ('REDISTRIBUTE_CONNECTED', point.routes_redistribution.connected_routes)

            # Configure BGP routing
            if ce_pe_rp == 'e-bgp':
                for neighbor in point.ce_pe_prot.e_bgp.bgp_neighbors:
                    self.log.info ("            Configure CE-PE Protocol: ", ce_pe_rp, " neighbor: ", neighbor.neighbor_ip, " AS: ", neighbor.ce_as_no)
                    l3_tv.add ('CE_PE_AS', neighbor.ce_as_no)
                    l3_tv.add ('CE_PE_NEI', neighbor.neighbor_ip)
                    l3_tv.add ('NEI_DESCR', neighbor.neighbor_description)

                    l3_tv.add ('CE_AS_MD5', neighbor.ce_as_md5)
                    self.log.info ("                AS: ", neighbor.ce_as_md5)
                    l3_tv.add ('CE_KEEPALIVE', neighbor.ce_keepalive)
                    self.log.info ("                Keep alive: ", neighbor.ce_keepalive)

                    l3_tv.add ('CE_HOLD', neighbor.ce_hold)
                    self.log.info ("                Hold time: ", neighbor.ce_hold)

                    l3_tv.add ('VPN_INTERNAL', neighbor.internal_vpn_client)
                    self.log.info ("                VPN Internal: ", neighbor.internal_vpn_client)

                    l3_tv.add ('RR-CLIENT', neighbor.route_reflector_client)
                    self.log.info ("                RR Client: ", neighbor.route_reflector_client)
                    l3_tv.add ('NH-SELF', neighbor.next_hop_self)
                    self.log.info ("                Next-hp-self: ", neighbor.next_hop_self)

                    l3_tv.add ('AS-OVERRIDE', neighbor.as_override)
                    self.log.info ('                AS-Override: ', neighbor.as_override)
                    l3_tv.add ('ALLOW-AS', neighbor.allow_as_in)
                    self.log.info ('                Allow-as in: ', neighbor.allow_as_in)

                    l3_tv.add ('MAX-PREF', neighbor.max_prefix)
                    self.log.info ('                Max prefix: ', neighbor.max_prefix)

                    l3_tv.add ('DMZ_LINK', neighbor.dmzlink_bw)
                    self.log.info ('                Dmzlink-bw: ', neighbor.dmzlink_bw)

                    l3_tv.add ('DEF-ORIG', neighbor.default_originate)
                    self.log.info ('                Default-originate: ', neighbor.default_originate)
                    l3_tv.add ('RM-IN', neighbor.enable_bgp_route_map_in)
                    self.log.info ('                BGP Route-map Input: ', neighbor.enable_bgp_route_map_in)
                    l3_tv.add ('RM-OUT', neighbor.enable_bgp_route_map_out)
                    self.log.info ('                BGP Route-map Output: ', neighbor.enable_bgp_route_map_out)

                    if device_type_yang == "ios-id:cisco-ios":
                        tmpl.apply ('cruise-xe-generic-template', l3_tv)
                    elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                        tmpl.apply ('cruise-xr-generic-template', l3_tv)

                    if neighbor.enable_bgp_route_map_in == "enable":
                        bgp_nei = str(neighbor.neighbor_ip).replace(".", "-")
                        route_map_in = "RM-" + service_name + "-" + bgp_nei + "-IN"
                        l3_tv.add ('PE-CE-RM-IN', route_map_in)
                        self.log.info ("            Configure Input bgp-route-map: ", route_map_in)
                        setup_bgp_route_map_in(service, point, neighbor, route_map_in)
                    if neighbor.enable_bgp_route_map_out == "enable":
                        bgp_nei = str(neighbor.neighbor_ip).replace(".", "-")
                        route_map_out = "RM-" + service_name + "-" + bgp_nei + "-OUT"
                        l3_tv.add ('PE-CE-RM-OUT', route_map_out)
                        self.log.info ("            Configure Output bgp-route-map: ", route_map_out)
                        setup_bgp_route_map_out(service, point, neighbor, route_map_out)

                    if device_type_yang == "ios-id:cisco-ios":
                        tmpl.apply ('cruise-xe-generic-template', l3_tv)
                    elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                        tmpl.apply ('cruise-xr-generic-template', l3_tv)
            else:
                l3_tv.add ('CE_PE_AS', "")
                l3_tv.add ('CE_PE_NEI', "")
                l3_tv.add ('NEI_DESCR', "")
                l3_tv.add ('CE_AS_MD5', "")
                l3_tv.add ('CE_HOLD', "")
                l3_tv.add ('CE_KEEPALIVE', "")
                l3_tv.add ('VPN_INTERNAL', "")
                l3_tv.add ('RR-CLIENT', "")
                l3_tv.add ('NH-SELF', "")
                l3_tv.add ('PL_NAME', "")
                l3_tv.add ('BGP_PREFIX_LIST', "")

                l3_tv.add ('ALLOW-AS', "")
                l3_tv.add ('AS-OVERRIDE', "")
                l3_tv.add ('DEF-ORIG', "")
                l3_tv.add ('MAX-PREF', "")
                l3_tv.add ('RM-IN', "")
                l3_tv.add ('RM-OUT', "")
                l3_tv.add ('PE-CE-RM-IN', "")
                l3_tv.add ('PE-CE-RM-OUT', "")
                l3_tv.add ('DMZ_LINK', "")
            # End Configure BGP routing

            # Configure Customer routing
            customer_prefixes = point.ce_pe_prot.static.customer_routes
            for customer_route in customer_prefixes:
                serv_customer_prefix = customer_route.customer_prefix
                serv_customer_prefix_mask = customer_route.customer_prefix_mask
                serv_customer_prefix_nh = customer_route.customer_prefix_nh
                setup_static_customer_routes (service, point, pe, serv_customer_prefix, serv_customer_prefix_mask, serv_customer_prefix_nh)
            # End Configure Customer routing

            # Configure VRF leaking
            if len(point.vrf_leaking.vrf_import_export_remote) == 0:
                l3_tv.add ('RT_IMPORT_EXT', '')
                l3_tv.add ('RT_EXPORT_EXT', '')
            else:
                for ext_vrf in point.vrf_leaking.vrf_import_export_remote:
                    self.log.info ("            Configure external VRF Import: ", pe, " VRF: ", ext_vrf)
                    ext_vrf_id = root.ralloc__resource_pools.idalloc__id_pool['VPN_ID'].idalloc__allocation[ext_vrf].idalloc__response.id
                    l3_tv.add ('RT_IMPORT_EXT', "12684:" + str(ext_vrf_id))
                    l3_tv.add ('RT_EXPORT_EXT', "12684:" + str(ext_vrf_id))
                    if device_type_yang == "ios-id:cisco-ios":
                        tmpl.apply ('cruise-xe-generic-template', l3_tv)
                    elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                        tmpl.apply ('cruise-xr-generic-template', l3_tv)
            # End Configure VRF leaking

            if device_type_yang == "ios-id:cisco-ios":
                tmpl.apply ('cruise-xe-generic-template', l3_tv)
            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                tmpl.apply ('cruise-xr-generic-template', l3_tv)

            serv_site_id = vpn_id

            serv_pe_interfaces = point.pe_interfaces
            interfaces_list = []
            interface_pool = get_pool (pe)

            peint_tv = ncs.template.Variables ()
            tmpl = ncs.template.Template (service)

            # Configure Interfaces PE
            self.log.info ('            Configure interfaces device: ', pe)

            for pe_interface in serv_pe_interfaces:
                device_type = device_helper.get_device_details (root, pe)


                if "ASR-920" in device_type:
                    peint_tv.add ('DEVICE_TYPE', "ASR-920")

                else:
                    peint_tv.add ('DEVICE_TYPE', "ASR-1001")

                peint_tv.add ('PE', pe)
                peint_tv.add ('SERV_SITE_ID', serv_site_id)
                peint_tv.add ('VRF_NAME', service_name)
                peint_tv.add ('SERVICE_NAME', service.name)
                interface_description = pe_interface.interface_description
                peint_tv.add ('CUSTOM_DESCR', interface_description)
                peint_tv.add ('SERVICE_TYPE', "CRUISE L3VPN")
                peint_tv.add ('EOAM', service.ethernet_oam)

                serv_if_type = pe_interface.if_type

                if device_type_yang == "ios-id:cisco-ios":
                    if pe_interface.if_num_ge is not None:
                        serv_if_num = pe_interface.if_num_ge
                        serv_if_size = "GigabitEthernet"
                        peint_tv.add ('IF_SIZE', serv_if_size)
                        peint_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_tenge is not None:
                        serv_if_num = pe_interface.if_num_tenge
                        serv_if_size = "TenGigabitEthernet"
                        peint_tv.add ('IF_SIZE', serv_if_size)
                        peint_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_po is not None:
                        serv_if_num = pe_interface.if_num_po
                        serv_if_size = "Port-channel"
                        peint_tv.add ('IF_SIZE', serv_if_size)
                        peint_tv.add ('IF_NUM', serv_if_num)
                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                    if pe_interface.if_num_ge_xr is not None:
                        serv_if_num = pe_interface.if_num_ge_xr
                        serv_if_size = "GigabitEthernet"
                        peint_tv.add ('IF_SIZE', serv_if_size)
                        peint_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_tenge_xr is not None:
                        serv_if_num = pe_interface.if_num_tenge_xr
                        serv_if_size = "TenGigE"
                        peint_tv.add ('IF_SIZE', serv_if_size)
                        peint_tv.add ('IF_NUM', serv_if_num)
                    if pe_interface.if_num_po_xr is not None:
                        serv_if_num = pe_interface.if_num_po_xr
                        serv_if_size = "Bundle-Ether"
                        peint_tv.add ('IF_SIZE', serv_if_size)
                        peint_tv.add ('IF_NUM', serv_if_num)

                int_temp = str (serv_if_size) + "_" + str (serv_if_num) + "_" + str (pe_interface.id_int)

                self.log.info ('                Configure interfaces device: ', pe, " interface: ", int_temp)

                serv_if_encap = pe_interface.encapsulation
                serv_if_end_type = pe_interface.end_type
                peint_tv.add ('IF_ENCAP', pe_interface.encapsulation)
                peint_tv.add ('IF_END_TYPE', pe_interface.end_type)
                peint_tv.add ('SERV_INST_ID', "")
                peint_tv.add ('IF_S_VLAN_ID', "")
                peint_tv.add ('IF_C_VLAN_ID', "")


                if serv_if_end_type == 'serv-inst':
                    service_path = "/services/CRUISE-SERVICES[service-type = 'L3VPN'][name = '{}']".format (service.name)

                    int = str(serv_if_size) + str(serv_if_num)

                    # Generate BDI

                    self.log.info ('                    Configure BDI on device: ', pe, " interface: ", int_temp)
                    root.ralloc__resource_pools.id_pool.create (pe + '_BDID_POOL').range.start = bdid_pool_start
                    root.ralloc__resource_pools.id_pool.create (pe + '_BDID_POOL').range.end = bdid_pool_end

                    bd_id, a = bdid_allocation (service, pe + '_BDID_POOL', pe_interface.id_int, tctx, root)

                    if a is False:
                        return
                    else:
                        peint_tv.add ('BD_ID', bd_id)
                        bdi_bandwidth = pe_interface.QoS.interface_bandwidth
                        peint_tv.add ('BDI_BW', bdi_bandwidth)
                        self.log.info ("                        Configure allocated BD ID for: ", pe, " interface: ", int_temp, " BD ID: ", bd_id)
                        self.log.info ("                        Configure allocated interface BVI for: ", pe, " interface: ",  " BVI", bd_id)
                        if device_in_service_gen is True:
                            with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                                path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/pe-interfaces{%s}/bd-id' % (
                                service.name, point.id, pe_interface.id_int)
                                t.set_elem (bd_id, path)
                                t.apply ()
                                t.finish_trans ()

                        if pe_interface.bdi_mac is not None:
                            self.log.info ("                        Configure allocated interface BVI for: ", pe, " interface: ", " BVI", bd_id, " MAC ", pe_interface.bdi_mac)
                            peint_tv.add ('BDI_MAC', pe_interface.bdi_mac)
                        else:
                            peint_tv.add ('BDI_MAC', 'None')


                    # Generate EVC

                    self.log.info ('                    Configure EVC on device: ', pe, " interface: ", int_temp)
                    root.ralloc__resource_pools.id_pool.create (pe + '_EVCID_POOL').range.start = bdid_pool_start
                    root.ralloc__resource_pools.id_pool.create (pe + '_EVCID_POOL').range.end = bdid_pool_end

                    vc_id, d = vcid_allocation (service, pe + '_EVCID_POOL', pe_interface.id_int, tctx, root)

                    if d is False:
                        return
                    else:
                        peint_tv.add ('VC_ID', vc_id)
                        self.log.info ("                        Configure allocated EVC ID for: ", pe, " interface: ", int_temp, " EVC ID: ", vc_id)
                        if device_in_service_gen is True:
                            with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                                path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/pe-interfaces{%s}/vc-id' % (
                                service.name, point.id, pe_interface.id_int)
                                t.set_elem (vc_id, path)
                                t.apply ()
                                t.finish_trans ()

                    # Generate Service-instance
                    self.log.info ('                    Configure Service Instance on device: ', pe, " interface: ", int_temp)

                    per_interface_servinst_pool = pe + '_' + int + '_SERV_INST_POOL'
                    root.ralloc__resource_pools.id_pool.create (per_interface_servinst_pool).range.start = serv_pool_start
                    root.ralloc__resource_pools.id_pool.create ( per_interface_servinst_pool).range.end = serv_pool_end
                    service_instance = int + "_" + str (pe_interface.id_int)

                    b, serv_inst_id = servinst_allocation (service, service_path, per_interface_servinst_pool, 'sspa-bot', root, service_instance)

                    # Generate S-TAG
                    self.log.info ('                    Configure S-VLAN on device: ', pe, " interface: ", int_temp)

                    if serv_if_encap == 'dot1q-2tags':
                        per_interface_vlan_pool = pe + '_' + int + '_VLANID_POOL_DOT1Q-2TAGS' + "_" + service.name
                        root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.start = vlan_pool_start
                        root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.end = vlan_pool_end
                        a, serv_if_s_vlan_id = vlan_allocation (pe_interface.s_vlan_id, service, service_path, per_interface_vlan_pool, tctx, root, int)
                    else:
                        per_interface_vlan_pool = pe + '_' + str(serv_if_size) + str(serv_if_num) + '_VLANID_POOL'
                        root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.start = vlan_pool_start
                        root.ralloc__resource_pools.id_pool.create (per_interface_vlan_pool).range.end = vlan_pool_end
                        a, serv_if_s_vlan_id = vlan_allocation (pe_interface.s_vlan_id, service, service_path, per_interface_vlan_pool, 'sspa-bot', root, int )

                    if service.ethernet_oam == "active":
                        self.log.info ('                    Configure MEP ID on device: ', pe, " interface: ", int_temp)
                        mep_id, c = mep_allocation (service, tctx, root, pe, pe_interface.id_int)
                    else:
                        c = True

                    if a is False:
                        peint_tv.add ('IF_S_VLAN_ID', "")
                        return
                    elif b is False:
                        peint_tv.add ('SERV_INST_ID', "")
                        return
                    elif c is False:
                        peint_tv.add ('MEP_ID', "None")
                        return
                    else:
                        self.log.info ("                    Configure allocated Service Instance on device: ", pe, " interface: ", int_temp, " SE ID: ", serv_inst_id)
                        self.log.info ("                    Configure allocated S-VLAN on device: ", pe, " interface: ", int_temp, " SE ID: ", serv_if_s_vlan_id)

                        if device_in_service_gen is True:
                            with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                                path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/pe-interfaces{%s}/se-id' % (
                                service.name, point.id, pe_interface.id_int)
                                t.set_elem (serv_inst_id, path)
                                t.apply ()
                                t.finish_trans ()

                        if service.ethernet_oam == "active":
                            self.log.info ("                    Configure allocated MEP ID on device: ", pe, " interface: ", int_temp, " MEP ID: ", mep_id )
                            peint_tv.add ('MEP_ID', mep_id)
                            if device_in_service_gen is True:
                                with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                                    path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/pe-interfaces{%s}/mep-id' % (service.name, point.id, pe_interface.id_int)
                                    t.set_elem (mep_id, path)
                                    t.apply ()
                                    t.finish_trans ()


                        interface = str(serv_if_size) + str(serv_if_num) + "_" + str(serv_if_s_vlan_id)

                        peint_tv.add ('BD_ID', bd_id)
                        peint_tv.add ('SERV_INST_ID', serv_inst_id)
                        peint_tv.add ('IF_S_VLAN_ID', serv_if_s_vlan_id)
                        if interface not in interfaces_list:
                            interfaces_list.append (interface)

                        if pe_interface.ip_assignment == "auto":
                            self.log.info ('                    Configure IP Allocation on device: ', pe,
                                           " interface: ", int_temp)
                            network = ip_allocation (service, interface_pool, tctx, root, ge_iface, 30)
                            if network is False:
                                peint_tv.add ('IF_PE_IP_ADDR', "")
                                peint_tv.add ('IF_PE_MASK', "")
                                return
                            else:
                                ip_address, net_mask = get_first_ip (network)
                                self.log.info ('IP and Mask: ', ip_address, " ", net_mask)
                                peint_tv.add ('IF_PE_IP_ADDR', ip_address)
                                peint_tv.add ('IF_PE_IP_ADDR', net_mask)
                        else:
                            if device_type_yang == "ios-id:cisco-ios":
                                self.log.info ('                    Configure manually allocated IP and Mask: ', str (serv_if_size) + str (serv_if_num), " BDI: ", bd_id, " ", pe_interface.pe_ip_addr, " ", pe_interface.pe_mask)
                            elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                                self.log.info ('                    Configure manually allocated IP and Mask: ', str (serv_if_size) + str (serv_if_num), " VLAN: ", serv_if_s_vlan_id, " ", pe_interface.pe_ip_addr, " ", pe_interface.pe_mask)
                            peint_tv.add ('IF_PE_IP_ADDR', pe_interface.pe_ip_addr)
                            peint_tv.add ('IF_PE_MASK', pe_interface.pe_mask)

                        peint_tv.add ('IF_TYPE', pe_interface.if_type)
                        peint_tv.add ('REWRITE', pe_interface.rewrite)
                        peint_tv.add ('GW_RED', pe_interface.gw_redundancy)
                        peint_tv.add ('VIP_IP_ADDR', pe_interface.vip_ip_addr)
                        peint_tv.add ('VIP_GROUP', pe_interface.vip_group)
                        peint_tv.add ('VIP_PRIORITY', pe_interface.vip_priority)
                        peint_tv.add ('IF_C_VLAN_ID', "")
                        peint_tv.add ('MS_SERVICE_TYPE', "")
                        peint_tv.add ('CPE_CFM', "")
                        peint_tv.add ('MEP_ID', "")
                        peint_tv.add ('ID_INT', pe_interface.id_int)

                        if device_type_yang == "ios-id:cisco-ios":
                            tmpl.apply ('cruise-xe-pe-interfaces', peint_tv)
                        elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                            tmpl.apply ('cruise-xr-pe-interfaces', peint_tv)
                else:
                    interface = str (serv_if_size) + str (serv_if_num)
                    serv_if_s_vlan_id = ""

                    if interface not in interfaces_list:
                        interfaces_list.append (interface)

                key_interface = pe
                interfaces_dict[key_interface] = interfaces_list

                if serv_if_end_type == 'serv-inst' and serv_if_encap == 'dot1q-2tags':
                    c_vlans_list = []
                    for c_vlan_id in pe_interface.c_vlan_id:
                        c_vlans_list.append (c_vlan_id)

                    c_vlans = ','.join (str (e) for e in c_vlans_list)
                    peint_tv.add ('IF_C_VLAN_ID', c_vlans)
                    tmpl.apply ('cruise-xe-pe-interfaces', peint_tv)

                if serv_if_end_type == 'serv-inst' and serv_if_encap == 'qinq':
                    for c_vlan_id in pe_interface.c_vlan_id:
                        self.log.info ("Configured C-VLAN: ", str(serv_if_size) + str(serv_if_num), " C-VLAN: ", c_vlan_id)
                        peint_tv.add ('IF_C_VLAN_ID', c_vlan_id)
                        tmpl.apply ('cruise-xe-pe-interfaces', peint_tv)

                if device_type_yang == "ios-id:cisco-ios":
                    tmpl.apply ('cruise-xe-pe-interfaces', peint_tv)
                elif device_type_yang == "cisco-ios-xr-id:cisco-ios-xr":
                    tmpl.apply ('cruise-xr-pe-interfaces', peint_tv)

                interfaces = json.dumps (interfaces_dict)
                with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                    path_interface= "/services/CRUISE-SERVICES/{L3VPN %s}/interfaces-list" % (service.name)
                    t.set_elem (interfaces, path_interface)
                    t.apply ()
                    t.finish ()

                # Configure QoS
                self.log.info ("                    Configure QoS on device: ", pe, " interface: ", pe_interface.id_int)
                setup_pe_interfaces_policy (service, point, pe, service_name, pe_interface)
                # End Configure QoS

                self.log.info ('                    Configure PE interfaces for connected CPE: ', pe, ' interface: ', int_temp)
                cpe_list = []

                if pe_interface.connected_cpe.cpe_device_in_nso == "true":
                    cpe_device_managed = "NSO"
                    cpe_device = pe_interface.connected_cpe.cpe_device
                    cpe_list.append (cpe_device)
                else:
                    cpe_device_managed = "MANUAL"
                    cpe_device = pe_interface.connected_cpe.cpe_device_manual
                    cpe_list.append (cpe_device)

                if service.ethernet_oam == 'active':
                    # Configure PE-CPE interfaces OAM
                    self.log.info ('                        Configure PE interfaces for connected CPE: ', pe, ' interface: ', int_temp, ' CPE device: ', cpe_device, ' / ', cpe_device_managed)
                    configure_ethernet_oam (pe, pe_interface)

                    if service.ethernet_sla.ethernet_sla_type == 'enable' or pe_interface.connected_cpe.cpe_device_ethernet_sla == "true":
                        pe_ipsla_pool = pe + '_IPSLA_POOL'
                        root.ralloc__resource_pools.id_pool.create (pe_ipsla_pool).range.start = ip_sla_pool_start
                        root.ralloc__resource_pools.id_pool.create (pe_ipsla_pool).range.end = ip_sla_pool_end
                        # Configure PE SLA
                        self.log.info ("                        Configure PE-CPE SLA")
                        configure_sla_pe (pe, pe_interface)

                    self.log.info ('                        Configure MEP-IF interfaces for connected CPE: ', pe, ' interface: ', int_temp, ' MEP-ID ', pe_interface.mep_id)

                    if pe_interface.connected_cpe.connected_cpe == "true" and pe_interface.connected_cpe.cpe_device_oam == "true" and pe_interface.connected_cpe.cpe_device_in_nso == "true":
                        # Configure CPE
                        configure_cpe(pe,   pe_interface, cpe_device)


                # End Configure CFM and SLA

                # Configure service activation testing

                if pe_interface.service_activation_testing.service_activation_testing == "true":
                    self.log.info ('                        Configure PE interfaces for service activation testing ', pe, ' interface: ', int_temp)
                    pe_ipsla_pool = pe + '_IPSLA_POOL'
                    root.ralloc__resource_pools.id_pool.create (pe_ipsla_pool).range.start = ip_sla_pool_start
                    root.ralloc__resource_pools.id_pool.create (pe_ipsla_pool).range.end = ip_sla_pool_end
                    configure_service_activation_testing(point, pe, pe_interface)



             # End Configure Interfaces PE

        if service.service_type == "L3VPN":
            self.log.info (" Provisioning service ", service.service_type, " ", service.name)

            pool_allocation_mode = 'production'
            if pool_allocation_mode == 'production':
                tunnel_pool_start = 500
                tunnel_pool_end = 65535
                bdid_pool_start = 3000
                bdid_pool_end = 3500
                serv_pool_start = 3501
                serv_pool_end = 4000
                vlan_pool_start = 2
                vlan_pool_end = 4092
                ip_sla_pool_start = 300
                ip_sla_pool_end = 500
            elif pool_allocation_mode == 'development':
                tunnel_pool_start = 10000
                tunnel_pool_end = 20000
                bdid_pool_start = 3700
                bdid_pool_end = 3800
                serv_pool_start = 3700
                serv_pool_end = 3800
                vlan_pool_start = 2
                vlan_pool_end = 3500
                ip_sla_pool_start = 500
                ip_sla_pool_end = 700

            service_name = service.name
            endpoints = service.endpoint

            service_path = "/services/CRUISE-SERVICES[service-type = 'L3VPN'][name = '{}']".format (service.name)

            self.log.info ("    Allocate VPN ID ", service_name)
            vpn_id_pool = 'VPN_ID'
            id_allocator.id_request (service, service_path, 'sspa-bot', vpn_id_pool, str (service.name), True)
            vpn_id = id_allocator.id_read ('sspa-bot', root, vpn_id_pool, str (service.name))

            if not vpn_id:
                self.log.info ("        VPN ID ID Alloc not ready")
                return
            else:
                self.log.info ("        Allocated VPN ID ", vpn_id)
                with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                    path = "/services/CRUISE-SERVICES/{L3VPN %s}/allocated-vpn-id" % (service.name)
                    t.set_elem (vpn_id, path)
                    t.apply ()
                    t.finish_trans ()

            # Configure VPN Base

            endpoints_list = []
            configured_devices = []
            new_devices = []
            devices = []

            for endpoint in service.endpoint:
                devices.append (endpoint)

            for device in devices:
                with ncs.maapi.single_read_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                    device_path = '/services/CRUISE-SERVICES/{L3VPN %s}/endpoint{%s}/access-pe' % (service.name, device.id)
                    device_in_service = t.exists (device_path)

                if device_in_service is True and device.access_pe in service.device_list:
                    if device.access_pe not in configured_devices:
                        configured_devices.append (device.access_pe)

                elif device_in_service is True and device.access_pe not in service.device_list:
                    if device.access_pe not in new_devices:
                        new_devices.append (device.access_pe)

                elif device_in_service is False and device.access_pe not in service.device_list:
                    if device.access_pe not in new_devices:
                        new_devices.append (device.access_pe)

            for device in devices:
                if device.access_pe in configured_devices:
                    self.log.info ("    Configured devices first ")
                    configure_l3vpn (tctx, root, service, device, proplist)

                if device.access_pe in new_devices:
                    self.log.info ("    New devices after ")
                    configure_l3vpn (tctx, root, service, device, proplist)

            # Check if service is configured
            in_sync = list (set (endpoints_list) - set (service.device_list))
            if len (in_sync) == 0:
                self.log.info ('===== Service configured ======')
                with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
                    path_provisioned = "/services/CRUISE-SERVICES/{L3VPN %s}/provisioned" % (service.name)
                    t.set_elem ("true", path_provisioned)
                    t.apply ()
                    t.finish ()



class Cruise_L3VPN_DevicesSyncFrom (Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, service, output):
        _ncs.dp.action_set_timeout (uinfo, 1800)

        with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
            root = ncs.maagic.get_root (t)
            service = ncs.maagic.cd (root, kp)
            for device in service.device_list:
                self.log.info ('Sync-from device: ', device)
                output = root.devices.device[device].sync_from ()

class Cruise_Services_Sync_all_devices (Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, service, output):
        _ncs.dp.action_set_timeout (uinfo, 3600)
        self.log.info ('Search for all devices in CRUISE-SERVICES services')

        with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
            root = ncs.maagic.get_root (t)
            cruise_services = root.services.CRUISE_SERVICES__CRUISE_SERVICES
            devices_for_sync = []
            for cruise_service in cruise_services:
                self.log.info ('    Found CRUISE-SERVICE service: ', cruise_service.name)
                for endpoint in cruise_service.endpoint:
                    self.log.info ("        Found PE device ", endpoint.access_pe, " in CRUISE-SERVICE service: ", cruise_service.name)
                    if endpoint.access_pe not in devices_for_sync:
                        devices_for_sync.append(endpoint.access_pe)

            for device_for_sync in sorted(devices_for_sync):
                self.log.info ('Sync-from PE device: ', device_for_sync)
                output = root.devices.device[device_for_sync].sync_from ()

class Cruise_L3VPN_stop_sat (Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, service, output):
        _ncs.dp.action_set_timeout (uinfo, 1800)

        with ncs.maapi.Maapi () as m:
            with ncs.maapi.Session (m, 'sspa-bot', uinfo.clearpass):
                with m.start_write_trans () as t:
                    root = ncs.maagic.get_root (t)
                    configured_sla_source = str(kp) + "/sat-sla-source"
                    configured_sla_sat_id = str(kp) + "/sat-sla-id"
                    sla_id = t.get_elem(configured_sla_sat_id)
                    sla_source = t.get_elem(configured_sla_source)
                    self.log.info ('Action Command: ', name , " ID: ", sla_id, " on ", sla_source)
                    device = root.ncs__devices.device[sla_source]
                    del device.config.ios__ip.sla.schedule[sla_id]
                    t.apply ()

class Cruise_L3VPN_start_sat (Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, service, output):
        _ncs.dp.action_set_timeout (uinfo, 1800)

        with ncs.maapi.Maapi () as m:
            with ncs.maapi.Session (m, 'sspa-bot', uinfo.clearpass):
                with m.start_write_trans () as t:
                    root = ncs.maagic.get_root (t)
                    configured_sla_source = str(kp) + "/sat-sla-source"
                    configured_sla_sat_id = str(kp) + "/sat-sla-id"
                    sla_id = t.get_elem(configured_sla_sat_id)
                    sla_source = t.get_elem(configured_sla_source)
                    self.log.info ('Action Command: ', name , " ID: ", sla_id, " on ", sla_source)
                    device = root.ncs__devices.device[sla_source]
                    device.config.ios__ip.sla.schedule.create(sla_id)
                    sat_sla_config = device.config.ios__ip.sla.schedule[sla_id]
                    sat_sla_config.start_time.now.create ()
                    t.apply ()

class Cruise_L3VPN_show_sat (Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, service, output):
        _ncs.dp.action_set_timeout (uinfo, 1800)

        with ncs.maapi.Maapi () as m:
            with ncs.maapi.Session (m, 'sspa-bot', uinfo.clearpass):
                with m.start_write_trans () as t:
                    root = ncs.maagic.get_root (t)
                    configured_sla_source = str(kp) + "/sat-sla-source"
                    configured_sla_sat_id = str(kp) + "/sat-sla-id"
                    sla_id = t.get_elem(configured_sla_sat_id)
                    sla_source = t.get_elem(configured_sla_source)
                    self.log.info ('Action Command: ', name , " ID: ", sla_id, " on ", sla_source)
                    tgen_source_exec_cmd = root.devices.device[sla_source].live_status.ios_stats__exec.any
                    cmd_input = tgen_source_exec_cmd.get_input ()
                    command = "show ip sla statistics " + str(sla_id) + " details | noprompts"
                    cmd_input.args = [command]
                    cmd_output = tgen_source_exec_cmd (cmd_input)
        output.result = cmd_output.result


# ---------------------------------------------
# COMPONENT THREAD THAT WILL BE STARTED BY NCS.
# ---------------------------------------------
class ses_cruise_services (ncs.application.Application):
    def setup(self):
        self.log.info ('SES CRUISE SERVICES RUNNING')
        self.register_service ('ses_cruise_services-servicepoint', ServiceCallbacks)
        self.register_action ('Cruise_L3VPN_DevicesSyncFrom-action', Cruise_L3VPN_DevicesSyncFrom)
        self.register_action ('Cruise_L3VPN_start-service-activation-testing-action', Cruise_L3VPN_start_sat)
        self.register_action ('Cruise_L3VPN_stop-service-activation-testing-action', Cruise_L3VPN_stop_sat)
        self.register_action ('Cruise_L3VPN_show-service-activation-testing-action', Cruise_L3VPN_show_sat)
        self.register_action ('CRUISE-SERVICES-SYNC-ALL-action', Cruise_Services_Sync_all_devices)

        self.log.info ('SES CRUISE SERVICES START')

    def teardown(self):
        self.log.info ('SES CRUISE SERVICES FINISHED')
