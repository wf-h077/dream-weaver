from memory import StoryBible

_bible = StoryBible()

def search_lore(query: str, novel_id: str | None = None) -> str:
    """检索故事宝典中的关键设定。"""
    print(f"    🔍 [Tools] 模型主动触发检索知识库: '{query}'")
    bible = StoryBible(novel_id) if novel_id else _bible
    result = bible.recall(query, top_k=5)
    if not result or result.startswith("（"):
        return f"查询 '{query}' 结果：未找到相关设定，可能该事物尚未出现，请可以自行合理创造。"
    return f"查询 '{query}' 结果：\n{result}"

# OpenAI 工具定义格式
SEARCH_LORE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_lore",
        "description": "当你遇到不确定的往期事件、角色身世、人物关系或物品法宝名字时，你可以调用此工具在故事宝典中搜索查证，防止设定吃书。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要查询的核心名词或短语，尽可能简练。例如：'主角的本命法宝' 或 '青云宗掌门是谁'"
                }
            },
            "required": ["query"]
        }
    }
}
