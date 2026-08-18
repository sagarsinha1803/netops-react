# cat troubleshoot_agent_mcp.py
# Reconstructed from your screenshots -- verify against your original.
"""
Don't run this MCP server directly from the base machine.
Escalation will arise.
"""

from fastmcp import FastMCP
from pydantic import Field
from typing import Annotated, Literal
import yaml


# credentials.yml sits at the project root, one level up from mcp_tools/, so
# this finds it whatever directory the server is launched from
import os

_CREDENTIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "credentials.yml")


class Settings:

    TRAP_HTTP_EXCEPTIONS = True
    ERROR_404_HELP = True
    BUNDLE_ERRORS = True

    try:
        with open(_CREDENTIALS, "r") as f:
            credentials = yaml.safe_load(f)
    except FileNotFoundError:
        credentials = {}

    SSH_JUMPHOST_DETAILS = credentials.get("SSH_JUMPHOST_DETAILS", {})
    DEVICE_DETAILS_SSH = credentials.get("DEVICE_DETAILS_SSH", {})


mcp = FastMCP("device-troubleshooting-server")


@mcp.prompt(
    name="system_prompt",
    description="System prompt for the device troubleshooter agent.")
def system_prompt() -> str:
    return """
    You are a Network CLI Assistant

    Your job is to interact with network devices by:
      - Interpreting natural language requests
      - Converting them into safe, read-only CLI commands
      - Executing them using a secure tool
      - Returning human-friendly summaries of the results

    -------------------------------------------------------------
    Device Details (provided by user):
    {device_details}

    -------------------------------------------------------------
    Workflow:

      Intent Detection:
        - Understand the user's request (e.g., check CPU, OSPF, interface status)

      System Identification:
        - Use the device details provided by the user.

      Cache Check:
        - If (device_ip, region, command) has been executed before, return cached result.

      Command Generation:
        - Convert the request into a valid read-only CLI command.
        - If vendor/OS is known, adjust the command format accordingly.

      Execution:
        - execute_query_on_server(device_ip: str, region: str, command: str, port: int = 22)

      Output Interpretation:
        - Summarize CLI output in clear, user-friendly language.

      Session Management:
        - Retain session state for follow-up queries.
        - Use 'logout' to clear session context.

    -------------------------------------------------------------
    Security Rules:
      Only execute read-only commands: show, ping, traceroute
      Never run configuration or destructive commands: configure terminal, reload, set, etc.
    """


import socket
from paramiko import SSHClient, AutoAddPolicy
from paramiko.ssh_exception import AuthenticationException, SSHException
import logging
import time

logging.getLogger("paramiko").setLevel(logging.CRITICAL)


class BastionError(Exception):
    pass


class Bastion:
    def __init__(self, region, timeout=15, keepalive=30):
        self.host = Settings.SSH_JUMPHOST_DETAILS[region]["IP"]
        self.user = Settings.SSH_JUMPHOST_DETAILS[region]["USERNAME"]
        self.key = Settings.SSH_JUMPHOST_DETAILS[region]["KEY_PATH"]
        self.port = Settings.SSH_JUMPHOST_DETAILS[region].get("PORT", 22)
        self.region = region
        self.password = None
        self.timeout = timeout
        self.keepalive = keepalive
        self.client: SSHClient | None = None

    def open(self):
        try:
            c = SSHClient()
            c.set_missing_host_key_policy(AutoAddPolicy())
            c.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                key_filename=self.key,
                password=self.password,
                look_for_keys=False,
                allow_agent=False,
                timeout=self.timeout,
            )
            c.get_transport().set_keepalive(self.keepalive)
            self.client = c
        except (AuthenticationException, SSHException,
                socket.timeout, OSError) as e:
            self.close()
            raise BastionError(f"Failed to connect bastion: {e}")

    def open_channel(self, target_host, target_port=22):
        if not self.client:
            raise BastionError("Bastion not opened")
        transport = self.client.get_transport()
        dest = (target_host, target_port)
        src = (self.host, 0)
        return transport.open_channel("direct-tcpip", dest, src)

    def close(self):
        try:
            if self.client:
                self.client.close()
        finally:
            self.client = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, *_):
        self.close()
        return False


def _run_composite_sequence(
    client: SSHClient,
    parts: list[str],
    per_cmd_wait: float = 0.3,
    read_window: float = 1.5,
) -> dict:
    """
    Execute a sequence of commands in a single interactive shell.
    ignore_output_for: set of command strings whose output should be discarded
    (case-insensitive). Example: {'bash','exit'}
    """
    ignore_output_for = {"bash", "exit"}   # default ignore list
    norm_ignore = {c.lower().strip() for c in ignore_output_for}

    shell = client.invoke_shell()
    kept_chunks: list[str] = []
    try:
        for part in parts:
            cmd_sent = part.strip()
            shell.send(part + '\n')
            time.sleep(per_cmd_wait)
            end = time.time() + read_window
            chunk = ''
            while time.time() < end:
                while shell.recv_ready():
                    chunk += shell.recv(4096).decode(errors='ignore')
                time.sleep(0.05)
            if cmd_sent.lower() not in norm_ignore:
                kept_chunks.append(chunk)
        if parts[-1].strip().lower() != 'exit':
            shell.send('exit\n')
            time.sleep(0.2)
            exit_chunk = ''
            while shell.recv_ready():
                exit_chunk += shell.recv(4096).decode(errors='ignore')
            if 'exit' not in norm_ignore:
                kept_chunks.append(exit_chunk)
        rc = 0
    except Exception as e:
        kept_chunks.append(f"[COMPOSITE_ERROR] {e}")
        rc = -1
    finally:
        try:
            shell.close()
        except Exception:
            pass
    return {
        "stdout": ''.join(kept_chunks),
        "stderr": "",
        "rc": rc,
    }


@mcp.tool(
    name='execute_query_on_server',
    description='This tool execute read only commands on the networking device',
)
def execute_query_on_server(
    device_ip: Annotated[str, Field(description="IP address of the device")],
    commands: Annotated[list, Field(description="list of command to execute on the device")],
    region: Annotated[Literal['PARIS', 'ASIA', 'AMER', 'UK', 'INDIA', 'IBFS'],
                      Field(description="Region device belongs to")],
    port: Annotated[int, Field(description='port of the device', default=22)] = 22,
):
    """
    device = { 'host': 'x.x.x.x', 'user': 'username',
               'password': 'pwd' | 'key': 'path', 'port': 22 }
    """
    region = region.lower()
    channel = None
    client = SSHClient()
    client.set_missing_host_key_policy(AutoAddPolicy())
    try:
        with Bastion(region.lower()) as bastion:
            channel = bastion.open_channel(device_ip, port)
            client.connect(
                hostname=device_ip,
                port=port,
                username=Settings.DEVICE_DETAILS_SSH[bastion.region]["username"],
                password=Settings.DEVICE_DETAILS_SSH[bastion.region]["password"],
                key_filename=None,
                sock=channel,
                look_for_keys=False,
                allow_agent=False,
                timeout=30,
            )
            results = []
            for cmd in commands:
                if isinstance(cmd, str) and '**' in cmd:
                    parts = [p for p in cmd.split('**') if p.strip()]
                    composite_res = _run_composite_sequence(client, parts)
                    results.append({
                        "cmd": cmd,
                        "stdout": composite_res["stdout"],
                        "stderr": composite_res["stderr"],
                        "rc": composite_res["rc"],
                    })
                else:
                    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
                    out = stdout.read().decode(errors="ignore")
                    err = stderr.read().decode(errors="ignore")
                    rc = stdout.channel.recv_exit_status()
                    results.append({
                        "cmd": cmd,
                        "stdout": out,
                        "stderr": err,
                        "rc": rc,
                    })
            return results
    except Exception as e:
        return {"host": device_ip, "ok": False, "error": str(e)}
    finally:
        try:
            client.close()
        except Exception:
            pass
        if channel:
            try:
                channel.close()
            except Exception:
                pass


if __name__ == "__main__":
    print('nemo Server Started...')
    mcp.run(transport='stdio')
