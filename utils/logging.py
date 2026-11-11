import logging
import re


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        message = re.sub(r"(/bot)[^/\s]+", r"\1[REDACTED]", message)
        message = re.sub(
            r'(?i)((?:access_token|refresh_token|client_secret|appsecret_proof)=)[^&\s\"]+',
            r"\1[REDACTED]", message,
        )
        message = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", message)
        return message
