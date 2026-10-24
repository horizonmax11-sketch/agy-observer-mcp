"""Offline preview. Emits labeled synthetic events; never launches agy or calls a model."""

import argparse
import asyncio
from pathlib import Path

from .monitor import Monitor
from .runtime import RunRecord
from .discussions import Discussions


async def preview(args):
    monitor = Monitor(args.state_dir, args.port)
    print(
        monitor.start() + "?tests=1" + ("&view=discussions" if args.collaboration else ""),
        flush=True,
    )
    if args.open:
        await monitor.open(include_tests=True, view="discussions" if args.collaboration else None)
    if args.collaboration:
        discussions = Discussions(args.state_dir)
        task = discussions.create(
            "离线协作演示：让代码审阅形成反馈闭环",
            "负责方案、实现、测试并记录取舍。",
            "检查缺陷和证据，逐点回应反馈。",
            2,
            kind="离线模拟协作",
        )
        identifier = task["discussion_id"]
        discussions.note(
            identifier,
            "方案：历史列表只保留摘要，详情按需加载。请检查是否会丢失完整回复。",
            "proposal",
        )
        first, _ = discussions.reserve(identifier, "审阅方案并指出风险。", 1)
        discussions.finish(
            identifier,
            first,
            "success",
            "离线模拟回复：建议保留完整 status，并测试摘要不含 response、详情仍可读取。",
        )
        discussions.note(
            identifier,
            "已保留完整状态。请进一步审阅缓存失效条件；此处全部是模拟内容。",
            "feedback",
            first,
        )
        second, _ = discussions.reserve(identifier, "回应缓存失效反馈。", 3)
        discussions.finish(
            identifier,
            second,
            "success",
            "离线模拟回复：使用文件签名识别变化，补充终态更新测试；我没有实际修改或运行代码。",
        )
        discussions.note(
            identifier,
            "采纳缓存失效测试建议，具体实现与测试由主维护者负责。",
            "decision",
            second,
            "accepted",
        )
        discussions.note(
            identifier, "离线演示完成：分工、引用、反馈、决定均可追踪；没有模型请求。", "summary"
        )
    else:
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
    parser.add_argument(
        "--collaboration", action="store_true", help="Show a synthetic two-party review workflow"
    )
    args = parser.parse_args()
    try:
        asyncio.run(preview(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
