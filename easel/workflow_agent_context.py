"""Project-scoped public memory shared by chat, generation and Skill turns.

The project file is the durable truth. Only visible conversation, feedback and
artifact references enter this snapshot; raw agent reasoning never does.
"""
from __future__ import annotations

from pathlib import Path
import json
import re


def session_key(project_id: str) -> str:
    if not isinstance(project_id, str) or not re.fullmatch(r"wf-[a-f0-9]{12}", project_id):
        raise ValueError("工作流编号无效。")
    return f"workflow-{project_id}"


def current_checkpoint(context: dict | None, project_id: str) -> str:
    """Put current saved facts after history, without deriving facts from prose."""
    memory = (context or {}).get("project_memory", context or {})
    if not isinstance(memory, dict) or memory.get("project_id") != project_id:
        return ""
    primary = memory.get("primary_manuscript") or {}
    settings = memory.get("settings") or {}
    facts = {"project_title": memory.get("title"), "primary_manuscript_id": primary.get("id"),
        "primary_manuscript_title": primary.get("title"),
        "saved_settings": {key: settings[key] for key in ("subtitle_max_chars", "subtitle_style", "visual_style", "output_ratio", "render_preferences") if key in settings},
        "current_node_results": [{"node": n["id"], "status": n.get("status"), "message": n.get("message", "")}
                                 for n in memory.get("nodes", [])]}
    return ("\n\n当前已保存的项目事实（读取表单和宿主结果，不是历史助手的推断）：\n" +
        json.dumps(facts, ensure_ascii=False) +
        "\n主稿名称必须以 primary_manuscript_title 为准；project_title 是项目名称，不能代作稿名。"
        "历史助手叫错稿名或宣称旧产物完成时，以此处实际状态为准。保留用户的创作要求和修改意见。")


def project_memory(project: dict, root: Path | None = None, *, compact: bool = False) -> dict:
    """Rebuild memory after every edit, so old agent history cannot override it."""
    nodes, history = [], []
    for node in project.get("nodes", []):
        nodes.append({"id": node["id"], "title": node.get("title"), "status": node.get("status"),
            "version": node.get("version"), "approved_version": node.get("approved_version"),
            "message": node.get("message", ""), "artifacts": node.get("artifacts", [])[-12:],
            "render_receipt": {key: node["render_receipt"].get(key) for key in
                ("version", "preferences", "props_path", "props_sha256")} if node.get("render_receipt") else None,
            "feedback": [{"text": f.get("text", ""), "applied_run_id": f.get("applied_run_id")}
                         for f in node.get("feedback", [])[-20:]]})
        for message in node.get("chat", {}).get("messages", []):
            if message.get("content") and message.get("status") in {"completed", "failed", "stopped"}:
                history.append({"node": node["id"], "role": message["role"],
                    "content": message["content"][:24000], "at": message.get("created_at", ""),
                    "status": message.get("status"), "execution": message.get("execution")})
    history.sort(key=lambda item: item["at"])
    if compact:
        # Native transcript already holds full conversation. Repeat current
        # user intent and a few completed receipts, not every long Agent reply.
        users = [m for m in history if m["role"] == "user"][-16:]
        replies = [m for m in history if m["role"] == "assistant" and m["status"] == "completed"][-4:]
        history = sorted([{**m, "content": m["content"][:1600 if m["role"] == "user" else 500]}
                          for m in users + replies], key=lambda item: item["at"])
    recent, remaining = [], 120000
    for message in reversed(history[-60:]):
        if remaining <= 0:
            break
        content = message["content"][:remaining]
        recent.append({**message, "content": content})
        remaining -= len(content)
    recent.reverse()
    primary = next((m for m in project.get("manuscripts", [])
                    if m.get("id") == project.get("primary_manuscript_id")), None)
    memory = {"project_id": project["id"], "session_key": session_key(project["id"]),
        "content_version": project.get("content_version"), "title": project.get("title"),
        "brief": project.get("brief", {}), "settings": project.get("settings", {}),
        "media": project.get("media", {}), "primary_manuscript": primary,
        "nodes": nodes, "project_conversation": recent,
        "precedence": "当前项目表单、已选主稿和最新用户要求优先于旧会话；过期或失败产物不能当作已完成。"}
    if root is not None:
        memory["project_state_path"] = str(Path(root) / "outputs/视频工作流" / project["id"] / "project.json")
        memory["older_history"] = "更早的节点对话和修改意见仍保存在 project_state_path，需要时只读查询。"
    return memory
