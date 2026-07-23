"""
Offline regression tests for the Groq schema-validation fix.

Run with:
  MYSQL_HOST=x MYSQL_USER=x MYSQL_PASSWORD=x MYSQL_DB=x SECRET_KEY=x OPENAI_API_KEY=x \
  PYTHONPATH=/home/claude/work/stubs python3 test_schema_fix.py

Uses lightweight stub packages (pymysql/barcode/apscheduler/openai) so the
real app.py can be imported without a live MySQL server or model API key —
this exercises the actual functions that were changed, not reimplementations
of them.
"""
import sys
import types
import json

import app as appmod


def test_no_lingering_union_types():
    """The whole point of the fix: Groq rejects `"type": [...]` arrays.
    Confirm zero tool-schema fields still declare one."""
    for tool in appmod.ASSISTANT_TOOLS:
        props = tool.get('input_schema', {}).get('properties', {})
        for field, spec in props.items():
            t = spec.get('type')
            assert not isinstance(t, list), (
                f"{tool['name']}.{field} still has a union type: {t}"
            )
    print("PASS: no tool-schema field uses a union type")


def test_fallback_message_localization():
    for key in ('too_many_steps', 'quota_exceeded', 'rate_limited', 'unreachable'):
        de = appmod._fallback_message(key, 'de')
        en = appmod._fallback_message(key, 'en')
        ar = appmod._fallback_message(key, 'ar')
        assert de and en and ar, f"missing translation for {key}"
        assert de != en != ar, f"{key} not actually localized (identical strings)"
    # unknown lang falls back to English, doesn't crash / return empty
    assert appmod._fallback_message('unreachable', 'fr') == appmod._fallback_message('unreachable', 'en')
    print("PASS: fallback messages are localized de/en/ar with safe fallback")


def test_schema_validation_error_detector():
    real_groq_error = Exception(
        "tool call validation failed: parameters for tool quick_sell did not "
        "match schema: errors: [`/quantity`: expected integer, but got string]"
    )
    unrelated_error = Exception("Connection timed out")
    assert appmod._is_tool_schema_validation_error(real_groq_error) is True
    assert appmod._is_tool_schema_validation_error(unrelated_error) is False
    print("PASS: schema-validation error detector matches the real Groq error text")


def test_retry_wrapper_recovers_on_second_attempt():
    """Simulates exactly what happened in production: first call raises the
    Groq schema error, retry succeeds. Confirms the wrapper (a) catches it,
    (b) appends a corrective system message, (c) returns the retry's result
    unchanged, without calling the client a third time."""
    calls = []

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        raise Exception(
                            "tool call validation failed: parameters for tool "
                            "quick_sell did not match schema: errors: "
                            "[`/quantity`: expected integer, but got string]"
                        )
                    return {"ok": True, "attempt": len(calls)}

    result = appmod._create_completion_with_schema_retry(
        FakeClient,
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": "sell item X for 30"}],
    )
    assert len(calls) == 2, f"expected exactly 2 attempts, got {len(calls)}"
    assert result == {"ok": True, "attempt": 2}
    assert len(calls[1]['messages']) == len(calls[0]['messages']) + 1
    assert 'quoted string' in calls[1]['messages'][-1]['content']
    print("PASS: retry wrapper recovers after one Groq schema-validation failure")


def test_retry_wrapper_gives_up_after_second_failure():
    """If the retry ALSO fails, the exception must propagate (not loop
    forever, not swallow the error silently)."""
    calls = []

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs)
                    raise Exception("tool call validation failed: parameters for tool quick_sell did not match schema")

    threw = False
    try:
        appmod._create_completion_with_schema_retry(
            FakeClient, model="m", messages=[{"role": "user", "content": "x"}]
        )
    except Exception:
        threw = True
    assert threw, "expected the second failure to propagate"
    assert len(calls) == 2, f"expected exactly 2 attempts (no infinite retry), got {len(calls)}"
    print("PASS: retry wrapper gives up cleanly after a second failure (no infinite loop)")


def test_retry_wrapper_passthrough_for_unrelated_errors():
    """A non-schema error (e.g. real rate limiting) must NOT trigger a
    retry — only this specific failure mode should."""
    calls = []

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs)
                    raise Exception("Rate limit exceeded, please slow down")

    threw = False
    try:
        appmod._create_completion_with_schema_retry(
            FakeClient, model="m", messages=[{"role": "user", "content": "x"}]
        )
    except Exception:
        threw = True
    assert threw
    assert len(calls) == 1, f"unrelated errors must not be retried, got {len(calls)} attempts"
    print("PASS: unrelated errors (e.g. real rate limiting) are not retried")


def test_lang_param_defaults_safely():
    """_run_assistant_chat/_stream must not blow up on a missing/garbage
    lang value — should silently fall back to English."""
    import inspect
    sig = inspect.signature(appmod._run_assistant_chat)
    assert 'lang' in sig.parameters and sig.parameters['lang'].default is None
    sig2 = inspect.signature(appmod._run_assistant_chat_stream)
    assert 'lang' in sig2.parameters and sig2.parameters['lang'].default is None
    print("PASS: lang parameter exists with a safe default on both chat functions")


def test_seller_rest_endpoints_match_seller_allowed_tools():
    """The actual bug this session found: SELLER_ALLOWED_TOOLS said a
    seller could get_summary/list_items/list_low_stock/list_recent_sales/
    list_recent_orders/print_barcode via the AI chat, but the *REST*
    endpoints the button-driven widget calls for those same actions were
    still @login_required('admin')-only — so the AI path worked and the
    button path silently failed for sellers. Read app.py's source directly
    (decorators aren't introspectable after import) and assert each of
    these known routes accepts both roles."""
    src = open('app.py', encoding='utf-8').read()
    routes_that_must_allow_sellers = [
        "/assistant/api/summary",
        "/assistant/api/items', methods=['GET']",
        "/assistant/api/stock/low",
        "/assistant/api/sales/recent",
        "/assistant/api/orders/recent",
        "/admin/items/barcode_print/<barcode_value>",
    ]
    for marker in routes_that_must_allow_sellers:
        idx = src.find(marker)
        assert idx != -1, f"route marker not found (did the route move/get renamed?): {marker}"
        # the @login_required(...) decorator is the very next non-blank line
        following = src[idx:idx + 400]
        deco_start = following.find('@login_required(')
        assert deco_start != -1, f"no @login_required decorator found near: {marker}"
        deco_line = following[deco_start:following.find('\n', deco_start)]
        assert "'seller'" in deco_line or '"seller"' in deco_line, (
            f"route for {marker!r} still admin-only: {deco_line.strip()}"
        )
    print("PASS: every seller-allowed-tool's REST endpoint accepts the seller role")


def test_assistant_js_has_role_awareness():
    """The other half of the same bug: the widget's menu showed every
    category to every role. Confirm the role-gating machinery is actually
    present in the shipped JS (can't execute JS here, so this is a
    structural check, not a behavioral one — see manual QA notes)."""
    js = open('static/js/assistant.js', encoding='utf-8').read()
    assert 'window.ASSISTANT_ROLE' in js, "assistant.js no longer reads window.ASSISTANT_ROLE"
    assert 'SELLER_ALLOWED_TOOLS' in js, "assistant.js no longer mirrors SELLER_ALLOWED_TOOLS"
    assert 'canUseTool' in js, "assistant.js no longer has a canUseTool() gate"
    # spot-check that the main menu actually uses the gate for admin-only categories
    menu_section = js[js.find('function showMainMenu'):js.find('function showMainMenu') + 1500]
    for admin_only_menu in ('menu.factures', 'menu.sellers', 'menu.salary', 'menu.kasse'):
        assert admin_only_menu in menu_section and "ROLE === 'admin'" in menu_section, (
            f"{admin_only_menu} entry in showMainMenu doesn't look role-gated"
        )
    print("PASS: assistant.js has role-awareness wired into the main menu")


def test_templates_expose_role_before_assistant_js_loads():
    """window.ASSISTANT_ROLE must be set *before* the assistant.js <script>
    tag, since assistant.js reads it once at the top of its IIFE — setting
    it after would be a silent no-op (ROLE would already be frozen at
    'seller', the safe default, but not for the reason anyone intended)."""
    for tmpl in ('templates/base.html', 'templates/chat_app.html'):
        html = open(tmpl, encoding='utf-8').read()
        role_idx = html.find('window.ASSISTANT_ROLE')
        script_idx = html.find("js/assistant.js")
        assert role_idx != -1, f"{tmpl}: window.ASSISTANT_ROLE not set at all"
        assert script_idx != -1, f"{tmpl}: assistant.js script tag not found"
        assert role_idx < script_idx, f"{tmpl}: window.ASSISTANT_ROLE is set AFTER assistant.js loads"
    print("PASS: ASSISTANT_ROLE is set before assistant.js loads in both templates")


def test_legacy_barcode_static_path_has_priority():
    """A screenshot showed 404s for /static/barcodes/code_barres_*.png —
    a filename pattern only the purchase-order flow ever actually writes
    to disk; item barcodes were never written there. Rather than track
    down which stale frontend build still requests that exact path,
    barcode_print_legacy_static() catches it and falls back to generating
    the image on the fly. Confirm Werkzeug actually routes requests there
    instead of to the generic /static/<path:filename> handler — a literal
    route can silently lose to the wildcard one if registered wrong."""
    adapter = appmod.app.url_map.bind('127.0.0.1')
    endpoint, args = adapter.match('/static/barcodes/code_barres_d20e89323d36.png', method='GET')
    assert endpoint == 'barcode_print_legacy_static', f"wrong route matched: {endpoint}"
    assert args == {'barcode_value': 'd20e89323d36'}
    print("PASS: legacy barcode static path is caught before the generic static handler")


if __name__ == '__main__':
    tests = [v for k, v in list(globals().items()) if k.startswith('test_')]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests PASSED")
