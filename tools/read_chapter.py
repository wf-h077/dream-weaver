from memory import StoryBible

_bible = StoryBible()

def read_chapter(chapter_num: int, novel_id: str | None = None) -> str:
    """读取已完成的章节正文。"""
    print(f"    📖 [Tools] 模型主动触发翻阅历史: 第{chapter_num}章")
    bible = StoryBible(novel_id) if novel_id else _bible
    content = bible.get_chapter_content(chapter_num)
    if not content:
        return f"读取失败：第{chapter_num}章尚未生成或不存在。"
    # 如果正文太长，可以在这里截断或者只返回开头结尾，但通常 LLM 的上下文够长。
    return f"【第{chapter_num}章内容】：\n{content}"

READ_CHAPTER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_chapter",
        "description": "当你想准确回顾某一个特定历史章节的对话、氛围或详细情节以保证剧情原汁原味地继续时，可以调用此工具读取该章的原文。",
        "parameters": {
            "type": "object",
            "properties": {
                "chapter_num": {
                    "type": "integer",
                    "description": "要读取的历史章节号，如 10"
                }
            },
            "required": ["chapter_num"]
        }
    }
}
