"""SQLite 故事宝典管理模块

使用 SQLite 替代 ChromaDB，解决 Windows 上的索引损坏问题。
支持全文匹配和分类检索，确保长期记忆的稳定性。
"""
import requests

MCP_SERVER_URL = "http://127.0.0.1:8001/mcp"
DEFAULT_NOVEL_ID = "legacy"

class StoryBible:
    """故事宝典：基于 MCP Client (HTTP) 的长期记忆代理层"""

    def __init__(self, novel_id: str = DEFAULT_NOVEL_ID):
        self.novel_id = novel_id or DEFAULT_NOVEL_ID

    def _payload(self, data: dict | None = None) -> dict:
        payload = {"novel_id": self.novel_id}
        if data:
            payload.update(data)
        return payload

    def _params(self) -> dict:
        return {"novel_id": self.novel_id}

    def _check_server(self):
        """确保 MCP 服务器在线。此为简单验证。实际生产可用更鲁棒的检查"""
        try:
            requests.get(f"http://127.0.0.1:8001/docs", timeout=2)
        except requests.exceptions.RequestException:
            print("⚠️ 无法连接到小说记忆 MCP 服务器，请确保已在新终端运行 `python mcp_server.py` !!!")

    def recall(self, query: str, top_k: int = 5) -> str:
        self._check_server()
        try:
            res = requests.post(f"{MCP_SERVER_URL}/recall", json=self._payload({"query": query, "top_k": top_k}), timeout=10)
            return res.json().get("result", "")
        except:
            return "（MCP 服务离线或查询超时，暂无数据）"

    def summarize_chapter(self, chapter_content: str) -> str:
        self._check_server()
        try:
            res = requests.post(f"{MCP_SERVER_URL}/summarize_chapter", json=self._payload({"chapter_content": chapter_content}), timeout=30)
            return res.json().get("result", "")
        except Exception as e:
            print(f"⚠️ 生成摘要失败: {e}")
            return "摘要生成出错"

    def extract_keywords(self, chapter_outline: str) -> str:
        self._check_server()
        if not chapter_outline: return ""
        try:
            res = requests.post(f"{MCP_SERVER_URL}/extract_keywords", json=self._payload({"chapter_outline": chapter_outline}), timeout=30)
            return res.json().get("result", "")
        except:
            return ""

    def update_from_chapter(self, chapter_num: int, chapter_content: str):
        self._check_server()
        try:
            requests.post(f"{MCP_SERVER_URL}/update_chapter", json={
                "novel_id": self.novel_id,
                "chapter_num": chapter_num,
                "chapter_content": chapter_content
            }, timeout=60)
            print(f"  📚 故事宝典已通过 MCP 更新（第{chapter_num}章）")
        except Exception as e:
            print(f"  ⚠️ MCP 更新失败: {e}")

    def get_chapter_content(self, chapter_num: int) -> str:
        self._check_server()
        try:
            res = requests.get(f"{MCP_SERVER_URL}/chapter/{chapter_num}", params=self._params(), timeout=10)
            return res.json().get("result", "")
        except:
            return ""

    def get_entity_status(self) -> str:
        self._check_server()
        try:
            res = requests.get(f"{MCP_SERVER_URL}/entity_status", params=self._params(), timeout=10)
            return res.json().get("result", "{}")
        except:
            return "{}"

    def update_entity_status(self, new_status_json: str):
        self._check_server()
        try:
            requests.post(f"{MCP_SERVER_URL}/update_entity_status", json=self._payload({"new_status_json": new_status_json}), timeout=10)
            print("  📊 核心角色与物品状态面板通过 MCP 更新完成")
        except Exception as e:
            print(f"  ⚠️ MCP 状态更新失败: {e}")

    def get_entity_cards(self, card_type: str = "") -> list:
        try:
            params = self._params()
            if card_type:
                params["card_type"] = card_type
            return requests.get(f"{MCP_SERVER_URL}/entity_cards", params=params, timeout=5).json().get("result", [])
        except:
            return []

    def save_entity_card(self, card_type: str, name: str, fields: dict, note: str = ""):
        try:
            return requests.post(f"{MCP_SERVER_URL}/entity_cards", json={
                "novel_id": self.novel_id,
                "card_type": card_type,
                "name": name,
                "fields": fields,
                "note": note,
            }, timeout=5).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}

    def delete_entity_card(self, card_type: str, name: str):
        try:
            return requests.delete(
                f"{MCP_SERVER_URL}/entity_cards/{card_type}/{name}",
                params=self._params(),
                timeout=5
            ).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}

    def get_entity_cards_context(self) -> str:
        cards = self.get_entity_cards()
        if not cards:
            return ""
        type_names = {
            "character": "角色卡",
            "item": "物品卡",
            "faction": "势力卡",
            "location": "地点卡",
        }
        lines = ["【结构化设定卡片】"]
        for card in cards[:40]:
            fields = card.get("fields") or {}
            field_text = "；".join(f"{k}: {v}" for k, v in fields.items() if v not in ("", None))
            note = card.get("note") or ""
            lines.append(f"- [{type_names.get(card.get('card_type'), card.get('card_type'))}] {card.get('name')}：{field_text}{('；备注: ' + note) if note else ''}")
        return "\n".join(lines)

    def get_style_profiles(self) -> list:
        try:
            return requests.get(
                f"{MCP_SERVER_URL}/style_profiles",
                params=self._params(),
                timeout=5
            ).json().get("result", [])
        except:
            return []

    def save_style_profile(self, name: str, sample_text: str, fingerprint: dict, is_default: bool = False):
        try:
            return requests.post(f"{MCP_SERVER_URL}/style_profiles", json={
                "novel_id": self.novel_id,
                "name": name,
                "sample_text": sample_text,
                "fingerprint": fingerprint or {},
                "is_default": is_default,
            }, timeout=10).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}

    def set_default_style_profile(self, profile_id: int):
        try:
            return requests.post(
                f"{MCP_SERVER_URL}/style_profiles/{profile_id}/default",
                params=self._params(),
                timeout=5
            ).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}

    def delete_style_profile(self, profile_id: int):
        try:
            return requests.delete(
                f"{MCP_SERVER_URL}/style_profiles/{profile_id}",
                params=self._params(),
                timeout=5
            ).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}

    def get_default_style_profile(self) -> dict | None:
        try:
            return requests.get(
                f"{MCP_SERVER_URL}/style_profiles/default",
                params=self._params(),
                timeout=5
            ).json().get("result")
        except:
            return None

    def get_style_fingerprint_context(self) -> str:
        profile = self.get_default_style_profile()
        if not profile:
            return ""
        fingerprint = profile.get("fingerprint") or {}
        lines = [
            "【默认文风指纹】",
            f"名称：{profile.get('name') or '未命名文风'}",
        ]
        if fingerprint.get("summary"):
            lines.append(f"概括：{fingerprint.get('summary')}")
        if fingerprint.get("writer_instruction"):
            lines.append(f"写作约束：{fingerprint.get('writer_instruction')}")
        must_keep = fingerprint.get("must_keep") or []
        if isinstance(must_keep, list) and must_keep:
            lines.append("必须保留：" + "；".join(str(item) for item in must_keep[:8]))
        must_avoid = fingerprint.get("must_avoid") or []
        if isinstance(must_avoid, list) and must_avoid:
            lines.append("必须避免：" + "；".join(str(item) for item in must_avoid[:8]))
        return "\n".join(lines)

    def clear_database(self):
        self._check_server()
        try:
            requests.post(f"{MCP_SERVER_URL}/clear_database", params=self._params(), timeout=10)
            print("  🧹 故事宝典及待确认库已通过 MCP 重置清空")
        except:
            print("⚠️ 重置 MCP 数据库失败")

    def init_from_setting(self, setting_text: str, chapter_num: int = 0):
        self._check_server()
        try:
            payload = {"setting_text": setting_text}
            if chapter_num:
                payload["chapter_num"] = chapter_num
            requests.post(f"{MCP_SERVER_URL}/init_setting", json=self._payload(payload), timeout=10)
            print("  📚 初始设定已录入故事宝典（MCP Remote）")
        except:
            print("⚠️ 初始设定录入 MCP 失败")

    # ================= 作家工作台 MCP Client API =================

    def get_pending_inspirations(self) -> list:
        try:
            return requests.get(f"{MCP_SERVER_URL}/inspirations/pending", params=self._params(), timeout=5).json().get("result", [])
        except: return []

    def mark_inspiration_used(self, item_id: int):
        try: requests.post(f"{MCP_SERVER_URL}/inspirations/mark_used/{item_id}", params=self._params(), timeout=5)
        except: pass

    def get_pending_plot_hooks(self, chapter_num: int) -> list:
        try:
            return requests.get(f"{MCP_SERVER_URL}/plot_hooks/pending/{chapter_num}", params=self._params(), timeout=5).json().get("result", [])
        except: return []

    def get_world_rules(self) -> list:
        try:
            return requests.get(f"{MCP_SERVER_URL}/world_rules", params=self._params(), timeout=5).json().get("result", [])
        except: return []

    def add_world_rule(self, category: str, rule_text: str):
        try:
            return requests.post(f"{MCP_SERVER_URL}/world_rules", json={
                "novel_id": self.novel_id,
                "category": category,
                "rule_text": rule_text,
            }, timeout=5).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}

    def add_plot_hook(self, content: str, target_chapter: int):
        try:
            return requests.post(f"{MCP_SERVER_URL}/plot_hooks", json={
                "novel_id": self.novel_id,
                "content": content,
                "target_chapter": target_chapter,
            }, timeout=5).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}
        
    def add_pending_extraction(self, category: str, content: str, chapter_num: int):
        try:
            requests.post(f"{MCP_SERVER_URL}/pending_extractions", json={
                "novel_id": self.novel_id, "category": category, "content": content, "chapter_num": chapter_num
            }, timeout=5)
        except: pass

    def add_ai_review(self, chapter_num: int, review_content: str):
        try:
            requests.post(f"{MCP_SERVER_URL}/ai_reviews", json={
                "novel_id": self.novel_id, "chapter_num": chapter_num, "review_content": review_content
            }, timeout=5)
        except: pass

    def add_chapter_version(self, chapter_num: int, source: str, content: str, note: str = ""):
        try:
            return requests.post(f"{MCP_SERVER_URL}/chapter_versions", json={
                "novel_id": self.novel_id,
                "chapter_num": chapter_num,
                "source": source,
                "content": content,
                "note": note,
            }, timeout=10).json()
        except Exception as e:
            print(f"⚠️ 章节版本保存失败: {e}")
            return {"result": "error", "error": str(e)}

    def get_chapter_versions(self, chapter_num: int) -> list:
        try:
            return requests.get(
                f"{MCP_SERVER_URL}/chapter_versions/{chapter_num}",
                params=self._params(),
                timeout=5
            ).json().get("result", [])
        except:
            return []

    def get_chapter_version_detail(self, version_id: int) -> dict:
        try:
            return requests.get(
                f"{MCP_SERVER_URL}/chapter_versions/detail/{version_id}",
                params=self._params(),
                timeout=5
            ).json().get("result", {})
        except:
            return {}

    def add_patch_record(
        self,
        chapter_num: int,
        edit_round: int,
        patch_index: int,
        target_text: str,
        instruction: str,
        replacement_text: str = "",
        success: bool = False,
        reason: str = "",
    ):
        try:
            return requests.post(f"{MCP_SERVER_URL}/patch_records", json={
                "novel_id": self.novel_id,
                "chapter_num": chapter_num,
                "edit_round": edit_round,
                "patch_index": patch_index,
                "target_text": target_text,
                "instruction": instruction,
                "replacement_text": replacement_text,
                "success": success,
                "reason": reason,
            }, timeout=10).json()
        except Exception as e:
            print(f"⚠️ 补丁记录保存失败: {e}")
            return {"result": "error", "error": str(e)}

    def get_patch_records(self, chapter_num: int) -> list:
        try:
            return requests.get(
                f"{MCP_SERVER_URL}/patch_records/{chapter_num}",
                params=self._params(),
                timeout=5
            ).json().get("result", [])
        except:
            return []

    def add_consistency_report(
        self,
        chapter_num: int,
        review_round: int,
        severity: str,
        category: str,
        message: str,
        suggestion: str = "",
        status: str = "open",
    ):
        try:
            return requests.post(f"{MCP_SERVER_URL}/consistency_reports", json={
                "novel_id": self.novel_id,
                "chapter_num": chapter_num,
                "review_round": review_round,
                "severity": severity,
                "category": category,
                "message": message,
                "suggestion": suggestion,
                "status": status,
            }, timeout=5).json()
        except Exception as e:
            print(f"⚠️ 一致性报告保存失败: {e}")
            return {"result": "error", "error": str(e)}

    def get_consistency_reports(self, chapter_num: int) -> list:
        try:
            return requests.get(
                f"{MCP_SERVER_URL}/consistency_reports/{chapter_num}",
                params=self._params(),
                timeout=5
            ).json().get("result", [])
        except:
            return []

    def close_consistency_report(self, report_id: int):
        try:
            return requests.post(
                f"{MCP_SERVER_URL}/consistency_reports/{report_id}/close",
                params=self._params(),
                timeout=5
            ).json()
        except Exception as e:
            return {"result": "error", "error": str(e)}
