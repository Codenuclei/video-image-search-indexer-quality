from app.gemini.rate_limit import gemini_error_is_billing_exhausted, gemini_error_is_retryable


def test_depleted_credits_are_not_retried() -> None:
    msg = "429 RESOURCE_EXHAUSTED. {'error': {'message': 'Your prepayment credits are depleted. Please go to AI Studio'}}"
    assert gemini_error_is_billing_exhausted(msg) is True
    assert gemini_error_is_retryable(msg) is False


def test_transient_429_without_credit_message_is_retried() -> None:
    msg = "429 RESOURCE_EXHAUSTED. Quota exceeded for embed_content_requests_per_minute"
    assert gemini_error_is_billing_exhausted(msg) is False
    assert gemini_error_is_retryable(msg) is True
