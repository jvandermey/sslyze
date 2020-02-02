import socket
import types
from concurrent.futures._base import Future
from dataclasses import dataclass
from typing import List, Optional

from nassl._nassl import WantReadError
from nassl.ssl_client import OpenSslVersionEnum

from sslyze.plugins.plugin_base import ScanCommandResult, ScanCommandImplementation, ScanCommandExtraArguments, ScanJob
from tls_parser.alert_protocol import TlsAlertRecord
from tls_parser.application_data_protocol import TlsApplicationDataRecord
from tls_parser.change_cipher_spec_protocol import TlsChangeCipherSpecRecord
from tls_parser.exceptions import NotEnoughData
from tls_parser.handshake_protocol import TlsHandshakeRecord, TlsHandshakeTypeByte
from tls_parser.parser import TlsRecordParser
from tls_parser.tls_version import TlsVersionEnum

from sslyze.server_connectivity_tester import ServerConnectivityInfo


@dataclass(frozen=True)
class OpenSslCcsInjectionScanResult(ScanCommandResult):
    """The result of testing a server for the OpenSSL CCS Injection vulnerability (CVE-2014-0224).

    Attributes:
        is_vulnerable_to_ccs_injection: True if the server is vulnerable to the OpenSSL CCS Injection vulnerability.
    """

    is_vulnerable_to_ccs_injection: bool


class OpenSslCcsInjectionImplementation(ScanCommandImplementation):
    @classmethod
    def scan_jobs_for_scan_command(
        cls, server_info: ServerConnectivityInfo, extra_arguments: Optional[ScanCommandExtraArguments] = None
    ) -> List[ScanJob]:
        if extra_arguments:
            raise ValueError("This plugin does not take extra arguments")

        return [ScanJob(function_to_call=_test_for_ccs_injection, function_arguments=[server_info])]

    @classmethod
    def result_for_completed_scan_jobs(
        cls, server_info: ServerConnectivityInfo, completed_scan_jobs: List[Future]
    ) -> ScanCommandResult:
        if len(completed_scan_jobs) != 1:
            raise RuntimeError(f"Unexpected number of scan jobs received: {completed_scan_jobs}")

        return OpenSslCcsInjectionScanResult(is_vulnerable_to_ccs_injection=completed_scan_jobs[0].result())


def _test_for_ccs_injection(server_info: ServerConnectivityInfo) -> bool:
    if server_info.tls_probing_result.highest_tls_version_supported >= OpenSslVersionEnum.TLSV1_3:
        # The server uses a recent version of OpenSSL and it cannot be vulnerable to CCS Injection
        return False

    ssl_connection = server_info.get_preconfigured_tls_connection()
    # Replace nassl.sslClient.do_handshake() with a CCS checking SSL handshake so that all the SSLyze options
    # (startTLS, proxy, etc.) still work
    ssl_connection.ssl_client.do_handshake = types.MethodType(
        _do_handshake_with_ccs_injection, ssl_connection.ssl_client
    )

    is_vulnerable = False
    try:
        # Start the SSL handshake
        ssl_connection.connect()
    except _VulnerableToCcsInjection:
        # The test was completed and the server is vulnerable
        is_vulnerable = True
    except _NotVulnerableToCcsInjection:
        # The test was completed and the server is NOT vulnerable
        pass
    finally:
        ssl_connection.close()

    return is_vulnerable


class _VulnerableToCcsInjection(Exception):
    """Exception to raise during the handshake to hijack the flow and test for CCS.
    """


class _NotVulnerableToCcsInjection(Exception):
    """Exception to raise during the handshake to hijack the flow and test for CCS.
    """


def _do_handshake_with_ccs_injection(self):  # type: ignore
    """Modified do_handshake() to send a CCS injection payload and return the result.
    """
    try:
        # Start the handshake using nassl - will throw WantReadError right away
        self._ssl.do_handshake()
    except WantReadError:
        # Send the Client Hello
        len_to_read = self._network_bio.pending()
        while len_to_read:
            # Get the data from the SSL engine
            handshake_data_out = self._network_bio.read(len_to_read)
            # Send it to the peer
            self._sock.send(handshake_data_out)
            len_to_read = self._network_bio.pending()

    # Retrieve the server's response - directly read the underlying network socket
    # Retrieve data until we get to the ServerHelloDone
    # The server may send back a ServerHello, an Alert or a CertificateRequest first
    did_receive_hello_done = False
    remaining_bytes = b""
    while not did_receive_hello_done:
        try:
            tls_record, len_consumed = TlsRecordParser.parse_bytes(remaining_bytes)
            remaining_bytes = remaining_bytes[len_consumed::]
        except NotEnoughData:
            # Try to get more data
            raw_ssl_bytes = self._sock.recv(16381)
            if not raw_ssl_bytes:
                # No data?
                break

            remaining_bytes = remaining_bytes + raw_ssl_bytes
            continue

        if isinstance(tls_record, TlsHandshakeRecord):
            # Does the record contain a ServerDone message?
            for handshake_message in tls_record.subprotocol_messages:
                if handshake_message.handshake_type == TlsHandshakeTypeByte.SERVER_DONE:
                    did_receive_hello_done = True
                    break
            # If not, it could be a ServerHello, Certificate or a CertificateRequest if the server requires client auth
        elif isinstance(tls_record, TlsAlertRecord):
            # Server returned a TLS alert
            break
        else:
            raise ValueError("Unknown record? Type {}".format(tls_record.header.type))

    if did_receive_hello_done:
        # Send an early CCS record - this should be rejected by the server
        payload = TlsChangeCipherSpecRecord.from_parameters(
            tls_version=TlsVersionEnum[self._ssl_version.name]
        ).to_bytes()
        self._sock.send(payload)

        # Send an early application data record which should be ignored by the server
        app_data_record = TlsApplicationDataRecord.from_parameters(
            tls_version=TlsVersionEnum[self._ssl_version.name], application_data=b"\x00\x00"
        )
        self._sock.send(app_data_record.to_bytes())

        # Check if an alert was sent back
        while True:
            try:
                tls_record, len_consumed = TlsRecordParser.parse_bytes(remaining_bytes)
                remaining_bytes = remaining_bytes[len_consumed::]
            except NotEnoughData:
                # Try to get more data
                try:
                    raw_ssl_bytes = self._sock.recv(16381)
                    if not raw_ssl_bytes:
                        # No data?
                        raise _NotVulnerableToCcsInjection()
                except socket.error:
                    # Server closed the connection after receiving the CCS payload
                    raise _NotVulnerableToCcsInjection()

                remaining_bytes = remaining_bytes + raw_ssl_bytes
                continue

            if isinstance(tls_record, TlsAlertRecord):
                # Server returned a TLS alert but which one?
                if tls_record.alert_description == 0x14:
                    # BAD_RECORD_MAC: This means that the server actually tried to decrypt our early application data
                    # record instead of ignoring it; server is vulnerable
                    raise _VulnerableToCcsInjection()

                # Any other alert means that the server rejected the early CCS record
                raise _NotVulnerableToCcsInjection()
            else:
                break

        raise _NotVulnerableToCcsInjection()


# TODO
class CliConnector:
    def as_text(self) -> List[str]:
        result_txt = [self._format_title(self.scan_command.get_title())]

        ccs_text = (
            "VULNERABLE - Server is vulnerable to OpenSSL CCS injection"
            if self.is_vulnerable_to_ccs_injection
            else "OK - Not vulnerable to OpenSSL CCS injection"
        )
        result_txt.append(self._format_field("", ccs_text))
        return result_txt
