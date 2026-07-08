import asyncio
import os

from agents import Agent, Runner, set_default_openai_api, set_default_openai_client, set_tracing_disabled
from dotenv import load_dotenv
from openai import AsyncOpenAI


def configure_model_client() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")

    if not api_key:
        raise RuntimeError(
            "缺少 OPENAI_API_KEY。请复制 .env.example 为 .env，并把你的 API key 填进去。"
        )

    if base_url:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        set_default_openai_client(client, use_for_tracing=False)

    set_default_openai_api("chat_completions")
    set_tracing_disabled(True)


def build_agent() -> Agent:
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    return Agent(
        name="qa_agent",
        model=model,
        instructions=(
            "你是一个简洁、可靠的中文问答助手。"
            "优先直接回答用户问题。"
            "如果信息不足，先说明缺少什么，不要编造。"
            "如果问题涉及步骤，请用清晰的短步骤回答。"
        ),
    )


async def ask_once(agent: Agent, question: str) -> str:
    result = await Runner.run(agent, question)
    return result.final_output


async def main() -> None:
    load_dotenv()

    configure_model_client()
    agent = build_agent()

    print("问答 Agent 已启动。输入问题后回车；输入 exit 或 quit 退出。")
    while True:
        question = input("\n你：").strip()
        if question.lower() in {"exit", "quit"}:
            print("已退出。")
            break
        if not question:
            continue

        answer = await ask_once(agent, question)
        print(f"\nAgent：{answer}")


if __name__ == "__main__":
    asyncio.run(main())
