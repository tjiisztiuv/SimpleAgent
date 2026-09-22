"""`sa serve` 的启动体验：端口被占用时说人话，退出时把端口还回去。

起因：端口被上一次残留的 serve 进程占着时，用户只看到裸的
`OSError: [Errno 48] Address already in use`，既不知道谁占的，也不知道能换端口。
"""

from __future__ import annotations

import socket

from simpleagent import cli
from simpleagent.serve import app as app_module


def _occupy() -> socket.socket:
    """占住一个随机端口，模拟上一次 sa serve 没退干净。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def _port_is_free(port: int) -> bool:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_serve_explains_when_port_is_taken(config, sa_home, monkeypatch, capsys):
    sock = _occupy()
    port = sock.getsockname()[1]
    monkeypatch.setattr(cli, "load_config", lambda: config)
    try:
        code = cli.main(["serve", "--port", str(port)])
    finally:
        sock.close()

    assert code == 1
    err = capsys.readouterr().err
    assert str(port) in err
    assert "占用" in err
    assert "lsof" in err, "应当告诉用户怎么查占用者"
    assert f"--port {port + 1}" in err, "应当给出可执行的替代方案"


def test_serve_releases_port_on_exit(config, sa_home, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: config)
    started: list[int] = []
    real_make_server = app_module.make_server

    def make_server_then_ctrl_c(cfg, host="127.0.0.1", port=8384):
        httpd = real_make_server(cfg, host=host, port=0)
        started.append(httpd.server_address[1])

        def interrupt() -> None:
            raise KeyboardInterrupt

        httpd.serve_forever = interrupt  # type: ignore[method-assign]
        return httpd

    monkeypatch.setattr(app_module, "make_server", make_server_then_ctrl_c)
    assert cli.main(["serve"]) == 0

    assert started, "serve 没有真的起来"
    assert _port_is_free(started[0]), "退出后端口没释放"


def test_serve_default_port_differs_in_dev_mode(config, sa_home, monkeypatch, capsys):
    """开发版和日常版的 sa serve 默认端口错开，两个能同时开。"""
    monkeypatch.setattr(cli, "load_config", lambda: config)
    ports: list[int] = []
    real_make_server = app_module.make_server

    def record_port(cfg, host="127.0.0.1", port=cli.SERVE_PORT):
        ports.append(port)
        httpd = real_make_server(cfg, host=host, port=0)

        def interrupt() -> None:
            raise KeyboardInterrupt

        httpd.serve_forever = interrupt  # type: ignore[method-assign]
        return httpd

    monkeypatch.setattr(app_module, "make_server", record_port)

    monkeypatch.setattr(cli, "dev_checkout", lambda: None)
    assert cli.main(["serve"]) == 0
    assert "开发模式" not in capsys.readouterr().out

    monkeypatch.setattr(cli, "dev_checkout", lambda: sa_home)
    assert cli.main(["serve"]) == 0
    out = capsys.readouterr().out
    assert "开发模式" in out and str(sa_home) in out

    assert ports == [cli.SERVE_PORT, cli.DEV_SERVE_PORT]
