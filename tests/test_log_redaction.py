import logging

from utils.logging import RedactingFormatter


def test_credentials_are_redacted_in_messages_and_exception_tracebacks():
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError('Request https://graph.test/media?access_token=query-secret&limit=10 failed')
    except RuntimeError:
        import sys
        record = logging.LogRecord("httpx", logging.ERROR, __file__, 1,
                                   'GET https://api.telegram.org/bottelegram-secret/getUpdates Authorization: Bearer bearer-secret',
                                   (), sys.exc_info())
    output = formatter.format(record)
    for secret in ("query-secret", "telegram-secret", "bearer-secret"):
        assert secret not in output
    assert "limit=10" in output
    assert "RuntimeError" in output
