import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/shared/scripts'))
import xhs_publish as xhs


class ErrorPage:
    def __init__(self, body, url='https://www.xiaohongshu.com/website-login/error'):
        self.body = body
        self.url = url

    def title(self):
        return '安全限制'

    def locator(self, selector):
        return self

    def inner_text(self, timeout):
        return self.body


def test_generic_security_error_does_not_claim_ip_risk():
    result = xhs._login_error(ErrorPage('当前访问受限'))
    assert '尚不能确定是 IP 原因' in result
    assert 'IP 存在风险' not in result
    assert '300012' not in result


def test_reports_observed_ip_error_without_leaking_query_secrets():
    page = ErrorPage('IP存在风险，请切换可靠网络环境后重试',
                     'https://www.xiaohongshu.com/website-login/error?code=300012&token=secret')
    result = xhs._login_error(page)
    assert 'IP 存在风险' in result and '300012' in result
    assert 'secret' not in result and 'token' not in result


def test_dom_error_code_and_navigation_race():
    assert '300012' in xhs._login_error(ErrorPage('IP存在风险\n300012'))

    class Navigating(ErrorPage):
        def title(self):
            raise RuntimeError('Execution context was destroyed')

    assert xhs._login_error(Navigating('')) is None


def test_xhs_defaults_to_direct_even_when_foreign_services_use_proxy(monkeypatch):
    for key in ('https_proxy', 'http_proxy', 'EASEL_PROXY'):
        monkeypatch.setenv(key, 'http://127.0.0.1:7890')
    assert xhs._proxy(None, False) is None
    assert xhs._proxy('http://explicit:1234', False) == 'http://explicit:1234'
    assert xhs._proxy('http://explicit:1234', True) is None
