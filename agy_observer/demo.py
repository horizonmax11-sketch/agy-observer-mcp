"""Offline preview. Emits labeled synthetic events; never launches agy or calls a model."""

import argparse
import asyncio
from pathlib import Path

from .monitor import Monitor
from .runtime import RunRecord
from .discussions import Discussions
from .council import Council


async def preview(args):
    monitor = Monitor(args.state_dir, args.port)
    print(
        monitor.start()
        + "?tests=1"
        + ("&view=discussions" if args.collaboration or args.council else ""),
        flush=True,
    )
    if args.open:
        await monitor.open(
            include_tests=True, view="discussions" if args.collaboration or args.council else None
        )
    if args.council:
        import json

        council = Council(Discussions(args.state_dir))
        identifier = council.create(
            "离线模拟：三方独立判断后共同完善方案",
            "全部内容为合成数据，没有调用模型。",
            {"gemini": "simulated-gemini", "claude": "simulated-claude"},
            kind="离线模拟三方协作",
        )["discussion_id"]
        council.note(
            identifier,
            "模拟初判：需要验证讨论是否产生新信息。",
            "initial",
            initial_context="unknown",
        )
        initial = council.reserve(identifier, ["gemini", "claude"])
        for participant, message_id, _, _ in initial:
            council.finish(
                identifier,
                message_id,
                "success",
                json.dumps(
                    dict(
                        message="模拟初判："
                        + (
                            "应保留分歧，避免多数即正确。"
                            if participant == "gemini"
                            else "要比较发言顺序改变后的结果。"
                        ),
                        kind="observation",
                    )
                ),
            )
        council.reveal(identifier)
        council.note(
            identifier,
            "保留首轮判断；围绕证据讨论；交换顺序进行对照；未解决项保留。",
            "plan",
            [1, 2, 3],
            expected_plan_version=0,
        )
        batch = council.reserve(
            identifier, ["gemini", "claude"], "检查草案遗漏的前提，可pass，不要求一致。", 4
        )
        for participant, message_id, _, _ in batch:
            council.finish(
                identifier,
                message_id,
                "success",
                json.dumps(
                    dict(
                        message="模拟补充："
                        + (
                            "还需限制轮次，记录新增信息。"
                            if participant == "gemini"
                            else "一次成功不能证明避免了迎合，需要对照。"
                        ),
                        kind="challenge",
                        references=[4],
                        evidence="离线预设示例，未作实际评估。",
                    )
                ),
            )
        council.note(
            identifier,
            "增加次数上限和对照验证；保留初判、分歧及修改原因。",
            "plan",
            [4, 5, 6],
            change_reason="补充了成本与验证不足两个角度。",
            expected_plan_version=1,
        )
        council.note(identifier, "离线演示归档，不代表真实模型效果。", "summary")
    elif args.collaboration:
        discussions = Discussions(args.state_dir)
        import json

        task = discussions.create(
            "离线演示：双方主动提问、补证和修正观点",
            "共同提出方案和问题，负责可执行验证。",
            "共同提出方案和问题，独立质疑并修正自己。",
            2,
            kind="离线模拟协作",
        )
        identifier = task["discussion_id"]
        first, _ = discussions.reserve(identifier)
        discussions.finish(
            identifier,
            first,
            "success",
            json.dumps(
                dict(
                    message="模拟发言：我先提出一个担忧：未答问题可能累积，占满上下文。我的初步方案是超时就归并为冲突。",
                    question="你有哪些证据证明未答问题一定是双方冲突？",
                    answers_to=[],
                ),
                ensure_ascii=False,
            ),
        )
        discussions.note(
            identifier,
            "没有这样的证据。未答也可能来自材料不足或额度耗尽，不能据此判定冲突。",
            "answer",
            first,
        )
        discussions.note(
            identifier,
            "我不同意自动归并：这可能抹去原问题和少数意见。建议保留原文，显式列出本轮省略项。",
            "challenge",
            first,
        )
        discussions.note(
            identifier,
            "如果次数用尽仍有分歧，你是否愿意保留未解决，而不是强行产出共识？",
            "question",
            first,
        )
        second, _ = discussions.reserve(identifier)
        discussions.finish(
            identifier,
            second,
            "success",
            json.dumps(
                dict(
                    message="模拟发言：我修正“未答即冲突”的判断。愿意保留未解决并列出证据缺口；上下文筛选不能等同删除问题。",
                    question=None,
                    answers_to=[4],
                ),
                ensure_ascii=False,
            ),
        )
        discussions.note(
            identifier,
            "离线归档：展示了对方先开题、我方回应与质疑、对方修正。全部为模拟，未调用模型。",
            "summary",
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
    parser.add_argument(
        "--council",
        action="store_true",
        help="Show a synthetic GPT/Gemini/Claude workflow; no models",
    )
    args = parser.parse_args()
    try:
        asyncio.run(preview(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
