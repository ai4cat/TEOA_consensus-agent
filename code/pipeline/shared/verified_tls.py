"""Reuse immutable, verified TLS roots without changing request bytes or trust."""
import ssl
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


def create_verified_context(cafile=None):
    context = create_urllib3_context()
    context.load_verify_locations(cafile or requests.certs.where())
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError('Certificate and hostname verification required')
    return context


class VerifiedContextAdapter(HTTPAdapter):
    def __init__(self, context):
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError('Unsafe TLS context')
        self.context = context
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        kwargs['ssl_context'] = self.context
        return super().init_poolmanager(connections, maxsize, block, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        if verify is not True or cert is not None:
            raise ValueError('This workflow requires default verified TLS without client certificates')
        host, kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        for key in ('ca_certs', 'ca_cert_dir', 'ca_cert_data'):
            kwargs.pop(key, None)
        kwargs.update(ssl_context=self.context, cert_reqs='CERT_REQUIRED')
        return host, kwargs

    def cert_verify(self, conn, url, verify, cert):
        if not url.lower().startswith('https://') or verify is not True or cert is not None:
            raise ValueError('Verified HTTPS required')
        conn.cert_reqs = 'CERT_REQUIRED'
        conn.ca_certs = None
        conn.ca_cert_dir = None
        conn.ca_cert_data = None
