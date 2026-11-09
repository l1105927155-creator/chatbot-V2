import importlib.util
import json
import os
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "astrbot-plugins" / "v2_dsh_router" / "error_notices.py"
SPEC = importlib.util.spec_from_file_location("error_notices_under_test", MODULE_PATH)
error_notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(error_notices)


@pytest.fixture
def root_fd(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield tmp_path, fd
    finally:
        os.close(fd)


def put(path, value):
    path.write_text(value, encoding="utf-8")


def test_reads_each_time_and_handles_remove_recreate_and_isolated_roots(root_fd, tmp_path):
    path, fd = root_fd
    other = tmp_path / "other"
    other.mkdir()
    other_fd = os.open(other, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert error_notices.load_overrides(fd) == {}
        put(path / error_notices.FILE_NAME, '{"MODEL_TIMEOUT":"第一次"}')
        assert error_notices.load_overrides(fd) == {"MODEL_TIMEOUT": "第一次"}
        put(path / error_notices.FILE_NAME, '{"MODEL_TIMEOUT":"第二次"}')
        assert error_notices.load_overrides(fd) == {"MODEL_TIMEOUT": "第二次"}
        assert error_notices.load_overrides(other_fd) == {}
        (path / error_notices.FILE_NAME).unlink()
        assert error_notices.load_overrides(fd) == {}
        put(path / error_notices.FILE_NAME, '{"MODEL_TIMEOUT":"重建"}')
        assert error_notices.load_overrides(fd) == {"MODEL_TIMEOUT": "重建"}
    finally:
        os.close(other_fd)


@pytest.mark.parametrize("raw", [
    "not json", "[]", "null", '"text"', '{"bad-code":"x"}',
    '{"lowercase":"x"}', '{"TOO_LONG":""}', '{"OK":null}',
    '{"OK":1}', '{"OK":[]}', '{"OK":"   "}', '{"OK":"bad\\u0001text"}',
    '{"OK":"x","OK":"secret duplicate"}', '{"OK":"\\ud800"}',
])
def test_invalid_configuration_is_rejected_as_a_whole(root_fd, raw):
    path, fd = root_fd
    put(path / error_notices.FILE_NAME, raw)
    assert error_notices.load_overrides(fd) == {}


def test_limits_and_allowed_newline_tab(root_fd):
    path, fd = root_fd
    put(path / error_notices.FILE_NAME, json.dumps({"OK": "line\nwith\ttab"}))
    assert error_notices.load_overrides(fd) == {"OK": "line\nwith\ttab"}
    put(path / error_notices.FILE_NAME, json.dumps({"OK": "x" * 2001}))
    assert error_notices.load_overrides(fd) == {}
    put(path / error_notices.FILE_NAME, " " * (64 * 1024 + 1))
    assert error_notices.load_overrides(fd) == {}


@pytest.mark.parametrize("entry", ["symlink", "hardlink", "fifo"])
def test_refuses_unsafe_entries(root_fd, tmp_path, entry):
    path, fd = root_fd
    target = path / error_notices.FILE_NAME
    source = tmp_path / "outside.json"
    source.write_text('{"OK":"secret"}', encoding="utf-8")
    if entry == "symlink":
        target.symlink_to(source)
    elif entry == "hardlink":
        os.link(source, target)
    else:
        os.mkfifo(target)
    assert error_notices.load_overrides(fd) == {}


def test_failures_log_only_exception_type_and_never_message_body(root_fd, caplog):
    path, fd = root_fd
    put(path / error_notices.FILE_NAME, '{"OK":"private-body-secret","OK":"other"}')
    assert error_notices.load_overrides(fd) == {}
    assert "ValueError" in caplog.text
    assert "private-body-secret" not in caplog.text
    assert "other" not in caplog.text


def test_full_custom_notice_replaces_default_and_invalid_values_fall_back():
    default = "默认提示；请稍后重试"
    assert error_notices.render_notice({"MODEL_TIMEOUT": "完整自定义提示"},
                                       "MODEL_TIMEOUT", default) == "完整自定义提示"
    assert error_notices.render_notice({"MODEL_TIMEOUT": ""},
                                       "MODEL_TIMEOUT", default) == default
    assert error_notices.render_notice({"OTHER": "覆盖"}, "MODEL_TIMEOUT", default) == default
    assert error_notices.render_notice({"bad-code": "覆盖"}, "bad-code", default) == default
