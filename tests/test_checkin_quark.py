import io
import unittest
from contextlib import redirect_stdout

from checkIn_Quark import (
    ConfigError,
    Quark,
    QuarkAPIError,
    extract_params,
    main,
    parse_account,
    split_account_entries,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise QuarkAPIError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ParsingTests(unittest.TestCase):
    def test_split_accounts_supports_newlines_crlf_and_double_ampersand(self):
        entries = split_account_entries(" one \r\n\r\n two && three ")
        self.assertEqual(entries, ["one", "two", "three"])

    def test_extract_params_from_captured_url(self):
        params = extract_params(
            "https://drive-m.quark.cn/path?foo=1&kps=a%2Bb&sign=s&vcode=v"
        )
        self.assertEqual(params, {"kps": "a+b", "sign": "s", "vcode": "v"})

    def test_extract_params_preserves_literal_plus_in_new_captured_url(self):
        params = extract_params(
            "https://drive-m.quark.cn/1/clouddrive/act/growth/reward"
            "?mt=token+part&kps=AASx+abc%2Bencoded%3D"
            "&sign=AAQH+sig%2Bencoded%3D&vcode=1790667254217&app=clouddrive"
        )
        self.assertEqual(params["kps"], "AASx+abc+encoded=")
        self.assertEqual(params["sign"], "AAQH+sig+encoded=")
        self.assertEqual(params["vcode"], "1790667254217")

    def test_parse_account_supports_legacy_format(self):
        account = parse_account("user=张三; kps=k; sign=s; vcode=v;", 1)
        self.assertEqual(account["user"], "张三")
        self.assertEqual(account["kps"], "k")

    def test_parse_account_supports_captured_url_format(self):
        account = parse_account(
            "user=李四; url=https://example.test/reward?kps=k&sign=s&vcode=v;",
            1,
        )
        self.assertEqual(account["sign"], "s")

    def test_parse_account_reports_missing_parameters(self):
        with self.assertRaisesRegex(ConfigError, "sign, vcode"):
            parse_account("user=张三; kps=k;", 1)

    def test_bark_only_line_is_not_treated_as_an_account(self):
        raw = (
            "bark_server=https://api.day.app; bark_key=KEY;\n"
            "user=A; kps=k; sign=s; vcode=v;"
        )
        self.assertEqual(
            split_account_entries(raw), ["user=A; kps=k; sign=s; vcode=v;"]
        )


class FlakySession:
    """Fails the first `fail_times` calls, then succeeds."""

    def __init__(self, responses, fail_times=0, exc=None):
        self.responses = list(responses)
        self.fail_times = fail_times
        self.exc = exc or __import__("requests").Timeout("boom")
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if len(self.calls) <= self.fail_times:
            raise self.exc
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.account = {"user": "测试", "kps": "k", "sign": "s", "vcode": "v"}

    def test_already_signed_is_successful(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "88VIP": False,
                            "total_capacity": 1024,
                            "cap_composition": {"sign_reward": 768},
                            "cap_sign": {
                                "sign_daily": True,
                                "sign_daily_reward": 256,
                                "sign_progress": 3,
                                "sign_target": 7,
                            },
                        }
                    }
                ),
            ]
        )
        result = Quark(self.account, session=session).do_sign()
        self.assertIn("今日已签到", result)
        # 签到后需重新拉取 growth/info，让通知里的累计容量反映最新状态
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])
        self.assertIn("768.00 B", result)

    def test_unsigned_account_posts_sign_request(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "88VIP": True,
                            "total_capacity": 2048,
                            "cap_composition": {},
                            "cap_sign": {
                                "sign_daily": False,
                                "sign_progress": 2,
                                "sign_target": 7,
                            },
                        }
                    }
                ),
                FakeResponse({"data": {"sign_daily_reward": 1024}}),
                FakeResponse(
                    {
                        "data": {
                            "88VIP": True,
                            "total_capacity": 3072,
                            "cap_composition": {"sign_reward": 1024},
                            "cap_sign": {
                                "sign_daily": True,
                                "sign_daily_reward": 1024,
                                "sign_progress": 3,
                                "sign_target": 7,
                            },
                        }
                    }
                ),
            ]
        )
        result = Quark(self.account, session=session).do_sign()
        # 刷新后状态已变为"今日已签到"，这才是签到后的真实情况
        self.assertIn("今日已签到", result)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "GET"])
        # 刷新后应报告签到后的进度与累计容量
        self.assertIn("3/7", result)
        self.assertIn("1.00 KB", result)

    def test_api_error_is_not_treated_as_success(self):
        session = FakeSession([FakeResponse({"code": 401, "message": "凭证失效"})])
        with self.assertRaisesRegex(QuarkAPIError, "凭证失效"):
            Quark(self.account, session=session).do_sign()

    def test_transient_timeout_is_retried_then_succeeds(self):
        payload = {
            "data": {
                "88VIP": False,
                "total_capacity": 1024,
                "cap_composition": {"sign_reward": 512},
                "cap_sign": {
                    "sign_daily": True,
                    "sign_daily_reward": 256,
                    "sign_progress": 2,
                    "sign_target": 7,
                },
            }
        }
        session = FlakySession([FakeResponse(payload), FakeResponse(payload)], fail_times=2)
        client = Quark(self.account, session=session, retry_backoff=0)
        with redirect_stdout(io.StringIO()) as out:
            result = client.do_sign()
        self.assertIn("今日已签到", result)
        # 前两次调用失败并重试；成功后 do_sign 还会再拉一次 info 刷新容量
        self.assertEqual(len(session.calls), 4)
        self.assertIn("重试", out.getvalue())

    def test_permanent_failure_reports_after_all_retries(self):
        session = FlakySession([], fail_times=99)
        client = Quark(self.account, session=session, retries=2, retry_backoff=0)
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(QuarkAPIError, "已重试 2 次"):
                client.do_sign()
        self.assertEqual(len(session.calls), 2)

    def test_non_retryable_status_fails_immediately(self):
        import requests

        response = FakeResponse({"code": 403}, status_code=403)
        error = requests.HTTPError("forbidden")
        error.response = response
        session = FlakySession([response], fail_times=99, exc=error)
        client = Quark(self.account, session=session, retries=3, retry_backoff=0)
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(QuarkAPIError, "HTTP 403"):
                client.do_sign()
        self.assertEqual(len(session.calls), 1)

    def test_sign_failure_raises(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "88VIP": False,
                            "total_capacity": 1024,
                            "cap_composition": {},
                            "cap_sign": {
                                "sign_daily": False,
                                "sign_progress": 1,
                                "sign_target": 7,
                            },
                        }
                    }
                ),
                FakeResponse({"code": 500, "message": "服务异常"}),
            ]
        )
        with self.assertRaisesRegex(QuarkAPIError, "服务异常"):
            Quark(self.account, session=session).do_sign()


class MainTests(unittest.TestCase):
    def test_multi_account_continues_after_one_failure_and_returns_nonzero(self):
        seen = []

        class StubQuark:
            def __init__(self, account):
                self.account = account

            def do_sign(self):
                seen.append(self.account["user"])
                if self.account["user"] == "坏账号":
                    raise QuarkAPIError("凭证失效")
                return "✅ 签到成功"

        raw = (
            "user=坏账号;kps=1;sign=1;vcode=1;\n"
            "user=好账号;kps=2;sign=2;vcode=2;"
        )
        with redirect_stdout(io.StringIO()):
            exit_code = main(raw, quark_factory=StubQuark)

        self.assertEqual(exit_code, 1)
        self.assertEqual(seen, ["坏账号", "好账号"])

    def test_all_accounts_success_returns_zero(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account

            def do_sign(self):
                return "✅ 签到成功"

        with redirect_stdout(io.StringIO()):
            exit_code = main("user=账号;kps=1;sign=1;vcode=1;", quark_factory=StubQuark)
        self.assertEqual(exit_code, 0)

    def test_missing_environment_returns_configuration_error(self):
        with redirect_stdout(io.StringIO()):
            exit_code = main("")
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()