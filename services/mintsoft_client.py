import copy
import ipaddress
import json
import logging
import re
import time
import uuid
from urllib.parse import urlparse, urljoin

import requests
from odoo import fields, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Mintsoft GET endpoints that mutate state on the server and MUST be treated as WRITE operations
MUTATING_GET_PATH_PATTERNS = (
    re.compile(r'/api/Order/\d+/Cancel($|/)', re.I),
    re.compile(r'/api/Order/\d+/CancelNoStock($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkAwaitingConfirmation($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkAwaitingPayment($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkConfirmed($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkDespatched($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkPackAndHold($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkPaymentReceived($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkPrinted($|/)', re.I),
    re.compile(r'/api/Order/\d+/MarkReleasePackAndHold($|/)', re.I),
    re.compile(r'/api/Order/\d+/UpdateNumberOfParcels($|/)', re.I),
    re.compile(r'/api/Order/\d+/Documents/\d+/MarkPrinted($|/)', re.I),
    re.compile(r'/api/ASN/\d+/(BookIn|BookInPartial|Confirm|PartBook|MarkAwaitingPutAway|MarkPutAwayComplete)($|/)', re.I),
    re.compile(r'/api/WarehouseTransfer/\d+/(Cancel|Confirm)($|/)', re.I),
)

SENSITIVE_KEYS = {
    'ms-apikey', 'apikey', 'api_key', 'authorization', 'password', 'secret',
    'webhook_secret', 'sensitivedata', 'token', 'access_token', 'card_number', 'cvv',
}


class MintsoftAPIError(Exception):
    """Structured exception for Mintsoft API errors."""

    def __init__(
        self,
        message,
        http_status=0,
        error_classification='permanent',
        is_retryable=False,
        retry_after=0,
        response_data=None,
        correlation_id=None,
        unknown_outcome=False,
    ):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.error_classification = error_classification
        self.is_retryable = is_retryable
        self.retry_after = retry_after
        self.response_data = response_data
        self.correlation_id = correlation_id
        self.unknown_outcome = unknown_outcome


class MintsoftSecurityError(MintsoftAPIError):
    """Raised when URL validation, host allowlisting, or TLS rules are violated."""


class MintsoftDryRunBlockedError(MintsoftAPIError):
    """Raised if a mutating API call is attempted while instance is in Dry Run mode."""


def sanitize_payload(data, mask_pii=False):
    """Recursively mask sensitive credentials (and optionally PII) in dicts/lists/strings."""
    if data is None:
        return None
    if isinstance(data, (int, float, bool)):
        return data
    if isinstance(data, str):
        # Mask any inline ms-apikey occurrences
        masked = re.sub(
            r'(ms-apikey["\'\s:=]+)([A-Za-z0-9\-_]{4})[A-Za-z0-9\-_]+',
            r'\1\2********',
            data,
            flags=re.I,
        )
        return masked
    if isinstance(data, list):
        return [sanitize_payload(item, mask_pii=mask_pii) for item in data]
    if isinstance(data, dict):
        result = {}
        pii_keys = {'address1', 'address2', 'address3', 'phone', 'mobile', 'email', 'postcode'}
        for k, v in data.items():
            k_lower = str(k).lower().strip()
            if k_lower in SENSITIVE_KEYS:
                if isinstance(v, str) and len(v) > 4:
                    result[k] = f"{v[:4]}{'*' * 8}"
                else:
                    result[k] = "********"
            elif mask_pii and k_lower in pii_keys and isinstance(v, str) and len(v) > 2:
                result[k] = f"{v[:2]}***"
            else:
                result[k] = sanitize_payload(v, mask_pii=mask_pii)
        return result
    return str(data)


def is_mutating_request(method, endpoint):
    """Determine if an HTTP method + endpoint combination mutates server state."""
    method_upper = (method or 'GET').upper()
    if method_upper in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return True
    for pattern in MUTATING_GET_PATH_PATTERNS:
        if pattern.search(endpoint or ''):
            return True
    return False


def validate_and_normalize_api_url(base_url, allowed_hosts_str='api.mintsoft.co.uk'):
    """Validate URL format, HTTPS scheme, private IP blocking (SSRF protection), and host allowlist."""
    if not base_url or not isinstance(base_url, str):
        raise MintsoftSecurityError(_("API Base URL must not be empty."))

    cleaned_url = base_url.strip().rstrip('/')
    parsed = urlparse(cleaned_url)

    if parsed.scheme.lower() != 'https':
        raise MintsoftSecurityError(
            _("Insecure scheme '%s' rejected. Only HTTPS URLs are permitted for Mintsoft API connections.")
            % (parsed.scheme or 'none')
        )

    hostname = (parsed.hostname or '').lower().strip()
    if not hostname:
        raise MintsoftSecurityError(_("API Base URL must include a valid hostname."))

    if parsed.username or parsed.password:
        raise MintsoftSecurityError(_("Credentials must not be embedded in the API Base URL."))

    # Block raw IP literals and private/loopback addresses to prevent SSRF
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            raise MintsoftSecurityError(
                _("Private, loopback, or reserved IP addresses (%s) are forbidden.") % hostname
            )
    except ValueError:
        # Hostname is a domain name, not an IP literal
        pass

    if hostname in ('localhost', 'localhost.localdomain') or hostname.endswith('.local') or hostname.endswith('.internal'):
        raise MintsoftSecurityError(_("Localhost and internal hostnames (%s) are forbidden.") % hostname)

    allowed_hosts = {
        h.strip().lower()
        for h in (allowed_hosts_str or 'api.mintsoft.co.uk').split(',')
        if h.strip()
    }
    allowed_hosts.add('api.mintsoft.co.uk')

    if hostname not in allowed_hosts:
        raise MintsoftSecurityError(
            _("Hostname '%(host)s' is not in the approved allowlist (%(allowed)s).")
            % {'host': hostname, 'allowed': ', '.join(sorted(allowed_hosts))}
        )

    return cleaned_url


class MintsoftClient:
    """Production Mintsoft REST API V1 Client with safety, logging, and retry classification."""

    DEFAULT_TIMEOUT = (10, 30)  # (connect_timeout, read_timeout) in seconds

    def __init__(self, instance, session=None):
        self.instance = instance
        self.env = instance.env
        self.session = session or requests.Session()

    def _get_api_key(self):
        """Retrieve API key securely using sudo only within the authorized instance's company context."""
        key = self.instance.sudo().api_key
        if not key or not key.strip():
            raise MintsoftAPIError(
                _("Mintsoft API Key is not configured on connection '%s'.") % self.instance.display_name,
                http_status=401,
                error_classification='auth_error',
                is_retryable=False,
            )
        return key.strip()

    def _build_url(self, endpoint):
        """Validate base URL and join with endpoint path."""
        base_url = validate_and_normalize_api_url(
            self.instance.active_api_url,
            self.instance.allowed_hosts,
        )
        # Normalize endpoint: if base_url ends with /api and endpoint starts with /api/, deduplicate /api
        ep = (endpoint or '').strip()
        if not ep.startswith('/'):
            ep = '/' + ep
        if base_url.endswith('/api') and ep.startswith('/api/'):
            return base_url + ep[4:]
        if base_url.endswith('/api') and ep == '/api':
            return base_url
        return base_url + ep

    def request(
        self,
        method,
        endpoint,
        params=None,
        json_payload=None,
        operation='api_call',
        sale_order=None,
        kays_order=None,
        mintsoft_order_id=0,
        sync_job=None,
        retry_count=0,
        correlation_id=None,
        timeout=None,
        allow_404=False,
    ):
        """Execute a validated, logged HTTP request to the Mintsoft REST API V1."""
        correlation_id = correlation_id or str(uuid.uuid4())
        method_upper = (method or 'GET').upper()
        mutating = is_mutating_request(method_upper, endpoint)

        # Check connection active & master integration switch for operational calls
        if not self.instance.active:
            raise MintsoftAPIError(
                _("Connection '%s' is disabled.") % self.instance.display_name,
                error_classification='validation_error',
                is_retryable=False,
                correlation_id=correlation_id,
            )

        # Enforce Dry Run protection: NEVER send write/mutating requests in Dry Run mode
        if mutating and self.instance.environment == 'dry_run':
            self._log_call(
                operation=f"{operation} [DRY RUN BLOCKED]",
                http_method=method_upper,
                endpoint=endpoint,
                http_status=0,
                duration_ms=0,
                sale_order=sale_order,
                kays_order=kays_order,
                mintsoft_order_id=mintsoft_order_id,
                sync_job=sync_job,
                status='warning',
                error_code='DRY_RUN_WRITE_BLOCKED',
                retry_count=retry_count,
                correlation_id=correlation_id,
                request_payload={'params': params, 'body': json_payload},
                response_payload={'dry_run': True, 'message': 'Write request blocked in Dry Run mode.'},
                error_message='Write request blocked because connection is in Dry Run mode.',
            )
            raise MintsoftDryRunBlockedError(
                _("Write request '%(method)s %(endpoint)s' blocked because connection '%(conn)s' is in Dry Run mode.")
                % {'method': method_upper, 'endpoint': endpoint, 'conn': self.instance.display_name},
                error_classification='validation_error',
                is_retryable=False,
                correlation_id=correlation_id,
            )

        # In Test Client mode, require explicit write acknowledgment before mutating calls
        if mutating and self.instance.environment == 'test_client' and not self.instance.test_write_acknowledged:
            raise MintsoftAPIError(
                _("Test Client write authorization has not been acknowledged on connection '%s'.")
                % self.instance.display_name,
                error_classification='validation_error',
                is_retryable=False,
                correlation_id=correlation_id,
            )

        # In Production mode, require explicit administrator production approval before mutating calls
        if mutating and self.instance.environment == 'production' and not self.instance.production_approved:
            raise MintsoftAPIError(
                _("Production mode on connection '%s' has not been approved by an administrator.")
                % self.instance.display_name,
                error_classification='validation_error',
                is_retryable=False,
                correlation_id=correlation_id,
            )

        url = self._build_url(endpoint)
        api_key = self._get_api_key()

        headers = {
            'ms-apikey': api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'Odoo19-KaysMintsoftIntegration/1.0',
            'X-Correlation-ID': correlation_id,
        }

        start_time = time.monotonic()
        http_status = 0
        response_data = None
        raw_text = ''

        try:
            response = self.session.request(
                method=method_upper,
                url=url,
                headers=headers,
                params=params,
                json=json_payload,
                timeout=timeout or self.DEFAULT_TIMEOUT,
                verify=True,  # Mandatory TLS verification — never disable!
                allow_redirects=False,  # Prevent redirect-based credential leakage
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)
            http_status = response.status_code
            raw_text = response.text or ''

            if raw_text.strip():
                try:
                    response_data = response.json()
                except ValueError as json_err:
                    if 200 <= http_status < 300:
                        self._record_instance_failure()
                        self._log_call(
                            operation=operation,
                            http_method=method_upper,
                            endpoint=endpoint,
                            http_status=http_status,
                            duration_ms=duration_ms,
                            sale_order=sale_order,
                            kays_order=kays_order,
                            mintsoft_order_id=mintsoft_order_id,
                            sync_job=sync_job,
                            status='failure',
                            error_code='INVALID_JSON',
                            retry_count=retry_count,
                            correlation_id=correlation_id,
                            request_payload={'params': params, 'body': json_payload},
                            response_payload=raw_text[:2000],
                            error_message=f"Invalid JSON response: {json_err}",
                        )
                        raise MintsoftAPIError(
                            _("Malformed JSON response from Mintsoft API (HTTP %s).") % http_status,
                            http_status=http_status,
                            error_classification='transient',
                            is_retryable=not mutating,
                            response_data=raw_text[:2000],
                            correlation_id=correlation_id,
                            unknown_outcome=mutating,
                        ) from json_err
                    response_data = {'raw': raw_text[:2000]}

            # Handle 404 when caller expects it (e.g. GetOrderId not found)
            if http_status == 404 and allow_404:
                self._record_instance_success(duration_ms)
                self._log_call(
                    operation=operation,
                    http_method=method_upper,
                    endpoint=endpoint,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    sale_order=sale_order,
                    kays_order=kays_order,
                    mintsoft_order_id=mintsoft_order_id,
                    sync_job=sync_job,
                    status='success',
                    retry_count=retry_count,
                    correlation_id=correlation_id,
                    request_payload={'params': params, 'body': json_payload},
                    response_payload=response_data,
                )
                return None

            # Handle rate limiting (HTTP 429)
            if http_status == 429:
                retry_after = self._parse_retry_after(response.headers.get('Retry-After'))
                self._record_instance_failure()
                self._log_call(
                    operation=operation,
                    http_method=method_upper,
                    endpoint=endpoint,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    sale_order=sale_order,
                    kays_order=kays_order,
                    mintsoft_order_id=mintsoft_order_id,
                    sync_job=sync_job,
                    status='failure',
                    error_code='HTTP_429_RATE_LIMITED',
                    retry_count=retry_count,
                    correlation_id=correlation_id,
                    request_payload={'params': params, 'body': json_payload},
                    response_payload=response_data,
                    error_message=f"Rate limited (HTTP 429). Retry-After: {retry_after}s",
                )
                raise MintsoftAPIError(
                    _("Mintsoft API rate limit reached (HTTP 429). Retry after %s seconds.") % retry_after,
                    http_status=429,
                    error_classification='rate_limited',
                    is_retryable=True,
                    retry_after=retry_after,
                    response_data=response_data,
                    correlation_id=correlation_id,
                )

            # Handle authentication / permission errors (HTTP 401 / 403)
            if http_status in (401, 403):
                self._log_call(
                    operation=operation,
                    http_method=method_upper,
                    endpoint=endpoint,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    sale_order=sale_order,
                    kays_order=kays_order,
                    mintsoft_order_id=mintsoft_order_id,
                    sync_job=sync_job,
                    status='failure',
                    error_code=f'HTTP_{http_status}_AUTH',
                    retry_count=retry_count,
                    correlation_id=correlation_id,
                    request_payload={'params': params, 'body': json_payload},
                    response_payload=response_data,
                    error_message=f"Authentication or permission error (HTTP {http_status}) on {endpoint}",
                )
                raise MintsoftAPIError(
                    _("Mintsoft API returned HTTP %(code)s for endpoint '%(ep)s': permission or authentication issue.")
                    % {'code': http_status, 'ep': endpoint},
                    http_status=http_status,
                    error_classification='auth_error',
                    is_retryable=False,
                    response_data=response_data,
                    correlation_id=correlation_id,
                )

            # Handle server errors (HTTP 5xx)
            if http_status >= 500:
                self._record_instance_failure()
                self._log_call(
                    operation=operation,
                    http_method=method_upper,
                    endpoint=endpoint,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    sale_order=sale_order,
                    kays_order=kays_order,
                    mintsoft_order_id=mintsoft_order_id,
                    sync_job=sync_job,
                    status='failure',
                    error_code=f'HTTP_{http_status}_SERVER_ERROR',
                    retry_count=retry_count,
                    correlation_id=correlation_id,
                    request_payload={'params': params, 'body': json_payload},
                    response_payload=response_data,
                    error_message=f"Mintsoft server error (HTTP {http_status})",
                )
                raise MintsoftAPIError(
                    _("Mintsoft server error (HTTP %(code)s) on '%(ep)s'.")
                    % {'code': http_status, 'ep': endpoint},
                    http_status=http_status,
                    error_classification='timeout_unknown' if mutating else 'transient',
                    is_retryable=True,
                    response_data=response_data,
                    correlation_id=correlation_id,
                    unknown_outcome=mutating,
                )

            # Handle client/validation errors (HTTP 4xx)
            if http_status >= 400:
                err_msg = self._extract_error_message(response_data, raw_text, http_status)
                self._record_instance_failure()
                self._log_call(
                    operation=operation,
                    http_method=method_upper,
                    endpoint=endpoint,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    sale_order=sale_order,
                    kays_order=kays_order,
                    mintsoft_order_id=mintsoft_order_id,
                    sync_job=sync_job,
                    status='failure',
                    error_code=f'HTTP_{http_status}_CLIENT_ERROR',
                    retry_count=retry_count,
                    correlation_id=correlation_id,
                    request_payload={'params': params, 'body': json_payload},
                    response_payload=response_data,
                    error_message=err_msg,
                )
                raise MintsoftAPIError(
                    err_msg,
                    http_status=http_status,
                    error_classification='validation_error',
                    is_retryable=False,
                    response_data=response_data,
                    correlation_id=correlation_id,
                )

            # Successful HTTP 2xx response
            self._record_instance_success(duration_ms)
            self._log_call(
                operation=operation,
                http_method=method_upper,
                endpoint=endpoint,
                http_status=http_status,
                duration_ms=duration_ms,
                sale_order=sale_order,
                kays_order=kays_order,
                mintsoft_order_id=mintsoft_order_id,
                sync_job=sync_job,
                status='success',
                retry_count=retry_count,
                correlation_id=correlation_id,
                request_payload={'params': params, 'body': json_payload},
                response_payload=response_data,
            )
            return response_data

        except requests.exceptions.Timeout as timeout_err:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            self._record_instance_failure()
            self._log_call(
                operation=operation,
                http_method=method_upper,
                endpoint=endpoint,
                http_status=0,
                duration_ms=duration_ms,
                sale_order=sale_order,
                kays_order=kays_order,
                mintsoft_order_id=mintsoft_order_id,
                sync_job=sync_job,
                status='failure',
                error_code='TIMEOUT',
                retry_count=retry_count,
                correlation_id=correlation_id,
                request_payload={'params': params, 'body': json_payload},
                error_message=f"Request timed out after {duration_ms}ms: {timeout_err}",
            )
            raise MintsoftAPIError(
                _("Request to Mintsoft endpoint '%s' timed out.") % endpoint,
                http_status=0,
                error_classification='timeout_unknown' if mutating else 'transient',
                is_retryable=True,
                correlation_id=correlation_id,
                unknown_outcome=mutating,
            ) from timeout_err

        except requests.exceptions.SSLError as ssl_err:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            self._record_instance_failure()
            self._log_call(
                operation=operation,
                http_method=method_upper,
                endpoint=endpoint,
                http_status=0,
                duration_ms=duration_ms,
                sale_order=sale_order,
                kays_order=kays_order,
                mintsoft_order_id=mintsoft_order_id,
                sync_job=sync_job,
                status='failure',
                error_code='SSL_TLS_ERROR',
                retry_count=retry_count,
                correlation_id=correlation_id,
                request_payload={'params': params, 'body': json_payload},
                error_message=f"TLS certificate verification failed: {ssl_err}",
            )
            raise MintsoftSecurityError(
                _("TLS certificate verification failed for '%s'.") % url,
                http_status=0,
                error_classification='permanent',
                is_retryable=False,
                correlation_id=correlation_id,
            ) from ssl_err

        except requests.exceptions.RequestException as req_err:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            self._record_instance_failure()
            self._log_call(
                operation=operation,
                http_method=method_upper,
                endpoint=endpoint,
                http_status=0,
                duration_ms=duration_ms,
                sale_order=sale_order,
                kays_order=kays_order,
                mintsoft_order_id=mintsoft_order_id,
                sync_job=sync_job,
                status='failure',
                error_code='CONNECTION_ERROR',
                retry_count=retry_count,
                correlation_id=correlation_id,
                request_payload={'params': params, 'body': json_payload},
                error_message=f"Network connection error: {req_err}",
            )
            raise MintsoftAPIError(
                _("Network connection error communicating with Mintsoft endpoint '%s'.") % endpoint,
                http_status=0,
                error_classification='transient',
                is_retryable=True,
                correlation_id=correlation_id,
                unknown_outcome=False,
            ) from req_err

    @staticmethod
    def _parse_retry_after(header_val):
        if not header_val:
            return 60
        try:
            return max(1, min(int(header_val), 3600))
        except (ValueError, TypeError):
            return 60

    @staticmethod
    def _extract_error_message(response_data, raw_text, http_status):
        if isinstance(response_data, dict):
            msg = (
                response_data.get('Message')
                or response_data.get('message')
                or response_data.get('ErrorMessage')
                or response_data.get('error')
            )
            if msg:
                return f"Mintsoft HTTP {http_status}: {msg}"
        if isinstance(response_data, list) and response_data:
            first = response_data[0]
            if isinstance(first, dict) and first.get('Message'):
                return f"Mintsoft HTTP {http_status}: {first.get('Message')}"
        snippet = (raw_text or '').strip()[:300]
        return f"Mintsoft HTTP {http_status}: {snippet or 'Bad Request'}"

    def _record_instance_success(self, duration_ms):
        try:
            inst = self.instance.sudo()
            old_avg = inst.avg_response_time_ms or duration_ms
            new_avg = round((old_avg * 0.7) + (duration_ms * 0.3), 2)
            inst.write({
                'last_successful_request': fields.Datetime.now(),
                'avg_response_time_ms': new_avg,
            })
        except Exception as exc:
            _logger.debug("Could not update instance success metrics: %s", exc)

    def _record_instance_failure(self):
        try:
            self.instance.sudo().write({
                'last_failed_request': fields.Datetime.now(),
            })
        except Exception as exc:
            _logger.debug("Could not update instance failure metrics: %s", exc)

    def _log_call(
        self,
        operation,
        http_method,
        endpoint,
        http_status,
        duration_ms,
        sale_order=None,
        kays_order=None,
        mintsoft_order_id=0,
        sync_job=None,
        status='success',
        error_code='',
        retry_count=0,
        correlation_id='',
        request_payload=None,
        response_payload=None,
        error_message='',
    ):
        """Record a sanitized API log entry in kays.mintsoft.log."""
        try:
            is_debug = bool(self.instance.enable_debug_logging)
            # Always mask PII in normal logs; keep sanitized structure in debug logs
            sanitized_req = sanitize_payload(request_payload, mask_pii=not is_debug)
            sanitized_resp = sanitize_payload(response_payload, mask_pii=not is_debug)

            req_str = json.dumps(sanitized_req, indent=2, default=str)[:8000] if sanitized_req else ''
            resp_str = json.dumps(sanitized_resp, indent=2, default=str)[:8000] if sanitized_resp else ''

            self.env['kays.mintsoft.log'].sudo().create({
                'timestamp': fields.Datetime.now(),
                'company_id': self.instance.company_id.id,
                'instance_id': self.instance.id,
                'environment': self.instance.environment,
                'operation': operation,
                'http_method': http_method,
                'endpoint': endpoint,
                'http_status': http_status,
                'duration_ms': duration_ms,
                'sale_order_id': sale_order.id if sale_order else (kays_order.sale_order_id.id if kays_order else False),
                'kays_order_id': kays_order.id if kays_order else False,
                'mintsoft_order_id': mintsoft_order_id or (kays_order.mintsoft_order_id if kays_order else 0),
                'sync_job_id': sync_job.id if sync_job else False,
                'status': status,
                'is_debug': is_debug and status == 'success',
                'error_code': error_code or '',
                'retry_count': retry_count,
                'correlation_id': correlation_id,
                'request_summary': req_str,
                'response_summary': resp_str,
                'error_message': sanitize_payload(error_message) or '',
            })
        except Exception as log_exc:
            _logger.warning("Failed to write kays.mintsoft.log record: %s", log_exc)

    # -------------------------------------------------------------------------
    # High-Level Safe Read-Only Connection & Permission Testing
    # -------------------------------------------------------------------------
    def run_connection_diagnostics(self):
        """
        Execute safe read-only API operations to verify connectivity and endpoint permissions.
        Never declares a full connection failure if only /api/Client (Admin-only) returns 401.
        """
        probes = [
            ('warehouse', 'Warehouse API', 'GET', '/api/Warehouse', None, True),
            ('products', 'Products API', 'GET', '/api/Product/List', {'PageNo': 1, 'Limit': 10}, True),
            ('orders', 'Orders API', 'GET', '/api/Order/List', {'PageNo': 1, 'Limit': 10}, True),
            ('stock', 'Stock API', 'GET', '/api/Product/StockLevels', None, True),
            ('client', 'Client API', 'GET', '/api/Client', {'pageNo': 1, 'limit': 10}, False),
        ]

        results = {}
        core_passed = 0
        core_total = 0

        for key, label, method, ep, params, is_core in probes:
            if is_core:
                core_total += 1
            try:
                data = self.request(method, ep, params=params, operation=f'test_{key}')
                count = len(data) if isinstance(data, list) else (1 if data else 0)
                results[key] = {
                    'label': label,
                    'http_status': 200,
                    'ok': True,
                    'count': count,
                    'message': f"HTTP 200 ({count} record{'s' if count != 1 else ''})",
                    'data': data,
                }
                if is_core:
                    core_passed += 1
            except MintsoftAPIError as err:
                status_code = err.http_status or 0
                if key == 'client' and status_code in (401, 403):
                    msg = f"HTTP {status_code} — permission or authentication issue for this endpoint (Admin-only endpoint)"
                else:
                    msg = f"HTTP {status_code} — {err.message}" if status_code else err.message
                results[key] = {
                    'label': label,
                    'http_status': status_code,
                    'ok': False,
                    'count': 0,
                    'message': msg,
                    'data': None,
                }

        if core_passed == core_total:
            overall_status = 'connected'
            headline = _("Connection: Successful")
        elif core_passed > 0:
            overall_status = 'partial'
            headline = _("Connection: Partial (Some core endpoints restricted)")
        else:
            overall_status = 'failed'
            headline = _("Connection: Failed")

        lines = [headline, ""]
        for key, _label, *_rest in probes:
            r = results[key]
            lines.append(f"{r['label']}: {r['message']}")

        summary_text = "\n".join(lines)
        self.instance.sudo().write({
            'last_auth_check': fields.Datetime.now(),
            'connection_status': overall_status,
            'health_summary': summary_text,
        })

        return {
            'overall_status': overall_status,
            'headline': headline,
            'summary_text': summary_text,
            'probes': results,
        }
