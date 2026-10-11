from fenrir.server.terminal import Input, Resize, child_env, parse_message, shell_argv


def test_child_env_drops_fenrir_vars():
    env = {"PATH": "/bin", "FENRIR_NT_TOKEN": "x", "FENRIR_PTY_USER": "claude", "HOME": "/h"}
    out = child_env(env, "tok")
    assert "FENRIR_NT_TOKEN" not in out and "FENRIR_PTY_USER" not in out
    assert out["PATH"] == "/bin" and out["HOME"] == "/h"


def test_child_env_drops_any_var_holding_token():
    out = child_env({"TOKEN": "abc-tok-def", "OTHER": "fine"}, "tok")
    assert "TOKEN" not in out
    assert out["OTHER"] == "fine"


def test_child_env_empty_token_keeps_env():
    assert child_env({"PATH": "/bin", "FENRIR_X": "y"}, "") == {
        "PATH": "/bin",
        "TERM": "xterm-256color",
    }


def test_child_env_sets_term_and_leaves_input_alone():
    env = {"FENRIR_X": "y"}
    assert child_env(env, "tok")["TERM"] == "xterm-256color"
    assert env == {"FENRIR_X": "y"}


def test_shell_argv():
    assert shell_argv({"SHELL": "/bin/zsh"}, "darwin") == ["/bin/zsh"]
    assert shell_argv({}, "linux") == ["/bin/bash"]
    assert shell_argv({"SHELL": ""}, "linux") == ["/bin/bash"]
    assert shell_argv({"SHELL": "/bin/zsh"}, "win32") == ["powershell.exe"]


def test_parse_message_input():
    assert parse_message('{"type":"input","data":"ls\\r"}') == Input("ls\r")


def test_parse_message_resize_clamped():
    assert parse_message('{"type":"resize","cols":120,"rows":40}') == Resize(120, 40)
    assert parse_message('{"type":"resize","cols":1000000000,"rows":-5}') == Resize(1000, 1)
    assert parse_message('{"type":"resize","cols":0,"rows":1000}') == Resize(1, 1000)


def test_parse_message_rejects_garbage():
    assert parse_message("not json") is None
    assert parse_message('{"type":"nope"}') is None
    assert parse_message('{"type":"input","data":5}') is None
    assert parse_message('{"type":"resize","cols":"a","rows":2}') is None
    assert parse_message('{"type":"resize","cols":2.5,"rows":2}') is None
    assert parse_message('{"type":"resize","cols":true,"rows":2}') is None
    assert parse_message('{"type":"resize","cols":2}') is None
    assert parse_message('{"type":"resize","cols":' + "9" * 5000 + ',"rows":2}') is None
    assert parse_message("[]") is None
    assert parse_message("") is None
    assert parse_message("[" * 100_000) is None  # RecursionError, not a crash
