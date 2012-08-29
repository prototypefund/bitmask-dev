import ConfigParser
import os
import platform

try:
    import unittest2 as unittest
except ImportError:
    import unittest

from leap.base import constants
from leap.eip import config as eip_config
from leap.testing.basetest import BaseLeapTest

_system = platform.system()


class EIPConfigTest(BaseLeapTest):

    __name__ = "eip_config_tests"

    def setUp(self):
        pass

    def tearDown(self):
        pass

    #
    # helpers
    #

    def touch_exec(self):
        tfile = os.path.join(
            self.tempfile,
            'bin',
            'openvpn')
        open(tfile, 'bw').close()

    def get_empty_config(self):
        _config = ConfigParser.ConfigParser()
        return _config

    def get_minimal_config(self):
        _config = ConfigParser.ConfigParser()
        return _config

    def get_expected_openvpn_args(self):
        args = []
        username = self.get_username()
        groupname = self.get_groupname()

        args.append('--client')
        args.append('--dev')
        #does this have to be tap for win??
        args.append('tun')
        args.append('--persist-tun')
        args.append('--persist-key')
        args.append('--remote')
        args.append('testprovider.example.org')
        # XXX get port!?
        args.append('1194')
        # XXX get proto
        args.append('udp')
        args.append('--tls-client')
        args.append('--remote-cert-tls')
        args.append('server')

        args.append('--user')
        args.append(username)
        args.append('--group')
        args.append(groupname)
        args.append('--management-client-user')
        args.append(username)
        args.append('--management-signal')

        args.append('--management')
        #XXX hey!
        #get platform switches here!
        args.append('/tmp/.eip.sock')
        args.append('unix')

        # certs
        # XXX get values from specs?
        args.append('--cert')
        args.append(os.path.join(
            self.home,
            '.config', 'leap', 'providers',
            'testprovider.example.org',
            'keys', 'client',
            'openvpn.pem'))
        args.append('--key')
        args.append(os.path.join(
            self.home,
            '.config', 'leap', 'providers',
            'testprovider.example.org',
            'keys', 'client',
            'openvpn.pem'))
        args.append('--ca')
        args.append(os.path.join(
            self.home,
            '.config', 'leap', 'providers',
            'testprovider.example.org',
            'keys', 'ca',
            'testprovider-ca-cert.pem'))
        return args

    # build command string
    # these tests are going to have to check
    # many combinations. we should inject some
    # params in the function call, to disable
    # some checks.

    def test_build_ovpn_command_empty_config(self):
        _config = self.get_empty_config()
        command, args = eip_config.build_ovpn_command(
            _config,
            do_pkexec_check=False)
        self.assertEqual(command, 'openvpn')
        self.assertEqual(args, self.get_expected_openvpn_args())

    # XXX TODO:
    # - should use touch_exec to plant an "executable" in the path
    # - should check that "which" for openvpn returns what's expected.


if __name__ == "__main__":
    unittest.main()
