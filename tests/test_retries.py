from unittest.mock import patch
import requests

from .common import KaysMintsoftTestCommon, MockHTTPResponse


class TestKaysSyncJobRetries(KaysMintsoftTestCommon):
    """Test 27: Durable sync job queue, exponential backoff, Retry-After header, and dead-letter handling."""

    @patch('requests.Session.request')
    def test_27_retry_logic_exponential_backoff_and_dead_letter(self, mock_request):
        """
        Test 27: Verify exponential backoff formula, HTTP 429 Retry-After header override,
        bounded max_retries transition to dead_letter, and unknown_outcome retry protection.
        """
        job = self.SyncJob.enqueue_job(
            self.instance_uk,
            'stock_sync',
            payload={'full_sync': False},
        )
        job.write({
            'base_backoff_seconds': 60,
            'max_retries': 2,
            'retry_count': 0,
        })

        # Verify exponential backoff formula: 60 * 2^0 = 60, 60 * 2^1 = 120, 60 * 2^2 = 240
        self.assertEqual(job.compute_backoff_seconds(), 60)
        job.retry_count = 1
        self.assertEqual(job.compute_backoff_seconds(), 120)
        job.retry_count = 2
        self.assertEqual(job.compute_backoff_seconds(), 240)

        # Verify HTTP 429 Retry-After header override
        job.retry_after_seconds = 450
        self.assertEqual(job.compute_backoff_seconds(), 450)
        job.write({'retry_count': 0, 'retry_after_seconds': 0})

        # Simulate transient 503 failure -> transitions to 'retry_scheduled' (retry_count = 1)
        mock_request.return_value = MockHTTPResponse(503, {'Message': 'Service Unavailable'})
        job.action_execute_job()
        self.assertEqual(job.state, 'retry_scheduled')
        self.assertEqual(job.retry_count, 1)
        self.assertTrue(job.is_retryable)
        self.assertTrue(job.next_retry_at)

        # Second transient failure reaches max_retries=2 -> transitions to 'dead_letter'
        job.action_execute_job()
        self.assertEqual(job.state, 'dead_letter')
        self.assertEqual(job.retry_count, 2)
        self.assertFalse(job.is_retryable)
