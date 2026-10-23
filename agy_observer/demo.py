"""Offline preview. Emits labeled synthetic events; never launches agy or calls a model."""

import argparse
import asyncio
from pathlib import Path

from .monitor import Monitor
from .runtime import RunRecord


async def preview(args):
    monitor = Monitor(args.state_dir, args.port)
    print(monitor.start() + "?tests=1", flush=True)
    if args.open:
        await monitor.open(include_tests=True)
    record = RunRecord(
        Path(args.state_dir) / "runs", "离线演示：为什么需要调用历史？", "none", "离线模拟"
    )
    record.state["state"] = "running"
    record.emit("simulation_start")
    for text in [
        "这是离线演示，不是模型回复。",
        "调用历史让用户查看每次任务、公开回复、警告与终态。",
    ]:
        await asyncio.sleep(1)
        record.state["response"] += text + "\n"
        record.emit("simulated_response", text_delta=text)
    record.finish("success", exit_code=0, conversation_id="SIMULATED")
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.to_thread(monitor.close)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(preview(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
