import sqlite3
import json
import os
import sys
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
from config import SQLITE_DB_PATH
from models import call_llm
from prompts import EXTRACTOR_SYSTEM, EXTRACTOR_PROMPT, SUMMARIZER_PROMPT, RAG_KEYWORD_PROMPT

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

app = FastAPI(title="Novel MCP Server", version="1.0.0")

DEFAULT_NOVEL_ID = "legacy"

class SQLiteManager:
    # P1 2：慢查询日志阈值（毫秒）。超过这个值的 SQL 输出到 stderr 便于排查
    SLOW_QUERY_THRESHOLD_MS = 30
    SLOW_QUERY_LOG_ENABLED = os.environ.get("MCP_SLOW_QUERY_LOG", "0") == "1"

    def __init__(self):
        self.conn = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._init_db()
        # P1 2：包装 cursor.execute，自动记录慢查询（仅在 MCP_SLOW_QUERY_LOG=1 时启用）
        if self.SLOW_QUERY_LOG_ENABLED:
            self._install_slow_query_logger()

    def _install_slow_query_logger(self):
        """monkey-patch cursor.execute 记录超过阈值的慢查询。

        用法：MCP_SLOW_QUERY_LOG=1 启动 mcp_server.py 即可看到 [SLOW] 日志
        """
        original_execute = self.cursor.execute
        threshold = self.SLOW_QUERY_THRESHOLD_MS

        def logged_execute(sql, params=None):
            t0 = time.time()
            try:
                if params is None:
                    return original_execute(sql)
                return original_execute(sql, params)
            finally:
                elapsed_ms = (time.time() - t0) * 1000
                if elapsed_ms >= threshold:
                    sql_short = " ".join(sql.split())[:200]
                    print(
                        f"[SLOW {elapsed_ms:.1f}ms] {sql_short}",
                        file=sys.stderr,
                        flush=True,
                    )

        self.cursor.execute = logged_execute

    def _init_db(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS lore (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                category TEXT,
                content TEXT,
                chapter_num INTEGER
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS chapters (
                novel_id TEXT DEFAULT 'legacy',
                chapter_num INTEGER,
                content TEXT,
                PRIMARY KEY (novel_id, chapter_num)
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity_status (
                novel_id TEXT DEFAULT 'legacy',
                entity_name TEXT,
                status_json TEXT,
                PRIMARY KEY (novel_id, entity_name)
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity_cards (
                novel_id TEXT DEFAULT 'legacy',
                card_type TEXT,
                name TEXT,
                fields_json TEXT,
                note TEXT,
                updated_at REAL,
                PRIMARY KEY (novel_id, card_type, name)
            )
        """)
        # --- 作家工作台新增表 ---
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS inspirations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                content TEXT,
                tags TEXT,
                is_used INTEGER DEFAULT 0
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS plot_hooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                content TEXT,
                target_chapter INTEGER,
                is_triggered INTEGER DEFAULT 0
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS world_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                category TEXT,
                rule_text TEXT
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                category TEXT,
                content TEXT,
                chapter_num INTEGER
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_reviews (
                novel_id TEXT DEFAULT 'legacy',
                chapter_num INTEGER,
                review_content TEXT,
                PRIMARY KEY (novel_id, chapter_num)
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS chapter_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                chapter_num INTEGER,
                source TEXT,
                content TEXT,
                note TEXT,
                created_at REAL
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS patch_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                chapter_num INTEGER,
                edit_round INTEGER,
                patch_index INTEGER,
                target_text TEXT,
                instruction TEXT,
                replacement_text TEXT,
                success INTEGER,
                reason TEXT,
                created_at REAL
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS consistency_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                chapter_num INTEGER,
                review_round INTEGER,
                severity TEXT,
                category TEXT,
                message TEXT,
                suggestion TEXT,
                status TEXT,
                created_at REAL
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS style_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id TEXT DEFAULT 'legacy',
                name TEXT,
                sample_text TEXT,
                fingerprint_json TEXT,
                is_default INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL
            )
        """)
        self._migrate_multi_novel_schema()
        self.conn.commit()

    def _columns(self, table: str) -> dict:
        rows = self.cursor.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1]: row for row in rows}

    def _ensure_novel_column(self, table: str):
        if "novel_id" not in self._columns(table):
            self.cursor.execute(f"ALTER TABLE {table} ADD COLUMN novel_id TEXT DEFAULT 'legacy'")
        self.cursor.execute(
            f"UPDATE {table} SET novel_id=? WHERE novel_id IS NULL OR novel_id=''",
            (DEFAULT_NOVEL_ID,)
        )

    def _recreate_table_with_composite_pk(self, table: str, create_sql: str, expected_pk: list[str], copy_columns: list[str]):
        columns = self._columns(table)
        pk_columns = [row[1] for row in sorted(columns.values(), key=lambda r: r[5]) if row[5] > 0]
        if pk_columns == expected_pk:
            self._ensure_novel_column(table)
            return

        tmp_table = f"{table}_old_migration"
        self.cursor.execute(f"ALTER TABLE {table} RENAME TO {tmp_table}")
        self.cursor.execute(create_sql)

        old_columns = self._columns(tmp_table)
        novel_expr = "COALESCE(novel_id, ?)" if "novel_id" in old_columns else "?"
        source_cols = ", ".join(copy_columns)
        target_cols = ", ".join(["novel_id", *copy_columns])
        self.cursor.execute(
            f"INSERT OR REPLACE INTO {table} ({target_cols}) "
            f"SELECT {novel_expr}, {source_cols} FROM {tmp_table}",
            (DEFAULT_NOVEL_ID,)
        )
        self.cursor.execute(f"DROP TABLE {tmp_table}")

    def _migrate_multi_novel_schema(self):
        for table in ["lore", "inspirations", "plot_hooks", "world_rules", "pending_extractions", "chapter_versions", "patch_records", "consistency_reports", "style_profiles"]:
            self._ensure_novel_column(table)

        self._recreate_table_with_composite_pk(
            "chapters",
            """
            CREATE TABLE chapters (
                novel_id TEXT DEFAULT 'legacy',
                chapter_num INTEGER,
                content TEXT,
                PRIMARY KEY (novel_id, chapter_num)
            )
            """,
            ["novel_id", "chapter_num"],
            ["chapter_num", "content"]
        )
        self._recreate_table_with_composite_pk(
            "entity_status",
            """
            CREATE TABLE entity_status (
                novel_id TEXT DEFAULT 'legacy',
                entity_name TEXT,
                status_json TEXT,
                PRIMARY KEY (novel_id, entity_name)
            )
            """,
            ["novel_id", "entity_name"],
            ["entity_name", "status_json"]
        )
        self._recreate_table_with_composite_pk(
            "ai_reviews",
            """
            CREATE TABLE ai_reviews (
                novel_id TEXT DEFAULT 'legacy',
                chapter_num INTEGER,
                review_content TEXT,
                PRIMARY KEY (novel_id, chapter_num)
            )
            """,
            ["novel_id", "chapter_num"],
            ["chapter_num", "review_content"]
        )
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_lore_novel_category ON lore(novel_id, category)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_chapters_novel_num ON chapters(novel_id, chapter_num)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_versions_novel_chapter ON chapter_versions(novel_id, chapter_num, created_at)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_patch_records_novel_chapter ON patch_records(novel_id, chapter_num, created_at)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_entity_cards_novel_type ON entity_cards(novel_id, card_type)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_consistency_reports_novel_chapter ON consistency_reports(novel_id, chapter_num, created_at)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_style_profiles_novel_default ON style_profiles(novel_id, is_default, updated_at)")
        # P1 2：补全剩余表的 novel_id 索引（之前 inspirations/plot_hooks/world_rules/pending_extractions 全表扫描）
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_inspirations_novel_used ON inspirations(novel_id, is_used)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_plot_hooks_novel_chapter ON plot_hooks(novel_id, target_chapter, is_triggered)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_world_rules_novel ON world_rules(novel_id)")
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_extractions_novel ON pending_extractions(novel_id)")
        # P1 2：复合 PK 已经有 (novel_id, entity_name)，但单独按 entity_name 查的 case 也加个索引
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_entity_status_name ON entity_status(entity_name)")

db = SQLiteManager()


# P1 2：数据库索引健康检查端点（看每个表的所有索引、记录数、是否有冗余索引）
@app.get("/mcp/admin/index_health")
def index_health():
    """返回每个表的索引列表 + 记录数 + 是否使用索引的提示（admin 用）"""
    out = {}
    tables = [
        "lore", "chapters", "entity_status", "entity_cards",
        "inspirations", "plot_hooks", "world_rules", "pending_extractions",
        "ai_reviews", "chapter_versions", "patch_records",
        "consistency_reports", "style_profiles",
    ]
    for t in tables:
        try:
            count = db.cursor.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            idx = db.cursor.execute(f"PRAGMA index_list({t})").fetchall()
            indexes = []
            for row in idx:
                idx_name = row[1]
                cols = db.cursor.execute(f"PRAGMA index_info({idx_name})").fetchall()
                indexes.append({
                    "name": idx_name,
                    "columns": [c[2] for c in cols],
                    "unique": bool(row[2]),
                })
            out[t] = {"count": count, "indexes": indexes}
        except sqlite3.OperationalError as e:
            out[t] = {"error": str(e)}
    return {
        "result": out,
        "slow_query_log_enabled": db.SLOW_QUERY_LOG_ENABLED,
        "slow_query_threshold_ms": db.SLOW_QUERY_THRESHOLD_MS,
    }


# P1 2：开启/关闭慢查询日志（热切换，无需重启 mcp_server）
@app.post("/mcp/admin/slow_query_log")
def toggle_slow_query_log(enable: bool = True, threshold_ms: int = 30):
    if enable and not db.SLOW_QUERY_LOG_ENABLED:
        db.SLOW_QUERY_LOG_ENABLED = True
        db.SLOW_QUERY_THRESHOLD_MS = threshold_ms
        db._install_slow_query_logger()
    elif not enable:
        db.SLOW_QUERY_LOG_ENABLED = False
    return {
        "result": "ok",
        "enabled": db.SLOW_QUERY_LOG_ENABLED,
        "threshold_ms": db.SLOW_QUERY_THRESHOLD_MS,
    }

def infer_card_type(name: str, status: object) -> str:
    text = f"{name} {json.dumps(status, ensure_ascii=False) if not isinstance(status, str) else status}"
    lowered = text.lower()
    if any(word in text for word in ["法宝", "物品", "道具", "武器", "丹药", "灵器", "装备"]) or any(word in lowered for word in ["item", "weapon", "artifact"]):
        return "item"
    if any(word in text for word in ["宗门", "势力", "家族", "公司", "组织", "帮派", "门派"]) or any(word in lowered for word in ["faction", "sect", "clan", "organization"]):
        return "faction"
    if any(word in text for word in ["地点", "城市", "秘境", "洞府", "山", "城", "大陆", "星球"]) or any(word in lowered for word in ["location", "place", "city"]):
        return "location"
    return "character"

def upsert_entity_card(novel_id: str, card_type: str, name: str, fields: dict, note: str = ""):
    import time
    db.cursor.execute(
        """
        INSERT OR REPLACE INTO entity_cards (novel_id, card_type, name, fields_json, note, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (novel_id, card_type, name, json.dumps(fields, ensure_ascii=False), note, time.time())
    )

# --- 请求数据模型 ---
class RecallRequest(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    query: str
    top_k: int = 5

class ChapterContentRequest(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    chapter_content: str

class OutlineRequest(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    chapter_outline: str

class UpdateChapterRequest(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    chapter_num: int
    chapter_content: str

class InitSettingRequest(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    setting_text: str

class UpdateStatusRequest(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    new_status_json: str

class EntityCardReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    card_type: str
    name: str
    fields: dict = {}
    note: str = ""

class StyleProfileReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    name: str
    sample_text: str
    fingerprint: dict = {}
    is_default: bool = False

# 作家工作台相关的请求模型
class InspirationReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    content: str
    tags: str = ""

class PlotHookReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    content: str
    target_chapter: int

class WorldRuleReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    category: str
    rule_text: str

class PendingExtractionReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    category: str
    content: str
    chapter_num: int

class ApproveExtractionToCardReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    card_type: str
    name: str
    fields: dict = {}
    note: str = ""

class AIReviewReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    chapter_num: int
    review_content: str

class ChapterVersionReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    chapter_num: int
    source: str
    content: str
    note: str = ""

class PatchRecordReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    chapter_num: int
    edit_round: int = 0
    patch_index: int = 0
    target_text: str
    instruction: str
    replacement_text: str = ""
    success: bool = False
    reason: str = ""

class ConsistencyReportReq(BaseModel):
    novel_id: str = DEFAULT_NOVEL_ID
    chapter_num: int
    review_round: int = 0
    severity: str
    category: str
    message: str
    suggestion: str = ""
    status: str = "open"

# --- HTTP (MCP 模拟) 端点 ---

@app.post("/mcp/recall")
def mcp_recall(req: RecallRequest):
    if not req.query:
        return {"result": "（没有设定被提及，这是故事的开端。）"}

    keywords = [k.strip() for k in req.query.split(",") if k.strip()]
    if not keywords:
        keywords = [req.query]

    results = []
    categories = {"characters": "### 人物信息", "world_lore": "### 世界设定", "plot_points": "### 剧情节点"}

    for cat_key, cat_name in categories.items():
        params = [req.novel_id, cat_key]
        where_clauses = []
        for kw in keywords:
            where_clauses.append("content LIKE ?")
            params.append(f"%{kw}%")
        
        sql = f"SELECT content FROM lore WHERE novel_id=? AND category=? AND ({' OR '.join(where_clauses)}) ORDER BY chapter_num DESC LIMIT ?"
        params.append(req.top_k)

        db.cursor.execute(sql, tuple(params))
        hits = db.cursor.fetchall()

        if hits:
            results.append(cat_name)
            for (content,) in hits:
                results.append(f"- {content}")

    if not results:
        return {"result": "（故事宝典暂无相关匹配记录。）"}
    return {"result": "\n".join(results)}

@app.get("/mcp/chapter/{chapter_num}")
def mcp_get_chapter(chapter_num: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT content FROM chapters WHERE novel_id=? AND chapter_num=?", (novel_id, chapter_num))
    row = db.cursor.fetchone()
    if row:
        return {"result": row[0]}
    return {"result": ""}

@app.get("/mcp/entity_status")
def mcp_get_entity_status(novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT entity_name, status_json FROM entity_status WHERE novel_id=?", (novel_id,))
    rows = db.cursor.fetchall()
    if not rows:
        return {"result": "{}"}
    status_dict = {}
    for name, status_json in rows:
        try:
            status_dict[name] = json.loads(status_json)
        except:
            pass
    return {"result": json.dumps(status_dict, ensure_ascii=False, indent=2)}

@app.post("/mcp/update_entity_status")
def mcp_update_entity_status(req: UpdateStatusRequest):
    if not req.new_status_json or req.new_status_json.strip() == "{}":
        return {"result": "ok"}
    try:
        status_dict = json.loads(req.new_status_json)
        for name, status in status_dict.items():
            db.cursor.execute(
                "INSERT OR REPLACE INTO entity_status (novel_id, entity_name, status_json) VALUES (?, ?, ?)",
                (req.novel_id, name, json.dumps(status, ensure_ascii=False))
            )
            if isinstance(status, dict):
                upsert_entity_card(req.novel_id, infer_card_type(name, status), name, status, "由章节状态抽取自动同步")
        db.conn.commit()
        return {"result": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/mcp/entity_cards")
def get_entity_cards(novel_id: str = DEFAULT_NOVEL_ID, card_type: str = ""):
    if card_type:
        db.cursor.execute(
            "SELECT card_type, name, fields_json, note, updated_at FROM entity_cards WHERE novel_id=? AND card_type=? ORDER BY updated_at DESC",
            (novel_id, card_type)
        )
    else:
        db.cursor.execute(
            "SELECT card_type, name, fields_json, note, updated_at FROM entity_cards WHERE novel_id=? ORDER BY card_type, updated_at DESC",
            (novel_id,)
        )
    rows = db.cursor.fetchall()
    result = []
    for card_type_value, name, fields_json, note, updated_at in rows:
        try:
            fields = json.loads(fields_json) if fields_json else {}
        except Exception:
            fields = {}
        result.append({
            "card_type": card_type_value,
            "name": name,
            "fields": fields,
            "note": note,
            "updated_at": updated_at,
        })
    return {"result": result}

@app.post("/mcp/entity_cards")
def save_entity_card(req: EntityCardReq):
    if not req.card_type or not req.name:
        raise HTTPException(status_code=400, detail="card_type and name are required")
    upsert_entity_card(req.novel_id, req.card_type, req.name, req.fields, req.note)
    db.conn.commit()
    return {"result": "ok"}

@app.delete("/mcp/entity_cards/{card_type}/{name}")
def delete_entity_card(card_type: str, name: str, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        "DELETE FROM entity_cards WHERE novel_id=? AND card_type=? AND name=?",
        (novel_id, card_type, name)
    )
    db.conn.commit()
    return {"result": "ok"}

def _row_to_style_profile(row) -> dict:
    profile_id, name, sample_text, fingerprint_json, is_default, created_at, updated_at = row
    try:
        fingerprint = json.loads(fingerprint_json) if fingerprint_json else {}
    except Exception:
        fingerprint = {}
    return {
        "id": profile_id,
        "name": name,
        "sample_text": sample_text or "",
        "fingerprint": fingerprint,
        "is_default": bool(is_default),
        "created_at": created_at,
        "updated_at": updated_at,
    }

@app.get("/mcp/style_profiles")
def get_style_profiles(novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        """
        SELECT id, name, sample_text, fingerprint_json, is_default, created_at, updated_at
        FROM style_profiles
        WHERE novel_id=?
        ORDER BY is_default DESC, updated_at DESC
        """,
        (novel_id,)
    )
    return {"result": [_row_to_style_profile(row) for row in db.cursor.fetchall()]}

@app.get("/mcp/style_profiles/default")
def get_default_style_profile(novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        """
        SELECT id, name, sample_text, fingerprint_json, is_default, created_at, updated_at
        FROM style_profiles
        WHERE novel_id=? AND is_default=1
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (novel_id,)
    )
    row = db.cursor.fetchone()
    return {"result": _row_to_style_profile(row) if row else None}

@app.post("/mcp/style_profiles")
def save_style_profile(req: StyleProfileReq):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if not req.sample_text.strip():
        raise HTTPException(status_code=400, detail="sample_text is required")
    now = time.time()
    if req.is_default:
        db.cursor.execute("UPDATE style_profiles SET is_default=0 WHERE novel_id=?", (req.novel_id,))
    db.cursor.execute(
        """
        INSERT INTO style_profiles (novel_id, name, sample_text, fingerprint_json, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            req.novel_id,
            req.name.strip(),
            req.sample_text,
            json.dumps(req.fingerprint or {}, ensure_ascii=False),
            1 if req.is_default else 0,
            now,
            now,
        )
    )
    profile_id = db.cursor.lastrowid
    db.conn.commit()
    db.cursor.execute(
        """
        SELECT id, name, sample_text, fingerprint_json, is_default, created_at, updated_at
        FROM style_profiles
        WHERE id=? AND novel_id=?
        """,
        (profile_id, req.novel_id)
    )
    return {"result": _row_to_style_profile(db.cursor.fetchone())}

@app.post("/mcp/style_profiles/{profile_id}/default")
def set_default_style_profile(profile_id: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT id FROM style_profiles WHERE novel_id=? AND id=?", (novel_id, profile_id))
    if not db.cursor.fetchone():
        raise HTTPException(status_code=404, detail="style profile not found")
    db.cursor.execute("UPDATE style_profiles SET is_default=0 WHERE novel_id=?", (novel_id,))
    db.cursor.execute(
        "UPDATE style_profiles SET is_default=1, updated_at=? WHERE novel_id=? AND id=?",
        (time.time(), novel_id, profile_id)
    )
    db.conn.commit()
    return {"result": "ok"}

@app.delete("/mcp/style_profiles/{profile_id}")
def delete_style_profile(profile_id: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("DELETE FROM style_profiles WHERE novel_id=? AND id=?", (novel_id, profile_id))
    db.conn.commit()
    return {"result": "ok"}

@app.post("/mcp/summarize_chapter")
def mcp_summarize_chapter(req: ChapterContentRequest):
    summary = call_llm(
        role="extractor",
        system_prompt="你是一个擅长提炼核心剧情的助手。",
        prompt=SUMMARIZER_PROMPT.format(chapter_content=req.chapter_content),
        temperature=0.3,
    )
    return {"result": summary.strip()}

@app.post("/mcp/extract_keywords")
def mcp_extract_keywords(req: OutlineRequest):
    if not req.chapter_outline:
        return {"result": ""}
    keywords = call_llm(
        role="extractor",
        system_prompt="你是一个专门提取搜索关键词的助手。",
        prompt=RAG_KEYWORD_PROMPT.format(chapter_outline=req.chapter_outline),
        temperature=0.1,
    )
    return {"result": keywords.strip()}

@app.post("/mcp/update_chapter")
def mcp_update_chapter(req: UpdateChapterRequest):
    db.cursor.execute(
        "INSERT OR REPLACE INTO chapters (novel_id, chapter_num, content) VALUES (?, ?, ?)",
        (req.novel_id, req.chapter_num, req.chapter_content)
    )
    db.conn.commit()

    extracted = call_llm(
        role="extractor",
        system_prompt=EXTRACTOR_SYSTEM,
        prompt=EXTRACTOR_PROMPT.format(chapter_content=req.chapter_content),
        temperature=0.2,
    )
    
    sections = extracted.split("##")
    for section in sections:
        section = section.strip()
        if not section: continue

        lines = section.split("\n")
        title = lines[0].strip()
        content_lines = [l.strip("- ").strip() for l in lines[1:] if l.strip() and "无新增" not in l]

        category = "plot_points"
        if "人物" in title: category = "characters"
        elif "世界" in title or "设定" in title: category = "world_lore"

        for content in content_lines:
            if content:
                db.cursor.execute(
                    "INSERT INTO pending_extractions (novel_id, category, content, chapter_num) VALUES (?, ?, ?, ?)",
                    (req.novel_id, category, content, req.chapter_num)
                )
    db.conn.commit()
    return {"result": "ok"}

@app.post("/mcp/clear_database")
def mcp_clear_database(novel_id: str = DEFAULT_NOVEL_ID):
    tables = [
        "lore", "chapters", "entity_status", "entity_cards", "inspirations", 
        "plot_hooks", "world_rules", "pending_extractions", "ai_reviews", "chapter_versions", "patch_records", "consistency_reports", "style_profiles"
    ]
    for table in tables:
        db.cursor.execute(f"DELETE FROM {table} WHERE novel_id=?", (novel_id,))
    db.conn.commit()
    return {"result": "ok"}

@app.post("/mcp/init_setting")
def mcp_init_setting(req: InitSettingRequest):
    db.cursor.execute(
        "INSERT INTO lore (novel_id, category, content, chapter_num) VALUES (?, ?, ?, ?)",
        (req.novel_id, "world_lore", req.setting_text, 0)
    )
    db.conn.commit()
    return {"result": "ok"}

# ================= 作家工作台 API =================

@app.post("/mcp/inspirations")
def add_inspiration(req: InspirationReq):
    db.cursor.execute("INSERT INTO inspirations (novel_id, content, tags) VALUES (?, ?, ?)", (req.novel_id, req.content, req.tags))
    db.conn.commit()
    return {"result": "ok"}

@app.get("/mcp/inspirations/pending")
def get_pending_inspirations(novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT id, content, tags FROM inspirations WHERE novel_id=? AND is_used=0", (novel_id,))
    rows = db.cursor.fetchall()
    return {"result": [{"id": r[0], "content": r[1], "tags": r[2]} for r in rows]}

@app.post("/mcp/inspirations/mark_used/{item_id}")
def mark_inspiration_used(item_id: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("UPDATE inspirations SET is_used=1 WHERE novel_id=? AND id=?", (novel_id, item_id))
    db.conn.commit()
    return {"result": "ok"}


@app.post("/mcp/plot_hooks")
def add_plot_hook(req: PlotHookReq):
    db.cursor.execute("INSERT INTO plot_hooks (novel_id, content, target_chapter) VALUES (?, ?, ?)", (req.novel_id, req.content, req.target_chapter))
    db.conn.commit()
    return {"result": "ok"}

@app.get("/mcp/plot_hooks/pending/{chapter_num}")
def get_pending_plot_hooks(chapter_num: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT id, content FROM plot_hooks WHERE novel_id=? AND is_triggered=0 AND target_chapter=?", (novel_id, chapter_num))
    rows = db.cursor.fetchall()
    return {"result": [{"id": r[0], "content": r[1]} for r in rows]}


@app.post("/mcp/world_rules")
def add_world_rule(req: WorldRuleReq):
    db.cursor.execute("INSERT INTO world_rules (novel_id, category, rule_text) VALUES (?, ?, ?)", (req.novel_id, req.category, req.rule_text))
    db.conn.commit()
    return {"result": "ok"}

@app.get("/mcp/world_rules")
def get_world_rules(novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT category, rule_text FROM world_rules WHERE novel_id=?", (novel_id,))
    rows = db.cursor.fetchall()
    return {"result": [{"category": r[0], "rule_text": r[1]} for r in rows]}


@app.post("/mcp/pending_extractions")
def add_pending_extraction(req: PendingExtractionReq):
    db.cursor.execute("INSERT INTO pending_extractions (novel_id, category, content, chapter_num) VALUES (?, ?, ?, ?)", 
                      (req.novel_id, req.category, req.content, req.chapter_num))
    db.conn.commit()
    return {"result": "ok"}

@app.get("/mcp/pending_extractions")
def get_pending_extractions(novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT id, category, content, chapter_num FROM pending_extractions WHERE novel_id=?", (novel_id,))
    rows = db.cursor.fetchall()
    return {"result": [{"id": r[0], "category": r[1], "content": r[2], "chapter_num": r[3]} for r in rows]}

@app.post("/mcp/pending_extractions/approve/{item_id}")
def approve_pending_extraction(item_id: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT category, content, chapter_num FROM pending_extractions WHERE novel_id=? AND id=?", (novel_id, item_id))
    row = db.cursor.fetchone()
    if not row: return {"result": "not found"}
    
    db.cursor.execute("INSERT INTO lore (novel_id, category, content, chapter_num) VALUES (?, ?, ?, ?)", (novel_id, row[0], row[1], row[2]))
    db.cursor.execute("DELETE FROM pending_extractions WHERE novel_id=? AND id=?", (novel_id, item_id))
    db.conn.commit()
    return {"result": "ok"}

@app.post("/mcp/pending_extractions/approve_to_card/{item_id}")
def approve_pending_extraction_to_card(item_id: int, req: ApproveExtractionToCardReq):
    db.cursor.execute(
        "SELECT category, content, chapter_num FROM pending_extractions WHERE novel_id=? AND id=?",
        (req.novel_id, item_id)
    )
    row = db.cursor.fetchone()
    if not row:
        return {"result": "not found"}

    category, content, chapter_num = row
    fields = dict(req.fields or {})
    if "原文" not in fields:
        fields["原文"] = content
    if "来源章节" not in fields:
        fields["来源章节"] = chapter_num

    note = req.note or f"由待确认设定转入：{content[:60]}"
    upsert_entity_card(req.novel_id, req.card_type, req.name, fields, note)
    db.cursor.execute(
        "INSERT INTO lore (novel_id, category, content, chapter_num) VALUES (?, ?, ?, ?)",
        (req.novel_id, category, content, chapter_num)
    )
    db.cursor.execute("DELETE FROM pending_extractions WHERE novel_id=? AND id=?", (req.novel_id, item_id))
    db.conn.commit()
    return {"result": "ok"}


@app.post("/mcp/ai_reviews")
def add_ai_review(req: AIReviewReq):
    db.cursor.execute("INSERT OR REPLACE INTO ai_reviews (novel_id, chapter_num, review_content) VALUES (?, ?, ?)", 
                      (req.novel_id, req.chapter_num, req.review_content))
    db.conn.commit()
    return {"result": "ok"}

@app.get("/mcp/ai_reviews/{chapter_num}")
def get_ai_review(chapter_num: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute("SELECT review_content FROM ai_reviews WHERE novel_id=? AND chapter_num=?", (novel_id, chapter_num))
    row = db.cursor.fetchone()
    return {"result": row[0] if row else ""}

@app.post("/mcp/chapter_versions")
def add_chapter_version(req: ChapterVersionReq):
    import time
    db.cursor.execute(
        "INSERT INTO chapter_versions (novel_id, chapter_num, source, content, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (req.novel_id, req.chapter_num, req.source, req.content, req.note, time.time())
    )
    db.conn.commit()
    return {"result": "ok", "version_id": db.cursor.lastrowid}

@app.get("/mcp/chapter_versions/{chapter_num}")
def get_chapter_versions(chapter_num: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        "SELECT id, source, note, created_at, LENGTH(content) FROM chapter_versions WHERE novel_id=? AND chapter_num=? ORDER BY created_at DESC",
        (novel_id, chapter_num)
    )
    rows = db.cursor.fetchall()
    return {
        "result": [
            {"id": r[0], "source": r[1], "note": r[2], "created_at": r[3], "word_count": r[4] or 0}
            for r in rows
        ]
    }

@app.get("/mcp/chapter_versions/detail/{version_id}")
def get_chapter_version_detail(version_id: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        "SELECT id, chapter_num, source, note, created_at, content FROM chapter_versions WHERE novel_id=? AND id=?",
        (novel_id, version_id)
    )
    row = db.cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="version not found")
    return {
        "result": {
            "id": row[0],
            "chapter_num": row[1],
            "source": row[2],
            "note": row[3],
            "created_at": row[4],
            "content": row[5],
            "word_count": len(row[5] or "")
        }
    }

@app.post("/mcp/patch_records")
def add_patch_record(req: PatchRecordReq):
    import time
    db.cursor.execute(
        """
        INSERT INTO patch_records (
            novel_id, chapter_num, edit_round, patch_index, target_text,
            instruction, replacement_text, success, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            req.novel_id,
            req.chapter_num,
            req.edit_round,
            req.patch_index,
            req.target_text,
            req.instruction,
            req.replacement_text,
            1 if req.success else 0,
            req.reason,
            time.time(),
        )
    )
    db.conn.commit()
    return {"result": "ok", "record_id": db.cursor.lastrowid}

@app.get("/mcp/patch_records/{chapter_num}")
def get_patch_records(chapter_num: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        """
        SELECT id, edit_round, patch_index, target_text, instruction, replacement_text, success, reason, created_at
        FROM patch_records
        WHERE novel_id=? AND chapter_num=?
        ORDER BY created_at DESC, patch_index ASC
        """,
        (novel_id, chapter_num)
    )
    rows = db.cursor.fetchall()
    return {
        "result": [
            {
                "id": r[0],
                "edit_round": r[1],
                "patch_index": r[2],
                "target_text": r[3],
                "instruction": r[4],
                "replacement_text": r[5],
                "success": bool(r[6]),
                "reason": r[7],
                "created_at": r[8],
            }
            for r in rows
        ]
    }

@app.post("/mcp/consistency_reports")
def add_consistency_report(req: ConsistencyReportReq):
    import time
    db.cursor.execute(
        """
        INSERT INTO consistency_reports (
            novel_id, chapter_num, review_round, severity, category,
            message, suggestion, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            req.novel_id,
            req.chapter_num,
            req.review_round,
            req.severity,
            req.category,
            req.message,
            req.suggestion,
            req.status,
            time.time(),
        )
    )
    db.conn.commit()
    return {"result": "ok", "report_id": db.cursor.lastrowid}

@app.get("/mcp/consistency_reports/{chapter_num}")
def get_consistency_reports(chapter_num: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        """
        SELECT id, review_round, severity, category, message, suggestion, status, created_at
        FROM consistency_reports
        WHERE novel_id=? AND chapter_num=?
        ORDER BY created_at DESC, id DESC
        """,
        (novel_id, chapter_num)
    )
    rows = db.cursor.fetchall()
    return {
        "result": [
            {
                "id": r[0],
                "review_round": r[1],
                "severity": r[2],
                "category": r[3],
                "message": r[4],
                "suggestion": r[5],
                "status": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]
    }

@app.post("/mcp/consistency_reports/{report_id}/close")
def close_consistency_report(report_id: int, novel_id: str = DEFAULT_NOVEL_ID):
    db.cursor.execute(
        "UPDATE consistency_reports SET status='closed' WHERE novel_id=? AND id=?",
        (novel_id, report_id)
    )
    db.conn.commit()
    return {"result": "ok", "report_id": report_id}

if __name__ == "__main__":
    print("🚀 正在启动小说记忆库 MCP 服务中心...")
    uvicorn.run(app, host="127.0.0.1", port=8001)
