import logging
import threading
import multiprocessing as mp

logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
# suppresses following message
# WARNING: No route found for IPv6 destination :: (no default route?)
from scapy.all import sendp, ARP, Ether, ETHER_BROADCAST, ICMPv6NDOptDstLLAddr, ICMPv6ND_NA, IPv6

import util
from sniff_thread import SelectiveIPv4SniffThread, SelectiveIPv6SniffThread
from apate_redis import ApateRedis
from misc_thread import ARPDiscoveryThread, IGMPDiscoveryThread, PubSubThread, MulticastPingDiscoveryThread, MulticastListenerDiscoveryThread


class SelectiveIPv4Process(mp.Process):
    """Implements the abstract class _DaemonApp and also implements the selective spoofing mode of Apate.
    The selective spoofing mode requires more resources than the holistic spoofing mode,
    e.g.: the redis-server. This mode only generates packets for existing clients (not every possible client).
    This mode is suitable for bigger networks, as the bottleneck of this mode is virtually only the host discovery.
    """

    __SLEEP = 5
    """int: Defines the time to sleep after packets are sent before they are sent anew."""

    def __init__(self, logger, interface, ipv4):
        """Initialises several things needed to define the daemons behaviour.

        Args:
            logger (logging.Logger): Used for logging messages.
            interface (str): The network interface which should be used. (e.g. eth0)
            pidfile (str): Path of the pidfile, used by the daemon.
            stdout (str): Path of stdout, used by the daemon.
            stderr (str): Path of stderr, used by the daemon.
            dns_file (str): Path of file containing the nameservers.

        Raises:
            DaemonError: Signalises the failure of the daemon.
        """
        super(self.__class__, self).__init__()

        self.exit = mp.Event()

        self.interface = interface
        self.logger = logger
        # add redis objects to the ip tuples
        self.ipv4 = ipv4._replace(redis=ApateRedis(str(ipv4.network.network), logger))

        # used for thread synchronisation (for waking this thread)
        self.sleeper = threading.Condition()

        self.threads = {}
        # Initialise threads
        self.threads['sniffthread'] = SelectiveIPv4SniffThread(self.interface, self.ipv4, self.sleeper)
        self.threads['psthread'] = PubSubThread(self.ipv4, self.logger, self.spoof_devices)
        self.threads['arpthread'] = ARPDiscoveryThread(self.ipv4.ip, str(self.ipv4.network.network))#.gateway
        self.threads['igmpthread'] = IGMPDiscoveryThread(self.ipv4)

        # declare all threads as deamons
        for worker in self.threads:
            self.threads[worker].daemon = True

    def _return_to_normal(self):
        """This method is called when the daemon is stopping.
        First, sends a GARP broadcast request to all clients to tell them the real gateway.
        Then ARP replies for existing clients are sent to the gateway.
        If IPv6 is enabled, Apate tells the clients the real gateway via neighbor advertisements.
        """
        # spoof clients with GARP broadcast request

        with self.sleeper:
            sendp(
                Ether(dst=ETHER_BROADCAST) / ARP(op=1, psrc=self.ipv4.gateway, pdst=self.ipv4.gateway, hwdst=ETHER_BROADCAST,
                                                 hwsrc=self.ipv4.gate_mac))

    def shutdown(self):
        self.exit.set()
        # this would be self.sleeper of parent not child
        # with self.sleeper:
        #     self.sleeper.notify()

    def run(self):
        """Starts multiple threads sends out packets to spoof
        all existing clients on the network and the gateway. This packets are sent every __SLEEP seconds.
        The existing clients (device entries) are read from the redis database.

        Threads:
            A SniffThread, which sniffs for incoming ARP packets and adds new devices to the redis db.
            Several HostDiscoveryThread, which are searching for existing devices on the network.
            A PubSubThread, which is listening for redis expiry messages.

        Note:
            First, ARP replies to spoof the gateway entry of existing clients arp cache are generated.
            ARP relpies to spoof the entries of the gateway are generated next.
            Unlike the holistic mode only packets for existing clients are generated.

        """
        try:
            for worker in self.threads:
                self.threads[worker].start()

            # lamda expression to generate arp replies to spoof the clients
            exp1 = lambda dev: Ether(dst=dev[1]) / ARP(op=2, psrc=self.ipv4.gateway, pdst=dev[0], hwdst=dev[1])

            while not self.exit.is_set():
                # generates packets for existing clients

                sendp([p(dev) for dev in self.ipv4.redis.get_devices_values(filter_values=True) for p in (exp1,)])
                try:
                    with self.sleeper:
                        self.sleeper.wait(timeout=self.__SLEEP)
                except RuntimeError as e:
                    # this error is thrown by the with-statement when the thread is stopped
                    if len(e.args) > 0 and e.args[0] == "cannot release un-acquired lock":
                        return
                    else:
                        raise e

            self._return_to_normal()
        except Exception as e:
            self.logger.error("Process IPv4")
            self.logger.exception(e)

    @staticmethod
    def spoof_devices(ip, devs, logger):
        for entry in devs:
            dev_ip = util.get_device_ip(entry)
            dev_hw = ip.redis.get_device_mac(dev_ip, network=util.get_device_net(entry))
            if util.get_device_enabled(entry) == "1":
                sendp(Ether(dst=dev_hw) / ARP(op=2, psrc=ip.gateway, pdst=dev_ip, hwdst=dev_hw))
                # sendp(Ether(dst=ip.gate_mac) / ARP(op=2, psrc=dev_ip, pdst=ip.gateway, hwdst=ip.gate_mac))
            else:
                sendp(Ether(dst=dev_hw) / ARP(op=2, psrc=ip.gateway, pdst=dev_ip, hwdst=dev_hw, hwsrc=ip.gate_mac))
                # sendp(Ether(dst=ip.gate_mac) / ARP(op=2, psrc=dev_ip, pdst=ip.gateway, hwsrc=dev_hw))


class SelectiveIPv6Process(mp.Process):
    """Implements the abstract class _DaemonApp and also implements the selective spoofing mode of Apate.
    The selective spoofing mode requires more resources than the holistic spoofing mode,
    e.g.: the redis-server. This mode only generates packets for existing clients (not every possible client).
    This mode is suitable for bigger networks, as the bottleneck of this mode is virtually only the host discovery.
    """

    __SLEEP = 3
    """int: Defines the time to sleep after packets are sent before they are sent anew."""

    def __init__(self, logger, interface, ipv6):
        """Initialises several things needed to define the daemons behaviour.

        Args:
            logger (logging.Logger): Used for logging messages.
            interface (str): The network interface which should be used. (e.g. eth0)
            pidfile (str): Path of the pidfile, used by the daemon.
            stdout (str): Path of stdout, used by the daemon.
            stderr (str): Path of stderr, used by the daemon.
            dns_file (str): Path of file containing the nameservers.

        Raises:
            DaemonError: Signalises the failure of the daemon.
        """
        super(self.__class__, self).__init__()

        self.exit = mp.Event()

        self.interface = interface
        self.logger = logger

        # add redis objects to the ip tuples
        self.ipv6 = ipv6._replace(redis=ApateRedis(str(ipv6.network.network), logger))

        # used for thread synchronisation (for waking this thread)
        self.sleeper = threading.Condition()

        self.threads = {}
        # Initialise threads
        self.threads['sniffthread'] = SelectiveIPv6SniffThread(self.interface, self.ipv6, self.sleeper)
        self.threads['icmpv6thread'] = MulticastPingDiscoveryThread(self.interface)
        self.threads['mldv2thread'] = MulticastListenerDiscoveryThread(self.interface)
        self.threads['psthread6'] = PubSubThread(self.ipv6, self.logger, self.spoof_devices)

        # declare all threads as deamons
        for worker in self.threads:
            self.threads[worker].daemon = True

    def _return_to_normal(self):
        """This method is called when the daemon is stopping.
        First, sends a GARP broadcast request to all clients to tell them the real gateway.
        Then ARP replies for existing clients are sent to the gateway.
        If IPv6 is enabled, Apate tells the clients the real gateway via neighbor advertisements.
        """
        # spoof clients with GARP broadcast request
        with self.sleeper:
            # check if the impersonation of the DNS server is necessary
            tgt = (self.ipv6.gateway, self.ipv6.dns_servers[0]) if util.is_spoof_dns(self.ipv6) else (self.ipv6.gateway,)

            for source in tgt:
                sendp(Ether(dst=ETHER_BROADCAST) / IPv6(src=source, dst=MulticastPingDiscoveryThread._MULTICAST_DEST) /
                      ICMPv6ND_NA(tgt=source, R=0, S=0) / ICMPv6NDOptDstLLAddr(lladdr=self.ipv6.gate_mac))

    def shutdown(self):
        self.exit.set()
        # with self.sleeper:
        #     self.sleeper.notify()

    def run(self):
        """Starts multiple threads sends out packets to spoof
        all existing clients on the network and the gateway. This packets are sent every __SLEEP seconds.
        The existing clients (device entries) are read from the redis database.

        Threads:
            A SniffThread, which sniffs for incoming ARP packets and adds new devices to the redis db.
            Several HostDiscoveryThread, which are searching for existing devices on the network.
            A PubSubThread, which is listening for redis expiry messages.

        Note:
            First, ARP replies to spoof the gateway entry of existing clients arp cache are generated.
            ARP relpies to spoof the entries of the gateway are generated next.
            Unlike the holistic mode only packets for existing clients are generated.

        """
        try:
            for worker in self.threads:
                self.threads[worker].start()

            # check if the impersonation of the DNS server is necessary
            tgt = (self.ipv6.gateway, self.ipv6.dns_servers[0]) if util.is_spoof_dns(self.ipv6) else (self.ipv6.gateway,)

            while not self.exit.is_set():
                packets = []

                for source in tgt:
                    packets.extend([Ether(dst=dev[1]) / IPv6(src=source, dst=dev[0]) /
                                    ICMPv6ND_NA(tgt=source, R=0, S=1) / ICMPv6NDOptDstLLAddr(lladdr=self.ipv6.mac)
                                    for dev in self.ipv6.redis.get_devices_values(filter_values=True)])

                sendp(packets)
                try:
                    with self.sleeper:
                        self.sleeper.wait(timeout=self.__SLEEP)
                except RuntimeError as e:
                    # this error is thrown by the with-statement when the thread is stopped
                    if len(e.args) > 0 and e.args[0] == "cannot release un-acquired lock":
                        return
                    else:
                        raise e
            self._return_to_normal()
        except Exception as e:
            self.logger.error("Process IPv6")
            self.logger.exception(e)

    @staticmethod
    def spoof_devices(ip, devs, logger):
        tgt = (ip.gateway, ip.dns_servers[0]) if util.is_spoof_dns(ip) else (ip.gateway,)

        for entry in devs:
            dev_ip = util.get_device_ip(entry)
            dev_hw = ip.redis.get_device_mac(dev_ip, network=util.get_device_net(entry))

            for source in tgt:
                if util.get_device_enabled(entry) == "1":
                    # sendp(Ether(dst=dev_hw) / ARP(op=2, psrc=ip.gateway, pdst=dev_ip, hwdst=dev_hw))
                    sendp([Ether(dst=dev_hw) / IPv6(src=source, dst=dev_ip) /
                           ICMPv6ND_NA(tgt=source, R=0, S=1) / ICMPv6NDOptDstLLAddr(lladdr=ip.mac)])
                    # sendp(Ether(dst=ip.gate_mac) / ARP(op=2, psrc=dev_ip, pdst=ip.gateway, hwdst=ip.gate_mac))
                else:
                    # sendp(Ether(dst=dev_hw) / ARP(op=2, psrc=ip.gateway, pdst=dev_ip, hwdst=dev_hw, hwsrc=ip.gate_mac))
                    # sendp(Ether(dst=ip.gate_mac) / ARP(op=2, psrc=dev_ip, pdst=ip.gateway, hwsrc=dev_hw))
                    sendp([Ether(dst=dev_hw) / IPv6(src=source, dst=dev_ip) /
                           ICMPv6ND_NA(tgt=source, R=0, S=1) / ICMPv6NDOptDstLLAddr(lladdr=ip.gate_mac)])
