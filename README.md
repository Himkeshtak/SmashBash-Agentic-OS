# SmashBash Agentic OS

An operating systems project combining a Python agent with the RISC-V xv6 teaching operating system. The agent converts supported requests into structured tool calls, validates them, and dispatches them to mock tools or an xv6 guest running in QEMU.

## Project layout

- `agent/`: request planning, action validation, tool dispatch, and the QEMU bridge.
- `xv6/`: the xv6 source tree, including its upstream README and license.
- `problem understanding.md`: project requirements and design notes.
- `requirements.txt`: Python dependencies.

## Run the agent

Use Linux or WSL for the QEMU integration, which uses `pexpect` and a Unix pseudo-terminal.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python agent/main.py
```

The default mode uses mock tools. Supported requests include `list`, `read README`, and `quit`.

## Run with xv6

Install a RISC-V compiler toolchain, GNU Make, and `qemu-system-riscv64`. See `xv6/README` for the upstream build instructions.

The imported bridge has a machine-specific `QEMU_PATH` in `agent/bridge.py`; set it to your local QEMU executable before starting the integration:

```sh
python agent/main.py --qemu
```

The bridge starts xv6 with `make qemu`. Guest file reads are restricted to `README`, `report.txt`, and `inbox.txt`; those files must exist in the guest filesystem.

## Run the existing tests

```sh
python -m unittest discover -s agent -p 'test_*.py' -v
```

## Source import

Imported from `OS-Project.zip`. The xv6 sources are included directly so a normal clone contains the project. Archived Git metadata, Python caches, and xv6 build outputs are excluded from version control.

The xv6 portion retains its license in `xv6/LICENSE`.
