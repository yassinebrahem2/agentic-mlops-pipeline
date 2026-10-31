"""A fake SMTP server for tests.

A small asyncio SMTP server on 127.0.0.1:0 (EHLO, AUTH PLAIN/LOGIN, MAIL, RCPT, DATA, QUIT,
and a STARTTLS that answers 454); records each message and can fail the next N sends.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import email
import email.policy
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage


@dataclass
class RecordedEmail:
    from_addr: str
    to_addrs: list[str]
    raw_data: str
    parsed_message: EmailMessage


@dataclass
class FakeSmtpState:
    messages: list[RecordedEmail] = field(default_factory=lambda: list[RecordedEmail]())
    fail_next_sends: int = 0
    fail_auth_message: str | None = None
    fail_rcpt: bool = False

    def reset(self) -> None:
        self.messages.clear()
        self.fail_next_sends = 0
        self.fail_auth_message = None
        self.fail_rcpt = False


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: FakeSmtpState,
) -> None:
    try:
        writer.write(b"220 127.0.0.1 Fake SMTP Service Ready\r\n")
        await writer.drain()

        from_addr = ""
        to_addrs: list[str] = []

        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            parts = line.split(maxsplit=1)
            cmd = parts[0].upper() if parts else ""
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("EHLO", "HELO"):
                writer.write(
                    b"250-127.0.0.1 Hello\r\n"
                    b"250-AUTH LOGIN PLAIN\r\n"
                    b"250-STARTTLS\r\n"
                    b"250-8BITMIME\r\n"
                    b"250 OK\r\n"
                )
                await writer.drain()
            elif cmd == "STARTTLS":
                # STARTTLS answers 454 (TLS not available) per spec
                writer.write(b"454 TLS not available\r\n")
                await writer.drain()
            elif cmd == "AUTH":
                if state.fail_auth_message is not None:
                    writer.write(f"535 5.7.8 {state.fail_auth_message}\r\n".encode())
                    await writer.drain()
                    continue
                subcmd = arg.upper()
                if subcmd.startswith("LOGIN"):
                    writer.write(b"334 VXNlcm5hbWU6\r\n")  # Base64 for 'Username:'
                    await writer.drain()
                    _ = await reader.readline()
                    writer.write(b"334 UGFzc3dvcmQ6\r\n")  # Base64 for 'Password:'
                    await writer.drain()
                    _ = await reader.readline()
                    writer.write(b"235 2.7.0 Authentication successful\r\n")
                    await writer.drain()
                elif subcmd.startswith("PLAIN"):
                    if len(arg.split()) > 1:
                        # Credentials supplied in initial response
                        writer.write(b"235 2.7.0 Authentication successful\r\n")
                    else:
                        writer.write(b"334 \r\n")
                        await writer.drain()
                        _ = await reader.readline()
                        writer.write(b"235 2.7.0 Authentication successful\r\n")
                    await writer.drain()
                else:
                    writer.write(b"504 Unrecognized authentication type\r\n")
                    await writer.drain()
            elif cmd == "MAIL":
                if state.fail_next_sends > 0:
                    state.fail_next_sends -= 1
                    writer.write(b"451 4.3.0 Local error in processing\r\n")
                    await writer.drain()
                    continue
                # Extract address from MAIL FROM:<addr>
                addr = arg
                if ":" in addr:
                    addr = addr.split(":", 1)[1].strip()
                addr = addr.strip("<> ")
                from_addr = addr
                to_addrs = []
                writer.write(b"250 2.1.0 OK\r\n")
                await writer.drain()
            elif cmd == "RCPT":
                if state.fail_rcpt:
                    writer.write(b"550 5.1.1 User unknown\r\n")
                    await writer.drain()
                    continue
                addr = arg
                if ":" in addr:
                    addr = addr.split(":", 1)[1].strip()
                addr = addr.strip("<> ")
                to_addrs.append(addr)
                writer.write(b"250 2.1.5 OK\r\n")
                await writer.drain()
            elif cmd == "DATA":
                writer.write(b"354 Start mail input; end with <CRLF>.<CRLF>\r\n")
                await writer.drain()
                data_lines: list[str] = []
                while True:
                    d_line_bytes = await reader.readline()
                    if not d_line_bytes:
                        break
                    d_line = d_line_bytes.decode("utf-8", errors="replace")
                    if d_line in (".\r\n", ".\n", ".\r"):
                        break
                    # Strip leading dot per SMTP transparency
                    if d_line.startswith(".."):
                        d_line = d_line[1:]
                    data_lines.append(d_line)
                raw_data = "".join(data_lines)
                parsed = email.message_from_string(raw_data, policy=email.policy.default)
                state.messages.append(
                    RecordedEmail(
                        from_addr=from_addr,
                        to_addrs=list(to_addrs),
                        raw_data=raw_data,
                        parsed_message=parsed,
                    )
                )
                writer.write(b"250 2.0.0 OK: message queued\r\n")
                await writer.drain()
            elif cmd == "RSET":
                from_addr = ""
                to_addrs.clear()
                writer.write(b"250 2.0.0 OK\r\n")
                await writer.drain()
            elif cmd == "NOOP":
                writer.write(b"250 2.0.0 OK\r\n")
                await writer.drain()
            elif cmd == "QUIT":
                writer.write(b"221 2.0.0 Bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"500 5.5.1 Command unrecognized\r\n")
                await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


@dataclass
class RunningFakeSmtp:
    host: str
    port: int
    state: FakeSmtpState
    _loop: asyncio.AbstractEventLoop
    _thread: threading.Thread
    _server: asyncio.Server

    def stop(self) -> None:
        async def _close() -> None:
            self._server.close()
            await self._server.wait_closed()

        asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


def start_fake_smtp(state: FakeSmtpState, port: int = 0) -> RunningFakeSmtp:
    loop = asyncio.new_event_loop()
    ready_evt = threading.Event()
    server_holder: list[asyncio.Server] = []
    actual_port_holder: list[int] = []

    def _run() -> None:
        asyncio.set_event_loop(loop)

        async def _start() -> None:
            server = await asyncio.start_server(
                lambda r, w: _handle_client(r, w, state),
                host="127.0.0.1",
                port=port,
            )
            server_holder.append(server)
            actual_port = server.sockets[0].getsockname()[1]
            actual_port_holder.append(actual_port)
            ready_evt.set()

        loop.run_until_complete(_start())
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    ready_evt.wait(timeout=10)

    return RunningFakeSmtp(
        host="127.0.0.1",
        port=actual_port_holder[0],
        state=state,
        _loop=loop,
        _thread=thread,
        _server=server_holder[0],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake SMTP server for tests")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    state = FakeSmtpState()
    server = start_fake_smtp(state, port=args.port)
    print(f"Fake SMTP running on 127.0.0.1:{server.port}")
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
