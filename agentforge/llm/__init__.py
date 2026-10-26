"""模型接入层（llm）：把「跟模型说话」这件事收在一处。

    ollama.py      Ollama 原生 /api/chat 客户端（流式、工具调用、思考过滤、
                   空闲超时与串流观测）。本项目的默认后端。
    providers.py   OpenAI / Anthropic 兼容接口适配，以及按 provider 分发的
                   ClientHub——上层只跟 `hub.chat_stream(spec, ...)` 打交道，
                   不关心背后是 Ollama 还是哪家的 HTTP 接口。
    streamprobe.py 流式观测哨：长任务卡住时，「等锁多久 / 收到多少字 /
                   最后一行是几秒前」要能说出来，否则只剩「超时」两个字。

判据：凡是与「具体某家的 HTTP 协议」打交道的都在这里；编排、闸门、台账
都不应直接拼 HTTP 请求。

注意历史命名：本包原为单文件 `agentforge/llm.py`（Ollama 客户端），拆包后
它改名为 `ollama.py`——因为包里现在还有别家 provider，叫 llm.py 会误导。
"""
