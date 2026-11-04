from unittest.mock import patch
import requests
from odoo.exceptions import ValidationError

from .common import KaysMintsoftTestCommon, MockHTTPResponse
from ..services.mintsoft_client import (
    MintsoftClient,
    MintsoftAPIError,
    is_mutating_request,
    sanitize_payload,
    validate_api_base_url,
)


class TestMintsoftAPIClient(KaysMintsoftTestCommon):
    """Tests 1–7: Mintsoft HTTP client authentication, formatting, error classification, and diagnostics."""

    def test_01_api_authentication_header_and_sanitization(self):
        """Test 1: Verify ms-apikey header injection and strict credential redaction in logs."""
        client = MintsoftClient(self.env, self.instance_uk)
        headers = client._build_headers('corr-test-001')

        self.assertEqual(headers['ms-apikey'], 'TEST_SYNTHETIC_API_KEY_DO_NOT_USE_IN_PROD_12345')
        self.assertEqual(headers['Accept'], 'application/json')
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['X-Correlation-ID'], 'corr-test-001')

        # Ensure sanitize_payload redacts ms-apikey and secrets
        redacted = sanitize_payload(headers)
        self.assertEqual(redacted['ms-apikey'], '***REDACTED***')

    @patch('requests.Session.request')
    def test_02_request_formatting_and_url_building(self, mock_request):
        """Test 2: Verify endpoint URL normalization, query params, JSON serialization, and audit logging."""
        mock_request.return_value = MockHTTPResponse(
            status_code=200,
            json_data=[{'ID': 3, 'Name': 'Main Warehouse', 'Active': True}],
        )
        client = MintsoftClient(self.env, self.instance_uk)
        result = client.request('GET', '/api/Warehouse', operation='test_warehouse_fetch')

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['ID'], 3)
        self.assertEqual(result[0]['Name'], 'Main Warehouse')

        called_args, called_kwargs = mock_request.call_args
        self.assertEqual(called_kwargs['method'], 'GET')
        self.assertEqual(called_kwargs['url'], 'https://api.mintsoft.co.uk/api/Warehouse')
        self.assertEqual(called_kwargs['headers']['ms-apikey'], self.instance_uk.api_key)

        # Verify audit log record was created and never contains raw api_key
        log_rec = self.env['kays.mintsoft.log'].search([
            ('instance_id', '=', self.instance_uk.id),
            ('operation', '=', 'test_warehouse_fetch'),
        ], limit=1)
        self.assertTrue(log_rec)
        self.assertEqual(log_rec.http_status, 200)
        self.assertEqual(log_rec.status, 'success')
        self.assertNotIn(self.instance_uk.api_key, log_rec.request_summary or '')

    @patch('requests.Session.request')
    def test_03_401_403_handling_and_5_probe_diagnostics(self, mock_request):
        """
        Test 3: Verify 401/403 handling and confirm that HTTP 401 on /api/Client does NOT
        invalidate the connection when Warehouse, Product, Order, and Stock endpoints return 200.
        """
        def side_effect_5_probes(**kwargs):
            url = kwargs.get('url', '')
            if url.endswith('/api/Warehouse'):
                return MockHTTPResponse(200, [{'ID': 3, 'Name': 'Main Warehouse'}])
            if url.endswith('/api/Product/List'):
                return MockHTTPResponse(200, [{'ID': i, 'SKU': f'SKU-{i}'} for i in range(34)])
            if url.endswith('/api/Order/List'):
                return MockHTTPResponse(200, [{'ID': i, 'OrderNumber': f'ORD-{i}'} for i in range(100)])
            if url.endswith('/api/Product/StockLevels'):
                return MockHTTPResponse(200, [{'ProductId': i, 'Quantity': 10} for i in range(34)])
            if url.endswith('/api/Client'):
                return MockHTTPResponse(401, {'Message': 'Authorization has been denied for this request.'})
            return MockHTTPResponse(200, [])

        mock_request.side_effect = side_effect_5_probes
        client = MintsoftClient(self.env, self.instance_uk)
        diag = client.run_connection_diagnostics()

        self.assertEqual(diag['overall_status'], 'connected')
        self.assertTrue(diag['probes']['warehouse']['ok'])
        self.assertTrue(diag['probes']['products']['ok'])
        self.assertTrue(diag['probes']['orders']['ok'])
        self.assertTrue(diag['probes']['stock']['ok'])
        self.assertFalse(diag['probes']['client']['ok'])
        self.assertEqual(diag['probes']['client']['http_status'], 401)
        self.assertIn('Client API: HTTP 401', diag['summary_text'])
        self.assertEqual(self.instance_uk.connection_status, 'connected')

    @patch('requests.Session.request')
    def test_04_429_rate_limit_handling(self, mock_request):
        """Test 4: Verify HTTP 429 rate-limit response extracts Retry-After seconds and marks error retryable."""
        mock_request.return_value = MockHTTPResponse(
            status_code=429,
            json_data={'Message': 'Too Many Requests'},
            headers={'Retry-After': '120', 'Content-Type': 'application/json'},
        )
        client = MintsoftClient(self.env, self.instance_uk)
        with self.assertRaises(MintsoftAPIError) as ctx:
            client.request('GET', '/api/Product/StockLevels', operation='test_rate_limit')

        err = ctx.exception
        self.assertEqual(err.http_status, 429)
        self.assertEqual(err.error_code, 'HTTP_429_RATE_LIMITED')
        self.assertTrue(err.retryable)
        self.assertEqual(err.retry_after, 120)

    @patch('requests.Session.request')
    def test_05_timeout_handling_read_vs_mutating_write(self, mock_request):
        """Test 5: Verify timeout on read is retryable, whereas timeout on write sets unknown_outcome=True."""
        mock_request.side_effect = requests.exceptions.Timeout("Read timed out after 25s")
        client = MintsoftClient(self.env, self.instance_uk)

        # Read timeout -> retryable=True, unknown_outcome=False
        with self.assertRaises(MintsoftAPIError) as read_ctx:
            client.request('GET', '/api/Order/List', operation='test_read_timeout')
        self.assertTrue(read_ctx.exception.retryable)
        self.assertFalse(read_ctx.exception.unknown_outcome)

        # Write timeout (PUT /api/Order) -> retryable=False, unknown_outcome=True
        with self.assertRaises(MintsoftAPIError) as write_ctx:
            client.request('PUT', '/api/Order', json_payload={'OrderNumber': 'SO-TIMEOUT'}, operation='test_write_timeout')
        self.assertFalse(write_ctx.exception.retryable)
        self.assertTrue(write_ctx.exception.unknown_outcome)
        self.assertEqual(write_ctx.exception.error_code, 'TIMEOUT_UNKNOWN_OUTCOME')

    @patch('requests.Session.request')
    def test_06_connection_error_handling(self, mock_request):
        """Test 6: Verify network ConnectionError is caught, logged, and classified correctly."""
        mock_request.side_effect = requests.exceptions.ConnectionError("DNS resolution failed")
        client = MintsoftClient(self.env, self.instance_uk)

        with self.assertRaises(MintsoftAPIError) as ctx:
            client.request('GET', '/api/Warehouse', operation='test_conn_err')

        self.assertEqual(ctx.exception.error_code, 'CONNECTION_ERROR')
        self.assertTrue(ctx.exception.retryable)
        self.assertFalse(ctx.exception.unknown_outcome)

    @patch('requests.Session.request')
    def test_07_json_parse_error_handling_and_ssrf_protection(self, mock_request):
        """Test 7: Verify malformed JSON response handling, SSRF host blocking, and Dry Run mutating-GET protection."""
        mock_request.return_value = MockHTTPResponse(
            status_code=200,
            text='<html><body>Gateway Error Non-JSON</body></html>',
            headers={'Content-Type': 'text/html'},
        )
        client = MintsoftClient(self.env, self.instance_uk)
        with self.assertRaises(MintsoftAPIError) as ctx:
            client.request('GET', '/api/Warehouse', operation='test_bad_json')
        self.assertEqual(ctx.exception.error_code, 'JSON_PARSE_ERROR')

        # Verify SSRF / non-HTTPS protection
        with self.assertRaises(ValidationError):
            validate_api_base_url('http://api.mintsoft.co.uk/api')
        with self.assertRaises(ValidationError):
            validate_api_base_url('https://127.0.0.1/api')
        with self.assertRaises(ValidationError):
            validate_api_base_url('https://evil.example.com/api')

        # Verify mutating GET detection (/api/Order/{id}/Cancel is a write operation!)
        self.assertTrue(is_mutating_request('GET', '/api/Order/1055/Cancel'))
        self.assertFalse(is_mutating_request('GET', '/api/Order/1055'))
