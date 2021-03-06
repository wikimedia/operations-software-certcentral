import collections
import os
import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta

import mock
from cryptography.hazmat.primitives.asymmetric import ec

from acme_chief.acme_requests import (ACMEAccount,
                                      ACMEChallengeNotValidatedError,
                                      ACMEChallengeType,
                                      ACMEChallengeValidation, ACMEError,
                                      ACMEInvalidChallengeError,
                                      ACMEIssuedCertificateError,
                                      DNS01ACMEChallenge)
from acme_chief.acme_chief import (CERTIFICATE_TYPES,
                                   DEFAULT_DNS_ZONE_UPDATE_CMD,
                                   DEFAULT_DNS_ZONE_UPDATE_CMD_TIMEOUT,
                                   KEY_TYPES, ACMEChief, ACMEChiefConfig,
                                   CertificateState, CertificateStatus)
from acme_chief.x509 import (Certificate, CertificateSaveMode, ECPrivateKey,
                             PrivateKeyLoader, X509Error)
from tests.test_pebble import (BaseDNSRequestHandler,
                               BasePebbleIntegrationTest,
                               HTTP01ChallengeHandler)

DIRECTORY_URL = 'https://127.0.0.1:14000/dir'

VALID_CONFIG_EXAMPLE = '''
accounts:
  - id: ee566f9e436e120082f0770c0d58dd6d
    directory: https://acme-staging-v02.api.letsencrypt.org/directory
    default: true
  - id: 621b49f9c6ccbbfbff9acb6e18f71205
    directory: https://127.0.0.1:14000/dir
certificates:
  default_account_certificate:
    CN: acmechieftest.beta.wmflabs.org
    SNI:
        - acmechieftest.beta.wmflabs.org
    challenge: http-01
    authorized_hosts:
        - deployment-acmechief-testclient03.deployment-prep.eqiad.wmflabs
  non_default_account_certificate:
    account: 621b49f9c6ccbbfbff9acb6e18f71205
    CN: 'test.wmflabs.org'
    SNI:
        - '*.test.wmflabs.org'
    challenge: dns-01
    staging_time: 7200
  certificate_auth_by_regex:
    CN: regex.test.wmflabs.org
    SNI:
        - regex.test.wmflabs.org
    challenge: http-01
    authorized_regexes:
        - '^deployment-acmechief-testclient0[1-3]\.deployment-prep\.eqiad\.wmflabs$'
    staging_time: 3600
challenges:
    dns-01:
        validation_dns_servers:
            - 127.0.0.1
        sync_dns_servers:
            - 127.0.0.1
        zone_update_cmd: /usr/bin/dns-update-zone
        zone_update_cmd_timeout: 30.5
'''

VALID_CONFIG_EXAMPLE_WITHOUT_DEFAULT_ACCOUNT = '''
accounts:
  - id: 621b49f9c6ccbbfbff9acb6e18f71205
    directory: https://127.0.0.1:14000/dir
  - id: ee566f9e436e120082f0770c0d58dd6d
    directory: https://acme-staging-v02.api.letsencrypt.org/directory
certificates:
  default_account_certificate:
    CN: acmechieftest.beta.wmflabs.org
    SNI:
        - acmechieftest.beta.wmflabs.org
    challenge: http-01
    staging_time: 3600
  non_default_account_certificate:
    account: 621b49f9c6ccbbfbff9acb6e18f71205
    CN: 'test.wmflabs.org'
    SNI:
        - '*.test.wmflabs.org'
    challenge: dns-01
    staging_time: 3600
challenges:
    dns-01:
        validation_dns_servers:
            - 127.0.0.1
        sync_dns_servers:
            - 127.0.0.1
'''

CONFD_VALID_FILE_EXAMPLE = '''
certname: default_account_certificate
hostname: deployment-acmechief-testclient02.deployment-prep.eqiad.wmflabs
'''

class InfiniteLoopBreaker(Exception):
    """Exception to be raised when time.sleep() is mocked"""


class ACMEChiefConfigTest(unittest.TestCase):
    def setUp(self):
        self.base_path = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.base_path.name, ACMEChief.config_path)
        self.confd_path = os.path.join(self.base_path.name, ACMEChief.confd_path)
        os.mkdir(self.confd_path)

        with open(os.path.join(self.confd_path, 'confd_file_example.yaml'), 'w') as confd_file:
            confd_file.write(CONFD_VALID_FILE_EXAMPLE)

    def tearDown(self):
        self.base_path.cleanup()

    @mock.patch('os.access', return_value=True)
    def test_config_parsing(self, access_mock):
        with open(self.config_path, 'w') as config_file:
            config_file.write(VALID_CONFIG_EXAMPLE)

        config = ACMEChiefConfig.load(self.config_path, confd_path=self.confd_path)
        self.assertEqual(len(config.accounts), 2)
        self.assertEqual(len(config.certificates), 3)
        self.assertEqual(config.default_account, 'ee566f9e436e120082f0770c0d58dd6d')
        self.assertIn('default_account_certificate', config.authorized_hosts)
        self.assertIn('deployment-acmechief-testclient02.deployment-prep.eqiad.wmflabs',
                      config.authorized_hosts['default_account_certificate'])
        self.assertIn('deployment-acmechief-testclient03.deployment-prep.eqiad.wmflabs',
                      config.authorized_hosts['default_account_certificate'])
        self.assertIn(ACMEChallengeType.DNS01, config.challenges)
        self.assertEqual(config.certificates['default_account_certificate']['staging_time'],
                         timedelta(seconds=3600))
        self.assertEqual(config.certificates['non_default_account_certificate']['staging_time'],
                         timedelta(seconds=7200))
        self.assertEqual(config.challenges[ACMEChallengeType.DNS01]['zone_update_cmd'], '/usr/bin/dns-update-zone')
        self.assertEqual(config.challenges[ACMEChallengeType.DNS01]['zone_update_cmd_timeout'], 30.5)
        access_mock.assert_called_once_with('/usr/bin/dns-update-zone', os.X_OK)

    def test_config_without_explicit_default(self):
        with open(self.config_path, 'w') as config_file:
            config_file.write(VALID_CONFIG_EXAMPLE_WITHOUT_DEFAULT_ACCOUNT)

        config = ACMEChiefConfig.load(self.config_path, confd_path=self.confd_path)
        self.assertEqual(config.default_account, '621b49f9c6ccbbfbff9acb6e18f71205')

    def test_access_check(self):
        with open(self.config_path, 'w') as config_file:
            config_file.write(VALID_CONFIG_EXAMPLE)

        config = ACMEChiefConfig.load(self.config_path, confd_path=self.confd_path)
        self.assertTrue(config.check_access('deployment-acmechief-testclient03.deployment-prep.eqiad.wmflabs',
                                            'default_account_certificate'))
        self.assertTrue(config.check_access('deployment-acmechief-testclient02.deployment-prep.eqiad.wmflabs',
                                            'default_account_certificate'))
        self.assertFalse(config.check_access('deployment-acmechief-testclient04.deployment-prep.eqiad.wmflabs',
                                             'default_account_certificate'))

        self.assertTrue(config.check_access('deployment-acmechief-testclient03.deployment-prep.eqiad.wmflabs',
                                            'certificate_auth_by_regex'))
        self.assertTrue(config.check_access('deployment-acmechief-testclient02.deployment-prep.eqiad.wmflabs',
                                            'certificate_auth_by_regex'))
        self.assertFalse(config.check_access('deployment-acmechief-testclient04.deployment-prep.eqiad.wmflabs',
                                             'certificate_auth_by_regex'))


class CertificateStateTest(unittest.TestCase):
    def setUp(self):
        self.state = CertificateState(CertificateStatus.INITIAL)

    def test_init(self):
        self.assertIs(self.state.status, CertificateStatus.INITIAL)
        self.assertEqual(self.state.retries, 0)
        self.assertGreaterEqual(datetime.utcnow(), self.state.next_retry)

    def test_set(self):
        for attribute in ('retries', 'next_retry'):
            with self.assertRaises(AttributeError) as context:
                self.state.__setattr__(attribute, 1)

            self.assertIn("can't set attribute", str(context.exception))

    def test_set_status_without_retries(self):
        for status in (CertificateStatus.INITIAL, CertificateStatus.SELF_SIGNED, CertificateStatus.VALID,
                       CertificateStatus.NEEDS_RENEWAL, CertificateStatus.EXPIRED, CertificateStatus.SUBJECTS_CHANGED):
            self.state.status = status
            self.assertEqual(self.state.retries, 0)
            self.assertGreaterEqual(datetime.utcnow(), self.state.next_retry)
            self.assertTrue(self.state.retry)

    def test_set_status_with_slow_retries(self):
        for status in CertificateState.STATUS_WITH_SLOW_RETRIES:
            current_retries = self.state.retries
            self.state.status = status
            self.assertEqual(self.state.retries, current_retries+1)
            self.assertFalse(self.state.retry)
            self.assertAlmostEqual(self.state.next_retry, datetime.utcnow() + CertificateState.SLOW_RETRY,
                                   delta=timedelta(seconds=1))

    def test_set_status_with_retries(self):
        for status in CertificateState.STATUS_WITH_RETRIES:
            self.state.status = CertificateStatus.INITIAL  # force setting number of retries to 0
            self.assertEqual(self.state.retries, 0)        # sanity check

            for _ in range(CertificateState.MAX_CONSECUTIVE_RETRIES):
                current_retries = self.state.retries
                self.state.status = status
                self.assertEqual(self.state.retries, current_retries+1)
                self.assertGreater(datetime.utcnow(), self.state.next_retry)
                self.assertTrue(self.state.retry)

            self.assertEqual(self.state.retries, CertificateState.MAX_CONSECUTIVE_RETRIES)  # sanity check
            for _ in range(self.state.retries, CertificateState.MAX_RETRIES):
                current_retries = self.state.retries
                self.state.status = status
                self.assertEqual(self.state.retries, current_retries+1)
                self.assertAlmostEqual(self.state.next_retry, datetime.utcnow() +
                                       timedelta(seconds=2**(current_retries+1)),
                                       delta=timedelta(seconds=1))
                self.assertFalse(self.state.retry)

            self.assertEqual(self.state.retries, CertificateState.MAX_RETRIES)  # sanity check
            self.state.status = status
            self.assertIsNone(self.state.next_retry)
            self.assertFalse(self.state.retry)


class ACMEChiefTest(unittest.TestCase):
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    def setUp(self, sighup_handler_mock, signal_mock):
        self.instance = ACMEChief()
        self.instance.config = ACMEChiefConfig(
            accounts=[{'id': '1945e767ad72a532ebca519242a801bf', 'directory': 'https://127.0.0.1:14000/dir'}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'http-01',
                    'staging_time': timedelta(seconds=3600),
                },
            },
            default_account='1945e767ad72a532ebca519242a801bf',
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                }
            }
        )

        signal_mock.assert_called_once()
        sighup_handler_mock.assert_called_once()

    def test_run(self):
        with mock.patch.object(self.instance, 'certificate_management') as certificate_management_mock:
            self.instance.run()

        certificate_management_mock.assert_called_once()

    @mock.patch.object(ACMEChiefConfig, 'load')
    @mock.patch.object(ACMEChief, '_set_cert_status')
    @mock.patch.object(ACMEChief, 'create_initial_certs')
    def test_sighup_handler(self, create_initial_certs_mock, set_cert_status_mock, load_mock):
        self.instance.sighup_handler()

        load_mock_calls = [mock.call(confd_path=self.instance.confd_path, file_name=self.instance.config_path)]
        load_mock.assert_has_calls(load_mock_calls)
        load_mock.assert_called_once()
        set_cert_status_mock.assert_called_once()
        create_initial_certs_mock.assert_called_once()

    @mock.patch.object(ACMEChiefConfig, 'load')
    @mock.patch.object(ACMEChief, '_set_cert_status')
    @mock.patch.object(ACMEChief, 'create_initial_certs')
    def test_sighup_handler_status_reporting(self, create_initial_certs_mock, set_cert_status_mock, load_mock):
        self.instance.cert_status['test_certificate']['rsa-2048'] = CertificateState(CertificateStatus.INITIAL)
        self.instance.cert_status['test_certificate']['ec-prime256v1'] = CertificateState(CertificateStatus.INITIAL)
        set_cert_status_mock.return_value = self.instance.cert_status
        with self.assertLogs('acme_chief.acme_chief', level='INFO') as log_trap:
            self.instance.sighup_handler()

        self.assertIn('INFO:acme_chief.acme_chief:Number of certificates per status', log_trap.output[-1])

        set_cert_status_mock.return_value = collections.defaultdict(dict)
        with self.assertLogs('acme_chief.acme_chief', level='INFO') as log_trap:
            self.instance.sighup_handler()

        self.assertIn('INFO:acme_chief.acme_chief:Removed certificates', log_trap.output[-2])

        set_cert_status_mock.return_value = {
            'test_certificate': {
                'rsa-2048': CertificateState(CertificateStatus.INITIAL),
                'ec-prime256v1': CertificateState(CertificateStatus.INITIAL),
            },
            'test_certificate2': {
                'rsa-2048': CertificateState(CertificateStatus.INITIAL),
                'ec-prime256v1': CertificateState(CertificateStatus.INITIAL),
            },
        }
        self.instance.cert_status['test_certificate']['rsa-2048'] = CertificateState(CertificateStatus.INITIAL)
        self.instance.cert_status['test_certificate']['ec-prime256v1'] = CertificateState(CertificateStatus.INITIAL)
        with self.assertLogs('acme_chief.acme_chief', level='INFO') as log_trap:
            self.instance.sighup_handler()

        self.assertIn('INFO:acme_chief.acme_chief:New configured certificates', log_trap.output[-2])

    @mock.patch('acme_chief.acme_chief.SelfSignedCertificate')
    @mock.patch('acme_chief.acme_chief.Certificate')
    def test_create_initial_tests(self, cert_mock, self_signed_cert_mock):
        self.instance.cert_status = {'test_certificate': {
            'ec-prime256v1': CertificateState(CertificateStatus.VALID),
            'rsa-2048': CertificateState(CertificateStatus.INITIAL),
        }}

        ec_key_mock = mock.MagicMock()
        rsa_key_mock = mock.MagicMock()
        ec_key = deepcopy(KEY_TYPES['ec-prime256v1'])
        ec_key['class'] = ec_key_mock
        rsa_key = deepcopy(KEY_TYPES['rsa-2048'])
        rsa_key['class'] = rsa_key_mock

        self_signed_cert_pem = b'-----BEGIN CERTIFICATE-----\nMIIBLjCB1qADAgECAhRxJCFPZ3GhYbLItsUmpIoJSJYR5zAKBggqhkjOPQQDAjAY\nMRYwFAYDVQQDDA1TbmFrZW9pbCBjZXJ0MB4XDTE4MDkwODAwNTQwMVoXDTE4MDkx\nMTAwNTQwMVowGDEWMBQGA1UEAwwNU25ha2VvaWwgY2VydDBZMBMGByqGSM49AgEG\nCCqGSM49AwEHA0IABDqt32diDH9nQxqFRq6v6KKiHqYMHtV17NaRx5MZaYa+W1kV\nfHYsaDgturMPH0mHgwyOIxeDsunNxQ9l9Ky/wPUwCgYIKoZIzj0EAwIDRwAwRAIg\nDKvGUasaWse5Lmv4vK+LuSxOt6bS/R2yqOML+9p1xk8CIHApbLL1bb2M2olXzPOE\ntgBTOv5Voi32fqjBMgXMh/Yd\n-----END CERTIFICATE-----\n'
        type(self_signed_cert_mock.return_value).pem = mock.PropertyMock(return_value=self_signed_cert_pem)
        with mock.patch.dict('acme_chief.acme_chief.KEY_TYPES', {'ec-prime256v1': ec_key, 'rsa-2048': rsa_key}):
            self.instance.create_initial_certs()

        rsa_key_mock.assert_called_once()
        expected_key_calls = [mock.call(),
                              mock.call().generate(**KEY_TYPES['rsa-2048']['params']),
                              mock.call().save(self.instance._get_path('test_certificate',
                                                                        'rsa-2048',
                                                                        public=False,
                                                                        kind='live'))]
        rsa_key_mock.assert_has_calls(expected_key_calls)
        ec_key_mock.assert_not_called()
        self_signed_cert_mock.assert_called_once()
        name, args, kwargs = self_signed_cert_mock.mock_calls[0]
        self.assertFalse(name)
        self.assertFalse(args)
        self.assertEqual(kwargs['common_name'], 'Snakeoil cert')
        self.assertFalse(kwargs['sans'])
        self.assertEqual(kwargs['private_key'], rsa_key_mock.return_value)
        self.assertLess(kwargs['until_date'] - kwargs['from_date'], timedelta(days=7))
        cert_mock.assert_has_calls([mock.call(self_signed_cert_pem)] + [
            mock.call().save(
                self.instance._get_path('test_certificate',
                                        'rsa-2048',
                                        public=True,
                                        kind='live',
                                        cert_type=cert_type
                ),
                mode=cert_type_details['save_mode']
            )
            for cert_type, cert_type_details in CERTIFICATE_TYPES.items()
        ])

    @mock.patch.object(ACMEAccount, 'load')
    @mock.patch('acme_chief.acme_chief.ACMERequests')
    def test_get_acme_session(self, requests_mock, account_load_mock):
        session = self.instance._get_acme_session({
            'CN': 'acmechieftest.beta.wmflabs.org',
            'SNI': ['acmechieftest.beta.wmflabs.org'],
        })

        account_load_mock.assert_called_once_with('1945e767ad72a532ebca519242a801bf',
                                                  base_path=self.instance.accounts_path,
                                                  directory_url='https://127.0.0.1:14000/dir')
        requests_mock.assert_called_once_with(account_load_mock.return_value)
        self.assertEqual(session, requests_mock.return_value)

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch.object(Certificate, 'load')
    @mock.patch.object(ACMEChief, '_get_path')
    def test_push_live_certificate_exceptions(self, get_path_mock, certificate_load_mock, pkey_load_mock):
        for side_effect in [OSError, X509Error]:
            pkey_load_mock.side_effect = side_effect
            status = self.instance._push_live_certificate('test_certificate', 'rsa-2048')
            self.assertEqual(status, CertificateStatus.CERTIFICATE_ISSUED)

        for side_effect in [OSError, X509Error]:
            certificate_load_mock.side_effect = side_effect
            status = self.instance._push_live_certificate('test_certificate', 'rsa-2048')
            self.assertEqual(status, CertificateStatus.CERTIFICATE_ISSUED)


    @mock.patch('subprocess.check_call')
    def test_update_dns_zone(self, check_call_mock):
        challenges = [DNS01ACMEChallenge('_acme-challenge.wmflabs.test', 'fake-challenge1'),
                      DNS01ACMEChallenge('_acme-challenge.wmflabs.test', 'fake-challenge2')]
        ret_value = self.instance._trigger_dns_zone_update(challenges)
        self.assertTrue(ret_value)
        params = ['--remote-servers']
        params += self.instance.config.challenges[ACMEChallengeType.DNS01]['sync_dns_servers']
        params += ['--']

        for challenge in challenges:
            params.append(challenge.validation_domain_name)
            params.append(challenge.validation)

        cmd = self.instance.config.challenges[ACMEChallengeType.DNS01]['zone_update_cmd']
        timeout = self.instance.config.challenges[ACMEChallengeType.DNS01]['zone_update_cmd_timeout']
        check_call_mock.assert_called_once_with([cmd] + params,
                                                stderr=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL,
                                                timeout=timeout)

    @mock.patch('subprocess.check_call')
    def test_update_dns_zone_hyphens(self, check_call_mock):
        """
        This ensures that if challenges begin with hyphen they do not appear in the DNS zone
        update command before the double hyphen, to prevent them being misinterpreted.
        """
        challenges = [DNS01ACMEChallenge('_acme-challenge.wmflabs.test', '-fake-challenge1'),
                      DNS01ACMEChallenge('_acme-challenge.wmflabs.test', '-fake-challenge2')]
        self.instance._trigger_dns_zone_update(challenges)
        args, _ = check_call_mock.call_args
        self.assertEqual(len(args), 1)
        params, = args

        self.assertIn('--', params)
        for check_param in params[:params.index('--')]:
            self.assertNotIn(check_param, ['-fake-challenge1', '-fake-challenge2'])

    @mock.patch('subprocess.check_call')
    def test_update_dns_zone_timeout(self, check_call_mock):
        cmd = self.instance.config.challenges[ACMEChallengeType.DNS01]['zone_update_cmd']
        timeout = self.instance.config.challenges[ACMEChallengeType.DNS01]['zone_update_cmd_timeout']
        check_call_mock.side_effect = subprocess.TimeoutExpired([cmd], timeout)
        ret_value = self.instance._trigger_dns_zone_update([])
        self.assertFalse(ret_value)

    @mock.patch('subprocess.check_call')
    def test_update_dns_zone_error(self, check_call_mock):
        cmd = self.instance.config.challenges[ACMEChallengeType.DNS01]['zone_update_cmd']
        check_call_mock.side_effect = subprocess.CalledProcessError(1, [cmd])
        ret_value = self.instance._trigger_dns_zone_update([])
        self.assertFalse(ret_value)

    def test_certificate_management(self):
        for status in [CertificateStatus.SELF_SIGNED,
                       CertificateStatus.NEEDS_RENEWAL,
                       CertificateStatus.EXPIRED]:
            self.instance.cert_status = {'test_certificate': {
                'ec-prime256v1': CertificateState(status),
                'rsa-2048': CertificateState(status),
            }}
            with mock.patch('acme_chief.acme_chief.sleep', side_effect=InfiniteLoopBreaker) as sleep_mock:
                with mock.patch.object(self.instance, '_new_certificate') as new_certificate_mock:
                    new_certificate_mock.return_value = CertificateStatus.VALID
                    with self.assertRaises(InfiniteLoopBreaker):
                        self.instance.certificate_management()

            sleep_mock.assert_called_once()

            for key_type_id, cert_status in self.instance.cert_status['test_certificate'].items():
                new_certificate_mock.assert_any_call('test_certificate', key_type_id)
                self.assertEqual(cert_status.status, new_certificate_mock.return_value)

            self.instance.cert_status = {'test_certificate': {
                'ec-prime256v1': CertificateState(CertificateStatus.CSR_PUSHED),
                'rsa-2048': CertificateState(CertificateStatus.CSR_PUSHED),
            }}

            with mock.patch('acme_chief.acme_chief.sleep', side_effect=InfiniteLoopBreaker) as sleep_mock:
                with mock.patch.object(self.instance, '_handle_pushed_csr') as handle_pushed_csr_mock:
                    handle_pushed_csr_mock.return_value = CertificateStatus.CHALLENGES_PUSHED
                    with self.assertRaises(InfiniteLoopBreaker):
                        self.instance.certificate_management()

            sleep_mock.assert_called_once()

            for key_type_id, cert_status in self.instance.cert_status['test_certificate'].items():
                handle_pushed_csr_mock.assert_any_call('test_certificate', key_type_id)
                self.assertEqual(cert_status.status, handle_pushed_csr_mock.return_value)

            self.instance.cert_status = {'test_certificate': {
                'ec-prime256v1': CertificateState(CertificateStatus.CHALLENGES_PUSHED),
                'rsa-2048': CertificateState(CertificateStatus.CHALLENGES_PUSHED),
            }}

            with mock.patch('acme_chief.acme_chief.sleep', side_effect=InfiniteLoopBreaker) as sleep_mock:
                with mock.patch.object(self.instance, '_handle_pushed_challenges') as handle_pushed_challenges_mock:
                    handle_pushed_challenges_mock.return_value = CertificateStatus.VALID
                    with self.assertRaises(InfiniteLoopBreaker):
                        self.instance.certificate_management()

            sleep_mock.assert_called_once()

            for key_type_id, cert_status in self.instance.cert_status['test_certificate'].items():
                handle_pushed_challenges_mock.assert_any_call('test_certificate', key_type_id)
                self.assertEqual(cert_status.status, handle_pushed_challenges_mock.return_value)


class ACMEChiefStatusTransitionTests(unittest.TestCase):
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    @mock.patch('os.access', return_value=True)
    def setUp(self, signal_mock, sighup_handler_mock, access_mock):
        self.instance = ACMEChief()

        self.instance.config = ACMEChiefConfig(
            accounts=[{'id': '1945e767ad72a532ebca519242a801bf', 'directory': 'https://127.0.0.1:14000/dir'}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'http-01',
                    'staging_time': timedelta(seconds=3600),
                },
                'test_certificate_dns01':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'dns-01',
                    'staging_time': timedelta(seconds=3600),
                },
            },
            default_account='1945e767ad72a532ebca519242a801bf',
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                    'zone_update_cmd': '/usr/bin/update-zone-dns',
                }
            }
        )

        self.patchers = []

        self.ec_key_mock = mock.MagicMock()
        self.rsa_key_mock = mock.MagicMock()
        ec_key = deepcopy(KEY_TYPES['ec-prime256v1'])
        ec_key['class'] = self.ec_key_mock
        rsa_key = deepcopy(KEY_TYPES['rsa-2048'])
        rsa_key['class'] = self.rsa_key_mock

        self.patchers.append(mock.patch.dict('acme_chief.acme_chief.KEY_TYPES',
                                             {'ec-prime256v1': ec_key, 'rsa-2048': rsa_key}))
        self.patchers[-1].start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()

    def _set_certificate_status(self, status):
        for cert_id in self.instance.cert_status:
            for key_type_id in KEY_TYPES:
                self.instance.cert_status[cert_id][key_type_id] = status

    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    @mock.patch.object(ACMEChief, '_handle_pushed_csr')
    def test_new_certificate(self, handle_pushed_csr_mock, get_acme_session_mock, csr_mock):
        handle_pushed_csr_mock.return_value = CertificateStatus.VALID
        http_challenge_mock = mock.MagicMock()
        http_challenge_mock.file_name = 'mocked_challenged_file_name'
        get_acme_session_mock.return_value.push_csr.return_value = {
            ACMEChallengeType.HTTP01: [http_challenge_mock],
        }
        status = self.instance._new_certificate('test_certificate', 'ec-prime256v1')

        csr_mock.assert_called_once_with(common_name='acmechieftest.beta.wmflabs.org',
                                         private_key=self.ec_key_mock.return_value,
                                         sans=['acmechieftest.beta.wmflabs.org'])
        self.ec_key_mock.assert_called_once()
        expected_key_calls = [mock.call(),
                              mock.call().generate(**KEY_TYPES['ec-prime256v1']['params']),
                              mock.call().save(self.instance._get_path('test_certificate',
                                                                       'ec-prime256v1',
                                                                       public=False,
                                                                       kind='new'))]
        self.ec_key_mock.assert_has_calls(expected_key_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().push_csr(csr_mock.return_value)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        http_challenge_mock_call = [mock.call.save(os.path.join(self.instance.challenges_path[ACMEChallengeType.HTTP01],
                                                                http_challenge_mock.file_name))]
        http_challenge_mock.assert_has_calls(http_challenge_mock_call)
        self.assertEqual(status, CertificateStatus.VALID)

    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_trigger_dns_zone_update')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    @mock.patch.object(ACMEChief, '_handle_pushed_csr')
    def test_new_certificate_dns01(self, handle_pushed_csr_mock, get_acme_session_mock, dns_zone_update_mock, csr_mock):
        handle_pushed_csr_mock.return_value = CertificateStatus.VALID
        dns_challenge_mock = mock.MagicMock()
        dns_challenge_mock.file_name = 'mocked_challenged_file_name'
        get_acme_session_mock.return_value.push_csr.return_value = {
            ACMEChallengeType.DNS01: [dns_challenge_mock],
        }
        status = self.instance._new_certificate('test_certificate_dns01', 'ec-prime256v1')

        csr_mock.assert_called_once_with(common_name='acmechieftest.beta.wmflabs.org',
                                         private_key=self.ec_key_mock.return_value,
                                         sans=['acmechieftest.beta.wmflabs.org'])
        self.ec_key_mock.assert_called_once()
        expected_key_calls = [mock.call(),
                              mock.call().generate(**KEY_TYPES['ec-prime256v1']['params']),
                              mock.call().save(self.instance._get_path('test_certificate_dns01',
                                                                       'ec-prime256v1',
                                                                       public=False,
                                                                       kind='new'))]
        self.ec_key_mock.assert_has_calls(expected_key_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate_dns01']),
                              mock.call().push_csr(csr_mock.return_value)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        dns_zone_update_mock.assert_called_once()
        dns_challenge_mock_call = [mock.call.save(os.path.join(self.instance.challenges_path[ACMEChallengeType.DNS01],
                                                               dns_challenge_mock.file_name))]
        dns_challenge_mock.assert_has_calls(dns_challenge_mock_call)
        self.assertEqual(status, CertificateStatus.VALID)

    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    def test_new_certificate_new_order_ready(self, get_acme_session_mock, csr_mock):
        dns_challenge_mock = mock.MagicMock()
        dns_challenge_mock.file_name = 'mocked_challenged_file_name'
        get_acme_session_mock.return_value.push_csr.return_value = {}
        status = self.instance._new_certificate('test_certificate_dns01', 'ec-prime256v1')

        csr_mock.assert_called_once_with(common_name='acmechieftest.beta.wmflabs.org',
                                         private_key=self.ec_key_mock.return_value,
                                         sans=['acmechieftest.beta.wmflabs.org'])
        self.ec_key_mock.assert_called_once()
        expected_key_calls = [mock.call(),
                              mock.call().generate(**KEY_TYPES['ec-prime256v1']['params']),
                              mock.call().save(self.instance._get_path('test_certificate_dns01',
                                                                       'ec-prime256v1',
                                                                       public=False,
                                                                       kind='new'))]
        self.ec_key_mock.assert_has_calls(expected_key_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate_dns01']),
                              mock.call().push_csr(csr_mock.return_value)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        dns_challenge_mock.assert_not_called()
        self.assertEqual(status, CertificateStatus.CHALLENGES_PUSHED)

    @mock.patch.object(PrivateKeyLoader, 'load')
    def test_handle_pushed_csr_pkey_error(self, pkey_loader_mock):
        for side_effect in [OSError, X509Error]:
            pkey_loader_mock.reset_mock()
            pkey_loader_mock.side_effect = side_effect
            status = self.instance._handle_pushed_csr('test_certificate', 'rsa-2048')
            self.assertEqual(status, CertificateStatus.ACMECHIEF_ERROR)

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    @mock.patch.object(ACMEChief, '_handle_validated_challenges')
    def test_handle_pushed_csr(self, handle_validated_challenges_mock,
                               get_acme_session_mock, csr_mock, pkey_loader_mock):
        handle_validated_challenges_mock.return_value = CertificateStatus.VALID
        challenge_mock = mock.MagicMock()
        challenge_mock.validate.return_value = ACMEChallengeValidation.VALID
        mocked_acme_session = mock.MagicMock()
        mocked_acme_session.challenges = {
            csr_mock.generate_csr_id.return_value: {
                ACMEChallengeType.HTTP01: [challenge_mock],
            }
        }
        get_acme_session_mock.return_value = mocked_acme_session
        status = self.instance._handle_pushed_csr('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.VALID)

        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                               public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)

        csr_expected_calls = [mock.call.generate_csr_id(common_name='acmechieftest.beta.wmflabs.org',
                                                        public_key_pem=pkey_loader_mock.return_value.public_pem,
                                                        sans=['acmechieftest.beta.wmflabs.org'])]
        csr_mock.assert_has_calls(csr_expected_calls)

        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate'])]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        challenge_mock.assert_has_calls([mock.call.validate()])
        handle_validated_challenges_mock.assert_called_once_with('test_certificate', 'rsa-2048')

    @mock.patch.object(PrivateKeyLoader, 'load')
    def test_handle_validated_challenges_pkey_error(self, pkey_loader_mock):
        for side_effect in [OSError, X509Error]:
            pkey_loader_mock.reset_mock()
            pkey_loader_mock.side_effect = side_effect
            status = self.instance._handle_validated_challenges('test_certificate', 'rsa-2048')
            self.assertEqual(status, CertificateStatus.ACMECHIEF_ERROR)

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    @mock.patch.object(ACMEChief, '_handle_pushed_challenges')
    def test_handle_validated_challenges(self, handle_pushed_challenges_mock, get_acme_session_mock,
                                         csr_mock, pkey_loader_mock):
        handle_pushed_challenges_mock.return_value = CertificateStatus.VALID
        status = self.instance._handle_validated_challenges('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.VALID)
        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate',
                                                               'rsa-2048', public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)
        csr_expected_calls = [mock.call.generate_csr_id(common_name='acmechieftest.beta.wmflabs.org',
                                                        public_key_pem=pkey_loader_mock.return_value.public_pem,
                                                        sans=['acmechieftest.beta.wmflabs.org'])]
        csr_mock.assert_has_calls(csr_expected_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().push_solved_challenges(csr_mock.generate_csr_id.return_value,
                                                                 challenge_type=ACMEChallengeType.HTTP01)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        handle_pushed_challenges_mock.assert_called_once_with('test_certificate', 'rsa-2048')


    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    @mock.patch.object(ACMEChief, '_handle_pushed_challenges')
    def test_handle_validated_challenges_solved_acme_error(self, handle_pushed_challenges_mock, get_acme_session_mock,
                                                           csr_mock, pkey_loader_mock):
        handle_pushed_challenges_mock.side_effect = ACMEError
        status = self.instance._handle_validated_challenges('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.CHALLENGES_PUSHED)
        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                               public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)
        csr_expected_calls = [mock.call.generate_csr_id(common_name='acmechieftest.beta.wmflabs.org',
                                                        public_key_pem=pkey_loader_mock.return_value.public_pem,
                                                        sans=['acmechieftest.beta.wmflabs.org'])]
        csr_mock.assert_has_calls(csr_expected_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().push_solved_challenges(csr_mock.generate_csr_id.return_value, challenge_type=ACMEChallengeType.HTTP01)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        handle_pushed_challenges_mock.assert_called_once_with('test_certificate', 'rsa-2048')

    @mock.patch.object(PrivateKeyLoader, 'load')
    def test_handle_pushed_challenges_pkey_error(self, pkey_loader_mock):
        for side_effect in [OSError, X509Error]:
            pkey_loader_mock.reset_mock()
            pkey_loader_mock.side_effect = side_effect
            status = self.instance._handle_pushed_challenges('test_certificate', 'rsa-2048')
            self.assertEqual(status, CertificateStatus.ACMECHIEF_ERROR)

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    def test_handle_pushed_challenges_not_validated_yet(self, get_acme_session_mock, csr_mock, pkey_loader_mock):
        get_acme_session_mock.return_value.finalize_order.side_effect = ACMEChallengeNotValidatedError
        status = self.instance._handle_pushed_challenges('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.CHALLENGES_PUSHED)
        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                               public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().finalize_order(csr_mock.generate_csr_id.return_value)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    def test_handle_pushed_challenges_validation_failed(self, get_acme_session_mock, csr_mock, pkey_loader_mock):
        get_acme_session_mock.return_value.finalize_order.side_effect = ACMEInvalidChallengeError
        status = self.instance._handle_pushed_challenges('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.CHALLENGES_REJECTED)
        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                               public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().finalize_order(csr_mock.generate_csr_id.return_value)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    @mock.patch.object(ACMEChief, '_handle_order_finalized')
    def test_handle_pushed_challenges(self, order_finalized_mock, get_acme_session_mock, csr_mock, pkey_loader_mock):
        order_finalized_mock.return_value = CertificateStatus.VALID
        status = self.instance._handle_pushed_challenges('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.VALID)
        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                               public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().finalize_order(csr_mock.generate_csr_id.return_value)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        order_finalized_mock.assert_called_once_with('test_certificate', 'rsa-2048')

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    def test_handle_order_finalized_error_parsing_cert(self, get_acme_session_mock, csr_mock, pkey_loader_mock):
        get_acme_session_mock.return_value.get_certificate.side_effect = ACMEIssuedCertificateError
        status = self.instance._handle_order_finalized('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.CERTIFICATE_ISSUED)
        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                               public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().get_certificate(csr_mock.generate_csr_id.return_value)]
        get_acme_session_mock.assert_has_calls(acme_session_calls)

    @mock.patch.object(PrivateKeyLoader, 'load')
    @mock.patch('acme_chief.acme_chief.CertificateSigningRequest')
    @mock.patch.object(ACMEChief, '_get_acme_session')
    @mock.patch.object(ACMEChief, '_handle_ready_to_be_pushed')
    def test_handle_order_finalized(self, handle_ready_mock, get_acme_session_mock, csr_mock, pkey_loader_mock):
        handle_ready_mock.return_value = CertificateStatus.VALID
        status = self.instance._handle_order_finalized('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.VALID)
        pkey_loader_calls = [mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                               public=False, kind='new'))]
        pkey_loader_mock.assert_has_calls(pkey_loader_calls)
        get_acme_session_mock.assert_called_once()
        acme_session_calls = [mock.call(self.instance.config.certificates['test_certificate']),
                              mock.call().get_certificate(csr_mock.generate_csr_id.return_value),
                              mock.call().get_certificate().save(self.instance._get_path('test_certificate',
                                                                                         'rsa-2048', public=True,
                                                                                         kind='new',
                                                                                         cert_type='full_chain'),
                                                                 mode=CertificateSaveMode.FULL_CHAIN)
        ]
        get_acme_session_mock.assert_has_calls(acme_session_calls)
        handle_ready_mock.assert_called_once_with('test_certificate', 'rsa-2048')

    @mock.patch.object(Certificate, 'load')
    @mock.patch.object(ACMEChief, '_get_path')
    @mock.patch.object(ACMEChief, '_push_live_certificate')
    def test_handle_ready_to_be_pushed(self, push_live_mock, get_path_mock, load_mock):
        push_live_mock.return_value = CertificateStatus.VALID
        load_mock.return_value.certificate.not_valid_before = datetime.fromtimestamp(0)
        status = self.instance._handle_ready_to_be_pushed('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.VALID)
        push_live_mock.assert_called_once_with('test_certificate', 'rsa-2048')

    @mock.patch.object(Certificate, 'load')
    @mock.patch.object(ACMEChief, '_get_path')
    @mock.patch.object(ACMEChief, '_push_live_certificate')
    def test_handle_ready_to_be_pushed_not_ready(self, push_live_mock, get_path_mock, load_mock):
        load_mock.return_value.certificate.not_valid_before = datetime.utcnow()
        status = self.instance._handle_ready_to_be_pushed('test_certificate', 'rsa-2048')
        self.assertEqual(status, CertificateStatus.READY_TO_BE_PUSHED)
        push_live_mock.assert_not_called()


class ACMEChiefDetermineStatusTest(unittest.TestCase):
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    def setUp(self, signal_mock, sighup_handler_mock):
        self.instance = ACMEChief()

        self.instance.config = ACMEChiefConfig(
            accounts=[{'id': '1945e767ad72a532ebca519242a801bf', 'directory': 'https://127.0.0.1:14000/dir'}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.alpha.wmflabs.org', 'acmechieftest.beta.wmflabs.org'],
                    'challenge': 'http-01',
                    'staging_time': timedelta(seconds=3600),
                },
            },
            default_account='1945e767ad72a532ebca519242a801bf',
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                }
            }
        )

    def _configure_load_return_value(self, status):
        return_value = mock.MagicMock()
        return_value.self_signed = False

        return_value.certificate.not_valid_before = datetime(1970, 1, 1)

        if status is CertificateStatus.SELF_SIGNED:
            return_value.self_signed = True
        elif status is CertificateStatus.EXPIRED:
            return_value.certificate.not_valid_after = datetime(1970, 1, 1)
        elif status is CertificateStatus.NEEDS_RENEWAL:
            return_value.certificate.not_valid_after = datetime.utcnow() + timedelta(days=10)
            return_value.needs_renew.return_value = True
        elif status is CertificateStatus.READY_TO_BE_PUSHED:
            return_value.certificate.not_valid_before = datetime(2019, 1, 1)
        elif status is CertificateStatus.VALID:
            return_value.certificate.not_valid_after = datetime.utcnow() + timedelta(days=10)
            return_value.needs_renew.return_value = False

        return_value.common_name = 'acmechieftest.beta.wmflabs.org'
        return_value.subject_alternative_names = ['acmechieftest.alpha.wmflabs.org',
                                                  'acmechieftest.beta.wmflabs.org']

        return return_value

    @mock.patch.object(Certificate, 'load')
    def test_initial_status(self, load_mock):
        for test_status in [CertificateStatus.SELF_SIGNED,
                            CertificateStatus.EXPIRED,
                            CertificateStatus.NEEDS_RENEWAL,
                            CertificateStatus.VALID]:
            load_mock.reset_mock()
            load_mock.return_value = self._configure_load_return_value(test_status)

            status = self.instance._set_cert_status()
            load_calls = [mock.call(self.instance._get_path('test_certificate', 'ec-prime256v1',
                                                            public=True, kind='live')),
                          mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                            public=True, kind='live')),
                         ]
            load_mock.assert_has_calls(load_calls, any_order=True)
            self.assertEqual(len(status), len(self.instance.config.certificates))
            for certificate in self.instance.config.certificates:
                self.assertEqual(len(status[certificate]), len(KEY_TYPES))
                for cert_status in status[certificate].values():
                    self.assertEqual(cert_status.status, test_status)

    @mock.patch.object(Certificate, 'load')
    def test_initial_status_ready_to_be_pushed(self, load_mock):
        load_mock.side_effect = [self._configure_load_return_value(CertificateStatus.VALID),
                                 self._configure_load_return_value(CertificateStatus.READY_TO_BE_PUSHED),
                                 self._configure_load_return_value(CertificateStatus.VALID),
                                 self._configure_load_return_value(CertificateStatus.READY_TO_BE_PUSHED)]
        status = self.instance._set_cert_status()
        print(load_mock.mock_calls)
        load_calls = [mock.call(self.instance._get_path('test_certificate', 'ec-prime256v1',
                                                        public=True, kind='live')),
                      mock.call(self.instance._get_path('test_certificate', 'ec-prime256v1',
                                                        public=True, kind='new', cert_type='full_chain')),
                      mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                        public=True, kind='live')),
                      mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                        public=True, kind='new', cert_type='full_chain')),
                     ]
        load_mock.assert_has_calls(load_calls, any_order=True)
        self.assertEqual(len(status), len(self.instance.config.certificates))
        for certificate in self.instance.config.certificates:
            self.assertEqual(len(status[certificate]), len(KEY_TYPES))
            for cert_status in status[certificate].values():
                self.assertEqual(cert_status.status, CertificateStatus.READY_TO_BE_PUSHED)


    @mock.patch.object(Certificate, 'load')
    def test_trigger_subjects_changed_status(self, load_mock):

        def _generate_return_values():
            return_value = mock.MagicMock()
            return_value.self_signed = False
            return_value.needs_renew.return_value = False
            return_value.certificate.not_valid_before = datetime.utcnow()
            return_value.certificate.not_valid_after = datetime.utcnow() + timedelta(days=10)
            return_value.common_name = 'acmechieftest.beta.wmflabs.org'

            for san in ([], ['acmechieftest.beta.wmflabs.org', 'acmechieftest.gamma.wmflabs.org'],
                        ['acmechieftest.alpha.wmflabs.org']):
                return_value.subject_alternative_names = san
                yield return_value

            return_value.common_name = 'acmechieftest.alpha.wmflabs.org'
            return_value.subject_alternative_names = ['acmechieftest.beta.wmflabs.org']
            yield return_value


        for load_mock_instance in _generate_return_values():
            load_mock.reset_mock()
            load_mock.return_value = load_mock_instance

            status = self.instance._set_cert_status()
            load_calls = [mock.call(self.instance._get_path('test_certificate', 'ec-prime256v1',
                                                    public=True, kind='live')),
                        mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                    public=True, kind='live')),
                        ]
            load_mock.assert_has_calls(load_calls, any_order=True)
            self.assertEqual(len(status), len(self.instance.config.certificates))
            for certificate in self.instance.config.certificates:
                self.assertEqual(len(status[certificate]), len(KEY_TYPES))
                for cert_status in status[certificate].values():
                    self.assertEqual(cert_status.status, CertificateStatus.SUBJECTS_CHANGED)

    @mock.patch.object(Certificate, 'load')
    def test_shouldnt_trigger_subjects_changed_status(self, load_mock):

        def _generate_return_values():
            return_value = mock.MagicMock()
            return_value.self_signed = False
            return_value.needs_renew.return_value = False
            return_value.certificate.not_valid_before = datetime.utcnow()
            return_value.certificate.not_valid_after = datetime.utcnow() + timedelta(days=10)
            return_value.common_name = 'acmechieftest.BETA.wmflabs.org'
            return_value.subject_alternative_names = ['acmechieftest.alpha.wmflabs.org',
                                                      'acmechieftest.beta.wmflabs.org']
            yield return_value

            return_value.common_name = 'acmechieftest.beta.wmflabs.org'
            for san in (['acmechieftest.alpha.wmflabs.org',  'acmechieftest.BETA.wmflabs.org'],
                        ['acmechieftest.beta.wmflabs.org', 'acmechieftest.alpha.wmflabs.org'],
                        ['acmechieftest.beta.wmflabs.org', 'acmechieftest.alpha.wmflabs.org',
                         'acmechieftest.alpha.wmflabs.org']):
                return_value.subject_alternative_names = san
                yield return_value

        for load_mock_instance in _generate_return_values():
            load_mock.reset_mock()
            load_mock.return_value = load_mock_instance

            status = self.instance._set_cert_status()
            load_calls = [mock.call(self.instance._get_path('test_certificate', 'ec-prime256v1',
                                                    public=True, kind='live')),
                        mock.call(self.instance._get_path('test_certificate', 'rsa-2048',
                                                    public=True, kind='live')),
                        ]
            load_mock.assert_has_calls(load_calls, any_order=True)
            self.assertEqual(len(status), len(self.instance.config.certificates))
            for certificate in self.instance.config.certificates:
                self.assertEqual(len(status[certificate]), len(KEY_TYPES))
                for cert_status in status[certificate].values():
                    self.assertEqual(cert_status.status, CertificateStatus.VALID)


class ACMEChiefIntegrationTest(BasePebbleIntegrationTest):
    @classmethod
    def setUpClass(cls, **kwargs):
        super().setUpClass(valid_challenges=True)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        self.certificates_dir = tempfile.TemporaryDirectory()
        config_path = self.config_dir.name
        certificates_path = self.certificates_dir.name
        self.acme_account_base_path = os.path.join(config_path, ACMEChief.accounts_path)
        for path in (ACMEChief.accounts_path,
                     ACMEChief.confd_path):
            os.makedirs(os.path.join(config_path, path))

        for path in (ACMEChief.new_certs_path,
                     ACMEChief.live_certs_path,
                     ACMEChief.csrs_path,
                     ACMEChief.dns_challenges_path,
                     ACMEChief.http_challenges_path):
            os.makedirs(os.path.join(certificates_path, path))

        HTTP01ChallengeHandler.challenges_path = os.path.join(certificates_path, ACMEChief.http_challenges_path)
        BaseDNSRequestHandler.challenges_path = os.path.join(certificates_path, ACMEChief.dns_challenges_path)
        proxy_host, proxy_port = self.proxy_server.server_address
        proxy_url = 'http://{}:{}'.format(proxy_host, proxy_port)
        dns_host, dns_port = self.dns_server.server_address
        self.patchers = [
            mock.patch.dict('acme_chief.acme_requests.HTTP_VALIDATOR_PROXIES', {'http': proxy_url}),
            mock.patch('acme_chief.acme_requests.DNS_SERVERS', [dns_host]),
            mock.patch('acme_chief.acme_requests.DNS_PORT', dns_port),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        self.config_dir.cleanup()
        self.certificates_dir.cleanup()
        for patcher in self.patchers:
            patcher.stop()


    @mock.patch('acme_chief.acme_requests.TLS_VERIFY', False)
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    def test_issue_new_certificate_http01(self, a, b):
        # Step 1 - create an ACME account
        account = ACMEAccount.create('tests-acmechief@wikimedia.org',
                                     base_path=self.acme_account_base_path,
                                     directory_url=DIRECTORY_URL)
        account.save()
        # Step 2 - Generate ACMEChief config
        acme_chief = ACMEChief(config_path=self.config_dir.name, certificates_path=self.certificates_dir.name)
        acme_chief.config = ACMEChiefConfig(
            accounts=[{'id': account.account_id, 'directory': DIRECTORY_URL}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'http-01',
                    'staging_time': timedelta(seconds=0),
                },
            },
            default_account=account.account_id,
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                }
            }
        )
        acme_chief.cert_status = {'test_certificate': {
            'ec-prime256v1': CertificateState(CertificateStatus.INITIAL),
            'rsa-2048': CertificateState(CertificateStatus.INITIAL),
        }}

        # Step 3 - Generate self signed certificates
        acme_chief.create_initial_certs()
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                self.assertEqual(acme_chief.cert_status[cert_id][key_type_id].status, CertificateStatus.SELF_SIGNED)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertTrue(cert.self_signed)

        # Step 4 - Request new certificates
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                status = acme_chief._new_certificate(cert_id, key_type_id)
                self.assertEqual(status, CertificateStatus.VALID)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertFalse(cert.self_signed)

    @mock.patch('acme_chief.acme_requests.TLS_VERIFY', False)
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    def test_issue_new_certificate_dns01(self, a, b):
        # Step 1 - create an ACME account
        account = ACMEAccount.create('tests-acmechief@wikimedia.org',
                                     base_path=self.acme_account_base_path,
                                     directory_url=DIRECTORY_URL)
        account.save()
        # Step 2 - Generate ACMEChief config
        acme_chief = ACMEChief(config_path=self.config_dir.name, certificates_path=self.certificates_dir.name)
        acme_chief.config = ACMEChiefConfig(
            accounts=[{'id': account.account_id, 'directory': DIRECTORY_URL}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'dns-01',
                    'staging_time': timedelta(seconds=0),
                },
            },
            default_account=account.account_id,
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['localhost', '127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                }
            }
        )
        acme_chief.cert_status = {'test_certificate': {
            'ec-prime256v1': CertificateState(CertificateStatus.INITIAL),
            'rsa-2048': CertificateState(CertificateStatus.INITIAL),
        }}

        # Step 3 - Generate self signed certificates
        acme_chief.create_initial_certs()
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                self.assertEqual(acme_chief.cert_status[cert_id][key_type_id].status, CertificateStatus.SELF_SIGNED)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertTrue(cert.self_signed)

        # Step 4 - Request new certificates
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                status = acme_chief._new_certificate(cert_id, key_type_id)
                self.assertEqual(status, CertificateStatus.VALID)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertFalse(cert.self_signed)

    @mock.patch('acme_chief.acme_requests.TLS_VERIFY', False)
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    def test_issue_new_certificate_force_validation_failure_acme_chief_side(self, a, b):
        # Step 1 - create an ACME account
        account = ACMEAccount.create('tests-acmechief@wikimedia.org',
                                     base_path=self.acme_account_base_path,
                                     directory_url=DIRECTORY_URL)
        account.save()
        # Step 2 - Generate ACMEChief config
        acme_chief = ACMEChief(config_path=self.config_dir.name, certificates_path=self.certificates_dir.name)
        acme_chief.config = ACMEChiefConfig(
            accounts=[{'id': account.account_id, 'directory': DIRECTORY_URL}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'http-01',
                    'staging_time': timedelta(seconds=0),
                },
            },
            default_account=account.account_id,
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                }
            }
        )
        acme_chief.cert_status = {'test_certificate': {
            'ec-prime256v1': CertificateState(CertificateStatus.INITIAL),
            'rsa-2048': CertificateState(CertificateStatus.INITIAL),
        }}

        # Step 3 - Generate self signed certificates
        acme_chief.create_initial_certs()
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                self.assertEqual(acme_chief.cert_status[cert_id][key_type_id].status, CertificateStatus.SELF_SIGNED)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertTrue(cert.self_signed)

        # Step 4 - Request new certificates setting a wrong challenge location
        # to force a challenge validation issue
        with tempfile.TemporaryDirectory() as fake_challenge_dir:
            valid_challenge_path = acme_chief.challenges_path[ACMEChallengeType.HTTP01]
            acme_chief.challenges_path[ACMEChallengeType.HTTP01] = fake_challenge_dir
            for cert_id in acme_chief.cert_status:
                for key_type_id in KEY_TYPES:
                    status = acme_chief._new_certificate(cert_id, key_type_id)
                    self.assertEqual(status, CertificateStatus.CSR_PUSHED)

            acme_chief.challenges_path[ACMEChallengeType.HTTP01] = valid_challenge_path
            # Copy the challenges to the correct challenge path
            for challenge_file in os.listdir(fake_challenge_dir):
                challenge_file_path = os.path.join(fake_challenge_dir, challenge_file)
                shutil.copy2(challenge_file_path, valid_challenge_path)

        # Step 5 - Resume the process
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                status = acme_chief._handle_pushed_csr(cert_id, key_type_id)
                self.assertEqual(status, CertificateStatus.VALID)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertFalse(cert.self_signed)

    @mock.patch('acme_chief.acme_requests.TLS_VERIFY', False)
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    def test_issue_new_certificate_force_validation_failure_acme_directory_side(self, a, b):
        # Step 1 - create an ACME account
        account = ACMEAccount.create('tests-acmechief@wikimedia.org',
                                     base_path=self.acme_account_base_path,
                                     directory_url=DIRECTORY_URL)
        account.save()
        # Step 2 - Generate ACMEChief config
        acme_chief = ACMEChief(config_path=self.config_dir.name, certificates_path=self.certificates_dir.name)
        acme_chief.config = ACMEChiefConfig(
            accounts=[{'id': account.account_id, 'directory': DIRECTORY_URL}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'http-01',
                },
            },
            default_account=account.account_id,
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                }
            }
        )
        acme_chief.cert_status = {'test_certificate': {
            'ec-prime256v1': CertificateState(CertificateStatus.INITIAL),
            'rsa-2048': CertificateState(CertificateStatus.INITIAL),
        }}

        # Step 3 - Generate self signed certificates
        acme_chief.create_initial_certs()
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                self.assertEqual(acme_chief.cert_status[cert_id][key_type_id].status, CertificateStatus.SELF_SIGNED)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertTrue(cert.self_signed)

        # Step 4 - Request new certificates mocking ACMEChief._handle_validated_challenges() method
        # to stop the process after the challenges have been validated on ACMEChief side
        with mock.patch.object(acme_chief, '_handle_validated_challenges',
                               return_value=CertificateStatus.CHALLENGES_VALIDATED) as mock_validated:
            for cert_id in acme_chief.cert_status:
                for key_type_id in KEY_TYPES:
                    mock_validated.reset_mock()
                    status = acme_chief._new_certificate(cert_id, key_type_id)
                    mock_validated.assert_called_once_with(cert_id, key_type_id)
                    self.assertEqual(status, CertificateStatus.CHALLENGES_VALIDATED)

        # Step 5 - Wipe the challenges to force a validation error on ACME directory side
        for root, _, files in os.walk(acme_chief.challenges_path[ACMEChallengeType.HTTP01]):
            for name in files:
                os.unlink(os.path.join(root, name))

        # Step 5 - Resume the process
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                status = acme_chief._handle_validated_challenges(cert_id, key_type_id)
                self.assertEqual(status, CertificateStatus.CHALLENGES_REJECTED)

    @mock.patch('acme_chief.acme_requests.TLS_VERIFY', False)
    @mock.patch('signal.signal')
    @mock.patch.object(ACMEChief, 'sighup_handler')
    @mock.patch('acme_chief.acme_chief.sleep', side_effect=InfiniteLoopBreaker)
    def test_certificate_management_with_config_change(self, a, b, c):
        # Step 1 - create an ACME account
        account = ACMEAccount.create('tests-acmechief@wikimedia.org',
                                     base_path=self.acme_account_base_path,
                                     directory_url=DIRECTORY_URL)
        account.save()
        # Step 2 - Generate ACMEChief config
        acme_chief = ACMEChief(config_path=self.config_dir.name, certificates_path=self.certificates_dir.name)
        acme_chief.config = ACMEChiefConfig(
            accounts=[{'id': account.account_id, 'directory': DIRECTORY_URL}],
            certificates={
                'test_certificate':
                {
                    'CN': 'acmechieftest.beta.wmflabs.org',
                    'SNI': ['acmechieftest.beta.wmflabs.org'],
                    'challenge': 'http-01',
                    'staging_time': timedelta(0),
                },
            },
            default_account=account.account_id,
            authorized_hosts={
                'test_certificate': ['localhost']
            },
            authorized_regexes={},
            challenges={
                'dns-01': {
                    'validation_dns_servers': ['127.0.0.1'],
                    'sync_dns_servers': ['127.0.0.1'],
                }
            }
        )
        acme_chief.cert_status = {'test_certificate': {
            'ec-prime256v1': CertificateState(CertificateStatus.INITIAL),
            'rsa-2048': CertificateState(CertificateStatus.INITIAL),
        }}

        # Step 3 - Generate self signed certificates
        acme_chief.create_initial_certs()

        # Step 4 - run one iteration of certificate management
        with self.assertRaises(InfiniteLoopBreaker):
            acme_chief.certificate_management()
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                self.assertEqual(acme_chief.cert_status[cert_id][key_type_id].status, CertificateStatus.VALID)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertEqual(cert.subject_alternative_names, ['acmechieftest.beta.wmflabs.org'])
                self.assertFalse(cert.self_signed)

        # Step 5 - Add a new SNI
        acme_chief.config.certificates['test_certificate']['SNI'].append('acmechieftest.alpha.wmflabs.org')

        # Step 6 - Trigger certificate status evaluation
        acme_chief.cert_status = acme_chief._set_cert_status()
        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                self.assertEqual(acme_chief.cert_status[cert_id][key_type_id].status,
                                 CertificateStatus.SUBJECTS_CHANGED)

        # Step 7 - Run another iteration of certificate management
        with self.assertRaises(InfiniteLoopBreaker):
            acme_chief.certificate_management()

        for cert_id in acme_chief.cert_status:
            for key_type_id in KEY_TYPES:
                self.assertEqual(acme_chief.cert_status[cert_id][key_type_id].status, CertificateStatus.VALID)
                cert = Certificate.load(acme_chief._get_path(cert_id, key_type_id, public=True, kind='live'))
                self.assertEqual(cert.subject_alternative_names, ['acmechieftest.beta.wmflabs.org',
                                                                  'acmechieftest.alpha.wmflabs.org'])
                self.assertFalse(cert.self_signed)
