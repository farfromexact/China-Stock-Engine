from __future__ import annotations

import unittest

from china_stock_engine.ifind_http import (
    IFindHTTPClient,
    IFindHTTPError,
    IFindHTTPTransientError,
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict | None]] = []

    def __call__(self, url: str, headers: dict, payload: dict | None, timeout: int) -> dict:
        self.calls.append((url, headers, payload))
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "get_access_token":
            return {"errorcode": 0, "data": {"access_token": "access-secret"}}
        if endpoint == "data_pool":
            return {
                "errorcode": 0,
                "tables": [
                    {
                        "table": {
                            "p03291_f001": ["20260818", "20260818", "20260818"],
                            "p03291_f002": ["600000.SH", "000001.SZ", "bad"],
                            "p03291_f003": ["浦发银行", "平安银行", "坏代码"],
                            "p03291_f004": ["浦发银行", "平安银行", "坏代码"],
                        }
                    }
                ],
            }
        if endpoint == "cmd_history_quotation":
            codes = payload["codes"].split(",")
            return {
                "errorcode": 0,
                "tables": [
                    {
                        "thscode": code,
                        "time": ["2026-08-18"],
                        "table": {
                            "open": [10.0],
                            "high": [11.0],
                            "low": [9.0],
                            "close": [10.5],
                            "volume": [1000],
                            "amount": [10000],
                            "changeRatio": [1.2],
                        },
                    }
                    for code in codes
                ],
            }
        raise AssertionError(endpoint)


class IFindHTTPClientTests(unittest.TestCase):
    def test_auth_is_cached_and_universe_is_normalized(self) -> None:
        transport = FakeTransport()
        client = IFindHTTPClient(refresh_token="refresh-secret", transport=transport)
        first = client.fetch_universe("2026-08-18")
        second = client.fetch_universe("2026-08-18")
        self.assertEqual(first["thscode"].tolist(), ["600000.SH", "000001.SZ"])
        self.assertEqual(first["as_of_date"].tolist(), ["2026-08-18", "2026-08-18"])
        self.assertEqual(len(second), 2)
        auth_calls = [call for call in transport.calls if call[0].endswith("get_access_token")]
        self.assertEqual(len(auth_calls), 1)

    def test_daily_quotes_are_batched_and_mapped(self) -> None:
        transport = FakeTransport()
        client = IFindHTTPClient(refresh_token="refresh-secret", transport=transport)
        frame = client.fetch_daily_quotes(
            ["600000.SH", "000001.SZ"],
            "2026-08-18",
            batch_size=1,
            request_interval_seconds=0,
        )
        self.assertEqual(len(frame), 2)
        self.assertEqual(set(frame["trade_date"]), {"2026-08-18"})
        self.assertEqual(set(frame["source_endpoint"]), {"cmd_history_quotation"})
        quote_calls = [
            call for call in transport.calls if call[0].endswith("cmd_history_quotation")
        ]
        self.assertEqual(len(quote_calls), 2)

    def test_api_error_does_not_include_credentials(self) -> None:
        def failing(url: str, headers: dict, payload: dict | None, timeout: int) -> dict:
            if url.endswith("get_access_token"):
                return {"errorcode": -1301, "errmsg": "Refresh_Token is expired"}
            raise AssertionError

        client = IFindHTTPClient(refresh_token="never-print-me", transport=failing)
        with self.assertRaises(IFindHTTPError) as context:
            client.get_access_token()
        self.assertNotIn("never-print-me", str(context.exception))
        self.assertIn("-1301", str(context.exception))

    def test_transient_transport_errors_are_retried_with_backoff(self) -> None:
        attempts = 0
        delays: list[float] = []

        def flaky(url: str, headers: dict, payload: dict | None, timeout: int) -> dict:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise IFindHTTPTransientError("temporary TLS handshake timeout")
            return {"errorcode": 0, "data": {"access_token": "access-secret"}}

        client = IFindHTTPClient(
            refresh_token="refresh-secret",
            transport=flaky,
            max_transport_attempts=3,
            retry_backoff_seconds=0.25,
            sleeper=delays.append,
        )
        self.assertEqual(client.get_access_token(), "access-secret")
        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [0.25, 0.5])

    def test_non_transient_transport_error_is_not_retried(self) -> None:
        attempts = 0

        def denied(url: str, headers: dict, payload: dict | None, timeout: int) -> dict:
            nonlocal attempts
            attempts += 1
            raise IFindHTTPError("HTTP 403")

        client = IFindHTTPClient(
            refresh_token="refresh-secret",
            transport=denied,
            sleeper=lambda _: None,
        )
        with self.assertRaises(IFindHTTPError):
            client.get_access_token()
        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
