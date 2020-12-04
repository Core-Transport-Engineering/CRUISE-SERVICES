import ncs

def get_device_type(root, device):
    modules = root.devices.device[device].module.keys ()
    if str (modules[0]) == "{tailf-ned-cisco-ios-xr}":
        return str (modules[0])
    elif str (modules[0]) == "{junos}":
        return str (modules[0])
    else:
        return str (modules[2])

def get_loopback_address(root, device, device_type,
                         loopback_interface):
    device_config = root.ncs__devices.ncs__device[device].ncs__config
    address = None
    if device_type == '{tailf-ned-cisco-ios}':
        loopback = device_config.ios__interface.ios__Loopback[loopback_interface]
        address = loopback.ip.address.primary.address
    elif device_type == '{tailf-ned-cisco-ios-xr}':
        loopback = device_config.cisco_ios_xr__interface.Loopback[loopback_interface]
        address = loopback.ipv4.address.ip
    elif device_type == '{junos}':
        loopbacks = device_config.junos__configuration.interfaces.interface['lo0']
        loopback = loopbacks.unit['0'].family.inet.address
        lo_list = []
        for loopback_ip in loopback:
            lo_list.append (loopback_ip.name)
        ip = lo_list[0]
        address, mask = ip.split ("/")
    else:
        raise Exception ('Unknown device type ' + device_type)
    return address


def get_device_details(root, current_device):
    with ncs.maapi.single_write_trans ('admin', 'system', db=ncs.OPERATIONAL) as t:
        for device in root.devices.device:
            if device.name == current_device:
                device_model = device.platform.model
    return device_model
